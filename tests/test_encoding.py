"""真实编码客户端的协议与输入边界。"""

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image

from driving_data_platform.common.errors import PlatformError
from driving_data_platform.embedding.images import RULES, validate_image
from driving_data_platform.embedding.providers import LocalLlama


@pytest.fixture
def runtime():
    seen = []
    response = {"data": [{"index": 0, "embedding": [1.0] + [0.0] * 2047}]}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, body))
            value = (
                {"prompt": "prefix" + body["messages"][1]["content"] + "suffix"}
                if self.path == "/apply-template"
                else {"tokens": [1, 2]}
                if self.path == "/tokenize"
                else response
            )
            data = json.dumps(value).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    client = LocalLlama({"endpoint": f"http://127.0.0.1:{server.server_port}"})
    client.check_identity = lambda: None
    client.config["space"] = {"runtime_parameters": {"context": 4096}}
    try:
        yield client, seen, response
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def png():
    stream = io.BytesIO()
    Image.new("RGB", (16, 16)).save(stream, format="PNG")
    return stream.getvalue()


def test_protocol_preserves_text_and_original_image(runtime):
    client, seen, _ = runtime
    assert len(client.encode_text("  中文\n")) == 2048
    assert seen[0][1]["messages"][1]["content"] == "  中文\n"
    assert seen[0][1]["messages"][0]["content"] == "Represent the user's input."
    image = png()
    assert len(client.encode_image(image)) == 2048
    import base64

    content = seen[-1][1]["input"][0]["content"]
    assert base64.b64decode(content[1]["image_url"]["url"].split(",")[1]) == image


@pytest.mark.parametrize(
    "data", [b"", b"not-a-jpeg", b"\xff\xd8\xffbroken", b"x" * (25 * 1024**2 + 1)]
)
def test_bad_image_never_requests(runtime, data):
    client, seen, _ = runtime
    with pytest.raises(PlatformError) as caught:
        client.encode_image(data)
    assert caught.value.category == "INPUT_INVALID"
    assert seen == []


@pytest.mark.parametrize("text", ["", " ", "\ud800", "中" * 4097])
def test_bad_text_never_requests(runtime, text):
    client, seen, _ = runtime
    with pytest.raises(PlatformError):
        client.encode_text(text)
    assert seen == []


@pytest.mark.parametrize(
    "record",
    [
        [],
        [{"index": 1, "embedding": [1.0] + [0.0] * 2047}],
        [{"index": 0, "embedding": [0.0] * 2048}],
        [{"index": 0, "embedding": [float("nan")] * 2048}],
        [{"index": 0, "embedding": [1.0, 0.0]}],
    ],
)
def test_invalid_vector_is_contract_conflict(runtime, record):
    client, _, response = runtime
    response["data"] = record
    with pytest.raises(PlatformError) as caught:
        client.encode_text("中文")
    assert caught.value.category == "CONTRACT_CONFLICT"


def test_truncated_animation_and_geometry():
    with pytest.raises(PlatformError):
        validate_image(png()[:40])
    stream = io.BytesIO()
    Image.new("RGB", (10, 10)).save(
        stream,
        format="GIF",
        save_all=True,
        append_images=[Image.new("RGB", (10, 10), "red")],
    )
    with pytest.raises(PlatformError):
        validate_image(stream.getvalue())
    for size in [(8193, 1), (5000, 4001)]:
        stream = io.BytesIO()
        Image.new("L", size).save(stream, format="PNG")
        with pytest.raises(PlatformError):
            validate_image(stream.getvalue())
    assert validate_image(png()) == "image/png"
    assert RULES["transmission"].startswith("original-bytes")


def test_identity_change_blocks_before_requests(runtime):
    client, seen, _ = runtime

    def changed():
        raise PlatformError("CONTRACT_CONFLICT", "identity", "身份变化")

    client.check_identity = changed
    with pytest.raises(PlatformError):
        client.encode_text("中文")
    assert seen == []


def test_response_limits_and_transport_failures(runtime):
    from urllib.error import HTTPError, URLError

    client, _, _ = runtime

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size):
            return b"x" * size

    client.opener.open = lambda *args, **kwargs: Response()
    with pytest.raises(PlatformError) as caught:
        client._request("/v1/embeddings", {})
    assert caught.value.category == "CONTRACT_CONFLICT"
    for error, expected in [
        (TimeoutError(), "DEPENDENCY_UNAVAILABLE"),
        (URLError("offline"), "DEPENDENCY_UNAVAILABLE"),
        (HTTPError(client.endpoint, 503, "down", {}, None), "DEPENDENCY_UNAVAILABLE"),
        (HTTPError(client.endpoint, 400, "bad", {}, None), "EXECUTION_FAILED"),
    ]:

        def fail(*args, error=error, **kwargs):
            raise error

        client.opener.open = fail
        with pytest.raises(PlatformError) as caught:
            client._request("/v1/embeddings", {})
        assert caught.value.category == expected


def test_context_limit_rejects_without_embeddings(runtime):
    client, seen, _ = runtime
    client.config["space"]["runtime_parameters"]["context"] = 1
    with pytest.raises(PlatformError) as caught:
        client.encode_text("中文")
    assert caught.value.category == "INPUT_INVALID"
    assert all(path != "/v1/embeddings" for path, _ in seen)


def test_real_process_and_file_identity_changes(tmp_path):
    import os

    from driving_data_platform.embedding import model
    from driving_data_platform.ingestion.nuscenes import atomic_json

    state = {
        "pid": os.getpid(),
        "process_identity": "different startup identity",
        "files": {},
    }
    path = model.state_path(tmp_path)
    atomic_json(path, state)
    with pytest.raises(PlatformError, match="进程启动身份变化"):
        model.checked(tmp_path)
    weight = tmp_path / "copied-weight"
    weight.write_bytes(b"original")
    state["process_identity"] = model.process_identity(os.getpid())
    state["files"] = {str(weight): model.stamp(weight)}
    atomic_json(path, state)
    weight.write_bytes(b"changed copied file")
    with pytest.raises(PlatformError, match="模型或二进制文件身份变化"):
        model.checked(tmp_path)
