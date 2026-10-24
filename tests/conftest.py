import json
from pathlib import Path

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


@pytest.fixture
def source(assets):
    repo, config, evidence, proof = assets
    root = Path(config["dataset"]["root"])
    tables = {
        "scene": [
            {
                "token": "scene",
                "log_token": "log",
                "first_sample_token": "sample",
                "last_sample_token": "sample",
                "nbr_samples": 1,
            }
        ],
        "sample": [
            {
                "token": "sample",
                "scene_token": "scene",
                "timestamp": 1500000000000000,
                "prev": "",
                "next": "",
            }
        ],
        "sample_data": [
            {
                "token": "media",
                "sample_token": "sample",
                "ego_pose_token": "pose",
                "calibrated_sensor_token": "calibration",
                "timestamp": 1500000000000000,
                "prev": "",
                "next": "",
                "filename": "samples/image.jpg",
                "is_key_frame": True,
            }
        ],
        "sensor": [{"token": "sensor", "modality": "camera", "channel": "CAM_FRONT"}],
        "calibrated_sensor": [{"token": "calibration", "sensor_token": "sensor"}],
        "ego_pose": [{"token": "pose", "timestamp": 1500000000000000}],
        "sample_annotation": [
            {
                "token": "annotation",
                "sample_token": "sample",
                "instance_token": "instance",
                "visibility_token": "visibility",
                "attribute_tokens": ["attribute"],
                "prev": "",
                "next": "",
            }
        ],
        "instance": [
            {
                "token": "instance",
                "category_token": "category",
                "first_annotation_token": "annotation",
                "last_annotation_token": "annotation",
                "nbr_annotations": 1,
            }
        ],
        "category": [{"token": "category"}],
        "attribute": [{"token": "attribute"}],
        "visibility": [{"token": "visibility"}],
        "log": [{"token": "log"}],
        "map": [
            {
                "token": "map",
                "log_tokens": ["log", "outside"],
                "filename": "maps/map.png",
            }
        ],
    }
    for table, rows in tables.items():
        (root / "v1.0-mini" / f"{table}.json").write_text(json.dumps(rows))
    for name in ("samples/image.jpg", "maps/map.png"):
        (root / name).write_bytes(b"ingestion-fixture-not-decoded")
    evidence["dataset"]["checksums"] = {
        str(p.relative_to(root)): digest(p) for p in (root / "v1.0-mini").iterdir()
    }
    proof.write_text(json.dumps(evidence))
    return repo, config, tables
