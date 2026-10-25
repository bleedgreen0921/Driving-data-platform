"""真实 Spark 重跑、缺输入失败与恢复。"""

import argparse
import json
import os
import uuid
from pathlib import Path

from driving_data_platform.warehouse.build import build, verify


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
    reused = build(root, repo)
    if not reused["reused"]:
        raise ValueError("既有数仓没有复用")
    if args.faults:
        version = "verify-" + uuid.uuid4().hex
        try:
            build(root, repo, version, fail=True)
        except ValueError as error:
            if "Spark 加工失败" not in str(error):
                raise
        else:
            raise ValueError("Spark 缺输入未阻断")
        if list((root / "warehouse").glob("*-" + version)):
            raise ValueError("失败提交了有效数仓")
        recovered = build(root, repo, version)
        verify(Path(recovered["directory"]))
    else:
        recovered = reused
    print(
        json.dumps(
            {
                "status": "passed",
                "reused": reused["reused"],
                "missing_input_blocked": args.faults,
                "recovered_directory": recovered["directory"],
                "counts": {k: v["count"] for k, v in recovered["counts"].items()},
            }
        )
    )


if __name__ == "__main__":
    main()
