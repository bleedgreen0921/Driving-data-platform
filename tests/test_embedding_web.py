"""HTTP 脱敏、严格输入、跨进程互斥及取消后的锁生命周期。"""

import asyncio
import json
import subprocess
import sys
import threading

import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from driving_data_platform.common.errors import PlatformError
from driving_data_platform.embedding import gate, web

GENERATION = "g_" + "1" * 64


def test_strict_body():
    for extra in (
        {"limit": True},
        {"limit": "20"},
        {"trial": "1" * 32},
        {"text": "中" * 4097},
        {"required_channels": ["x"] * 13},
    ):
        with pytest.raises(ValidationError):
            web.SearchBody.model_validate(
                {"generation": GENERATION, "text": "中文", **extra}
            )
    assert web.SearchBody(generation=GENERATION, text=" 中文\n ").text == " 中文\n "


def invoke(root, raw):
    app = FastAPI()
    app.include_router(web.router(root))
    messages = []

    async def run():
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": raw, "more_body": False}
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
                "path": "/api/embedding/search",
                "raw_path": b"/api/embedding/search",
                "query_string": b"",
                "headers": [(b"content-type", b"application/json")],
                "client": ("127.0.0.1", 1),
                "server": ("127.0.0.1", 18081),
            },
            receive,
            send,
        )

    asyncio.run(run())
    return messages[0]["status"], json.loads(
        b"".join(m.get("body", b"") for m in messages[1:])
    )


def test_http_parse_reports_and_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("DDP_EMBEDDING_TRIAL", raising=False)
    for raw, status in (
        (b"bad", 422),
        (b"x" * 65537, 413),
        (
            json.dumps(
                {"generation": GENERATION, "text": "秘密查询", "limit": "bad"}
            ).encode(),
            422,
        ),
        (json.dumps({"generation": GENERATION, "text": "秘密查询"}).encode(), 503),
    ):
        actual, payload = invoke(tmp_path, raw)
        assert actual == status and payload["request_id"]
        assert "秘密查询" not in json.dumps(payload, ensure_ascii=False)
    reports = list((tmp_path / "embedding/reports").glob("http-*.json"))
    assert len(reports) == 4
    assert all(
        "秘密查询" not in p.read_text() and '"text"' not in p.read_text()
        for p in reports
    )


def test_sanitized_failure_and_busy():
    response = web.error_response(
        PlatformError(
            "CONTRACT_CONFLICT", "model-identity", "password=secret /Users/private"
        ),
        "id",
    )
    assert (
        response.status_code == 503
        and b"secret" not in response.body
        and b"/Users" not in response.body
    )
    response = web.error_response(
        PlatformError("OPERATION_CONFLICT", "search-busy", "busy"), "id"
    )
    assert response.status_code == 429 and response.headers["retry-after"] == "1"


def test_process_gate(tmp_path):
    code = """from pathlib import Path
from driving_data_platform.embedding.gate import acquire
from driving_data_platform.common.errors import PlatformError
import sys
try:
 with acquire(Path(sys.argv[1])): pass
except PlatformError: sys.exit(2)
"""
    with gate.acquire(tmp_path):
        assert (
            subprocess.run(
                [sys.executable, "-c", code, str(tmp_path)], check=False
            ).returncode
            == 2
        )
    assert (
        subprocess.run(
            [sys.executable, "-c", code, str(tmp_path)], check=False
        ).returncode
        == 0
    )


def test_disconnect_keeps_worker_lock(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def worker():
        try:
            with gate.acquire(tmp_path):
                entered.set()
                release.wait(5)
        finally:
            finished.set()

    async def run():
        task = asyncio.create_task(run_in_threadpool(worker))
        await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        with pytest.raises(PlatformError), gate.acquire(tmp_path):
            pass
        release.set()
        await asyncio.to_thread(finished.wait, 5)

    asyncio.run(run())
    with gate.acquire(tmp_path):
        pass


def test_report_unavailable_returns_503(tmp_path, monkeypatch):
    def denied(*_):
        raise PermissionError("/Users/private report path")

    monkeypatch.setattr(web.a, "safe_parent", denied)
    response = web.recorded(tmp_path, "id", None)
    assert response.status_code == 503
    assert b"/Users" not in response.body


def test_startup_rejects_mixed_database(tmp_path, monkeypatch):
    monkeypatch.setenv("DDP_EMBEDDING_TRIAL", "1" * 32)
    monkeypatch.setenv("DDP_POSTGRES_DSN", "dbname=ddp password=private")
    monkeypatch.setattr(
        web.a,
        "trial_config",
        lambda *_: {
            "host": "127.0.0.1",
            "port": 55434,
            "database": "ddp_embedding_trial_" + "1" * 32,
            "user": "ddp",
            "password": "private",
        },
    )
    with pytest.raises(ValueError, match="拒绝启动") as error:
        web.validate_runtime(tmp_path)
    assert "private" not in str(error.value)


def test_api_does_not_take_unknown_listener(tmp_path, monkeypatch):
    import importlib.util
    import socket
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "embedding_api_process",
        Path(__file__).resolve().parents[1] / "scripts/embedding_api_process.py",
    )
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    monkeypatch.setattr(api, "validate", lambda *_: {})
    monkeypatch.setattr(api, "status", lambda *_: {"running": False})
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        monkeypatch.setattr(api, "PORT", server.getsockname()[1])
        with pytest.raises(OSError):
            api.start(tmp_path, Path("."), "1" * 32)
        assert server.getsockname()[1] == api.PORT


def test_openapi_documents_strict_search_body(tmp_path):
    app = FastAPI()
    app.include_router(web.router(tmp_path))
    schema = app.openapi()["paths"]["/api/embedding/search"]["post"]["requestBody"][
        "content"
    ]["application/json"]["schema"]
    assert set(schema["required"]) == {"generation", "text"}
    assert schema["additionalProperties"] is False
    assert schema["properties"]["limit"]["maximum"] == 100
