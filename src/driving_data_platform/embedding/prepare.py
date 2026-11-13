"""由已核验外部登记生成候选契约；只使用独立临时进程。"""

import hashlib
import socket
import subprocess
import time
import uuid

from driving_data_platform.common.config import port
from driving_data_platform.common.errors import PlatformError
from driving_data_platform.embedding import artifacts as a
from driving_data_platform.embedding import model
from driving_data_platform.embedding.images import RULES
from driving_data_platform.embedding.providers import LocalLlama


def execute(root):
    registered = model.config(root)
    provenance = a.read(root, "embedding/model-provenance.json")
    version = subprocess.check_output(
        [registered["binary"]["path"], "--version"], stderr=subprocess.STDOUT, text=True
    ).strip()
    if provenance["runtime_version"] != version:
        raise ValueError("实际 llama 版本与已验证来源不一致")
    with socket.socket() as check:
        check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        check.bind(("127.0.0.1", port("diagnostic")))
    command = [
        registered["binary"]["path"],
        "-m",
        registered["model"]["path"],
        "--mmproj",
        registered["projector"]["path"],
        "--embedding",
        "--pooling",
        "last",
        "--ctx-size",
        "4096",
        "--batch-size",
        "1024",
        "--ubatch-size",
        "1024",
        "--parallel",
        "1",
        "--image-min-tokens",
        "64",
        "--image-max-tokens",
        "256",
        "--host",
        "127.0.0.1",
        "--port",
        (str(port("diagnostic"))),
    ]
    log = a.safe_parent(root, "model-runtime") / (
        "prepare-" + uuid.uuid4().hex + ".log"
    )
    with log.open("xb") as stream:
        process = subprocess.Popen(
            command, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True
        )
    process_identity = model.process_identity(process.pid)
    try:
        client = LocalLlama(
            {
                "endpoint": ("http://127.0.0.1:" + str(port("diagnostic"))),
                "timeout_seconds": 2,
            }
        )
        for _ in range(120):
            if process.poll() is not None:
                raise ValueError("候选准备模型加载失败，请查看日志")
            try:
                client.probe()
                break
            except PlatformError:
                time.sleep(0.5)
        else:
            raise ValueError("候选准备模型加载超时")
        props = client._request("/props")
        meta = client._request("/v1/models")["data"][0]["meta"]
        if (
            meta["n_embd"] != 2048
            or meta["n_ctx"] != 4096
            or not props["modalities"]["vision"]
        ):
            raise ValueError("实际模型未满足候选维度、上下文或视觉要求")
        result = []
        for limit in (256, 512, 1024):
            value = {
                "schema_version": 2,
                "model": provenance["model"],
                "revision": provenance["revision"],
                "model_sha256": registered["model"]["sha256"],
                "projector_sha256": registered["projector"]["sha256"],
                "runtime": "llama.cpp",
                "runtime_version": version,
                "quantization": "F16",
                "image_preprocessing": RULES,
                "text_instruction": "Represent the user's input.",
                "image_instruction": "Represent the user's input.",
                "pooling": "last",
                "dimension": 2048,
                "dtype": "float32",
                "normalization": "l2",
                "distance": "cosine",
                "test_only": False,
                "binary_sha256": registered["binary"]["sha256"],
                "template_sha256": hashlib.sha256(
                    props["chat_template"].encode()
                ).hexdigest(),
                "implementation_sha256": model.implementation(),
                "runtime_parameters": {
                    "context": 4096,
                    "batch": 1024,
                    "ubatch": 1024,
                    "parallel": 1,
                    "image_min_tokens": 64,
                    "image_max_tokens": limit,
                },
            }
            sid = a.validate_space(value)
            a.commit(root, "spaces", sid, {"space.json": a.json_bytes(value)})
            result.append(sid)
        return result
    finally:
        if (
            process.poll() is None
            and model.process_identity(process.pid) == process_identity
        ):
            process.terminate()
            process.wait(timeout=30)
