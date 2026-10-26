"""默认主链路的真实发布、重跑及无旁路失败验证。"""

import argparse
import json
import os
import subprocess
from pathlib import Path

from driving_data_platform.pipeline import DEFAULT_VERSION, run
from driving_data_platform.publishing.store import active, connect
from driving_data_platform.warehouse.catalog import command, materialize


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
    repo = Path(__file__).resolve().parents[1]
    first = run(root, repo)
    with connect(root) as conn:
        counts = conn.execute("SELECT count(*) FROM control.generation").fetchone()[0]
    second = run(root, repo)
    if (
        first["steps"]["load"]["generation"] != second["steps"]["load"]["generation"]
        or not second["steps"]["load"]["reused"]
        or first["steps"]["load"]["catalog_id"] != second["steps"]["load"]["catalog_id"]
    ):
        raise ValueError("默认主链路重跑未复用身份")
    with connect(root) as conn:
        if (
            counts
            != conn.execute("SELECT count(*) FROM control.generation").fetchone()[0]
        ):
            raise ValueError("重跑追加了 generation")
        pointer = active(conn)
    blocked = []
    if args.faults:
        water = json.loads((root / "ingestion/watermark.json").read_text())
        source = root / "warehouse" / (water["snapshot_id"] + "-" + DEFAULT_VERSION)
        try:
            subprocess.run([*command(root, repo), "stop", "metastore"], check=True)
            for name, action in [
                ("run", lambda: run(root, repo)),
                ("load", lambda: materialize(root, repo, source)),
            ]:
                try:
                    action()
                except ValueError as error:
                    if "Metastore" not in str(error):
                        raise
                    blocked.append(name)
                else:
                    raise ValueError("HMS 停止时默认入口绕过目录继续成功")
            with connect(root) as conn:
                if active(conn) != pointer:
                    raise ValueError("HMS 失败改变了活动指针")
        finally:
            subprocess.run(
                [*command(root, repo), "up", "-d", "--wait", "--no-deps", "metastore"],
                check=True,
            )
        recovered = run(root, repo)
        if (
            recovered["steps"]["load"]["generation"]
            != first["steps"]["load"]["generation"]
        ):
            raise ValueError("恢复后身份不一致")
    print(
        json.dumps(
            {
                "status": "passed",
                "generation": first["steps"]["load"]["generation"],
                "catalog_id": first["steps"]["load"]["catalog_id"],
                "rerun_reused": True,
                "hms_outage_blocked": blocked,
            }
        )
    )


if __name__ == "__main__":
    main()
