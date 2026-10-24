"""Opt-in real-service failure and recovery checks; never enabled by default."""

import json
import os
import uuid
from pathlib import Path

import pytest

from driving_data_platform.common.config import load_config
from driving_data_platform.ingestion import hdfs
from driving_data_platform.ingestion.nuscenes import ingest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DDP_REAL_HDFS_ENV_FILE"),
    reason="explicit real HDFS opt-in required",
)


@pytest.mark.parametrize("boundary", ["before_rename", "before_marker"])
def test_interruption_has_no_completion_and_recovers(source, monkeypatch, boundary):
    repository, config, _ = source
    actual_repository = Path(__file__).resolve().parents[1]
    live_config = load_config(actual_repository / "configs/application.local.yaml")
    config["storage"]["output_root"] = live_config["storage"]["output_root"]
    # Different real bytes create a separate test identity; never modify mini.
    fixture_media = Path(config["dataset"]["root"]) / "samples/image.jpg"
    fixture_media.write_bytes(
        f"hdfs-interruption-fixture-{boundary}-{uuid.uuid4().hex}".encode()
    )
    result = ingest(config, repository)
    snapshot = f"{result['snapshot_id']}-nuscenes-ods-v1"
    env_file = Path(os.environ["DDP_REAL_HDFS_ENV_FILE"])
    remote = hdfs.Hdfs(env_file, actual_repository)
    target = f"/ddp/ods/snapshots/{snapshot}"
    assert not remote.exists(target), (
        "fixture identity already exists; retain evidence and choose a new fixture version"
    )
    execute = hdfs.Hdfs.execute
    interrupted_staging = []

    def interrupt(self, *args, **kwargs):
        if boundary == "before_rename" and args[0] == "-mv" and ".staging-" in args[1]:
            interrupted_staging.append(args[1])
            raise hdfs.HdfsError("intentional pre-rename interruption")
        if (
            boundary == "before_marker"
            and args[0] == "-put"
            and args[-1].endswith("._COMPLETE.upload")
        ):
            raise hdfs.HdfsError("intentional pre-marker interruption")
        return execute(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(hdfs.Hdfs, "execute", interrupt)
        with pytest.raises(hdfs.HdfsError, match="intentional"):
            hdfs.upload(config, actual_repository, snapshot, env_file)
    assert not remote.exists(f"{target}/_COMPLETE.json")
    if boundary == "before_rename":
        assert interrupted_staging and remote.exists(interrupted_staging[0])
        assert not remote.exists(target)
    else:
        assert remote.exists(target)
    recovered = hdfs.upload(config, actual_repository, snapshot, env_file)
    assert recovered["recovered"] == (boundary == "before_marker")
    assert recovered["reused"] is False
    assert remote.exists(f"{target}/_COMPLETE.json")
    evidence = Path(config["storage"]["output_root"]) / "execution-baseline-20261024"
    evidence.mkdir(exist_ok=True)
    (evidence / f"hdfs-failure-{boundary}.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "fixture": True,
                "boundary": boundary,
                "no_completion_after_interruption": True,
                "recovery": recovered,
                "retained_staging": interrupted_staging,
            },
            indent=2,
        )
    )
