"""图片上传边界、脱敏、实际字节限额及跨请求锁生命周期。"""

import asyncio
import io
import json
import threading

import pytest
from fastapi import FastAPI
from PIL import Image

from driving_data_platform.embedding import gate, image_web, web

GEN = "g_" + "1" * 64


def picture():
    stream = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(stream, format="PNG")
    return stream.getvalue()


async def invoke(root, chunks, *, query=None, headers=None, delay=0):
    app = FastAPI()
    app.include_router(web.router(root))
    messages = []
    parts = iter(chunks)

    async def receive():
        if delay:
            await asyncio.sleep(delay)
        try:
            return next(parts)
        except StopIteration:
            return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/embedding/search/image",
            "raw_path": b"/api/embedding/search/image",
            "query_string": query or ("generation=" + GEN).encode(),
            "headers": headers or [(b"content-type", b"image/png")],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 18081),
        },
        receive,
        send,
    )
    return messages[0]["status"], json.loads(
        b"".join(m.get("body", b"") for m in messages[1:])
    )


def send(root, data, **kwargs):
    return asyncio.run(
        invoke(
            root, [{"type": "http.request", "body": data, "more_body": False}], **kwargs
        )
    )


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("DDP_EMBEDDING_TRIAL", "1" * 32)
    monkeypatch.delenv("DDP_EMBEDDING_DEPLOYMENT", raising=False)


def test_image_input_boundaries(tmp_path, monkeypatch, enabled):
    called = []
    monkeypatch.setattr(web, "perform", lambda *args, **kwargs: called.append(True))
    for data, kwargs, expected in [
        (b"", {}, 422),
        (b"broken private image", {}, 422),
        (picture(), {"headers": [(b"content-type", b"image/jpeg")]}, 422),
        (picture(), {"headers": [(b"content-type", b"image/gif")]}, 415),
        (
            picture(),
            {
                "headers": [
                    (b"content-type", b"image/png"),
                    (b"content-length", b"26214401"),
                ]
            },
            413,
        ),
        (
            picture(),
            {"query": ("generation=" + GEN + "&limit=20&limit=30").encode()},
            422,
        ),
        (picture(), {"query": ("generation=" + GEN + "&text=secret").encode()}, 422),
    ]:
        code, result = send(tmp_path, data, **kwargs)
        assert code == expected and result["request_id"]
    assert not called
    reports = list((tmp_path / "embedding/reports").glob("http-*.json"))
    assert len(reports) == 7
    assert all(
        "broken private image" not in p.read_text() and "base64" not in p.read_text()
        for p in reports
    )


def test_chunk_limit_timeout_and_disconnect_release(tmp_path, monkeypatch, enabled):
    monkeypatch.setitem(image_web.RULES, "max_bytes", 8)
    chunks = [
        {"type": "http.request", "body": b"12345", "more_body": True},
        {"type": "http.request", "body": b"67890", "more_body": False},
    ]
    assert asyncio.run(invoke(tmp_path, chunks))[0] == 413
    monkeypatch.setattr(image_web, "UPLOAD_SECONDS", 0.01)
    assert asyncio.run(invoke(tmp_path, chunks, delay=0.05))[0] == 408
    from starlette.requests import ClientDisconnect

    with pytest.raises(ClientDisconnect):
        asyncio.run(invoke(tmp_path, [{"type": "http.disconnect"}]))
    with gate.acquire(tmp_path):
        pass


def test_busy_precedes_reading_upload(tmp_path, enabled):
    with gate.acquire(tmp_path):
        assert send(tmp_path, picture())[0] == 429
    with gate.acquire(tmp_path):
        pass


def test_cancelled_waiter_keeps_image_worker_lock(tmp_path, monkeypatch, enabled):
    entered, release = threading.Event(), threading.Event()

    def perform(*args, **kwargs):
        entered.set()
        release.wait(5)
        return {"items": [], "timings": {}}

    monkeypatch.setattr(web, "perform", perform)

    async def run():
        task = asyncio.create_task(
            invoke(
                tmp_path,
                [{"type": "http.request", "body": picture(), "more_body": False}],
            )
        )
        try:
            while not entered.is_set():
                await asyncio.sleep(0.005)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            from driving_data_platform.common.errors import PlatformError

            with pytest.raises(PlatformError), gate.acquire(tmp_path):
                pass
        finally:
            release.set()
            await asyncio.sleep(0.1)
        with gate.acquire(tmp_path):
            pass

    asyncio.run(run())
    assert (
        json.loads(
            next((tmp_path / "embedding/reports").glob("http-*.json")).read_text()
        )["status"]
        == "passed"
    )


def test_image_openapi_is_binary_and_has_version_parameters(tmp_path):
    app = FastAPI()
    app.include_router(web.router(tmp_path))
    route = app.openapi()["paths"]["/api/embedding/search/image"]["post"]
    assert set(route["requestBody"]["content"]) == image_web.TYPES
    assert {p["name"] for p in route["parameters"]} == set(web.QueryBody.model_fields)
    assert all(
        v["schema"]["format"] == "binary"
        for v in route["requestBody"]["content"].values()
    )
