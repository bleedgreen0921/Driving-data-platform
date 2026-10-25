"""真实 mini ODS 重跑、坏输入与显式提交故障验证。"""

import argparse
import copy
import json
import os
import uuid
from pathlib import Path

from driving_data_platform.common.config import load_config
from driving_data_platform.ingestion.nuscenes import (
    IngestionError,
    ingest,
    read_tables,
    validate,
)


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
    config = load_config(root / "application.yaml")
    repo = Path(__file__).resolve().parents[1]
    first, second = ingest(config, repo), ingest(config, repo)
    if (
        not second["reused"]
        or first["run_id"] != second["run_id"]
        or first["snapshot_id"] != second["snapshot_id"]
    ):
        raise ValueError("同输入重跑身份发生变化")
    tables = read_tables(Path(config["dataset"]["root"]), config["dataset"]["version"])
    tables["sample"][0]["scene_token"] = "invalid-test-reference"
    try:
        validate(tables)
    except IngestionError:
        pass
    else:
        raise ValueError("核心坏外键未阻断")
    faults = []
    if args.faults:
        for checkpoint in ("before_rename", "after_rename"):
            trial = copy.deepcopy(config)
            output = root / "experiments" / ("ods-" + uuid.uuid4().hex)
            trial["storage"]["output_root"] = str(output)
            try:
                ingest(trial, repo, fail_at=checkpoint)
            except IngestionError as error:
                if str(error) != "fault: " + checkpoint:
                    raise
            else:
                raise ValueError("故障未触发")
            if (output / "ingestion/watermark.json").exists():
                raise ValueError("失败推进了水位")
            recovered = ingest(trial, repo)
            if recovered["snapshot_id"] != first["snapshot_id"]:
                raise ValueError("恢复后快照不一致")
            faults.append(
                {
                    "checkpoint": checkpoint,
                    "reused": recovered["reused"],
                    "output": str(output),
                }
            )
    print(
        json.dumps(
            {
                "status": "passed",
                "snapshot_id": first["snapshot_id"],
                "counts": first["counts"],
                "files": first["files"],
                "rerun": second["reused"],
                "bad_fk_blocked": True,
                "faults": faults,
            }
        )
    )


if __name__ == "__main__":
    main()
