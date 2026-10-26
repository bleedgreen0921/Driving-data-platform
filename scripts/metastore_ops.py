"""Hive Metastore 候选环境准备；所有下载只在显式准备时执行。"""

import hashlib
import json
import secrets
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from psycopg import sql

from driving_data_platform.common.preflight import digest
from driving_data_platform.publishing.store import connect
from driving_data_platform.warehouse.catalog import check_image, command


def prepare(root: Path, repo: Path):
    from usage_report import usage

    from driving_data_platform.common.config import load_config
    from driving_data_platform.common.resources import inspect

    budget = load_config(repo / "configs/platform.example.yaml")
    budget["deployment"]["persistence_root"] = str(root)
    if (
        inspect(budget, repo)["status"] != "passed"
        or usage(root, repo)["project_bytes_conservative"] + 2 * 1024**3 > 24 * 1024**3
    ):
        raise ValueError("元数据候选资源门禁不通过")
    base = root / "metastore"
    (base / "dependencies").mkdir(parents=True, exist_ok=True)
    (base / "trials").mkdir(exist_ok=True)
    (base / "generations").mkdir(exist_ok=True)
    source = "apache/hive:4.1.0"
    if subprocess.run(
        ["docker", "image", "inspect", source], capture_output=True, check=False
    ).returncode:
        source = "mirror.gcr.io/apache/hive:4.1.0"
        subprocess.run(["docker", "pull", source], check=True)
    info = check_image(source)
    upstream = subprocess.check_output(
        ["docker", "buildx", "imagetools", "inspect", "apache/hive:4.1.0", "--raw"]
    )
    expected = "sha256:" + hashlib.sha256(upstream).hexdigest()
    if not any(ref.endswith("@" + expected) for ref in info["RepoDigests"]):
        raise ValueError("Hive 镜像与官方清单摘要不一致")
    hive = next(ref for ref in info["RepoDigests"] if ref.endswith("@" + expected))
    jar = base / "dependencies/postgresql.jar"
    url = "https://repo.maven.apache.org/maven2/org/postgresql/postgresql/42.7.8/postgresql-42.7.8.jar"
    if not jar.exists():
        temporary = jar.with_suffix(".download")
        subprocess.run(
            [
                "curl",
                "-fL",
                "--retry",
                "2",
                "--connect-timeout",
                "15",
                "--max-time",
                "300",
                url,
                "-o",
                str(temporary),
            ],
            check=True,
        )
        checksum = (
            subprocess.check_output(["curl", "-fsSL", url + ".sha1"], text=True)
            .strip()
            .split()[0]
        )
        if hashlib.sha1(temporary.read_bytes()).hexdigest() != checksum:
            raise ValueError("JDBC 下载校验失败")
        temporary.rename(jar)
    expected_jdbc = (
        subprocess.check_output(["curl", "-fsSL", url + ".sha1"], text=True)
        .strip()
        .split()[0]
    )
    if hashlib.sha1(jar.read_bytes()).hexdigest() != expected_jdbc:
        raise ValueError("JDBC 缓存摘要不符，不自动覆盖")
    spark = json.loads((repo / "infra/docker/versions.lock.json").read_text())["SPARK"][
        "image"
    ]
    tag = "driving-data-platform/hms-spark:4.2.0-hive4.1.0"
    subprocess.run(
        [
            "docker",
            "build",
            "--build-arg",
            "HIVE_IMAGE=" + hive,
            "--build-arg",
            "SPARK_IMAGE=" + spark,
            "-f",
            str(repo / "infra/metastore/Dockerfile"),
            "-t",
            tag,
            str(repo),
        ],
        check=True,
    )
    client = check_image(tag)
    env = base / "runtime.env"
    if not env.exists():
        env.write_text("DDP_HMS_PASSWORD=" + secrets.token_hex(24) + "\n")
        env.chmod(0o600)
    fields = dict(line.split("=", 1) for line in env.read_text().splitlines())
    fields.update(DDP_HMS_IMAGE=hive, DDP_HMS_SPARK_IMAGE=client["Id"])
    env.write_text("".join(f"{k}={v}\n" for k, v in fields.items()))
    with connect(root) as conn:
        conn.autocommit = True
        if (
            conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname=%s", ("ddp_metastore",)
            ).fetchone()
            is None
        ):
            conn.execute(
                sql.SQL("CREATE ROLE ddp_metastore LOGIN PASSWORD {}").format(
                    sql.Literal(fields["DDP_HMS_PASSWORD"])
                )
            )
        found = conn.execute(
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=%s",
            ("ddp_metastore_v410",),
        ).fetchone()
        if found is None:
            conn.execute("CREATE DATABASE ddp_metastore_v410 OWNER ddp_metastore")
        elif found != ("ddp_metastore",):
            raise ValueError("拒绝复用非本候选角色所有的元数据库")
        conn.execute("REVOKE CONNECT ON DATABASE ddp_metastore_v410 FROM PUBLIC")
        conn.execute(
            "GRANT CONNECT ON DATABASE ddp_metastore_v410 TO ddp_metastore,ddp"
        )
    with connect(root, database="ddp_metastore_v410") as conn:
        initialized = (
            conn.execute("SELECT to_regclass('public.\"VERSION\"')").fetchone()[0]
            is not None
        )
        if initialized and conn.execute(
            'SELECT "SCHEMA_VERSION" FROM "VERSION"'
        ).fetchall() != [("4.1.0",)]:
            raise ValueError("元数据库 schema 版本不匹配，不自动升级")
    if not initialized:
        subprocess.run(
            [*command(root, repo), "run", "--rm", "--no-deps", "metastore", "init"],
            check=True,
        )
    lock = {
        "hive": {
            "tag": "apache/hive:4.1.0",
            "image": hive,
            "size_bytes": info["Size"],
            "architecture": info["Architecture"],
            "source": "https://hub.docker.com/r/apache/hive",
            "license": "Apache-2.0",
        },
        "spark_client": {
            "tag": tag,
            "image": client["Id"],
            "size_bytes": client["Size"],
            "architecture": client["Architecture"],
            "dockerfile_sha256": digest(repo / "infra/metastore/Dockerfile"),
        },
        "jdbc": {
            "version": "42.7.8",
            "url": url,
            "sha256": digest(jar),
            "license": "BSD-2-Clause",
        },
        "prepared_at": datetime.now(UTC).isoformat(),
    }
    (repo / "infra/metastore/runtime.lock.json").write_text(
        json.dumps(lock, indent=2) + "\n"
    )
    # 客户端 JAR 不在运行时下载；记录完整离线类路径供兼容性追溯。
    inventory = subprocess.check_output(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "python3",
            client["Id"],
            "-c",
            "import glob,hashlib,json; print(json.dumps([{'file':p,'sha256':hashlib.sha256(open(p,'rb').read()).hexdigest()} for p in sorted(glob.glob('/opt/ddp/hive-client/*.jar')+glob.glob('/opt/spark/jars/hadoop-client-*.jar'))]))",
        ],
        text=True,
    )
    (repo / "infra/metastore/client-jars.lock.json").write_text(
        json.dumps(
            {
                "source_image": hive,
                "license_basis": "原始 JAR 内 META-INF LICENSE/NOTICE 及 Hive 分发包 LICENSE/NOTICE",
                "jars": json.loads(inventory),
            },
            indent=2,
        )
        + "\n"
    )
    return {"status": "passed", "scope": "candidate-preparation-only", "images": lock}
