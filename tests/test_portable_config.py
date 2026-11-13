"""Paths resolve predictably and invalid configuration fails before writes."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from driving_data_platform.common.config import (
    ConfigError,
    configure_runtime,
    load_config,
    port,
)
from driving_data_platform.common.run_query import EvidenceError, list_runs, normalized
from driving_data_platform.publishing.store import validate_publish_manifest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def restore_deployment_environment():
    before = {k: v for k, v in os.environ.items() if k.startswith("DDP_")}
    yield
    for key in [k for k in os.environ if k.startswith("DDP_")]:
        os.environ.pop(key)
    os.environ.update(before)


def test_file_relative_and_environment_relative_paths(tmp_path, monkeypatch):
    config_dir = tmp_path / "配置 空格"
    config_dir.mkdir()
    file = config_dir / "application.yaml"
    file.write_text(
        "dataset:\n  root: ../data\nstorage:\n  output_root: ../output\nembedding:\n  provider_file: provider.json\n"
    )
    monkeypatch.chdir(tmp_path)
    value = load_config(file, {})
    assert Path(value["dataset"]["root"]).resolve() == tmp_path / "data"
    assert value["embedding"]["provider_file"] == str(config_dir / "provider.json")
    value = load_config(file, {"DDP_OUTPUT_ROOT": "override"})
    assert value["storage"]["output_root"] == str(tmp_path / "override")


def test_port_override_validation(monkeypatch):
    monkeypatch.setenv("DDP_MODEL_PORT", "28190")
    assert port("model") == 28190
    for value in ("0", "65536", "bad", "1.5", ""):
        monkeypatch.setenv("DDP_MODEL_PORT", value)
        with pytest.raises(ConfigError):
            port("model")


def test_cli_overrides_file_and_environment(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("storage:\n  output_root: file-output\n")
    monkeypatch.setenv("DDP_OUTPUT_ROOT", str(tmp_path / "env-output"))
    monkeypatch.delenv("DDP_CONFIG", raising=False)
    result = configure_runtime(config=config, root=tmp_path / "cli-output")
    assert result == (tmp_path / "cli-output").resolve()
    assert not result.exists()


def test_legacy_report_skipped_without_rewrite(tmp_path):
    root = tmp_path / "reports"
    root.mkdir()
    run_id = "1" * 32
    file = root / f"pipeline-{run_id}.json"
    original = b'{"status":"passed","steps":{}}'
    file.write_bytes(original)
    with pytest.raises(EvidenceError):
        normalized(tmp_path, run_id)
    result = list_runs(tmp_path)
    assert result["skipped"] == 1 and result["warnings"]
    assert file.read_bytes() == original


def test_incomplete_generation_rejected():
    with pytest.raises(ValueError, match="禁止发布"):
        validate_publish_manifest({"status": "READY", "data_contract": {"id": "old"}})


def test_missing_root_and_retired_commands_do_not_write(tmp_path):
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"DDP_CONFIG", "DDP_OUTPUT_ROOT", "DDP_DATASET_ROOT"}
    }
    for command in (["prepare"], ["embedding-api-up"], ["hms-register"]):
        result = subprocess.run(
            [sys.executable, str(REPO / "scripts/manage_platform.py"), *command],
            cwd=tmp_path,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2
        assert not list(tmp_path.iterdir())


def test_help_from_other_directory(tmp_path):
    result = subprocess.run(
        [str(REPO / "platform.sh"), "--help"],
        cwd=tmp_path,
        env=dict(os.environ, DDP_PYTHON=sys.executable),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "embedding-trial" in result.stdout and "diagnose" in result.stdout
    assert not list(tmp_path.iterdir())


def test_independent_root_does_not_inherit_live_config(tmp_path, monkeypatch):
    from driving_data_platform.common.config import application_config

    live = tmp_path / "live"
    trial = tmp_path / "trial"
    live.mkdir()
    trial.mkdir()
    config = live / "application.yaml"
    config.write_text("dataset:\n  root: live-data\nstorage:\n  output_root: .\n")
    (trial / "application.yaml").write_text("dataset:\n  root: copied-data\n")
    monkeypatch.setenv("DDP_CONFIG", str(config))
    monkeypatch.setenv("DDP_OUTPUT_ROOT", str(live))
    monkeypatch.setenv("DDP_DATASET_ROOT", str(live / "live-data"))
    assert application_config(trial)["dataset"]["root"] == str(trial / "copied-data")


def test_relative_evidence_survives_directory_change(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DDP_ASSET_EVIDENCE", "proof.json")
    monkeypatch.delenv("DDP_CONFIG", raising=False)
    configure_runtime(root=tmp_path / "output")
    monkeypatch.chdir(REPO)
    assert os.environ["DDP_ASSET_EVIDENCE"] == str(tmp_path / "proof.json")


def test_backup_cannot_overlap_input_or_contain_output(tmp_path, monkeypatch):
    from driving_data_platform.common.config import storage_directory

    root = tmp_path / "output"
    dataset = tmp_path / "dataset"
    monkeypatch.delenv("DDP_CONFIG", raising=False)
    monkeypatch.setenv("DDP_OUTPUT_ROOT", str(root))
    root.mkdir()
    config = root / "application.yaml"
    monkeypatch.setenv("DDP_DATASET_ROOT", str(dataset))
    for backup in (dataset, dataset / "backups", tmp_path):
        config.write_text(f"storage:\n  backup_root: {backup}\n")
        with pytest.raises(ConfigError):
            storage_directory(root, "backup")
    config.write_text(f"storage:\n  backup_root: {tmp_path / 'separate-backups'}\n")
    assert storage_directory(root, "backup") == tmp_path / "separate-backups"


def test_deployment_requires_explicit_artifact_pair(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(REPO / "scripts"))
    from embedding_deploy import configured_artifacts

    monkeypatch.delenv("DDP_CONFIG", raising=False)
    monkeypatch.setenv("DDP_OUTPUT_ROOT", str(tmp_path))
    file = tmp_path / "application.yaml"
    file.write_text("embedding:\n  provider_file: null\n")
    with pytest.raises(ValueError, match="explicit"):
        configured_artifacts(tmp_path)
    file.write_text(
        "embedding:\n  deployment_artifacts:\n    main: '"
        + "1" * 64
        + "'\n    auxiliary: '"
        + "2" * 64
        + "'\n"
    )
    assert configured_artifacts(tmp_path) == {"main": "1" * 64, "auxiliary": "2" * 64}


def test_search_cli_invalid_argument_is_structured(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts/manage_platform.py"),
            "embedding-search",
            "--limit",
            "not-a-number",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "blocked"


def test_incomplete_active_generation_blocks_before_publish():
    from types import SimpleNamespace

    from driving_data_platform.publishing.store import validate_active_generation

    connection = SimpleNamespace(
        execute=lambda _: SimpleNamespace(
            fetchone=lambda: ({"schema_version": "warehouse-schema-v1"},)
        )
    )
    with pytest.raises(ValueError, match="活动版本"):
        validate_active_generation(connection)


def test_diagnostic_global_options_before_command(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts/manage_platform.py"),
            "--config",
            str(tmp_path / "not-created.yaml"),
            "diagnose",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "--backend" in result.stdout and "--image" in result.stdout
    assert not list(tmp_path.iterdir())


def test_run_show_does_not_create_query_reports(tmp_path):
    report = tmp_path / "reports"
    report.mkdir()
    run_id = "1" * 32
    file = report / ("pipeline-" + run_id + ".json")
    original = json.dumps({"report_schema_version": 1, "status": "failed", "steps": {}})
    file.write_text(original)
    result = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts/manage_platform.py"),
            "run-show",
            "--root",
            str(tmp_path),
            "--run-id",
            run_id,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout
    assert list(report.iterdir()) == [file]
    assert file.read_text() == original
    assert not (tmp_path / ".operation.lock").exists()
