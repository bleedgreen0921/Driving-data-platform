"""显式准备隔离向量镜像；绝不改正式 Compose 镜像或端口。"""

import hashlib
import json
import shutil
import subprocess
from datetime import UTC, datetime
from urllib.request import urlopen

from driving_data_platform.common.config import port
from driving_data_platform.embedding.artifacts import safe_parent
from driving_data_platform.ingestion.nuscenes import atomic_json

VERSION = "0.8.7"
SOURCE = f"https://codeload.github.com/pgvector/pgvector/tar.gz/refs/tags/v{VERSION}"


def prepare(root, repo):
    from driving_data_platform.common.config import load_config
    from driving_data_platform.common.resources import inspect

    budget = load_config(repo / "configs/platform.example.yaml")
    budget["deployment"]["persistence_root"] = str(root)
    if inspect(budget, repo)["status"] != "passed":
        raise ValueError("资源门禁未通过，禁止构建隔离镜像")
    base = json.loads((repo / "infra/docker/versions.lock.json").read_text())[
        "POSTGRES"
    ]
    if base["architecture"] != "arm64" or base["tag"] != "postgres:16.15":
        raise ValueError("仅验证锁定的 PostgreSQL 16.15 arm64 基础镜像")
    parent = safe_parent(root, "build")
    context = parent / ("pgvector-" + VERSION)
    context.mkdir(exist_ok=True)
    archive = context / "pgvector.tar.gz"
    lock_file = repo / "infra/embedding/runtime.lock.json"
    if not archive.exists():
        with urlopen(SOURCE, timeout=60) as response:
            data = response.read(16 * 1024**2 + 1)
        if len(data) > 16 * 1024**2:
            raise ValueError("扩展源码包超过限制")
        archive.write_bytes(data)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    source_lock = repo / "infra/embedding/source.lock.json"
    if source_lock.exists():
        expected = json.loads(source_lock.read_text())
        if (
            expected["version"] != VERSION
            or expected["sha256"] != checksum
            or expected["base_image"] != base["image"]
        ):
            raise ValueError("扩展源码或基础镜像摘要漂移")
    else:
        atomic_json(
            source_lock,
            {
                "version": VERSION,
                "url": SOURCE,
                "sha256": checksum,
                "base_image": base["image"],
                "license": "PostgreSQL License",
                "acquired_at": datetime.now(UTC).date().isoformat(),
            },
        )
    shutil.copy2(repo / "infra/embedding/Dockerfile", context / "Dockerfile")
    tag = "ddp-pgvector:pg16.15-vector" + VERSION
    log = (
        root
        / "reports"
        / ("embedding-build-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + ".log")
    )
    with log.open("w") as stream:
        subprocess.run(
            [
                "docker",
                "build",
                "--platform",
                "linux/arm64",
                "--build-arg",
                "PG_BASE=" + base["image"],
                "--build-arg",
                "SOURCE_SHA256=" + checksum,
                "-t",
                tag,
                str(context),
            ],
            check=True,
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
    image = json.loads(subprocess.check_output(["docker", "image", "inspect", tag]))[0]
    if image["Architecture"] != "arm64":
        raise ValueError("隔离镜像架构错误")
    result = {
        "version": VERSION,
        "image": image["Id"],
        "tag": tag,
        "architecture": "arm64",
        "size_bytes": image["Size"],
        "source_sha256": checksum,
        "base_image": base["image"],
        "dockerfile_sha256": hashlib.sha256(
            (repo / "infra/embedding/Dockerfile").read_bytes()
        ).hexdigest(),
    }
    atomic_json(lock_file, result)
    return {"status": "passed", "runtime": result, "log": str(log)}


def trial_state(root, trial):
    from driving_data_platform.embedding.artifacts import trial_config

    return trial_config(root, trial)


def trial_up(root, repo, trial):
    import secrets
    import time

    import psycopg

    from driving_data_platform.common.config import load_config
    from driving_data_platform.common.resources import inspect
    from driving_data_platform.common.run_query import identity
    from driving_data_platform.embedding.artifacts import read

    identity(trial)
    budget = load_config(repo / "configs/platform.example.yaml")
    budget["deployment"]["persistence_root"] = str(root)
    # 隔离库额外限额 1 GiB，不能借用现有组件的预算。
    budget["resources"]["postgres_mib"] += 1024
    if inspect(budget, repo)["status"] != "passed":
        raise ValueError("隔离库资源门禁未通过")
    runtime = read(repo, "infra/embedding/runtime.lock.json")
    if (
        runtime["version"] != VERSION
        or runtime["dockerfile_sha256"]
        != hashlib.sha256(
            (repo / "infra/embedding/Dockerfile").read_bytes()
        ).hexdigest()
    ):
        raise ValueError("隔离镜像未准备或构建配置已变化")
    folder = safe_parent(root, "trials") / trial
    if folder.is_symlink():
        raise ValueError("试验目录不得为符号链接")
    folder.mkdir(mode=0o700, exist_ok=True)
    folder.chmod(0o700)
    file = folder / "connection.json"
    name = "ddp-embedding-" + trial
    database = "ddp_embedding_trial_" + trial
    if file.exists():
        state = trial_state(root, trial)
        if state["image"] != runtime["image"]:
            raise ValueError("同一试验不得更换镜像")
    else:
        state = {
            "trial": trial,
            "container": name,
            "database": database,
            "host": "127.0.0.1",
            "port": port("trial"),
            "user": "ddp",
            "password": secrets.token_urlsafe(32),
            "image": runtime["image"],
        }
        atomic_json(file, state)
        file.chmod(0o600)
    env = folder / "container.env"
    if file.is_symlink() or env.is_symlink():
        raise ValueError("隔离凭据文件不得为符号链接")
    env.write_text(
        "POSTGRES_USER=ddp\nPOSTGRES_DB="
        + database
        + "\nPOSTGRES_PASSWORD="
        + state["password"]
        + "\n"
    )
    env.chmod(0o600)
    exists = subprocess.run(
        ["docker", "container", "inspect", name], capture_output=True, check=False
    )
    if exists.returncode == 0:
        info = json.loads(exists.stdout)[0]
        if (
            info["Config"]["Labels"].get("ddp.embedding.trial") != trial
            or info["Image"] != state["image"]
        ):
            raise ValueError("隔离容器身份冲突")
        subprocess.run(["docker", "start", name], check=True, capture_output=True)
    else:
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--label",
                "ddp.embedding.trial=" + trial,
                "--memory",
                "1g",
                "--cpus",
                "1",
                "--env-file",
                str(env),
                "--publish",
                ("127.0.0.1:" + str(port("trial")) + ":5432"),
                "--volume",
                "ddp-embedding-" + trial + ":/var/lib/postgresql/data",
                "--network",
                "bridge",
                state["image"],
                "-c",
                "max_wal_size=128MB",
                "-c",
                "min_wal_size=32MB",
            ],
            check=True,
            capture_output=True,
        )
    for _ in range(60):
        try:
            with psycopg.connect(
                host=state["host"],
                port=state["port"],
                dbname=database,
                user="ddp",
                password=state["password"],
                connect_timeout=1,
            ):
                return {
                    "status": "passed",
                    "trial": trial,
                    "database": database,
                    "port": port("trial"),
                }
        except psycopg.OperationalError:
            time.sleep(1)
    raise ValueError("隔离 PostgreSQL 启动超时，请查看试验容器日志")


def trial_stop(root, trial):
    state = trial_state(root, trial)
    info = subprocess.run(
        ["docker", "container", "inspect", state["container"]],
        capture_output=True,
        check=False,
    )
    if info.returncode == 0:
        value = json.loads(info.stdout)[0]
        if value["Config"]["Labels"].get("ddp.embedding.trial") != trial:
            raise ValueError("拒绝操作其他容器")
        subprocess.run(
            ["docker", "stop", state["container"]], check=True, capture_output=True
        )
        subprocess.run(
            ["docker", "rm", state["container"]], check=True, capture_output=True
        )
    return {
        "status": "passed",
        "trial": trial,
        "retained": "volume, credentials and evidence",
    }
