from pathlib import Path

import pytest

from driving_data_platform.common.config import ConfigError
from driving_data_platform.common.preflight import preflight
from driving_data_platform.core.model import AssetPolicy, AssetState
from driving_data_platform.ingestion.nuscenes import (
    IngestionError,
    ingest,
    inventory,
)


def test_policies_share_one_path_and_keep_identity_separate(source):
    repo, config, tables = source
    strict = ingest(config, repo)
    assert strict["asset_policy"] == "strict"
    assert strict["capabilities"]["original_media"] is True

    config["dataset"]["assets"] = {"policy": "registered"}
    root = Path(config["dataset"]["root"])
    (root / "samples/image.jpg").unlink()
    (root / "maps/map.png").unlink()
    registered = ingest(config, repo)

    assert registered["snapshot_id"] != strict["snapshot_id"]
    assert registered["asset_policy"] == "registered"
    assert registered["capabilities"]["original_media"] is False
    assets = inventory(root, "v1.0-mini", tables, AssetPolicy.REGISTERED)
    media = [item for item in assets if item.kind != "metadata"]
    assert all(item.state == AssetState.MISSING for item in media)
    assert all(item.sha256 is None for item in media)


def test_registered_policy_still_checks_paths(source):
    _, config, tables = source
    config["dataset"]["assets"] = {"policy": "registered"}
    for bad in ["../escape.jpg", "/escape.jpg"]:
        tables["sample_data"][0]["filename"] = bad
        with pytest.raises(IngestionError):
            inventory(
                Path(config["dataset"]["root"]),
                "v1.0-mini",
                tables,
                AssetPolicy.REGISTERED,
            )


def test_invalid_policy_and_bad_json_are_rejected(source):
    repo, config, _ = source
    config["dataset"]["assets"] = {"policy": "unknown"}
    with pytest.raises(ConfigError):
        preflight(config, repo)
    config["dataset"]["assets"] = {"policy": "strict"}
    table = Path(config["dataset"]["root"]) / "v1.0-mini/sample.json"
    table.write_text("{")
    with pytest.raises(ConfigError):
        preflight(config, repo)
    table.unlink()
    with pytest.raises(ConfigError):
        preflight(config, repo)
