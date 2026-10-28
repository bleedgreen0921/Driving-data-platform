"""READY 版本间差异；只写外部报告，不改变数据库。"""

import uuid
from datetime import UTC, datetime

from driving_data_platform.common.errors import PlatformError
from driving_data_platform.ingestion.nuscenes import atomic_json


def delta(before, after):
    return {
        "before": before,
        "after": after,
        "delta": after - before,
        "ratio": (after - before) / before if before else None,
    }


def compare_values(before, after):
    metrics = {
        key: delta(before["metrics"][key], after["metrics"][key])
        for key in after["metrics"]
    }
    distribution = []
    for key in sorted(before["distribution"].keys() | after["distribution"].keys()):
        old = before["distribution"].get(key)
        new = after["distribution"].get(key)
        distribution.append(
            {
                "dimension": key[0],
                "value": key[1],
                "change": "added"
                if old is None
                else "removed"
                if new is None
                else "retained",
                "counts": {
                    field: delta((old or {}).get(field, 0), (new or {}).get(field, 0))
                    for field in ("frame_count", "annotation_count", "object_count")
                },
            }
        )
    return {"metrics": metrics, "distribution": distribution}


def read_version(conn, generation):
    row = conn.execute(
        "SELECT status,manifest,manifest_sha256 FROM control.generation WHERE generation=%s",
        (generation,),
    ).fetchone()
    if row is None or row[0] != "READY":
        raise PlatformError(
            "INPUT_INVALID", "generation-not-ready", "比较目标必须是存在的 READY 版本"
        )
    metric_rows = conn.execute(
        "SELECT payload FROM serving.dimension WHERE generation=%s AND kind='metrics'",
        (generation,),
    ).fetchall()
    if len(metric_rows) != 1:
        raise PlatformError(
            "RECONCILIATION_FAILED", "missing-metrics", "必需全局指标缺失"
        )
    metrics = metric_rows[0][0]
    required = {
        "frame_count",
        "annotation_count",
        "object_count",
        "sweep_count",
        "keyframe_media_count",
    }
    if set(metrics) != required or any(
        type(v) is not int or v < 0 for v in metrics.values()
    ):
        raise PlatformError(
            "RECONCILIATION_FAILED", "invalid-metrics", "全局指标不完整或类型错误"
        )
    distribution = {}
    for (item,) in conn.execute(
        "SELECT payload FROM serving.statistics WHERE generation=%s", (generation,)
    ):
        if (
            item.get("dimension") not in {"location", "category", "channel"}
            or not isinstance(item.get("value"), str)
            or any(
                type(item.get(key)) is not int or item[key] < 0
                for key in ("frame_count", "annotation_count", "object_count")
            )
        ):
            raise PlatformError(
                "RECONCILIATION_FAILED",
                "invalid-distribution",
                "分布指标不完整或类型错误",
            )
        distribution[(item["dimension"], item["value"])] = item
    manifest = row[1]
    identity = {
        key: manifest.get(key)
        for key in (
            "snapshot_id",
            "processing_version",
            "schema_version",
            "code",
            "catalog_contract",
        )
    }
    identity["data_contract"] = manifest.get("data_contract", "legacy_unrecorded")
    identity["manifest_sha256"] = row[2]
    return {
        "generation": generation,
        "identity": identity,
        "metrics": metrics,
        "distribution": distribution,
    }


def difference(root, candidate, expected):
    from driving_data_platform.publishing.store import connect

    with connect(root) as conn:
        after = read_version(conn, candidate)
        before = (
            read_version(conn, expected["generation"])
            if expected["generation"]
            else None
        )
    result = {
        "report_schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "identity_changes": {
            key: {"before": before["identity"].get(key), "after": value}
            for key, value in after["identity"].items()
            if before and before["identity"].get(key) != value
        },
        "status": "passed",
        "baseline": expected,
        "candidate": candidate,
        "baseline_identity": before["identity"] if before else None,
        "candidate_identity": after["identity"],
        "comparison": compare_values(before, after) if before else None,
        "no_baseline": before is None,
    }
    path = root / "reports" / ("publish-diff-" + uuid.uuid4().hex + ".json")
    result["report"] = str(path)
    atomic_json(path, result)
    return result
