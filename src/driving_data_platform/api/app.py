"""仅回环监听的版本化数据查询与安全媒体服务。"""

import csv
import hashlib
import io
import json
import os
import stat
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import psycopg
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from driving_data_platform.common.config import (
    application_config,
    output_root,
)
from driving_data_platform.publishing.store import active, connect

# 导入时允许尚未配置，便于工具加载模块；真正启动服务时由 lifespan 严格检查。
# ROOT 是仓库外的运行目录；REPO 只用于定位仓库内的页面等项目文件。
ROOT = output_root(required=False)
REPO = Path(__file__).resolve().parents[3]


@asynccontextmanager
async def lifespan(application):
    # yield 前是启动检查：启用向量环境时，核对业务连接与登记的检索环境一致。
    # yield 将控制权交给服务运行阶段；当前没有额外的关闭清理操作。
    from driving_data_platform.embedding.web import validate_runtime

    if ROOT is None:
        raise ValueError(
            "storage.output_root: configure DDP_OUTPUT_ROOT or DDP_CONFIG before API startup"
        )
    from driving_data_platform.common.config import embedding_enabled

    if not embedding_enabled(ROOT):
        from driving_data_platform.embedding.web import enabled

        if enabled():
            raise RuntimeError(
                "embedding.enabled: false conflicts with selected embedding runtime"
            )
    validate_runtime(ROOT)
    yield


# 回环监听地址由启动脚本设置；此处声明应用和交互式接口文档位置。
app = FastAPI(title="驾驶数据质量与样本筛选", docs_url="/api/docs", lifespan=lifespan)
# 导出上限：行数，以及数据文件与清单的未压缩总字节数，不是 ZIP 大小。
MAX_ROWS = 100000
MAX_BYTES = 100 * 1024**2


@app.exception_handler(psycopg.Error)
async def database_error(request: Request, exc: psycopg.Error):
    # 对外统一返回 503，不回显原始 SQL、连接信息或数据库异常细节。
    return JSONResponse(
        status_code=503, content={"detail": "数据库不可用，请查看服务状态与运行日志"}
    )


def generation(conn, requested):
    # 显式版本优先；否则读取活动指针。后续查询复用该身份，避免一次响应混用版本。
    selected = requested if requested is not None else active(conn)["generation"]
    row = conn.execute(
        "SELECT snapshot_id,processing_version,manifest FROM control.generation WHERE generation=%s AND status='READY'",
        (selected,),
    ).fetchone()
    if row is None:
        # 指定版本不可用通常表示资源不存在；默认没有可服务版本表示服务未就绪。
        raise HTTPException(
            404 if requested else 503, "指定版本不存在或当前没有已发布版本"
        )
    manifest = row[2]
    return {
        "generation": selected,
        "snapshot_id": row[0],
        "processing_version": row[1],
        "profile_id": manifest.get("profile_id"),
        "profile_version": manifest.get("profile_version"),
        "asset_policy": manifest.get("asset_policy"),
        "assets": manifest.get("assets", {}),
        "capabilities": manifest.get("capabilities", {}),
    }


def time_us(value):
    # None 表示不设置该时间边界；其余输入必须带时区且 UTC 偏移为零。
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError()
        epoch = datetime.fromisoformat("1970-01-01T00:00:00+00:00")
        delta = parsed - epoch
        # 全程使用整数计算，避免 timestamp() 浮点转换损失微秒精度。
        return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
    except (ValueError, OverflowError):
        raise HTTPException(422, "时间必须是包含 Z 或 +00:00 的 UTC ISO 时间") from None


def filters(location, start, end, category, channel):
    # 列表查询、导出及检索共享筛选语义：时间闭区间，多个必需通道取交集。
    start_us, end_us = time_us(start), time_us(end)
    if start_us is not None and end_us is not None and start_us > end_us:
        raise HTTPException(422, "开始时间不得晚于结束时间")
    if len(channel) > 12:
        raise HTTPException(422, "必需通道最多 12 个")
    # 先限制原始通道条目数，再去重排序，使回传条件和导出清单具有稳定顺序。
    return {
        "location": location,
        "start_us": start_us,
        "end_us": end_us,
        "category": category,
        "channels": sorted(set(channel)),
    }


def where(selected, conditions):
    # 调用方必须使用 f 作为帧表别名；返回 SQL 片段和独立的参数列表。
    # 列名、运算符来自下方固定列表；用户输入仅通过 %s 绑定，不直接拼进 SQL。
    clauses = ["f.generation=%s"]
    values = [selected]
    for key, column, operator in [
        ("location", "location", "="),
        ("start_us", "timestamp_us", ">="),
        ("end_us", "timestamp_us", "<="),
    ]:
        if conditions[key] is not None:
            clauses.append(f"f.{column}{operator}%s")
            values.append(conditions[key])
    if conditions["category"] is not None:
        # EXISTS 只判断是否包含完整类别，避免同帧多个同类标注产生重复帧。
        clauses.append(
            "EXISTS(SELECT 1 FROM serving.frame_category c WHERE c.generation=f.generation AND c.frame_id=f.entity_id AND c.category=%s)"
        )
        values.append(conditions["category"])
    for channel in conditions["channels"]:
        # 每个通道各生成一个 EXISTS，再用 AND 连接，要求所有指定通道都具备。
        clauses.append(
            "EXISTS(SELECT 1 FROM serving.frame_channel c WHERE c.generation=f.generation AND c.frame_id=f.entity_id AND c.channel=%s)"
        )
        values.append(channel)
    return " AND ".join(clauses), values


@app.get("/api/health")
def health():
    # 检查数据库和当前 READY 活动业务版本；不代表模型、Spark/HMS 全部健康。
    with connect(ROOT) as conn:
        meta = generation(conn, None)
        return {"status": "ok", **meta}


@app.get("/api/versions")
def versions():
    # 同时返回活动指针（含 revision）和版本登记列表；列表不只包含 READY。
    with connect(ROOT) as conn:
        pointer = active(conn)
        values = conn.execute(
            "SELECT generation,snapshot_id,processing_version,status,created_at FROM control.generation ORDER BY created_at DESC"
        ).fetchall()
        return {
            "active": pointer,
            "versions": [
                dict(
                    zip(
                        (
                            "generation",
                            "snapshot_id",
                            "processing_version",
                            "status",
                            "created_at",
                        ),
                        row,
                    )
                )
                for row in values
            ],
        }


@app.get("/api/quality")
def quality(generation_id: str | None = Query(None, alias="generation")):
    # Python 参数名避开 generation() 函数；HTTP 参数仍名为 generation。
    # 读取已保存的质量报告，不在请求中重新检查原始数据集。
    with connect(ROOT) as conn:
        meta = generation(conn, generation_id)
        data = conn.execute(
            "SELECT quality FROM control.generation WHERE generation=%s",
            (meta["generation"],),
        ).fetchone()[0]
        return {**meta, "quality": data}


@app.get("/api/analytics")
def analytics(generation_id: str | None = Query(None, alias="generation")):
    # 在线只读取离线产物：statistics 为分布；dimension 包含字典和全局指标。
    with connect(ROOT) as conn:
        meta = generation(conn, generation_id)
        statistics = [
            r[0]
            for r in conn.execute(
                "SELECT payload FROM serving.statistics WHERE generation=%s ORDER BY dimension,value",
                (meta["generation"],),
            )
        ]
        dimensions = {}
        for kind, payload in conn.execute(
            "SELECT kind,payload FROM serving.dimension WHERE generation=%s ORDER BY kind,identity",
            (meta["generation"],),
        ):
            # 按 kind 分组返回，例如类别、地点、传感器和 metrics。
            dimensions.setdefault(kind, []).append(payload)
        return {**meta, "statistics": statistics, "dimensions": dimensions}


@app.get("/api/scenes")
def scenes(generation_id: str | None = Query(None, alias="generation")):
    # 当前一次返回该版本的全部场景，按实体身份排序，没有单独分页。
    with connect(ROOT) as conn:
        meta = generation(conn, generation_id)
        return {
            **meta,
            "items": [
                r[0]
                for r in conn.execute(
                    "SELECT payload FROM serving.scene WHERE generation=%s ORDER BY entity_id",
                    (meta["generation"],),
                )
            ],
        }


@app.get("/api/frames")
def frames(
    generation_id: str | None = Query(None, alias="generation"),
    location: str | None = None,
    start: str | None = None,
    end: str | None = None,
    category: str | None = None,
    channel: list[str] = Query(default=[]),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    # channel 可通过重复查询参数传入；limit/offset 范围由 FastAPI 在调用前验证。
    conditions = filters(location, start, end, category, channel)
    with connect(ROOT) as conn:
        meta = generation(conn, generation_id)
        clause, values = where(meta["generation"], conditions)
        # 总数与分页复用完全相同的条件；已选定的 READY 版本不会随发布而改写。
        total = conn.execute(
            "SELECT count(*) FROM serving.frame f WHERE " + clause, values
        ).fetchone()[0]
        items = [
            r[0]
            for r in conn.execute(
                "SELECT f.payload FROM serving.frame f WHERE "
                + clause
                # 同一时间戳再按实体身份排序，保证分页顺序明确。
                + " ORDER BY f.timestamp_us,f.entity_id LIMIT %s OFFSET %s",
                [*values, limit, offset],
            )
        ]
        return {
            **meta,
            "filters": conditions,
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": items,
        }


@app.get("/api/frames/{identity}")
def detail(identity: str, generation_id: str = Query(..., alias="generation")):
    # 详情必须显式携带列表所选版本，避免点击详情时活动版本已经切换。
    with connect(ROOT) as conn:
        meta = generation(conn, generation_id)
        frame = conn.execute(
            "SELECT payload FROM serving.frame WHERE generation=%s AND entity_id=%s",
            (generation_id, identity),
        ).fetchone()
        if frame is None:
            raise HTTPException(404, "关键帧不存在")
        # 只列关键帧媒体，不包含 sweep；这里未限定 camera，其他媒体登记也可返回。
        media = [
            r[0]
            for r in conn.execute(
                "SELECT payload FROM serving.media WHERE generation=%s AND frame_id=%s AND is_key_frame ORDER BY channel",
                (generation_id, identity),
            )
        ]
        annotations = [
            r[0]
            for r in conn.execute(
                "SELECT payload FROM serving.annotation WHERE generation=%s AND frame_id=%s ORDER BY entity_id",
                (generation_id, identity),
            )
        ]
        return {**meta, "frame": frame[0], "media": media, "annotations": annotations}


def image_bytes(root: Path, entry: dict):
    # entry 来自数据库媒体登记，不接受客户端直接指定文件路径。
    # 只服务 JPEG，并拒绝绝对路径和父目录跳转。
    relative = Path(entry["path"])
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.suffix.lower() not in {".jpg", ".jpeg"}
    ):
        raise ValueError("非法图片路径或类型")
    if any(p.is_symlink() for p in [root, *root.parents]):
        raise ValueError("图片根目录包含符号链接")
    # 逐级通过目录描述符打开路径，各级拒绝符号链接；不先 resolve 再按路径读取。
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
        file = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
        with os.fdopen(file, "rb") as stream:
            # fstat 校验实际已打开的文件，而不是重新按路径检查另一个文件对象。
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_size != entry["bytes"]
                or not 0 < info.st_size <= 25 * 1024**2
            ):
                raise ValueError("图片大小或文件类型不一致")
            data = stream.read(25 * 1024**2 + 1)
        # 起止字节是基础格式检查，不是完整解码；摘要用于核对已登记的文件身份。
        if (
            not data.startswith(b"\xff\xd8\xff")
            or not data.endswith(b"\xff\xd9")
            or hashlib.sha256(data).hexdigest() != entry["sha256"]
        ):
            raise ValueError("图片格式或摘要不一致")
        return data
    finally:
        # 无论路径检查、读取还是摘要校验失败，都释放当前目录描述符。
        os.close(descriptor)


@app.get("/api/media/{identity}")
def media(identity: str, generation_id: str = Query(..., alias="generation")):
    # 先按版本验证“关键帧相机媒体”登记，再访问仓库外只读数据集。
    with connect(ROOT) as conn:
        meta = generation(conn, generation_id)
        row = conn.execute(
            "SELECT payload FROM serving.media WHERE generation=%s AND entity_id=%s",
            (generation_id, identity),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "图片登记不存在")
    if (
        not meta["capabilities"].get("original_media")
        or row[0].get("asset_state") != "verified"
    ):
        raise HTTPException(409, "当前版本媒体文件未校验，不提供原图")
    if row[0]["modality"] != "camera" or not row[0]["is_key_frame"]:
        raise HTTPException(404, "图片登记不存在")
    # 文件读取放在数据库连接上下文之外，不占用连接完成图片 I/O 和摘要计算。
    try:
        config = application_config(ROOT)
        data = image_bytes(Path(config["dataset"]["root"]), row[0])
    except (OSError, ValueError):
        raise HTTPException(409, "图片不可用：路径、类型、大小或摘要检查失败") from None
    return Response(
        data,
        media_type="image/jpeg",
        headers={
            # ETag 标识内容；此处尚未处理 If-None-Match，也不会据此返回 304。
            "ETag": '"' + row[0]["sha256"] + '"',
            "X-Content-Type-Options": "nosniff",
        },
    )


def export_bundle(items, meta, conditions, format):
    # 内存构造清单 ZIP；不是流式导出，字节限制也不等于峰值内存限制。
    if len(items) > MAX_ROWS:
        raise HTTPException(413, "导出超过 100000 行")
    lines = []
    if format == "csv":
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "generation",
                "snapshot_id",
                "processing_version",
                "profile_id",
                "profile_version",
                "asset_policy",
                "original_media",
                "image_search",
                "entity_id",
                "source_token",
                "timestamp_us",
                "scene_id",
                "scene_name",
                "location",
            ],
        )
        writer.writeheader()
        for item in items:
            record = meta | item
            capabilities = meta.get("capabilities", {})
            record["original_media"] = capabilities.get("original_media", False)
            record["image_search"] = capabilities.get("image_search", False)
            # 防止导出文本被电子表格当作公式执行；原身份及值可由 JSONL 无损获取。
            writer.writerow(
                {
                    key: (
                        "'" + record[key]
                        if isinstance(record.get(key), str)
                        and record[key].startswith(("=", "+", "-", "@"))
                        else record.get(key)
                    )
                    for key in writer.fieldnames
                }
            )
        data = stream.getvalue().encode("utf-8")
    else:
        # JSONL 每行是一个完整 JSON 对象，附带版本信息，保留原始字符串值。
        for item in items:
            lines.append(
                json.dumps(meta | item, ensure_ascii=False, sort_keys=True) + "\n"
            )
        data = "".join(lines).encode("utf-8")
    # sha256/bytes 只描述 frames 数据文件；filters 与 order 说明筛选和排序口径。
    manifest = {
        **meta,
        "filters": conditions,
        "count": len(items),
        "format": format,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "order": ["timestamp_us", "entity_id"],
    }
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode(
        "utf-8"
    )
    if len(data) + len(manifest_bytes) > MAX_BYTES:
        raise HTTPException(413, "导出超过 100 MiB")
    output = io.BytesIO()
    # 仅打包关键帧清单和追溯清单，不包含图片、标注文件或原始数据集。
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("frames." + format, data)
        archive.writestr("manifest.json", manifest_bytes)
    return output.getvalue()


@app.get("/api/export")
def export(
    generation_id: str = Query(..., alias="generation"),
    format: str = Query("jsonl", pattern="^(jsonl|csv)$"),
    location: str | None = None,
    start: str | None = None,
    end: str | None = None,
    category: str | None = None,
    channel: list[str] = Query(default=[]),
):
    # 显式固定业务版本；格式限制与筛选规则分别由 Query、filters 负责校验。
    conditions = filters(location, start, end, category, channel)
    with connect(ROOT) as conn:
        meta = generation(conn, generation_id)
        clause, values = where(generation_id, conditions)
        count = conn.execute(
            "SELECT count(*) FROM serving.frame f WHERE " + clause, values
        ).fetchone()[0]
        # 先计数，防止超出行数上限后仍把全部匹配行取入内存。
        if count > MAX_ROWS:
            raise HTTPException(413, "导出超过 100000 行")
        items = [
            r[0]
            for r in conn.execute(
                "SELECT payload FROM serving.frame f WHERE "
                + clause
                + " ORDER BY timestamp_us,entity_id",
                values,
            )
        ]
    data = export_bundle(items, meta, conditions, format)
    return Response(
        data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="frames-{generation_id[2:14]}-{format}.zip"'
        },
    )


@app.get("/")
def index():
    # 仅提供工作台首页，此处没有挂载整个目录作为通用静态文件服务。
    return FileResponse(REPO / "frontend/index.html")


# 证据接口只暴露规范化字段；错误信息不含宿主路径或原始异常。
def evidence_query(function, *args, **kwargs):
    # 实际安全读取与 schema 校验在底层实现；这里统一转换为公开 HTTP 错误。
    from driving_data_platform.common.run_query import EvidenceError

    try:
        return function(*args, **kwargs)
    except FileNotFoundError:
        raise HTTPException(404, "运行或服务版本不存在") from None
    except EvidenceError as error:
        # EvidenceError 继承 ValueError，必须先捕获，保留证据冲突的 409 语义。
        raise HTTPException(409, str(error)) from None
    except ValueError:
        raise HTTPException(422, "非法查询参数") from None
    except OSError:
        raise HTTPException(409, "证据不可安全读取") from None


@app.get("/api/runs")
def runs(
    status: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    # 查询已有运行报告，不实时探测进程，也不补写历史报告。
    from driving_data_platform.common.run_query import list_runs, publication_status

    result = evidence_query(list_runs, ROOT, status or None, limit, offset)
    # 发布状态核对以 warnings 返回，不改写磁盘上的原报告。
    result["warnings"].extend(publication_status(ROOT, result["items"]))
    return result


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str):
    # normalized 只输出规范化公开字段，不把原始报告直接透传给客户端。
    from driving_data_platform.common.run_query import normalized, publication_status

    result = evidence_query(normalized, ROOT, run_id)
    result["warnings"] = publication_status(ROOT, [result])
    return result


@app.get("/api/lineage")
def lineage_query(run_id: str | None = None, generation: str | None = None):
    # 根据运行或业务版本追溯表级关系；参数组合及身份格式由 lineage 模块校验。
    from driving_data_platform.lineage import lineage

    return evidence_query(lineage, ROOT, REPO, run_id=run_id, generation=generation)


# 独立检索模式不改变默认实例的数据库及既有健康语义。
from driving_data_platform.embedding import web as embedding_web

app.include_router(embedding_web.router(ROOT))
