"""仅管理已登记、身份可核对的项目 llama 进程。"""

import hashlib
import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

from driving_data_platform.common.config import port as model_port
from driving_data_platform.common.errors import PlatformError
from driving_data_platform.embedding import artifacts as a
from driving_data_platform.embedding.providers import LocalLlama
from driving_data_platform.ingestion.nuscenes import atomic_json


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def implementation():
    return a.checksum(
        {
            name: digest(Path(__file__).with_name(name))
            for name in ("providers.py", "images.py", "model.py", "transport.py")
        }
    )


def stamp(path):
    info = Path(path).stat()
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def process_identity(pid):
    return subprocess.check_output(
        ["ps", "-p", str(pid), "-o", "lstart=,command="], text=True
    ).strip()


def space(root, space_id):
    from driving_data_platform.common.run_query import identity

    identity(space_id, r"[a-f0-9]{64}")
    value = a.read(root, "embedding/spaces/" + space_id + "/space.json")
    if a.validate_space(value) != space_id or value["schema_version"] not in (2, 3):
        raise ValueError("空间身份不匹配或不是受支持的真实空间")
    return value


def config(root):
    value = a.read(root, "embedding/model.json")
    for name in ("binary", "model", "projector"):
        from driving_data_platform.common.config import external_path, resolve_path

        path = resolve_path(value[name]["path"], root / "embedding")
        if path.is_symlink() or not path.is_file():
            raise ValueError("模型登记必须为真实普通文件路径")
        path = external_path(
            str(path), Path(__file__).resolve().parents[3], "model." + name
        )
        if digest(path) != value[name]["sha256"]:
            raise ValueError("模型或二进制摘要与登记不一致")
        value[name] = value[name] | {"path": str(path.absolute())}
    return value


def metadata(value):
    # /v1/models.created 是响应生成时间，不属于模型身份。
    return [
        {key: row[key] for key in ("id", "meta", "architecture")}
        for row in value["data"]
    ]


def state_path(root, port=None):
    port = model_port("model") if port is None else port
    return a.safe_parent(root, "model-runtime") / f"{port}.json"


def checked(root, port=None, expected=None):
    port = model_port("model") if port is None else port
    state = a.read(state_path(root, port).parent, f"{port}.json")
    try:
        rss = (
            int(
                subprocess.check_output(
                    ["ps", "-p", str(state["pid"]), "-o", "rss="], text=True
                )
            )
            * 1024
        )
        if rss > 6 * 1024**3:
            raise PlatformError(
                "RESOURCE_BLOCKED",
                "model-memory",
                "模型 RSS 已超过申报的 6 GiB，停止编码",
            )
        if process_identity(state["pid"]) != state["process_identity"]:
            raise ValueError("进程启动身份变化")
        if expected and state["space_id"] != expected:
            raise ValueError("运行空间不一致")
        for path, expected_stamp in state["files"].items():
            if stamp(path) != expected_stamp:
                raise ValueError("模型或二进制文件身份变化")
        contract = space(root, state["space_id"])
        if contract["implementation_sha256"] != implementation():
            raise ValueError("编码实现变化")
        client = LocalLlama({"endpoint": f"http://127.0.0.1:{port}"})
        props = client._request("/props")
        models = client._request("/v1/models")
        if (
            a.checksum(props) != state["props_sha256"]
            or a.checksum(metadata(models)) != state["models_sha256"]
        ):
            raise ValueError("模型元信息或运行参数变化")
        if (
            hashlib.sha256(props["chat_template"].encode()).hexdigest()
            != contract["template_sha256"]
        ):
            raise ValueError("模板身份变化")
        return state | {"status": "passed"}
    except PlatformError:
        raise
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        raise PlatformError(
            "CONTRACT_CONFLICT", "model-identity", f"模型身份核对失败：{error}"
        ) from None


def up(root, space_id, port=None):
    port = model_port("model") if port is None else port
    contract = space(root, space_id)
    if contract["schema_version"] != 2:
        raise ValueError("本地模型进程命令只接受本地 v2 空间；外部空间请使用 diagnose")
    registered = config(root)
    if contract["implementation_sha256"] != implementation():
        raise ValueError("空间编码实现已变化，需重新评测")
    for name, key in (
        ("binary", "binary_sha256"),
        ("model", "model_sha256"),
        ("projector", "projector_sha256"),
    ):
        if registered[name]["sha256"] != contract[key]:
            raise ValueError("空间与模型登记摘要冲突")
    path = state_path(root, port)
    if path.exists():
        old = a.read(path.parent, path.name)
        try:
            os.kill(old["pid"], 0)
        except ProcessLookupError:
            pass
        else:
            return checked(root, port, space_id) | {"reused": True}
    with socket.socket() as check:
        check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        check.bind(("127.0.0.1", port))
    if shutil.disk_usage(root).free < 12 * 1024**3:
        raise PlatformError("RESOURCE_BLOCKED", "disk-reserve", "磁盘剩余不足 12 GiB")
    physical = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"]))
    if physical < (3072 + 1024 + 1536 + 512 + 8192 + 6144) * 1024**2:
        raise PlatformError(
            "RESOURCE_BLOCKED", "model-memory", "包含模型 6 GiB 的内存预算不足"
        )
    params = contract["runtime_parameters"]
    command = [
        registered["binary"]["path"],
        "-m",
        registered["model"]["path"],
        "--mmproj",
        registered["projector"]["path"],
        "--embedding",
        "--pooling",
        "last",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    for flag, key in (
        ("--ctx-size", "context"),
        ("--batch-size", "batch"),
        ("--ubatch-size", "ubatch"),
        ("--parallel", "parallel"),
        ("--image-min-tokens", "image_min_tokens"),
        ("--image-max-tokens", "image_max_tokens"),
    ):
        command += [flag, str(params[key])]
    log = path.with_suffix("." + str(time.time_ns()) + ".log")
    with log.open("xb") as stream:
        process = subprocess.Popen(
            command, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True
        )
    start_identity = process_identity(process.pid)
    try:
        client = LocalLlama(
            {"endpoint": f"http://127.0.0.1:{port}", "timeout_seconds": 2}
        )
        for _ in range(120):
            if process.poll() is not None:
                raise ValueError("模型启动失败，请检查日志")
            try:
                client.probe()
                break
            except PlatformError:
                time.sleep(0.5)
        else:
            raise ValueError("模型启动超时")
        props, models = client._request("/props"), client._request("/v1/models")
        if (
            hashlib.sha256(props["chat_template"].encode()).hexdigest()
            != contract["template_sha256"]
        ):
            raise ValueError("实际模板不符合空间")
        meta = models["data"][0]["meta"]
        if meta["n_embd"] != 2048 or meta["n_ctx"] != params["context"]:
            raise ValueError("模型维度或上下文不符合空间")
        state = {
            "pid": process.pid,
            "process_identity": start_identity,
            "space_id": space_id,
            "command": command,
            "files": {v["path"]: stamp(v["path"]) for v in registered.values()},
            "props_sha256": a.checksum(props),
            "models_sha256": a.checksum(metadata(models)),
            "log": str(log),
        }
        atomic_json(path, state)
        return checked(root, port, space_id) | {"reused": False}
    except BaseException:
        if process.poll() is None and process_identity(process.pid) == start_identity:
            process.terminate()
            process.wait(timeout=30)
        raise


def stop(root, port=None):
    port = model_port("model") if port is None else port
    path = state_path(root, port)
    if not path.exists():
        return {"status": "passed", "process_status": "stopped"}
    state = a.read(path.parent, path.name)
    try:
        actual = process_identity(state["pid"])
    except subprocess.CalledProcessError:
        return {"status": "passed", "process_status": "stopped"}
    if actual != state["process_identity"]:
        raise ValueError("拒绝停止启动身份不匹配的进程")
    os.kill(state["pid"], signal.SIGTERM)
    for _ in range(60):
        try:
            process_identity(state["pid"])
        except subprocess.CalledProcessError:
            return {"status": "passed", "process_status": "stopped"}
        time.sleep(0.5)
    raise ValueError("模型停止超时，保留状态供核对")


def encoder(root, space_id, port=None):
    port = model_port("model") if port is None else port
    contract = space(root, space_id)
    checked(root, port, space_id)
    return LocalLlama(
        {
            "endpoint": f"http://127.0.0.1:{port}",
            "space": contract,
            "identity_check": lambda: checked(root, port, space_id),
        }
    )
