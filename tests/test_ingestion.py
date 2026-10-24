import json
from pathlib import Path

import pytest

from driving_data_platform.common.identity import entity_id
from driving_data_platform.common.preflight import digest
from driving_data_platform.ingestion import nuscenes


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


def test_real_pipeline_idempotent(source):
    repo, config, _ = source
    first = nuscenes.ingest(config, repo)
    second = nuscenes.ingest(config, repo)
    assert first["snapshot_id"] == second["snapshot_id"]
    assert first["run_id"] == second["run_id"]
    assert not first["reused"] and second["reused"]
    assert first["map_links"] == {"total": 2, "local": 1, "outside": 1}
    snapshots = Path(config["storage"]["output_root"]) / "ingestion/snapshots"
    directories = list(snapshots.iterdir())
    assert len(directories) == 1
    record = json.loads((directories[0] / "records/sample.jsonl").read_text())
    assert record["entity_id"] == entity_id(
        record["dataset_namespace"], "sample", "sample"
    )
    assert (directories[0] / "raw/map.json").read_bytes() == (
        Path(config["dataset"]["root"]) / "v1.0-mini/map.json"
    ).read_bytes()


@pytest.mark.parametrize(
    "failure",
    [
        "duplicate",
        "foreign_key",
        "timestamp",
        "chain",
        "map_missing",
        "map_ambiguous",
        "coverage",
    ],
)
def test_quality_gate(source, failure):
    _, _, tables = source
    if failure == "duplicate":
        tables["sample"].append(dict(tables["sample"][0]))
    elif failure == "foreign_key":
        tables["sample"][0]["scene_token"] = "outside"
    elif failure == "timestamp":
        tables["sample"][0]["timestamp"] = 1500000000
    elif failure == "chain":
        tables["sample"][0]["next"] = "absent"
    elif failure == "map_missing":
        tables["map"][0]["log_tokens"] = ["outside"]
    elif failure == "map_ambiguous":
        tables["map"].append({"token": "other-map", "log_tokens": ["log"]})
    else:
        tables["sample_data"][0]["is_key_frame"] = False
    with pytest.raises(nuscenes.IngestionError):
        nuscenes.validate(tables)


@pytest.mark.parametrize(
    "filename", ["../escape", "/etc/passwd", "samples/missing.jpg"]
)
def test_media_failure_no_watermark(source, filename):
    repo, config, tables = source
    tables["sample_data"][0]["filename"] = filename
    # Exercise inventory independently of evidence: changing source JSON would
    # correctly fail preflight first.
    with pytest.raises(nuscenes.IngestionError):
        nuscenes.inventory(Path(config["dataset"]["root"]), "v1.0-mini", tables)
    (Path(config["dataset"]["root"]) / "samples/image.jpg").unlink()
    with pytest.raises(nuscenes.IngestionError):
        nuscenes.ingest(config, repo)
    assert not (
        Path(config["storage"]["output_root"]) / "ingestion/watermark.json"
    ).exists()


def test_committed_corruption_blocks_and_preserves_watermark(source):
    repo, config, _ = source
    nuscenes.ingest(config, repo)
    output = Path(config["storage"]["output_root"]) / "ingestion"
    original = (output / "watermark.json").read_bytes()
    target = next((output / "snapshots").iterdir())
    (target / "records/sample.jsonl").write_text("corrupted")
    with pytest.raises(nuscenes.IngestionError, match="corruption"):
        nuscenes.ingest(config, repo)
    assert (output / "watermark.json").read_bytes() == original


def test_failed_commit_can_recover(source, monkeypatch):
    repo, config, _ = source
    original = nuscenes.atomic_json

    def fail(*args):
        raise OSError("injected watermark failure")

    monkeypatch.setattr(nuscenes, "atomic_json", fail)
    with pytest.raises(OSError):
        nuscenes.ingest(config, repo)
    output = Path(config["storage"]["output_root"]) / "ingestion"
    assert not (output / "watermark.json").exists()
    assert len(list((output / "snapshots").iterdir())) == 1
    monkeypatch.setattr(nuscenes, "atomic_json", original)
    assert nuscenes.ingest(config, repo)["reused"]


def test_incomplete_manifest_rejected(source):
    repo, config, _ = source
    nuscenes.ingest(config, repo)
    target = next(
        (Path(config["storage"]["output_root"]) / "ingestion/snapshots").iterdir()
    )
    path = target / "complete.json"
    complete = json.loads(path.read_text())
    del complete["artifacts"]["records/sample.jsonl"]
    path.write_text(json.dumps(complete))
    with pytest.raises(nuscenes.IngestionError, match="incomplete"):
        nuscenes.ingest(config, repo)


def test_output_symlink_cannot_modify_input(source):
    repo, config, _ = source
    output = Path(config["storage"]["output_root"])
    output.mkdir()
    root = Path(config["dataset"]["root"])
    (output / "ingestion").symlink_to(root, target_is_directory=True)
    with pytest.raises(nuscenes.IngestionError, match="symlink"):
        nuscenes.ingest(config, repo)
    assert not (root / ".lock").exists()


def test_interrupted_copy_does_not_advance_and_can_retry(source, monkeypatch):
    repo, config, _ = source
    original = nuscenes.shutil.copyfile

    def fail(*args):
        raise OSError("injected copy failure")

    monkeypatch.setattr(nuscenes.shutil, "copyfile", fail)
    with pytest.raises(OSError):
        nuscenes.ingest(config, repo)
    output = Path(config["storage"]["output_root"]) / "ingestion"
    assert not (output / "watermark.json").exists()
    assert all(p.name.startswith(".staging-") for p in (output / "snapshots").iterdir())
    monkeypatch.setattr(nuscenes.shutil, "copyfile", original)
    assert nuscenes.ingest(config, repo)["status"] == "passed"


def test_concurrent_runs_commit_one_snapshot(source):
    from concurrent.futures import ThreadPoolExecutor

    repo, config, _ = source
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: nuscenes.ingest(config, repo), range(2)))
    assert sorted(result["reused"] for result in results) == [False, True]
    assert len({result["run_id"] for result in results}) == 1
