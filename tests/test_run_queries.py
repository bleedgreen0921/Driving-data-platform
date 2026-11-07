"""运行查询安全、显式报告版本与依赖声明边界。"""

import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from driving_data_platform.common import run_query as query
from driving_data_platform.lineage import dependencies

REPO = Path(__file__).resolve().parents[1]


def save(root, number=1, **fields):
    run_id = f"{number:032x}"
    path = root / "reports" / f"pipeline-{run_id}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        json.dumps(
            {"report_schema_version": 1, "status": "running", "steps": {}, **fields}
        )
    )
    return run_id, path


def test_history_running_and_sensitive_fields(tmp_path):
    run_id, path = save(
        tmp_path,
        error={"category": "INPUT_INVALID", "message": "/private/password=secret"},
        report="/private",
        logs=["secret"],
    )
    before = path.read_bytes()
    value = query.normalized(tmp_path, run_id)
    assert value["time_source"] == "file_mtime"
    assert value["status"] == "running"
    assert value["process_liveness"] == "未核实进程存活"
    assert all(s["status"] == "历史未记录" for s in value["steps"])
    assert "secret" not in json.dumps(value)
    assert path.read_bytes() == before


def test_sort_filter_page_and_corrupt(tmp_path):
    save(tmp_path, 1, started_at="2026-01-01T00:00:00+00:00", status="passed")
    save(tmp_path, 2, started_at="2026-01-01T00:00:00+00:00", status="passed")
    _, path = save(tmp_path, 3)
    path.write_text("{")
    result = query.list_runs(tmp_path, "passed", 1, 1)
    assert result["total"] == 2 and result["items"][0]["run_id"] == f"{1:032x}"
    assert result["skipped"] == 1
    with pytest.raises(query.EvidenceError):
        query.normalized(tmp_path, f"{3:032x}")
    assert query.list_runs(tmp_path, "failed")["items"] == []


@pytest.mark.parametrize(
    "fields",
    [
        {"report_schema_version": 2},
        {"steps": {"ods": None}},
        {"error": []},
        {"steps": {"ods": {"result": []}}},
        {"processing_version": "/private/secret"},
        {"steps": {"ods": {"seconds": float("nan")}}},
        {"started_at": "yesterday"},
    ],
)
def test_malformed_fields_are_skipped(tmp_path, fields):
    save(tmp_path, **fields)
    assert query.list_runs(tmp_path)["skipped"] == 1


def test_limits_and_symlink(tmp_path, monkeypatch):
    run_id, path = save(tmp_path)
    target = tmp_path / "secret.json"
    path.rename(target)
    path.symlink_to(target)
    with pytest.raises(query.EvidenceError):
        query.normalized(tmp_path, run_id)
    with pytest.raises(query.EvidenceError):
        query.read_json(tmp_path, "../secret.json")
    path.unlink()
    path.write_bytes(b" " * 20)
    monkeypatch.setattr(query, "MAX_BYTES", 10)
    assert query.list_runs(tmp_path)["skipped"] == 1
    monkeypatch.setattr(query, "MAX_REPORTS", 0)
    with pytest.raises(query.EvidenceError, match="5000"):
        query.list_runs(tmp_path)


def test_query_cli_does_not_take_lock_or_write(tmp_path):
    save(tmp_path)
    with (tmp_path / ".operation.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = sorted(p.name for p in (tmp_path / "reports").iterdir())
        result = subprocess.run(
            [sys.executable, "scripts/manage_platform.py", "run-list"],
            env=dict(os.environ, DDP_OUTPUT_ROOT=str(tmp_path)),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stdout
        assert before == sorted(p.name for p in (tmp_path / "reports").iterdir())


def test_dependency_contract_and_no_cycles():
    declaration = json.loads((REPO / "configs/table-lineage-v1.json").read_text())
    entries = declaration["dependencies"]
    assert len(entries) == 11
    manifest = {
        "code": {e["sql"]: e["sha256"] for e in entries},
        "tables": {e["target"]: {} for e in entries},
    }
    assert dependencies(REPO, manifest)["status"] == "matched"
    manifest["code"][entries[0]["sql"]] = "changed"
    assert dependencies(REPO, manifest)["items"] == []
    graph = {e["target"]: e["upstream"] for e in entries}

    def walk(name, seen):
        assert name not in seen
        for parent in graph.get(name, []):
            walk(parent, seen | {name})

    for name in graph:
        walk(name, set())
    assert not {"attribute", "visibility"} & {x for e in entries for x in e["upstream"]}


def test_empty_invalid_query_and_estimated_duration(tmp_path):
    assert query.list_runs(tmp_path)["total"] == 0
    for kwargs in ({"limit": 0}, {"offset": -1}, {"status": "unknown"}):
        with pytest.raises(ValueError):
            query.list_runs(tmp_path, **kwargs)
    run_id, _ = save(
        tmp_path,
        started_at="2020-01-01T00:00:00+00:00",
        steps={
            "warehouse": {
                "status": "running",
                "started_at": "2020-01-01T00:00:00+00:00",
            }
        },
    )
    data = query.normalized(tmp_path, run_id)
    step = data["steps"][3]
    assert step["seconds"] is None and step["estimated_seconds"] > 0
    assert data["status"] == "running" and data["current_stage"] == "warehouse"


def test_sql_changed_after_declaration_is_not_applied(tmp_path):
    import shutil

    shutil.copytree(REPO / "configs", tmp_path / "configs")
    shutil.copytree(REPO / "sql", tmp_path / "sql")
    entries = json.loads((tmp_path / "configs/table-lineage-v1.json").read_text())[
        "dependencies"
    ]
    manifest = {
        "code": {e["sql"]: e["sha256"] for e in entries},
        "tables": {e["target"]: {} for e in entries},
    }
    (tmp_path / entries[0]["sql"]).write_text("SELECT 1")
    assert dependencies(tmp_path, manifest)["status"] == "unknown"


def test_parent_symlink_is_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    save(outside)
    (tmp_path / "reports").symlink_to(outside / "reports", target_is_directory=True)
    with pytest.raises(query.EvidenceError):
        query.normalized(tmp_path, f"{1:032x}")
