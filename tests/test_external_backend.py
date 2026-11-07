"""Protocol tests use a local test server, never claim model quality acceptance."""

import base64
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image

from driving_data_platform.common.errors import PlatformError
from driving_data_platform.embedding import artifacts as a
from driving_data_platform.embedding.external import ExternalAPI, implementation
from driving_data_platform.embedding.images import RULES


@pytest.fixture
def contract():
    return {
        "schema_version": 3,
        "implementation_sha256": implementation(),
        "backend": "external_api",
        "model": "fixture-fixed-v1",
        "revision": "fixture-fixed-v1",
        "revision_field": "model",
        "protocol": "openai_embeddings",
        "image_protocol": "content_parts_v1",
        "image_preprocessing": RULES.copy(),
        "text_instruction": "text:",
        "image_instruction": "image:",
        "dimension": 3,
        "dtype": "float32",
        "normalization": "l2",
        "distance": "cosine",
        "test_only": True,
    }


@pytest.fixture
def server(contract, monkeypatch):
    seen = []
    state = {
        "code": 200,
        "result": {
            "model": contract["revision"],
            "data": [{"index": 0, "embedding": [3, 4, 0]}],
        },
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            seen.append(
                (
                    self.path,
                    dict(self.headers),
                    json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                )
            )
            self.send_response(state["code"])
            if state["code"] == 302:
                self.send_header("Location", "/must-not-be-followed")
            self.end_headers()
            self.wfile.write(json.dumps(state["result"]).encode())

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=http.serve_forever)
    worker.start()
    monkeypatch.setenv("DDP_TEST_EXTERNAL_KEY", "test-secret")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    client = ExternalAPI(
        {
            "endpoint": f"http://127.0.0.1:{http.server_port}/v1",
            "space": contract,
            "model": contract["model"],
            "api_key_env": "DDP_TEST_EXTERNAL_KEY",
        }
    )
    try:
        yield client, seen, state
    finally:
        http.shutdown()
        http.server_close()
        worker.join()


def test_text_and_original_image_protocol(server):
    client, seen, _ = server
    vector = client.encode_text("a red car / 一辆红车")
    assert vector == pytest.approx([0.6, 0.8, 0])
    assert seen[0][0] == "/v1/embeddings"
    assert seen[0][1]["Authorization"] == "Bearer test-secret"
    assert seen[0][2]["input"] == "text:a red car / 一辆红车"
    data = io.BytesIO()
    Image.new("RGB", (4, 4)).save(data, format="PNG")
    client.encode_image(data.getvalue())
    parts = seen[1][2]["input"][0]["content"]
    assert parts[0] == {"type": "text", "text": "image:"}
    assert (
        base64.b64decode(parts[1]["image_url"]["url"].split(",")[1]) == data.getvalue()
    )
    assert "test-secret" not in json.dumps(client.runtime_config())


@pytest.mark.parametrize(
    "code,expected",
    [
        (401, "external-auth"),
        (403, "external-auth"),
        (429, "external-rate-limit"),
        (302, "external-redirect"),
        (503, "external-http"),
    ],
)
def test_http_errors_no_retry_or_redirect(server, code, expected):
    client, seen, state = server
    state["code"] = code
    with pytest.raises(PlatformError) as error:
        client.encode_text("hello")
    assert error.value.code == expected
    assert len(seen) == 1
    assert "test-secret" not in str(error.value)


@pytest.mark.parametrize(
    "values",
    [[0, 0, 0], [True, 1, 0], [1, 0], [float("nan"), 0, 0], [float("inf"), 1, 0]],
)
def test_invalid_vectors(server, values):
    client, _, state = server
    state["result"]["data"][0]["embedding"] = values
    with pytest.raises(PlatformError, match="数值"):
        client.encode_text("hello")


def test_revision_and_input_failures(server, monkeypatch):
    client, seen, state = server
    for text in ("", " " * 2, "x" * 4097, "\ud800"):
        with pytest.raises(PlatformError):
            client.encode_text(text)
    with pytest.raises(PlatformError):
        client.encode_image(b"not an image")
    assert not seen
    monkeypatch.delenv("DDP_TEST_EXTERNAL_KEY")
    with pytest.raises(PlatformError, match="凭据"):
        client.encode_text("hello")
    assert not seen
    monkeypatch.setenv("DDP_TEST_EXTERNAL_KEY", "test-secret")
    state["result"]["model"] = "changed-version"
    with pytest.raises(PlatformError) as error:
        client.encode_text("hello")
    assert error.value.code == "external-revision"


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://remote.invalid/v1",
        "https://user:secret@example.invalid",
        "https://example.invalid/v1?key=secret",
        "file:///tmp/socket",
    ],
)
def test_endpoint_rejected(endpoint):
    with pytest.raises(ValueError):
        ExternalAPI({"endpoint": endpoint})


def test_external_space_identity_and_artifact_roundtrip(tmp_path, contract):
    sid = a.validate_space(contract)
    assert a.validate_space(contract | {"revision": "another-fixed-version"}) != sid
    with pytest.raises(ValueError):
        a.validate_space(contract | {"revision": "latest"})
    value = {
        "schema_version": 1,
        "generation": "g_" + "a" * 64,
        "generation_manifest_sha256": "b" * 64,
        "selection": "keyframe_camera",
        "count": 1,
        "entries": [
            {
                "media_id": "1" * 64,
                "frame_id": "2" * 64,
                "channel": "CAM_FRONT",
                "path": "samples/a.jpg",
                "bytes": 1,
                "sha256": "3" * 64,
            }
        ],
    }
    result = a.create_artifact(
        tmp_path,
        value,
        contract,
        [{"media_id": "1" * 64, "sha256": "3" * 64, "vector": [1.0, 0.0, 0.0]}],
    )
    assert a.verify_artifact(tmp_path, result["artifact_id"])[2] == contract


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"model": "fixture-fixed-v1", "data": []},
        {
            "model": "fixture-fixed-v1",
            "data": [{"index": True, "embedding": [1, 0, 0]}],
        },
        {"model": "fixture-fixed-v1", "data": [{"index": 1, "embedding": [1, 0, 0]}]},
        [1, 2, 3],
    ],
)
def test_malformed_response(server, result):
    client, _, state = server
    state["result"] = result
    with pytest.raises(PlatformError):
        client.encode_text("hello")


def test_response_limit_and_adapter_identity(server):
    client, seen, state = server
    state["result"]["padding"] = "x" * (2 * 1024**2)
    with pytest.raises(PlatformError) as caught:
        client.encode_text("hello")
    assert caught.value.code == "external-response"
    client.space = client.space | {"implementation_sha256": "0" * 64}
    with pytest.raises(PlatformError) as caught:
        client.encode_text("hello")
    assert caught.value.code == "external-implementation" and len(seen) == 1


def test_registered_external_space_factory(tmp_path, contract, server, monkeypatch):
    from driving_data_platform.embedding import model, runtime

    client, seen, _ = server
    root = tmp_path / "output"
    root.mkdir()
    config = root / "application.yaml"
    config.write_text("embedding:\n  provider_file: provider.json\n")
    (root / "provider.json").write_text(
        json.dumps(
            {
                "backend": "external_api",
                "model": contract["model"],
                "endpoint": client.endpoint,
            }
        )
    )
    file = tmp_path / "space.json"
    file.write_text(json.dumps(contract | {"implementation_sha256": None}))
    monkeypatch.delenv("DDP_CONFIG", raising=False)
    monkeypatch.setenv("DDP_OUTPUT_ROOT", str(root))
    registered = runtime.register(root, file, test_only=True)
    sid = registered["space_id"]
    assert model.space(root, sid)["implementation_sha256"] == implementation()
    assert registered["acceptance"] == "not_verified"
    assert not seen
    assert runtime.encoder(root, sid).encode_text("hello") == pytest.approx(
        [0.6, 0.8, 0]
    )
    with pytest.raises(ValueError, match="test_only"):
        runtime.register(root, file, test_only=False)
    assert not model.state_path(root).exists()
