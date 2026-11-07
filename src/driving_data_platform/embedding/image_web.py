"""原字节图片查询适配：限量内存接收、无图片落盘、跨入口互斥。"""

import asyncio
import hashlib
import json
import time
import uuid
from datetime import UTC, datetime

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from driving_data_platform.common.errors import PlatformError
from driving_data_platform.embedding import artifacts as a
from driving_data_platform.embedding import gate, web
from driving_data_platform.embedding.images import RULES, validate_image
from driving_data_platform.ingestion.nuscenes import atomic_json

UPLOAD_SECONDS = 30
TYPES = {"image/jpeg", "image/png", "application/octet-stream"}


def parameters(request):
    values = {}
    for key in request.query_params:
        items = request.query_params.getlist(key)
        if key == "required_channels":
            values[key] = items
        elif len(items) != 1:
            raise ValueError("单值参数重复")
        else:
            values[key] = items[0]
    if "limit" in values:
        if not values["limit"].isascii() or not values["limit"].isdecimal():
            raise ValueError("limit非法")
        values["limit"] = int(values["limit"])
    return web.QueryBody.model_validate(values)


def finish(path, report, error=None, result=None):
    report["finished_at"] = datetime.now(UTC).isoformat()
    if error is not None:
        response = web.error_response(error, report["request_id"])
        report.update(status="failed", error=json.loads(response.body)["error"])
    else:
        report.update(
            status="passed",
            result={k: v for k, v in result.items() if k != "items"},
        )
        response = JSONResponse({"request_id": report["request_id"], **result})
    try:
        atomic_json(path, report)
    except OSError:
        return web.error_response(
            PlatformError(
                "RESOURCE_BLOCKED", "search-report-unavailable", "报告不可写"
            ),
            report["request_id"],
        )
    return response


def work(root, body, data, declared, path, report, began):
    try:
        mime = validate_image(data)
        if declared != "application/octet-stream" and declared != mime:
            raise PlatformError("INPUT_INVALID", "image-mime-mismatch", "图片格式不符")
        report["input"]["format"] = mime
        result = web.perform(root, body, True, report["resolved"], image=data)
        result["timings"]["upload_seconds"] = report["upload_seconds"]
        result["timings"]["total_seconds"] = time.monotonic() - began
        return finish(path, report, result=result)
    except Exception as error:  # noqa: BLE001 — HTTP边界统一脱敏并留失败报告
        return finish(path, report, error=error)


def register(routes, root, pending):
    @routes.post(
        "/api/embedding/search/image",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    t: {"schema": {"type": "string", "format": "binary"}} for t in TYPES
                },
            },
            "parameters": [
                {
                    "name": key,
                    "in": "query",
                    "required": key == "generation",
                    "schema": schema,
                }
                for key, schema in web.QueryBody.model_json_schema()[
                    "properties"
                ].items()
            ],
        },
    )
    async def endpoint(request: Request):
        began = time.monotonic()
        identifier = uuid.uuid4().hex
        path = None
        lease = None
        submitted = False
        report = {
            "report_schema_version": 1,
            "request_id": identifier,
            "command": "embedding-web-image-search",
            "environment": {"trial": web.trial(), "deployment": web.deployment()},
            "status": "running",
            "started_at": datetime.now(UTC).isoformat(),
            "resolved": {},
            "input": {"type": "image", "bytes_received": 0},
        }
        try:
            path = a.safe_parent(root, "reports") / ("http-" + identifier + ".json")
            atomic_json(path, report)
            try:
                body = parameters(request)
            except ValueError:
                raise PlatformError(
                    "INPUT_INVALID", "image-parameters", "图片检索参数非法"
                ) from None
            report["input"].update(body.model_dump())
            declared = request.headers.get("content-type", "").split(";", 1)[0].lower()
            if declared not in TYPES:
                raise PlatformError(
                    "INPUT_INVALID", "image-content-type", "请求类型不支持"
                )
            length = request.headers.get("content-length")
            if length is not None:
                if not length.isascii() or not length.isdecimal():
                    raise PlatformError("INPUT_INVALID", "image-length", "请求长度非法")
                if int(length) > RULES["max_bytes"]:
                    raise PlatformError("INPUT_INVALID", "image-body-limit", "图片超限")
            if not web.enabled():
                raise PlatformError(
                    "DEPENDENCY_UNAVAILABLE", "search-disabled", "检索未启用"
                )
            lease = gate.acquire(root)
            lease.__enter__()
            raw = bytearray()
            try:
                async with asyncio.timeout(UPLOAD_SECONDS):
                    async for chunk in request.stream():
                        report["input"]["bytes_received"] += len(chunk)
                        if len(raw) + len(chunk) > RULES["max_bytes"]:
                            raise PlatformError(
                                "INPUT_INVALID", "image-body-limit", "图片超限"
                            )
                        raw.extend(chunk)
            except TimeoutError:
                raise PlatformError(
                    "INPUT_INVALID", "image-upload-timeout", "接收超时"
                ) from None
            data = bytes(raw)
            del raw
            report["upload_seconds"] = time.monotonic() - began
            report["input"]["sha256"] = hashlib.sha256(data).hexdigest()
            task = asyncio.create_task(
                run_in_threadpool(work, root, body, data, declared, path, report, began)
            )
            pending.add(task)
            owned = lease

            def completed(future):
                owned.__exit__(None, None, None)
                pending.discard(future)
                if not future.cancelled():
                    future.exception()

            task.add_done_callback(completed)
            submitted = True
            return await asyncio.shield(task)
        except (ClientDisconnect, asyncio.CancelledError):
            if not submitted and path is not None:
                finish(
                    path,
                    report,
                    error=PlatformError(
                        "INTERRUPTED", "image-upload-disconnected", "上传中断"
                    ),
                )
            raise
        except Exception as error:  # noqa: BLE001 — 请求接收边界统一脱敏
            if path is None:
                return web.error_response(error, identifier)
            return finish(path, report, error=error)
        finally:
            if lease is not None and not submitted:
                lease.__exit__(None, None, None)
