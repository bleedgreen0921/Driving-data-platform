"""管理本项目 API 进程，核对 PID 启动时间后才允许停止。"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


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
        or "--port 18080" not in value
    ):
        return None
    return value


def status(root: Path):
    path = root / "api-process.json"
    if not path.exists():
        return {"running": False}
    state = json.loads(path.read_text())
    return state | {"running": identity(state["pid"]) == state["identity"]}


def start(root: Path, repo: Path):
    current = status(root)
    if current["running"]:
        return {"status": "passed", **current, "reused": True}
    log = root / "reports" / "api-runtime.log"
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
                "18080",
            ],
            cwd=repo,
            env=dict(os.environ, DDP_OUTPUT_ROOT=str(root)),
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    for _ in range(50):
        if process.poll() is not None:
            raise ValueError("API 启动失败，查看 " + str(log))
        try:
            with urlopen("http://127.0.0.1:18080/api/health", timeout=1):
                pass
            state = {"pid": process.pid, "identity": identity(process.pid)}
            if not state["identity"]:
                raise ValueError("无法核实 API 进程身份")
            (root / "api-process.json").write_text(json.dumps(state))
            return {"status": "passed", **state, "url": "http://127.0.0.1:18080"}
        except (URLError, OSError):
            time.sleep(0.1)
    process.terminate()
    raise ValueError("API 健康检查超时")


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
