"""契约、跨阶段证据与差异边界，不替代真实组件验证。"""

import json
from pathlib import Path

import pytest

from driving_data_platform.common.contract import load_contract
from driving_data_platform.common.errors import PlatformError, describe
from driving_data_platform.common.runs import RunContext, show
from driving_data_platform.publishing.difference import compare_values, delta


def test_contract_canonical_and_unknown_version(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    value = json.loads((repo / "contracts/nuscenes-mini-v1.json").read_text())
    (tmp_path / "contracts").mkdir()
    path = tmp_path / "contracts/nuscenes-mini-v1.json"
    path.write_text(json.dumps(value))
    assert load_contract(tmp_path) == load_contract(repo)
    value["version"] = "unknown"
    path.write_text(json.dumps(value))
    with pytest.raises(PlatformError):
        load_contract(tmp_path)


def test_failure_preserves_stage_and_skips_future(tmp_path):
    ctx = RunContext(tmp_path, "test")
    with pytest.raises(PlatformError), ctx.execute(), ctx.step("ods"):
        raise PlatformError("INPUT_INVALID", "bad-source", "测试源错误")
    saved = show(tmp_path, ctx.report["run_id"])
    assert saved["status"] == "failed"
    assert saved["steps"]["ods"]["status"] == "failed"
    assert saved["steps"]["publish"]["status"] == "skipped"
    assert saved["error"]["stage"] == "ods"


def test_interrupt_and_distinct_run_identity(tmp_path):
    one, two = RunContext(tmp_path, "same"), RunContext(tmp_path, "same")
    assert one.report["run_id"] != two.report["run_id"]
    with pytest.raises(KeyboardInterrupt), one.execute(), one.step("warehouse"):
        raise KeyboardInterrupt
    assert show(tmp_path, one.report["run_id"])["status"] == "interrupted"


def test_report_failure_does_not_mask_business_failure(tmp_path, monkeypatch):
    ctx = RunContext(tmp_path, "test")
    with pytest.raises(PlatformError, match="original"), ctx.execute():
        monkeypatch.setattr(ctx, "save", lambda: (_ for _ in ()).throw(OSError("disk")))
        raise PlatformError("CONTRACT_CONFLICT", "test", "original")


def test_error_is_structured_and_secret_safe():
    assert describe(BlockingIOError())["category"] == "OPERATION_CONFLICT"
    assert "password" not in json.dumps(describe(OSError("password=secret")))
    error = PlatformError("PUBLICATION_CONFLICT", "cas", "冲突")
    assert describe(error)["code"] == "cas"


def test_distribution_added_removed_and_zero_baseline():
    before = {
        "metrics": {"frame_count": 0},
        "distribution": {("channel", "old"): {"frame_count": 4}},
    }
    after = {
        "metrics": {"frame_count": 3},
        "distribution": {("channel", "new"): {"frame_count": 3}},
    }
    result = compare_values(before, after)
    assert result["metrics"]["frame_count"] == delta(0, 3)
    assert result["metrics"]["frame_count"]["ratio"] is None
    assert [r["change"] for r in result["distribution"]] == ["added", "removed"]
    assert result["distribution"][1]["counts"]["frame_count"]["delta"] == -4
