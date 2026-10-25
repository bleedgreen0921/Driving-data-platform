"""仅回环监听的版本化数据查询与安全媒体服务。"""

import csv
import hashlib
import io
import json
import os
import stat
import zipfile
from datetime import datetime
from pathlib import Path

import psycopg
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from driving_data_platform.common.config import load_config
from driving_data_platform.publishing.store import active, connect

ROOT = Path(
    os.environ.get(
        "DDP_OUTPUT_ROOT", "/Users/qiujiajun/Project/driving-data-platform-artifacts"
    )
)
REPO = Path(__file__).resolve().parents[3]
app = FastAPI(title="驾驶数据质量与样本筛选", docs_url="/api/docs")
MAX_ROWS = 100000
MAX_BYTES = 100 * 1024**2


@app.exception_handler(psycopg.Error)
async def database_error(request: Request, exc: psycopg.Error):
    return JSONResponse(
        status_code=503, content={"detail": "数据库不可用，请查看服务状态与运行日志"}
    )


def generation(conn, requested):
    selected = requested if requested is not None else active(conn)["generation"]
    row = conn.execute(
        "SELECT snapshot_id,processing_version FROM control.generation WHERE generation=%s AND status='READY'",
        (selected,),
    ).fetchone()
    if row is None:
        raise HTTPException(
            404 if requested else 503, "指定版本不存在或当前没有已发布版本"
        )
    return {"generation": selected, "snapshot_id": row[0], "processing_version": row[1]}


def time_us(value):
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError()
        epoch = datetime.fromisoformat("1970-01-01T00:00:00+00:00")
        delta = parsed - epoch
        return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
    except (ValueError, OverflowError):
        raise HTTPException(422, "时间必须是包含 Z 或 +00:00 的 UTC ISO 时间") from None


def filters(location, start, end, category, channel):
    start_us, end_us = time_us(start), time_us(end)
    if start_us is not None and end_us is not None and start_us > end_us:
        raise HTTPException(422, "开始时间不得晚于结束时间")
    if len(channel) > 12:
        raise HTTPException(422, "必需通道最多 12 个")
    return {
        "location": location,
        "start_us": start_us,
        "end_us": end_us,
        "category": category,
        "channels": sorted(set(channel)),
    }


def where(selected, conditions):
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
        clauses.append(
            "EXISTS(SELECT 1 FROM serving.frame_category c WHERE c.generation=f.generation AND c.frame_id=f.entity_id AND c.category=%s)"
        )
        values.append(conditions["category"])
    for channel in conditions["channels"]:
        clauses.append(
            "EXISTS(SELECT 1 FROM serving.frame_channel c WHERE c.generation=f.generation AND c.frame_id=f.entity_id AND c.channel=%s)"
        )
        values.append(channel)
    return " AND ".join(clauses), values


@app.get("/api/health")
def health():
    with connect(ROOT) as conn:
        meta = generation(conn, None)
        return {"status": "ok", **meta}


@app.get("/api/versions")
def versions():
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
    with connect(ROOT) as conn:
        meta = generation(conn, generation_id)
        data = conn.execute(
            "SELECT quality FROM control.generation WHERE generation=%s",
            (meta["generation"],),
        ).fetchone()[0]
        return {**meta, "quality": data}


@app.get("/api/analytics")
def analytics(generation_id: str | None = Query(None, alias="generation")):
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
            dimensions.setdefault(kind, []).append(payload)
        return {**meta, "statistics": statistics, "dimensions": dimensions}


@app.get("/api/scenes")
def scenes(generation_id: str | None = Query(None, alias="generation")):
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
    conditions = filters(location, start, end, category, channel)
    with connect(ROOT) as conn:
        meta = generation(conn, generation_id)
        clause, values = where(meta["generation"], conditions)
        total = conn.execute(
            "SELECT count(*) FROM serving.frame f WHERE " + clause, values
        ).fetchone()[0]
        items = [
            r[0]
            for r in conn.execute(
                "SELECT f.payload FROM serving.frame f WHERE "
                + clause
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
    with connect(ROOT) as conn:
        meta = generation(conn, generation_id)
        frame = conn.execute(
            "SELECT payload FROM serving.frame WHERE generation=%s AND entity_id=%s",
            (generation_id, identity),
        ).fetchone()
        if frame is None:
            raise HTTPException(404, "关键帧不存在")
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
    relative = Path(entry["path"])
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.suffix.lower() not in {".jpg", ".jpeg"}
    ):
        raise ValueError("非法图片路径或类型")
    if any(p.is_symlink() for p in [root, *root.parents]):
        raise ValueError("图片根目录包含符号链接")
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
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_size != entry["bytes"]
                or not 0 < info.st_size <= 25 * 1024**2
            ):
                raise ValueError("图片大小或文件类型不一致")
            data = stream.read(25 * 1024**2 + 1)
        if (
            not data.startswith(b"\xff\xd8\xff")
            or not data.endswith(b"\xff\xd9")
            or hashlib.sha256(data).hexdigest() != entry["sha256"]
        ):
            raise ValueError("图片格式或摘要不一致")
        return data
    finally:
        os.close(descriptor)


@app.get("/api/media/{identity}")
def media(identity: str, generation_id: str = Query(..., alias="generation")):
    with connect(ROOT) as conn:
        generation(conn, generation_id)
        row = conn.execute(
            "SELECT payload FROM serving.media WHERE generation=%s AND entity_id=%s AND modality='camera' AND is_key_frame",
            (generation_id, identity),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "图片登记不存在")
    try:
        config = load_config(ROOT / "application.yaml")
        data = image_bytes(Path(config["dataset"]["root"]), row[0])
    except (OSError, ValueError):
        raise HTTPException(409, "图片不可用：路径、类型、大小或摘要检查失败") from None
    return Response(
        data,
        media_type="image/jpeg",
        headers={
            "ETag": '"' + row[0]["sha256"] + '"',
            "X-Content-Type-Options": "nosniff",
        },
    )


def export_bundle(items, meta, conditions, format):
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
            # 防止导出文本被电子表格当作公式执行；原身份及值可由 JSONL 无损获取。
            writer.writerow(
                {
                    k: (
                        "'" + v
                        if isinstance(v, str) and v.startswith(("=", "+", "-", "@"))
                        else v
                    )
                    for k, v in (meta | item).items()
                }
            )
        data = stream.getvalue().encode("utf-8")
    else:
        for item in items:
            lines.append(
                json.dumps(meta | item, ensure_ascii=False, sort_keys=True) + "\n"
            )
        data = "".join(lines).encode("utf-8")
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
    conditions = filters(location, start, end, category, channel)
    with connect(ROOT) as conn:
        meta = generation(conn, generation_id)
        clause, values = where(generation_id, conditions)
        count = conn.execute(
            "SELECT count(*) FROM serving.frame f WHERE " + clause, values
        ).fetchone()[0]
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
    return FileResponse(REPO / "frontend/index.html")
