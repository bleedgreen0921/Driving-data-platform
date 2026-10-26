"""默认离线流程：所有发布数据必须经过 Hive 表名消费。"""

import uuid
from pathlib import Path

from driving_data_platform.common.config import load_config
from driving_data_platform.common.preflight import preflight
from driving_data_platform.ingestion.nuscenes import atomic_json, ingest
from driving_data_platform.publishing.store import active, connect, publish
from driving_data_platform.warehouse.build import build
from driving_data_platform.warehouse.catalog import materialize

DEFAULT_VERSION = "warehouse-v3"


def run(
    root: Path, repo: Path, version: str = DEFAULT_VERSION, *, on_ready=None
) -> dict:
    report_path = root / "reports" / ("pipeline-" + uuid.uuid4().hex + ".json")
    report = {
        "status": "running",
        "processing_version": version,
        "steps": {},
        "report": str(report_path),
    }
    atomic_json(report_path, report)
    try:
        with connect(root) as conn:
            expected = active(conn)
        config = load_config(root / "application.yaml")
        report["steps"]["preflight"] = preflight(config, repo)
        report["steps"]["ods"] = ingest(config, repo)
        atomic_json(report_path, report)
        warehouse = build(root, repo, version)
        report["steps"]["warehouse"] = {
            "directory": warehouse["directory"],
            "reused": warehouse["reused"],
        }
        atomic_json(report_path, report)
        loaded = materialize(root, repo, Path(warehouse["directory"]))
        report["steps"]["load"] = loaded
        atomic_json(report_path, report)
        if on_ready is not None:
            on_ready(loaded)
        report["steps"]["publish"] = publish(root, loaded["generation"], expected)
        report["status"] = "passed"
        return report
    finally:
        if report["status"] == "running":
            report["status"] = "failed"
        atomic_json(report_path, report)
