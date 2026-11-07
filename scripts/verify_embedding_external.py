"""Read-only real external retrieval verification against registered trial data."""

import argparse
import hashlib
import json
import uuid
from types import SimpleNamespace

from verify_backup import fingerprint
from verify_embedding_image import fixtures
from verify_embedding_search import baseline

from driving_data_platform.common.config import output_root
from driving_data_platform.common.errors import describe
from driving_data_platform.common.operations import operation_lock
from driving_data_platform.embedding import artifacts as a
from driving_data_platform.embedding import gate, runtime, search, store
from driving_data_platform.ingestion.nuscenes import atomic_json


def verify(root, trial, artifact_id):
    _, source, contract, rows = a.verify_artifact(root, artifact_id)
    if contract["schema_version"] != 3 or contract["test_only"]:
        raise ValueError("仅验收已登记的真实外部 v3 空间，禁止人工试验向量")
    sid = a.validate_space(contract)
    queries = [
        q["text"]
        for q in a.read(root, "embedding/evaluation-labels.json")["queries"]
        if q["positive_media_ids"]
    ]
    if len(queries) < 6:
        raise ValueError("至少需要六条固定的已审阅查询")
    folder = root / "reports" / ("embedding-external-" + uuid.uuid4().hex)
    folder.mkdir()
    report = {
        "status": "running",
        "scope": "real-external-read-only-trial",
        "trial": trial,
        "artifact_id": artifact_id,
        "space_id": sid,
        "checks": [],
        "not_verified": ["服务故障恢复", "浏览器", "正式发布回滚", "1800秒组合验收"],
    }
    atomic_json(folder / "report.json", report)
    try:
        with store.connect(root, trial=trial) as conn:
            conn.execute("SET default_transaction_read_only=on")
            before = fingerprint(conn)
            meta, _ = search.resolve(
                root, conn, source["generation"], "e_" + artifact_id
            )
            if meta["space_id"] != sid:
                raise ValueError("库内向量空间与本地真实产物不一致")
            images = fixtures(root, conn, source, folder / "image-fixtures")
        client = runtime.encoder(root, sid)
        cases = [("text", t) for t in queries] + [("image", f["data"]) for f in images]
        for kind, value in cases:
            with gate.acquire(root):
                vector = (
                    client.encode_text(value)
                    if kind == "text"
                    else client.encode_image(value)
                )
                conditions = {
                    "location": None,
                    "start_us": None,
                    "end_us": None,
                    "category": None,
                    "channels": [],
                }
                with store.connect(root, trial=trial) as conn:
                    conn.execute("SET default_transaction_read_only=on")
                    for changes in ({}, {"location": "not-a-location"}):
                        filters = conditions | changes
                        for limit in (1, 20, 100):
                            expected = baseline(
                                conn, source, rows, vector, filters, None, limit
                            )
                            actual = store.search(
                                conn,
                                source["generation"],
                                sid,
                                vector,
                                embedding_generation="e_" + artifact_id,
                                conditions=filters,
                                limit=limit,
                            )
                            if [r["media_id"] for r in actual["items"]] != [
                                r[0] for r in expected
                            ] or any(
                                abs(item["score"] - score) > 1e-5
                                for item, (_, score) in zip(
                                    actual["items"], expected, strict=True
                                )
                            ):
                                raise ValueError("独立余弦排序、分数或筛选不一致")
                args = SimpleNamespace(
                    trial=trial,
                    deployment=None,
                    generation=source["generation"],
                    embedding_generation="e_" + artifact_id,
                    text=value if kind == "text" else None,
                    location=None,
                    start=None,
                    end=None,
                    category=None,
                    required_channel=[],
                    image_channel=None,
                    limit=20,
                )
                result = search.query(
                    root, args, image=value if kind == "image" else None
                )
                if result["artifact_id"] != artifact_id or result["space_id"] != sid:
                    raise ValueError("端到端检索身份不一致")
            report["checks"].append(
                {
                    "query_type": kind,
                    "query_sha256": hashlib.sha256(
                        value.encode() if isinstance(value, str) else value
                    ).hexdigest(),
                    "count": result["count"],
                }
            )
            atomic_json(folder / "report.json", report)
        with store.connect(root, trial=trial) as conn:
            conn.execute("SET default_transaction_read_only=on")
            if fingerprint(conn) != before:
                raise ValueError("只读验收期间库内指纹发生变化")
        report["status"] = "passed"
        return report
    except BaseException as error:
        report.update(status="failed", error=describe(error, "embedding-external"))
        raise
    finally:
        atomic_json(folder / "report.json", report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial", required=True)
    parser.add_argument("--artifact-id", required=True)
    args = parser.parse_args()
    root = output_root()
    try:
        with operation_lock(root):
            print(json.dumps(verify(root, args.trial, args.artifact_id)))
        return 0
    except (ValueError, OSError) as error:
        print(
            json.dumps(
                {"status": "failed", "error": describe(error, "embedding-external")}
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
