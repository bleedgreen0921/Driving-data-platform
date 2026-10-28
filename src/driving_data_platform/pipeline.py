"""默认离线流程：固定输入身份，按表名消费后受控发布。"""

from pathlib import Path

from driving_data_platform.common.config import load_config
from driving_data_platform.common.contract import load_contract
from driving_data_platform.common.preflight import digest, preflight
from driving_data_platform.common.runs import RunContext
from driving_data_platform.ingestion.nuscenes import PROCESSING_VERSION, ingest
from driving_data_platform.publishing.store import active, connect, publish
from driving_data_platform.warehouse.build import build
from driving_data_platform.warehouse.catalog import materialize

DEFAULT_VERSION = "warehouse-v4"


def run(
    root: Path, repo: Path, version: str = DEFAULT_VERSION, *, on_ready=None
) -> dict:
    context = RunContext(root, version)
    report = context.report
    with context.execute():
        with context.step("capture") as item:
            report["data_contract"] = load_contract(repo)
            with connect(root) as conn:
                expected = active(conn)
            item["expected"] = expected
            report["expected_active"] = expected
        with context.step("preflight") as item:
            config = load_config(root / "application.yaml")
            item["result"] = preflight(config, repo)
        with context.step("ods") as item:
            result = ingest(config, repo)
            source = (
                root
                / "ingestion/snapshots"
                / (result["snapshot_id"] + "-" + PROCESSING_VERSION)
            )
            item.update(
                result=result,
                directory=str(source),
                completion_sha256=digest(source / "complete.json"),
                reused=result["reused"],
            )
        with context.step("warehouse") as item:
            previous_logs = set((root / "reports").glob("spark-*.log"))
            try:
                warehouse = build(root, repo, version, source=source)
            finally:
                item["logs"] = sorted(
                    str(p)
                    for p in set((root / "reports").glob("spark-*.log")) - previous_logs
                )
            item.update(
                directory=warehouse["directory"],
                reused=warehouse["reused"],
                completion_sha256=digest(
                    Path(warehouse["directory"]) / "complete.json"
                ),
            )
        with context.step("load") as item:
            catalogs = root / "metastore/generations"
            previous_logs = set(catalogs.glob("*/*.log"))
            try:
                loaded = materialize(root, repo, Path(warehouse["directory"]))
                item.update(loaded)
            finally:
                item["logs"] = sorted(
                    str(p) for p in set(catalogs.glob("*/*.log")) - previous_logs
                )
        if on_ready is not None:
            on_ready(loaded)
        with context.step("publish") as item:
            item.update(publish(root, loaded["generation"], expected))
    return report
