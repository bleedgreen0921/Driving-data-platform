"""文本 CLI 检索：编码前固定版本，不保存查询原文或向量。"""

import hashlib
import time
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException

from driving_data_platform.common.errors import PlatformError
from driving_data_platform.common.run_query import identity
from driving_data_platform.embedding import artifacts as a
from driving_data_platform.embedding import gate, model, store


def conditions(args):
    from driving_data_platform.api.app import filters

    if type(args.limit) is not int or not 1 <= args.limit <= 100:
        raise ValueError("limit 必须在 1–100")
    try:
        return filters(
            args.location, args.start, args.end, args.category, args.required_channel
        )
    except HTTPException as error:
        raise PlatformError(
            "INPUT_INVALID", "search-filter", str(error.detail)
        ) from None


def resolve(root, conn, generation, embedding_generation=None):
    """只解析一次活动指针，并校验数据库、本地空间和选参证据。"""
    identity(generation, r"g_[a-f0-9]{64}")
    store.check_environment(conn, False)
    if embedding_generation is None:
        row = conn.execute(
            "SELECT embedding_generation FROM control.embedding_active WHERE generation=%s",
            (generation,),
        ).fetchone()
        if not row or row[0] is None:
            raise PlatformError(
                "CONTRACT_CONFLICT", "search-no-data", "该业务版本尚未建立检索数据"
            )
        embedding_generation = row[0]
    meta = store.show(conn, embedding_generation)
    if meta["generation"] != generation or meta["version_status"] != "READY":
        raise PlatformError(
            "CONTRACT_CONFLICT",
            "search-version-conflict",
            "业务版本与 READY 向量版本不兼容",
        )
    contract = model.space(root, meta["space_id"])
    row = conn.execute(
        "SELECT contract FROM control.embedding_space WHERE space_id=%s",
        (meta["space_id"],),
    ).fetchone()
    if row != (contract,) or contract["test_only"]:
        raise PlatformError(
            "CONTRACT_CONFLICT",
            "search-space-conflict",
            "数据库与本地真实空间契约不一致",
        )
    from driving_data_platform.embedding.runtime import selection

    selected = selection(root, meta["space_id"])
    evaluation = a.read(root, selected["report"])
    if (
        selected.get("space_id") != meta["space_id"]
        or selected.get("status") != "passed"
        or a.checksum(evaluation) != selected["report_sha256"]
        or evaluation.get("status") != "passed"
        or evaluation.get("selected_space_id") != meta["space_id"]
    ):
        raise PlatformError(
            "CONTRACT_CONFLICT",
            "search-selection-conflict",
            "缺少已通过且摘要一致的选参记录",
        )
    return meta, contract


def execute(root, args, trace=None, *, image=None):
    with gate.acquire(root):
        return query(root, args, trace, image=image)


def query(root, args, trace=None, *, image=None):
    start = time.monotonic()
    deployment = getattr(args, "deployment", None)
    if (
        bool(args.trial) == bool(deployment)
        or not args.generation
        or (image is None and getattr(args, "text", None) is None)
    ):
        raise ValueError(
            "必须显式指定 --trial 或 --deployment，以及 --generation 和 --text"
        )
    identity(args.trial or deployment)
    normalized = conditions(args)
    target = {"deployment": deployment} if deployment else {"trial": args.trial}
    with store.connect(root, **target) as conn:
        conn.execute("SET statement_timeout='10s'")
        meta, _contract = resolve(
            root, conn, args.generation, args.embedding_generation
        )
        if trace is not None:
            trace.update(
                {
                    key: meta[key]
                    for key in (
                        "generation",
                        "embedding_generation",
                        "space_id",
                        "artifact_id",
                    )
                }
            )
            trace["filters"] = {
                **normalized,
                "image_channel": args.image_channel,
                "limit": args.limit,
            }
        from driving_data_platform.embedding.runtime import encoder

        client = encoder(root, meta["space_id"])
        encoding_start = time.monotonic()
        query = (
            client.encode_image(image)
            if image is not None
            else client.encode_text(args.text)
        )
        encoded = time.monotonic()
        result = store.search(
            conn,
            args.generation,
            meta["space_id"],
            query,
            embedding_generation=meta["embedding_generation"],
            conditions=normalized,
            channel=args.image_channel,
            limit=args.limit,
        )
        finished = time.monotonic()
    for rank, item in enumerate(result["items"], 1):
        item["rank"] = rank
        item["timestamp"] = (
            datetime(1970, 1, 1, tzinfo=UTC)
            + timedelta(microseconds=item["timestamp_us"])
        ).isoformat()
    result.update(
        status="passed",
        artifact_id=meta["artifact_id"],
        filters={
            **normalized,
            "image_channel": args.image_channel,
            "limit": args.limit,
        },
        query_sha256=hashlib.sha256(
            image if image is not None else args.text.encode("utf-8")
        ).hexdigest(),
        **(
            {"query_bytes": len(image)}
            if image is not None
            else {"query_characters": len(args.text)}
        ),
        count=len(result["items"]),
        timings={
            "encoding_seconds": encoded - encoding_start,
            "sql_seconds": finished - encoded,
            "total_seconds": finished - start,
        },
    )
    return result
