"""真实 PostgreSQL 事务失败、重跑、竞争、不可变与回滚。"""

import argparse
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg

from driving_data_platform.publishing.store import active, connect, publish
from driving_data_platform.warehouse.build import build
from driving_data_platform.warehouse.catalog import materialize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--faults", action="store_true")
    args = parser.parse_args()
    root = Path(
        os.environ.get(
            "DDP_OUTPUT_ROOT",
            "/Users/qiujiajun/Project/driving-data-platform-artifacts",
        )
    )
    snapshot = json.loads((root / "ingestion/watermark.json").read_text())[
        "snapshot_id"
    ]
    repo = Path(__file__).resolve().parents[1]
    first = materialize(root, repo, root / "warehouse" / (snapshot + "-warehouse-v3"))
    with connect(root) as conn:
        pointer = active(conn)
    original_pointer = pointer
    publish(root, first["generation"], pointer)
    trial = build(
        root, Path(__file__).resolve().parents[1], "publication-" + uuid.uuid4().hex
    )
    target = Path(trial["directory"])
    with connect(root) as conn:
        before = active(conn)
        existing = conn.execute("SELECT count(*) FROM control.generation").fetchone()[0]
    if args.faults:
        try:
            materialize(root, repo, target, load_fail=True)
        except ValueError as error:
            if str(error) != "fault: load-after-frame":
                raise
        else:
            raise ValueError("装载故障没有触发，请创建新的独立试验版本")
        with connect(root) as conn:
            if (
                active(conn) != before
                or conn.execute("SELECT count(*) FROM control.generation").fetchone()[0]
                != existing
            ):
                raise ValueError("装载失败改变了活动版本或留下半成品")
    second = materialize(root, repo, target, ack_fail=args.faults)
    if not materialize(root, repo, target)["reused"]:
        raise ValueError("重跑未复用")

    def compete(_):
        try:
            publish(root, second["generation"], before)
            return "won"
        except ValueError:
            return "lost"

    with ThreadPoolExecutor(max_workers=2) as pool:
        competition = list(pool.map(compete, range(2)))
    if sorted(competition) != ["lost", "won"]:
        raise ValueError("CAS 竞争结果不正确")
    try:
        with connect(root) as conn:
            conn.execute(
                "UPDATE serving.frame SET location='tamper' WHERE generation=%s",
                (second["generation"],),
            )
    except psycopg.Error:
        pass
    else:
        raise ValueError("活动版本可被修改")
    with connect(root) as conn:
        pointer = active(conn)
    restored = publish(
        root,
        original_pointer["generation"] or first["generation"],
        pointer,
        rollback=True,
    )
    print(
        json.dumps(
            {
                "status": "passed",
                "first": first["generation"],
                "second": second["generation"],
                "transaction_rollback": args.faults,
                "post_commit_ack_failure_recovered": args.faults,
                "competition": competition,
                "immutable": True,
                "restored": restored,
            }
        )
    )


if __name__ == "__main__":
    main()
