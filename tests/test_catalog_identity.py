"""只验证目录身份契约，真实读写另由 Metastore 验证器执行。"""

import json
import shutil
from pathlib import Path

import pytest

from driving_data_platform.common.contract import load_contract
from driving_data_platform.common.preflight import digest
from driving_data_platform.warehouse.catalog import production_entry


@pytest.fixture
def identity_files(tmp_path):
    root = tmp_path / "artifacts"
    source = root / "warehouse" / "version"
    source.mkdir(parents=True)
    (source / "data.bin").write_bytes(b"identity-contract")
    (source / "complete.json").write_text(
        json.dumps(
            {
                "artifacts": {"data.bin": digest(source / "data.bin")},
                "tables": {},
                "processing_version": "version",
            }
        )
    )
    repo = tmp_path / "repo"
    for name in ("catalog.py", "catalog_job.py", "catalog_contract.py"):
        path = repo / "src/driving_data_platform/warehouse" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("identity-contract")
    runtime = repo / "infra/metastore/runtime.lock.json"
    runtime.parent.mkdir(parents=True)
    runtime.write_text(
        json.dumps(
            {
                "hive": {"image": "test-hive"},
                "spark_client": {"image": "test-spark"},
                "jdbc": {"sha256": "test-jdbc"},
            }
        )
    )
    (repo / "contracts").mkdir()
    shutil.copy(
        Path(__file__).resolve().parents[1] / "contracts/nuscenes-mini-v1.json",
        repo / "contracts/nuscenes-mini-v1.json",
    )
    manifest = json.loads((source / "complete.json").read_text())
    manifest["data_contract"] = load_contract(repo)
    (source / "complete.json").write_text(json.dumps(manifest))
    return root, repo, source


def test_same_source_reuses_production_catalog(identity_files):
    root, repo, source = identity_files
    first = production_entry(root, repo, source)
    assert production_entry(root, repo, source) == first
    assert json.loads((first / "request.json").read_text())["production"] is True


def test_changed_consumer_requires_new_processing_version(identity_files):
    root, repo, source = identity_files
    production_entry(root, repo, source)
    (repo / "src/driving_data_platform/warehouse/catalog_job.py").write_text("changed")
    with pytest.raises(ValueError, match="新的 --version"):
        production_entry(root, repo, source)


def test_corruption_rejected_before_catalog_creation(identity_files):
    root, repo, source = identity_files
    (source / "data.bin").write_bytes(b"corrupted")
    with pytest.raises(ValueError):
        production_entry(root, repo, source)
    assert not (root / "metastore/generations").exists()
