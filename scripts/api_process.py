"""管理本项目 API 进程，核对 PID 启动时间后才允许停止。"""

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

from driving_data_platform.common.config import port


def identity(pid):
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart=", "-o", "args="],
        capture_output=True,
        text=True,
        check=False,
    )
    value = result.stdout.strip()
    if (
        result.returncode
        or "driving_data_platform.api.app:app" not in value
        or ("--port " + str(port("api"))) not in value
    ):
        return None
    return value


def status(root: Path):
    path = root / "api-process.json"
    if not path.exists():
        return {"running": False}
    state = json.loads(path.read_text())
    return state | {"running": identity(state["pid"]) == state["identity"]}


def start(root: Path, repo: Path, deployment=None):
    log = root / "reports" / "api-runtime.log"
    environment = dict(os.environ, DDP_OUTPUT_ROOT=str(root))
    environment.pop("DDP_EMBEDDING_TRIAL", None)
    environment.pop("DDP_EMBEDDING_DEPLOYMENT", None)
    pointer = root / "embedding/production.json"
    if deployment is None and pointer.exists():
        configuration = json.loads(pointer.read_text())
        if configuration.get("enabled"):
            deployment = configuration["deployment_id"]
    current = status(root)
    if current["running"]:
        if current.get("deployment") != deployment:
            raise ValueError("已有API部署身份不同，拒绝复用")
        return {"status": "passed", **current, "reused": True}
    from driving_data_platform.common.config import embedding_enabled

    if deployment and not embedding_enabled(root):
        raise ValueError("embedding.enabled: false; refusing model deployment")
    if deployment:
        from psycopg.conninfo import make_conninfo

        from driving_data_platform.embedding import deployment as d
        from driving_data_platform.embedding import store

        state = d.registration(root, deployment)
        d.container(state)
        with store.connect(root, deployment=deployment):
            pass
        fields = dict(
            line.split("=", 1)
            for line in (root / "compose.env").read_text().splitlines()
        )
        environment["DDP_POSTGRES_DSN"] = make_conninfo(
            host=state["host"],
            port=state["port"],
            dbname=state["database"],
            user=state["user"],
            password=fields["DDP_POSTGRES_PASSWORD"],
        )
        environment["DDP_EMBEDDING_DEPLOYMENT"] = deployment
    environment.pop("DDP_EMBEDDING_DSN", None)
    with socket.socket() as check:
        check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        check.bind(("127.0.0.1", port("api")))
    with log.open("a") as output:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "driving_data_platform.api.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                (str(port("api"))),
            ],
            cwd=repo,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    try:
        for _ in range(50):
            if process.poll() is not None:
                raise ValueError("API 启动失败，查看 " + str(log))
            try:
                with urlopen(
                    ("http://127.0.0.1:" + str(port("api")) + "/api/health"), timeout=1
                ):
                    pass
                state = {
                    "pid": process.pid,
                    "identity": identity(process.pid),
                    "deployment": deployment,
                }
                if not state["identity"]:
                    raise ValueError("无法核实 API 进程身份")
                (root / "api-process.json").write_text(json.dumps(state))
                return {
                    "status": "passed",
                    **state,
                    "reused": False,
                    "url": ("http://127.0.0.1:" + str(port("api"))),
                }
            except (URLError, OSError):
                time.sleep(0.1)
        raise ValueError("API 健康检查超时")
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        raise


def stop(root: Path):
    current = status(root)
    if current["running"]:
        os.kill(current["pid"], signal.SIGTERM)
        for _ in range(50):
            if identity(current["pid"]) is None:
                break
            time.sleep(0.1)
        else:
            raise ValueError("API 尚未停止，请查看进程日志")
    return {"status": "passed", "running": False}
