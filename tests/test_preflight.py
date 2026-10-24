import json
from pathlib import Path

import pytest

from driving_data_platform.common.config import ConfigError, external_path, load_config
from driving_data_platform.common.preflight import main, preflight


def test_read_only_and_task_scope(assets):
    repo, config, _, _ = assets
    assert preflight(config, repo)["status"] == "passed"
    assert not Path(config["storage"]["output_root"]).exists()
    assert set(preflight(config, repo, "dataset")["assets"]) == {"dataset"}


def test_env_empty_does_not_fallback(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("dataset:\n  root: /example\n")
    assert load_config(path, {"DDP_DATASET_ROOT": ""})["dataset"]["root"] == ""


@pytest.mark.parametrize(
    "content", ["", "[]", "dataset: secret", "dataset: [", "!!python/object:bad {}"]
)
def test_bad_yaml(tmp_path, content):
    path = tmp_path / "config.yaml"
    path.write_text(content)
    with pytest.raises(ConfigError):
        load_config(path, {})


@pytest.mark.parametrize("kind", ["inside", "ancestor", "relative", "symlink"])
def test_external_paths(tmp_path, kind):
    repo = tmp_path / "repo"
    repo.mkdir()
    link = tmp_path / "link"
    link.symlink_to(repo, target_is_directory=True)
    value = {
        "inside": repo / "data",
        "ancestor": tmp_path,
        "relative": Path("data"),
        "symlink": link,
    }[kind]
    with pytest.raises(ConfigError):
        external_path(str(value), repo, "input")


@pytest.mark.parametrize(
    "failure",
    [
        "missing_license",
        "changed",
        "escape",
        "missing_checksum",
        "version",
        "future",
        "output_overlap",
    ],
)
def test_gate_failures(assets, failure):
    repo, config, evidence, proof = assets
    if failure == "missing_license":
        del evidence["dataset"]["license_basis"]
    elif failure == "changed":
        (Path(config["dataset"]["root"]) / "v1.0-mini/scene.json").write_text(
            '[{"token": "changed"}]'
        )
    elif failure == "escape":
        evidence["dataset"]["checksums"]["../escape"] = "0" * 64
    elif failure == "missing_checksum":
        evidence["dataset"]["checksums"].pop("v1.0-mini/scene.json")
    elif failure == "version":
        evidence["dataset"]["version"] = "other"
    elif failure == "future":
        evidence["dataset"]["acquired_at"] = "9999-01-01"
    elif failure == "output_overlap":
        config["storage"]["output_root"] = config["dataset"]["root"]
    proof.write_text(json.dumps(evidence))
    with pytest.raises(ConfigError):
        preflight(config, repo)


def test_cli_failure_sanitized(tmp_path, capsys):
    path = tmp_path / "invalid.yaml"
    path.write_text('postgres: "postgres://secret:password@private"')
    assert main(["--config", str(path)]) == 2
    output = capsys.readouterr().out
    assert json.loads(output)["status"] == "blocked"
    assert "password" not in output


def test_missing_evidence(assets):
    repo, config, _, proof = assets
    proof.unlink()
    with pytest.raises(ConfigError, match="evidence_file"):
        preflight(config, repo)


@pytest.mark.parametrize("value", ["unknown", "TODO", "未知", "待确认"])
def test_unconfirmed_evidence_blocks(assets, value):
    repo, config, evidence, proof = assets
    evidence["dataset"]["license_basis"] = value
    proof.write_text(json.dumps(evidence))
    with pytest.raises(ConfigError, match="unconfirmed evidence"):
        preflight(config, repo)


@pytest.mark.parametrize("value", ["{}", "[1]", "not json"])
def test_table_structure_failure(assets, value):
    repo, config, _, _ = assets
    table = Path(config["dataset"]["root"]) / "v1.0-mini/scene.json"
    table.write_text(value)
    with pytest.raises(ConfigError, match="dataset:"):
        preflight(config, repo)


def test_cli_success(assets, capsys, monkeypatch):
    import yaml

    from driving_data_platform.common.config import ENV_FIELDS

    _, config, _, proof = assets
    for name in ENV_FIELDS:
        monkeypatch.delenv(name, raising=False)
    path = proof.parent / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    assert main(["--config", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["scope"] == "asset_preflight_only"


def test_asset_symlink_escape(assets):
    repo, config, _, proof = assets
    weight = Path(config["dataset"]["root"]) / "v1.0-mini/scene.json"
    weight.unlink()
    weight.symlink_to(proof)
    with pytest.raises(ConfigError, match="path escape"):
        preflight(config, repo)


@pytest.mark.parametrize("task", ["model", "all", "unknown"])
def test_removed_tasks_rejected(assets, task):
    repo, config, _, _ = assets
    with pytest.raises(ConfigError, match="only dataset"):
        preflight(config, repo, task)


def test_cli_ignores_obsolete_model_environment(assets, monkeypatch, capsys):
    import yaml

    from driving_data_platform.common.config import ENV_FIELDS

    _, config, _, proof = assets
    for name in ENV_FIELDS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DDP_MODEL_ROOT", "/does-not-exist")
    monkeypatch.setenv("DDP_MODEL_REVISION", "")
    monkeypatch.setenv("DDP_EMBEDDING_DIMENSION", "invalid")
    path = proof.parent / "data-only.yaml"
    path.write_text(yaml.safe_dump(config))
    assert "model" not in load_config(path)
    assert main(["--config", str(path)]) == 0
    assert set(json.loads(capsys.readouterr().out)["assets"]) == {"dataset"}
