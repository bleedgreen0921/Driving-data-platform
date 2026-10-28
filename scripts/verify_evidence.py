"""独立试验输入验证差异、契约冲突、未知提交及报告门禁。"""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import psycopg

from driving_data_platform.common.errors import PlatformError, describe
from driving_data_platform.common.operations import operation_lock
from driving_data_platform.common.preflight import digest
from driving_data_platform.common.runs import RunContext
from driving_data_platform.ingestion.nuscenes import PROCESSING_VERSION, atomic_json
from driving_data_platform.pipeline import DEFAULT_VERSION
from driving_data_platform.publishing.difference import (
    compare_values,
    difference,
    read_version,
)
from driving_data_platform.publishing.store import active, connect, publish
from driving_data_platform.warehouse.baseline import baseline
from driving_data_platform.warehouse.build import build
from driving_data_platform.warehouse.catalog import materialize


def verify(root, repo):
    evidence = root / "reports" / ("evidence-" + uuid.uuid4().hex + ".json")
    result = {"status": "running", "report": str(evidence), "checks": {}}
    atomic_json(evidence, result)
    try:
        with connect(root) as conn:
            expected = active(conn)
            current = read_version(conn, expected["generation"])
        water = json.loads((root / "ingestion/watermark.json").read_text())
        original = (
            root
            / "ingestion/snapshots"
            / (water["snapshot_id"] + "-" + PROCESSING_VERSION)
        )
        # 独立派生测试快照，保留来源说明；不改原快照、不推进正式水位、不复制媒体。
        trial_id = uuid.uuid4().hex
        source = original.parent / ("evidence-" + trial_id + "-" + PROCESSING_VERSION)
        shutil.copytree(original, source)
        category_path = source / "records/category.jsonl"
        records = [json.loads(line) for line in category_path.read_text().splitlines()]
        old, new = "vehicle.car", "evidence.vehicle.car"
        for row in records:
            if row["raw_record"]["name"] == old:
                row["raw_record"]["name"] = new
        category_path.write_text("".join(json.dumps(row) + "\n" for row in records))
        raw_path = source / "raw/category.json"
        raw = json.loads(raw_path.read_text())
        for row in raw:
            if row["name"] == old:
                row["name"] = new
        atomic_json(raw_path, raw)
        manifest = json.loads((source / "complete.json").read_text())
        manifest.update(
            snapshot_id="evidence-" + trial_id,
            test_derived_from=str(original),
            test_only=True,
        )
        manifest["artifacts"] = {
            name: digest(source / name) for name in manifest["artifacts"]
        }
        atomic_json(source / "complete.json", manifest)
        built = build(root, repo, "evidence-" + trial_id, source=source)
        loaded = materialize(root, repo, Path(built["directory"]))
        diff = difference(root, loaded["generation"], expected)
        distributions = diff["comparison"]["distribution"]
        assert any(
            r["value"] == old and r["change"] == "removed" for r in distributions
        )
        assert any(r["value"] == new and r["change"] == "added" for r in distributions)
        independent = baseline(source)
        with connect(root) as conn:
            candidate = read_version(conn, loaded["generation"])
        assert candidate["metrics"] == independent["metrics"][0]
        assert sorted(
            candidate["distribution"].values(),
            key=lambda r: (r["dimension"], r["value"]),
        ) == sorted(
            independent["statistics"], key=lambda r: (r["dimension"], r["value"])
        )
        result["checks"]["known_distribution_change"] = diff["report"]
        same = difference(root, expected["generation"], expected)
        assert all(v["delta"] == 0 for v in same["comparison"]["metrics"].values())
        first = difference(
            root, loaded["generation"], {"generation": None, "revision": 0}
        )
        assert first["no_baseline"] and first["comparison"] is None
        result["checks"]["same_and_first_publish"] = True
        # 真实文件写失败发生在独立临时目录；验证业务指针保持原值。
        blocker = root / ("evidence-readonly-" + trial_id)
        blocker.mkdir()
        (blocker / "reports").write_text("file, not directory")
        from driving_data_platform.publishing import difference as difference_module

        try:
            with patch.object(
                difference_module,
                "atomic_json",
                side_effect=lambda _p, value: atomic_json(
                    blocker / "reports/result.json", value
                ),
            ):
                publish(root, loaded["generation"], expected)
        except OSError:
            result["checks"]["report_write_blocks_publish"] = True
        else:
            raise AssertionError("摘要失败没有阻断发布")
        # 未知数据库结果不能被错误解释为回滚，诊断不泄露连接信息。
        try:
            psycopg.connect(
                "host=127.0.0.1 port=1 dbname=unavailable connect_timeout=1"
            )
        except psycopg.OperationalError as error:
            assert describe(error)["code"] == "database-unavailable-result-unknown"
            result["checks"]["database_unavailable_classified"] = True
        # 实际坏契约与坏输入失败，记录阶段与跳过项。
        fake_repo = root / ("evidence-contract-" + trial_id)
        (fake_repo / "contracts").mkdir(parents=True)
        contract = json.loads((repo / "contracts/nuscenes-mini-v1.json").read_text())
        contract["version"] = "unsupported"
        atomic_json(fake_repo / "contracts/nuscenes-mini-v1.json", contract)
        from driving_data_platform.common.contract import load_contract

        ctx = RunContext(root, DEFAULT_VERSION)
        try:
            with ctx.execute(), ctx.step("capture"):
                load_contract(fake_repo)
        except PlatformError:
            assert ctx.report["error"]["category"] == "CONTRACT_CONFLICT"
            assert ctx.report["steps"]["publish"]["status"] == "skipped"
            result["checks"]["bad_contract_report"] = str(ctx.path)
        changed = repo / "contracts/nuscenes-mini-v1.json"
        # 同版本契约内容变化在独立仓库副本中阻断复用，不修改项目契约。
        contract = json.loads(changed.read_text())
        contract["semantics"]["frame"] = "changed-test-contract"
        atomic_json(fake_repo / "contracts/nuscenes-mini-v1.json", contract)
        (fake_repo / "src/driving_data_platform/warehouse").mkdir(parents=True)
        for name in ("spark_job.py", "baseline.py"):
            shutil.copy(
                repo / "src/driving_data_platform/warehouse" / name,
                fake_repo / "src/driving_data_platform/warehouse" / name,
            )
        shutil.copytree(repo / "sql", fake_repo / "sql")
        try:
            build(root, fake_repo, "evidence-" + trial_id, source=source)
        except PlatformError as error:
            assert error.category == "CONTRACT_CONFLICT"
            result["checks"]["same_version_contract_drift_blocked"] = True
        else:
            raise AssertionError("契约漂移未阻断")
        with connect(root) as conn:
            assert active(conn) == expected
        assert json.loads((root / "ingestion/watermark.json").read_text()) == water
        interruption = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-c",
                "from pathlib import Path; import sys,time; from driving_data_platform.common.runs import RunContext; c=RunContext(Path(sys.argv[1]), 'interrupt-test'); print(c.report['run_id'], flush=True); exec(\"with c.execute(), c.step('warehouse'):\\n time.sleep(60)\")",
                str(root),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        interrupted_id = interruption.stdout.readline().strip()
        time.sleep(0.2)
        interruption.send_signal(signal.SIGTERM)
        interruption.communicate(timeout=10)
        interrupted = json.loads(
            (root / "reports" / ("pipeline-" + interrupted_id + ".json")).read_text()
        )
        assert interrupted["status"] == "interrupted"
        result["checks"]["real_sigterm_report"] = interrupted_id
        result["checks"]["active_and_watermark_unchanged"] = True
        result["checks"]["baseline_metrics_unchanged"] = compare_values(
            current, candidate
        )["metrics"]
        with connect(root) as conn:
            legacy = conn.execute(
                "SELECT generation FROM control.generation WHERE status='READY' AND NOT (manifest ? 'data_contract') ORDER BY generation LIMIT 1"
            ).fetchone()
        if legacy:
            legacy_diff = difference(root, legacy[0], expected)
            assert (
                legacy_diff["candidate_identity"]["data_contract"]
                == "legacy_unrecorded"
            )
            try:
                publish(root, legacy[0], expected, rollback=True)
                result["checks"]["legacy_rollback"] = legacy_diff["report"]
            finally:
                with connect(root) as conn:
                    pointer = active(conn)
                publish(root, expected["generation"], pointer, rollback=True)
        result["status"] = "passed"
    finally:
        if result["status"] == "running":
            result["status"] = "failed"
        atomic_json(evidence, result)
    return result


if __name__ == "__main__":
    root = Path(
        os.environ.get(
            "DDP_OUTPUT_ROOT",
            "/Users/qiujiajun/Project/driving-data-platform-artifacts",
        )
    )
    with operation_lock(root):
        print(
            json.dumps(
                verify(root, Path(__file__).resolve().parents[1]), ensure_ascii=False
            )
        )
