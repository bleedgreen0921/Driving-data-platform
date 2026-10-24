import json

import pytest

from driving_data_platform.common.preflight import TABLES, digest


@pytest.fixture
def assets(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    data = tmp_path / "data"
    for directory in ("v1.0-mini", "samples", "sweeps", "maps"):
        (data / directory).mkdir(parents=True)
    for table in TABLES:
        (data / "v1.0-mini" / f"{table}.json").write_text("[]")
    evidence = {}
    for label, root, version in [
        ("dataset", data, "v1.0-mini"),
    ]:
        evidence[label] = {
            "source_url": "https://example.invalid/source",
            "license": "test-only",
            "license_basis": "fixture",
            "acquired_at": "2026-01-01",
            "version": version,
            "checksums": {
                str(p.relative_to(root)): digest(p)
                for p in root.rglob("*")
                if p.is_file()
            },
        }
    proof = tmp_path / "evidence.json"
    proof.write_text(json.dumps(evidence))
    config = {
        "dataset": {"root": str(data), "version": "v1.0-mini"},
        "storage": {"output_root": str(tmp_path / "output")},
        "assets": {"evidence_file": str(proof)},
    }
    return repo, config, evidence, proof
