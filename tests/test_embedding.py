"""向量文件、契约及推理边界；真实数据库验证由 verify embedding 执行。"""

import copy
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from driving_data_platform.common.errors import PlatformError
from driving_data_platform.embedding import artifacts as a
from driving_data_platform.embedding.providers import ExternalAPI, LocalLlama, provider


@pytest.fixture
def source():
    return {
        "schema_version": 1,
        "generation": "g_" + "a" * 64,
        "generation_manifest_sha256": "b" * 64,
        "selection": "keyframe_camera",
        "count": 2,
        "entries": [
            {
                "media_id": c * 64,
                "frame_id": "c" * 64,
                "channel": "CAM_FRONT",
                "path": "samples/" + c + ".jpg",
                "bytes": 10,
                "sha256": "d" * 64,
            }
            for c in ("1", "2")
        ],
    }


@pytest.fixture
def space():
    return {
        "schema_version": 1,
        "model": "synthetic-test-only",
        "revision": "fixed",
        "model_sha256": "1" * 64,
        "projector_sha256": "2" * 64,
        "runtime": "test",
        "runtime_version": "1",
        "quantization": "none",
        "image_preprocessing": {"synthetic": True},
        "text_instruction": "",
        "image_instruction": "",
        "pooling": "last",
        "dimension": 3,
        "dtype": "float32",
        "normalization": "l2",
        "distance": "cosine",
        "test_only": True,
    }


@pytest.fixture
def vectors(source):
    return [
        {"media_id": r["media_id"], "sha256": r["sha256"], "vector": [1.0, 0.0, 0.0]}
        for r in source["entries"]
    ]


def test_artifact_roundtrip_and_reuse(tmp_path, source, space, vectors):
    first = a.create_artifact(tmp_path, source, space, vectors)
    second = a.create_artifact(tmp_path, source, space, vectors)
    assert not first["reused"] and second["reused"]
    complete, loaded_input, loaded_space, loaded_vectors = a.verify_artifact(
        tmp_path, first["artifact_id"]
    )
    assert (
        loaded_input == source and loaded_space == space and loaded_vectors == vectors
    )
    assert complete["test_only"] and complete["count"] == 2
    changed = copy.deepcopy(space)
    changed["pooling"] = "mean"
    assert a.validate_space(changed) != complete["space_id"]


@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "hash", "dimension", "nan", "zero", "norm"]
)
def test_vectors_rejected(tmp_path, source, space, vectors, mutation):
    if mutation == "missing":
        vectors.pop()
    if mutation == "duplicate":
        vectors[1] = vectors[0].copy()
    if mutation == "hash":
        vectors[0]["sha256"] = "0" * 64
    if mutation == "dimension":
        vectors[0]["vector"] = [1.0]
    if mutation == "nan":
        vectors[0]["vector"] = [float("nan"), 0.0, 0.0]
    if mutation == "zero":
        vectors[0]["vector"] = [0.0] * 3
    if mutation == "norm":
        vectors[0]["vector"] = [2.0, 0.0, 0.0]
    with pytest.raises(ValueError):
        a.create_artifact(tmp_path, source, space, vectors)
    assert not (tmp_path / "embedding/artifacts").exists()


def test_corrupt_missing_extra_and_symlink(tmp_path, source, space, vectors):
    result = a.create_artifact(tmp_path, source, space, vectors)
    target = a.directory(tmp_path, "artifacts", result["artifact_id"])
    file = target / "vectors.jsonl"
    original = file.read_bytes()
    file.write_bytes(original + b"\n")
    with pytest.raises(ValueError):
        a.verify_artifact(tmp_path, result["artifact_id"])
    file.write_bytes(original)
    (target / "extra").write_text("unexpected")
    with pytest.raises(ValueError):
        a.verify_artifact(tmp_path, result["artifact_id"])
    (target / "extra").unlink()
    file.unlink()
    with pytest.raises(ValueError):
        a.verify_artifact(tmp_path, result["artifact_id"])
    outside = tmp_path / "outside"
    outside.write_bytes(original)
    file.symlink_to(outside)
    with pytest.raises((ValueError, OSError)):
        a.verify_artifact(tmp_path, result["artifact_id"])
    with pytest.raises(ValueError):
        a.read_bytes(tmp_path, "../outside")
    with pytest.raises(ValueError):
        a.directory(tmp_path, "artifacts", "../outside")
    with pytest.raises(ValueError):
        a.read_bytes(tmp_path, "outside", maximum=1)


def test_input_integrity_and_no_arbitrary_path(source):
    for update in ({"count": 1}, {"selection": "all-media"}):
        bad = source | update
        with pytest.raises(ValueError):
            a.validate_input(bad)
    for path in ("/private/data.jpg", "../data.jpg", "samples/image.png"):
        bad = copy.deepcopy(source)
        bad["entries"][0]["path"] = path
        with pytest.raises(ValueError):
            a.validate_input(bad)
    bad = copy.deepcopy(source)
    bad["entries"].reverse()
    with pytest.raises(ValueError):
        a.validate_input(bad)


def test_space_pending_and_float32(space):
    for field in (
        "model_sha256",
        "projector_sha256",
        "dimension",
        "image_preprocessing",
        "runtime_version",
    ):
        bad = space | {field: None}
        with pytest.raises((ValueError, TypeError)):
            a.validate_space(bad)
    assert a.vector32([1, 0, 0], 3) == [1.0, 0.0, 0.0]
    for values in ([True, 0, 0], [float("inf"), 0, 0], [1e300, 0, 0]):
        with pytest.raises(ValueError):
            a.vector32(values, 3)


def test_no_placeholder_encoding(space):
    client = LocalLlama({"endpoint": "http://127.0.0.1:9"})
    assert client.capabilities()["text_encoding"] is False
    with pytest.raises(PlatformError, match="尚未就绪"):
        client.encode_text("路口有行人")
    with pytest.raises(PlatformError):
        client.encode_image(b"query")
    configured = LocalLlama({"endpoint": "http://127.0.0.1:9", "space": space})
    with pytest.raises(PlatformError):
        configured.encode_text("中文")
    with pytest.raises(PlatformError):
        ExternalAPI().encode_image(b"image")
    with pytest.raises(PlatformError):
        ExternalAPI().encode_text("中文")
    with pytest.raises(ValueError):
        provider({"backend": "unknown"})
    with pytest.raises(ValueError):
        client.encode_text("")
    with pytest.raises(ValueError):
        client.encode_image(b"")


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.com",
        "http://127.0.0.1",
        "http://user:secret@127.0.0.1:80",
        "http://127.0.0.1:80/api",
        "http://127.0.0.1:80?secret=x",
    ],
)
def test_no_external_requests(endpoint):
    with pytest.raises(ValueError):
        LocalLlama({"endpoint": endpoint})


def test_controlled_http_health_only():
    class Handler(BaseHTTPRequestHandler):
        body = b'{"status":"ok"}'
        delay = 0

        def do_GET(self):
            time.sleep(self.delay)
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(self.body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        client = LocalLlama(
            {
                "endpoint": f"http://127.0.0.1:{server.server_port}",
                "timeout_seconds": 0.05,
            }
        )
        assert client.probe()["model_compatibility"] == "not_verified"
        Handler.body = b"not json"
        with pytest.raises(PlatformError, match="响应不合法"):
            client.probe()
        Handler.body = b"{}"
        with pytest.raises(PlatformError):
            client.probe()
        Handler.delay = 0.15
        with pytest.raises(PlatformError, match="超时"):
            client.probe()
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
    with pytest.raises(PlatformError):
        client.probe()


def test_config_boundary_and_trial_registry(tmp_path):
    with pytest.raises(ValueError):
        LocalLlama(
            {
                "endpoint": "http://127.0.0.1:18090",
                "model_path": str(Path(__file__).resolve().parent / "model.gguf"),
            }
        )
    trial = "a" * 32
    folder = tmp_path / "embedding/trials" / trial
    folder.mkdir(parents=True)
    (folder / "connection.json").write_text(
        json.dumps(
            {
                "trial": trial,
                "container": "another-project",
                "database": "ddp",
                "host": "127.0.0.1",
                "port": 55433,
                "user": "ddp",
            }
        )
    )
    with pytest.raises(ValueError):
        a.trial_config(tmp_path, trial)


def test_independent_failed_task_report(tmp_path):
    from argparse import Namespace

    from driving_data_platform.embedding.cli import execute

    args = Namespace(
        command="embedding-input",
        generation="bad",
        artifact_id=None,
        embedding_generation=None,
        trial=None,
        verify_files=False,
    )
    # 在配置/依赖缺失时也必须留下独立失败报告，不伪造业务成功。
    with pytest.raises((OSError, ValueError)):
        execute(tmp_path, Path.cwd(), args)
    reports = list((tmp_path / "embedding/reports").glob("task-*.json"))
    assert len(reports) == 1
    value = json.loads(reports[0].read_text())
    assert value["status"] == "failed" and value["stage"] == "input"
    assert value["input"]["generation"] == "bad" and value["seconds"] >= 0


def test_artifact_binds_production_code(tmp_path, source, space, vectors):
    first = a.create_artifact(
        tmp_path,
        source,
        space,
        vectors,
        processing_version="real-code-test",
        code_sha256="a" * 64,
    )
    second = a.create_artifact(
        tmp_path,
        source,
        space,
        vectors,
        processing_version="real-code-test",
        code_sha256="b" * 64,
    )
    assert first["artifact_id"] != second["artifact_id"]
    complete, _, _, _ = a.verify_artifact(tmp_path, first["artifact_id"])
    assert complete["code_sha256"] == "a" * 64
    with pytest.raises(ValueError):
        a.create_artifact(tmp_path, source, space, vectors, code_sha256="invalid")
