import copy
from pathlib import Path

import pytest

from driving_data_platform.common.config import ConfigError, load_config
from driving_data_platform.common.resources import GIB, evaluate, main


@pytest.fixture
def budget():
    config = load_config(Path("configs/platform.example.yaml"), {})
    config["disk_budget_gib"] = dict(zip(config["disk_budget_gib"], [14, 3, 3, 4]))
    return config


def test_exact_boundary(budget):
    report = evaluate(budget, 11005853696, 36 * GIB, 36 * GIB)
    assert report["status"] == "passed"
    assert not any(report["shortfall_bytes"].values())


@pytest.mark.parametrize("resource", ["memory", "root_disk", "target_disk"])
def test_shortfall_is_not_hidden(budget, resource):
    capacities = {"memory": 11005853696, "root_disk": 36 * GIB, "target_disk": 36 * GIB}
    capacities[resource] -= 1
    report = evaluate(budget, *capacities.values())
    assert report["status"] == "blocked"
    assert report["shortfall_bytes"][resource] == 1


@pytest.mark.parametrize("value", [None, True, -1, 0, "14"])
def test_unverified_budget_rejected(budget, value):
    budget["disk_budget_gib"]["images_and_build"] = value
    with pytest.raises(ConfigError):
        evaluate(budget, 100 * GIB, 100 * GIB, 100 * GIB)


def test_cannot_relax_user_limits(budget):
    for field, value in [("max_growth_gib", 25), ("root_reserve_gib", 11)]:
        altered = copy.deepcopy(budget)
        altered["resources"][field] = value
        with pytest.raises(ConfigError):
            evaluate(altered, 100 * GIB, 100 * GIB, 100 * GIB)


def test_cli_missing_evidence_fails_closed(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text("deployment:\n  persistence_root: /not/a/real/directory\n")
    assert main(["--config", str(config)]) == 2
    assert "blocked" in capsys.readouterr().out
