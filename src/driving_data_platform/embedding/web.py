"""显式登记的正式或试验环境 HTTP 适配；不回显输入或内部异常。"""

import asyncio
import hashlib
import json
import os
import time
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import urlencode

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from driving_data_platform.common.errors import ADVICE, PlatformError, describe
from driving_data_platform.embedding import artifacts as a
from driving_data_platform.embedding import gate, search, store
from driving_data_platform.ingestion.nuscenes import atomic_json


class QueryBody(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    generation: str = Field(pattern=r"^g_[a-f0-9]{64}$")
    embedding_generation: str | None = Field(default=None, pattern=r"^e_[a-f0-9]{64}$")
    location: str | None = None
    category: str | None = None
    start: str | None = None
    end: str | None = None
    required_channels: list[str] = Field(default_factory=list, max_length=12)
    image_channel: str | None = None
    limit: int = Field(default=20, ge=1, le=100)


class SearchBody(QueryBody):
    text: str = Field(max_length=4096)


def trial():
    return os.environ.get("DDP_EMBEDDING_TRIAL") or None


def deployment():
    return os.environ.get("DDP_EMBEDDING_DEPLOYMENT") or None


def enabled():
    return bool(trial() or deployment())


def target():
    if trial() and deployment():
        raise ValueError("试验与部署环境不得同时指定")
    return {"deployment": deployment()} if deployment() else {"trial": trial()}


def validate_runtime(root):
    """启动时验证业务 DSN 与向量登记相同，阻止继承其他数据库连接。"""
    if not enabled():
        return
    from psycopg.conninfo import conninfo_to_dict

    if deployment():
        from driving_data_platform.embedding import deployment as d

        target()
        state = d.registration(root, deployment())
        d.container(state)
        state = {
            **state,
            "password": dict(
                line.split("=", 1)
                for line in (root / "compose.env").read_text().splitlines()
            )["DDP_POSTGRES_PASSWORD"],
        }
    else:
        state = a.trial_config(root, trial())
    connection = conninfo_to_dict(os.environ.get("DDP_POSTGRES_DSN", ""))
    if any(
        str(connection.get(k, "")) != str(state[v])
        for k, v in (
            ("host", "host"),
            ("port", "port"),
            ("dbname", "database"),
            ("user", "user"),
            ("password", "password"),
        )
    ):
        raise ValueError("API 业务连接与登记不一致，拒绝启动")
    with store.connect(root, **target()) as conn:
        store.check_environment(conn, False)


def error_response(error, request_id):
    value = describe(error, "embedding-search")
    category, code = value["category"], value["code"]
    status = {
        "INPUT_INVALID": 422,
        "CONTRACT_CONFLICT": 409,
        "DEPENDENCY_UNAVAILABLE": 503,
        "OPERATION_CONFLICT": 429,
    }.get(category, 500)
    if code == "search-not-found":
        status = 404
    if code == "image-upload-timeout":
        status = 408
    if code == "image-content-type":
        status = 415
    if code == "image-body-limit":
        status = 413
    if code == "search-body-limit":
        status = 413
    if code == "search-disabled":
        status = 503
    if code == "model-identity":
        status = 503
    if isinstance(error, psycopg.Error):
        status = 503
        value.update(
            category="DEPENDENCY_UNAVAILABLE", code="search-database-unavailable"
        )
    elif isinstance(error, OSError):
        status = 503
        value.update(category="DEPENDENCY_UNAVAILABLE", code="search-local-io")
    value["advice"] = ADVICE[value["category"]]
    messages = {
        422: "检索输入不合法，请检查文本、图片、时间和筛选条件。",
        408: "图片上传超过30秒，请重新提交。",
        415: "仅接受JPEG、PNG或二进制图片请求体。",
        404: "指定业务或向量版本不存在。",
        409: "检索数据未建立或版本、空间契约不兼容。",
        429: "当前检索繁忙，请稍后手动重试。",
        503: "检索依赖未就绪或当前不可用，请检查服务状态。",
        413: "请求体不得超过 64 KiB。",
        500: "检索内部错误，请按请求编号查看报告。",
    }
    value["message"] = (
        "图片不得超过25 MiB。" if code == "image-body-limit" else messages[status]
    )
    # 只保留规范化错误字段，禁止传播内部异常文本。
    return JSONResponse(
        status_code=status,
        content={"request_id": request_id, "error": value},
        headers={"Retry-After": "1"} if status == 429 else None,
    )


def missing(code, message):
    return PlatformError("CONTRACT_CONFLICT", code, message)


def check_version(conn, body):
    if (
        conn.execute(
            "SELECT 1 FROM control.generation WHERE generation=%s AND status='READY'",
            (body.generation,),
        ).fetchone()
        is None
    ):
        raise missing("search-not-found", "业务版本不存在")
    if body.embedding_generation is not None and (
        conn.execute(
            "SELECT 1 FROM control.embedding_generation WHERE embedding_generation=%s",
            (body.embedding_generation,),
        ).fetchone()
        is None
    ):
        raise missing("search-not-found", "向量版本不存在")


def perform(root, body, locked=False, trace=None, *, image=None):
    if not enabled():
        raise PlatformError("DEPENDENCY_UNAVAILABLE", "search-disabled", "未启用检索")
    values = body.model_dump()
    values["required_channel"] = values.pop("required_channels")
    args = SimpleNamespace(**values, trial=trial(), deployment=deployment())
    search.conditions(args)
    with store.connect(root, **target()) as conn:
        conn.execute("SET statement_timeout='10s'")
        check_version(conn, body)
    try:
        result = (
            search.query(
                root, args, trace, **({"image": image} if image is not None else {})
            )
            if locked
            else search.execute(
                root, args, trace, **({"image": image} if image is not None else {})
            )
        )
    except PlatformError:
        raise
    except (ValueError, OSError):
        raise missing("search-contract", "检索契约或本地空间不可用") from None
    for item in result["items"]:
        suffix = "?" + urlencode({"generation": result["generation"]})
        item["media_url"] = "/api/media/" + item["media_id"] + suffix
        item["detail_url"] = "/api/frames/" + item["frame_id"] + suffix
    return {
        key: result[key]
        for key in (
            "status",
            "generation",
            "embedding_generation",
            "space_id",
            "artifact_id",
            "filters",
            "count",
            "timings",
            "items",
        )
    }


def recorded(root, request_id, body, parse_error=None, summary=None, locked=False):
    start = time.monotonic()
    report = {
        "report_schema_version": 1,
        "request_id": request_id,
        "command": "embedding-web-search",
        "environment": {"trial": trial(), "deployment": deployment()},
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
        "resolved": {},
        "input": summary or {},
    }
    if body is not None:
        report["input"].update(
            generation=body.generation, embedding_generation=body.embedding_generation
        )
    path = None
    try:
        path = a.safe_parent(root, "reports") / ("http-" + request_id + ".json")
        atomic_json(path, report)
        if parse_error:
            raise parse_error
        result = perform(root, body, locked, report["resolved"])
        report.update(
            status="passed",
            result={
                k: result[k]
                for k in (
                    "generation",
                    "embedding_generation",
                    "space_id",
                    "artifact_id",
                    "count",
                    "filters",
                    "timings",
                )
            },
        )
        response = JSONResponse({"request_id": request_id, **result})
    except Exception as error:  # noqa: BLE001 — HTTP 边界统一脱敏并记录失败
        response = error_response(error, request_id)
        report.update(status="failed", error=json.loads(response.body)["error"])
    report["seconds"] = time.monotonic() - start
    report["finished_at"] = datetime.now(UTC).isoformat()
    try:
        if path is None:
            raise OSError("报告不可写")
        atomic_json(path, report)
    except OSError:
        return error_response(
            PlatformError(
                "DEPENDENCY_UNAVAILABLE", "search-report-unavailable", "报告不可写"
            ),
            request_id,
        )
    return response


def router(root):
    routes = APIRouter()
    pending = set()

    @routes.post(
        "/api/embedding/search",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {"schema": SearchBody.model_json_schema()}
                },
            }
        },
    )
    async def endpoint(request: Request):
        request_id = uuid.uuid4().hex
        body = None
        error = None
        summary = {}
        try:
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > 64 * 1024:
                    raise PlatformError(
                        "INPUT_INVALID", "search-body-limit", "请求体超限"
                    )
                raw.extend(chunk)
            value = json.loads(raw)
            if isinstance(value, dict) and isinstance(value.get("text"), str):
                summary = {
                    "query_sha256": hashlib.sha256(
                        value["text"].encode("utf-8", errors="surrogatepass")
                    ).hexdigest(),
                    "query_characters": len(value["text"]),
                }
            body = SearchBody.model_validate(value)
        except (ValueError, ValidationError, UnicodeError) as caught:
            error = (
                caught
                if isinstance(caught, PlatformError)
                else PlatformError("INPUT_INVALID", "search-input", "请求格式不合法")
            )
        lease = None
        if error is None and enabled():
            try:
                lease = gate.acquire(root)
                lease.__enter__()
            except (OSError, ValueError) as caught:
                lease = None
                error = caught
        # 先取得非阻塞文件锁，再提交工作线程；取消 HTTP 等待不会取消实际工作或提前放锁。
        task = asyncio.create_task(
            run_in_threadpool(
                recorded, root, request_id, body, error, summary, lease is not None
            )
        )
        pending.add(task)

        def completed(future):
            if lease is not None:
                lease.__exit__(None, None, None)
            pending.discard(future)
            if not future.cancelled():
                future.exception()

        task.add_done_callback(completed)
        return await asyncio.shield(task)

    @routes.get("/api/embedding/status")
    def status(generation: str):
        request_id = uuid.uuid4().hex
        if not enabled():
            return {"enabled": False, "status": "disabled", "generation": generation}
        try:
            from driving_data_platform.common.run_query import identity

            identity(generation, r"g_[a-f0-9]{64}")
            with store.connect(root, **target()) as conn:
                conn.execute("SET statement_timeout='10s'")
                if (
                    conn.execute(
                        "SELECT 1 FROM control.generation WHERE generation=%s AND status='READY'",
                        (generation,),
                    ).fetchone()
                    is None
                ):
                    raise missing("search-not-found", "业务版本不存在")
                try:
                    meta, _ = search.resolve(root, conn, generation)
                except PlatformError as error:
                    if error.code == "search-no-data":
                        return {
                            "enabled": True,
                            "status": "no_data",
                            "generation": generation,
                        }
                    if error.code == "search-no-media":
                        return {
                            "enabled": True,
                            "status": "no_media",
                            "generation": generation,
                        }
                    raise
                except (ValueError, OSError):
                    raise missing(
                        "search-contract", "检索空间或环境登记不兼容"
                    ) from None
            from driving_data_platform.embedding.runtime import encoder

            encoder(root, meta["space_id"]).probe()
            return {
                "enabled": True,
                "status": "ready",
                **{
                    k: meta[k]
                    for k in (
                        "generation",
                        "embedding_generation",
                        "space_id",
                        "artifact_id",
                    )
                },
            }
        except Exception as error:  # noqa: BLE001 — 状态接口不泄露内部信息
            return error_response(error, request_id)

    from driving_data_platform.embedding.image_web import register

    register(routes, root, pending)
    return routes
