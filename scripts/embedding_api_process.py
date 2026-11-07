"""固定回环端口的隔离 API 生命周期，凭据仅传入子进程环境。"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from psycopg.conninfo import make_conninfo

from driving_data_platform.common.config import port
from driving_data_platform.embedding import artifacts as a
from driving_data_platform.embedding import store
from driving_data_platform.ingestion.nuscenes import atomic_json

PORT = port("trial_api")


def process_identity(pid):
    value = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart=,args="],
        capture_output=True,
        text=True,
        check=False,
    )
    if (
        value.returncode
        or "driving_data_platform.api.app:app" not in value.stdout
        or f"--port {PORT}" not in value.stdout
    ):
        return None
    return value.stdout.strip()


def path(root, trial):
    from driving_data_platform.common.run_query import identity

    identity(trial)
    return a.safe_parent(root, "api-runtime") / (trial + ".json")


def status(root, trial):
    file = path(root, trial)
    if not file.exists():
        return {"status": "passed", "running": False, "trial": trial, "port": PORT}
    state = a.read(file.parent, file.name)
    if state["trial"] != trial or state["port"] != PORT:
        raise ValueError("隔离 API 运行登记冲突")
    return {
        "status": "passed",
        **state,
        "running": process_identity(state["pid"]) == state["identity"],
    }


def validate(root, trial):
    state = a.trial_config(root, trial)
    runtime = a.read(
        Path(__file__).resolve().parents[1], "infra/embedding/runtime.lock.json"
    )
    if state["image"] != runtime["image"]:
        raise ValueError("隔离镜像与仓库锁不一致")
    info = json.loads(
        subprocess.check_output(["docker", "container", "inspect", state["container"]])
    )[0]
    if (
        not info["State"]["Running"]
        or info["Image"] != state["image"]
        or info["Config"]["Labels"].get("ddp.embedding.trial") != trial
        or info["NetworkSettings"]["Ports"].get("5432/tcp")
        != [{"HostIp": "127.0.0.1", "HostPort": str(state["port"])}]
    ):
        raise ValueError("隔离数据库容器、镜像或端口身份不一致")
    with store.connect(root, trial=trial) as conn:
        store.check_environment(conn, False)
        conn.execute("SELECT generation FROM control.active WHERE singleton").fetchone()
    return state


def start(root, repo, trial):
    state = validate(root, trial)
    current = status(root, trial)
    if current["running"]:
        return {**current, "reused": True, "url": f"http://127.0.0.1:{PORT}"}
    # 在启动前拒绝未知监听者，失败清理只操作本次 Popen 对象。
    with socket.socket() as check:
        check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        check.bind(("127.0.0.1", PORT))
    env = dict(
        os.environ,
        DDP_OUTPUT_ROOT=str(root),
        DDP_EMBEDDING_TRIAL=trial,
        DDP_POSTGRES_DSN=make_conninfo(
            host=state["host"],
            port=state["port"],
            dbname=state["database"],
            user=state["user"],
            password=state["password"],
        ),
    )
    env.pop("DDP_EMBEDDING_DSN", None)
    env.pop("DDP_EMBEDDING_DEPLOYMENT", None)
    log = path(root, trial).with_suffix(".log")
    with log.open("a") as stream:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "driving_data_platform.api.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(PORT),
                "--workers",
                "1",
            ],
            cwd=repo,
            env=env,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    try:
        for _ in range(60):
            if process.poll() is not None:
                raise ValueError("隔离 API 启动失败，请查看独立运行日志")
            try:
                with urlopen(
                    f"http://127.0.0.1:{PORT}/api/health", timeout=1
                ) as response:
                    if json.load(response)["status"] != "ok":
                        raise ValueError("API 健康响应不合法")
                identity = process_identity(process.pid)
                if not identity:
                    raise ValueError("API 启动身份核对失败")
                result = {
                    "trial": trial,
                    "port": PORT,
                    "pid": process.pid,
                    "identity": identity,
                }
                atomic_json(path(root, trial), result)
                return {
                    "status": "passed",
                    **result,
                    "running": True,
                    "reused": False,
                    "url": f"http://127.0.0.1:{PORT}",
                }
            except (URLError, OSError):
                time.sleep(0.2)
        raise ValueError("隔离 API 启动超时")
    except BaseException:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise


def stop(root, trial):
    current = status(root, trial)
    if current["running"]:
        os.kill(current["pid"], signal.SIGTERM)
        for _ in range(100):
            if process_identity(current["pid"]) is None:
                break
            time.sleep(0.1)
        else:
            raise ValueError("隔离 API 尚未退出，请检查运行日志")
    return {"status": "passed", "running": False, "trial": trial, "port": PORT}
