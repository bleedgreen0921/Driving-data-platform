from pathlib import Path
from unittest.mock import Mock

import pytest

from driving_data_platform.common.config import ConfigError
from driving_data_platform.ingestion.hdfs import Hdfs, upload
from driving_data_platform.ingestion.nuscenes import IngestionError


@pytest.mark.parametrize(
    "identity",
    [
        "../escape",
        "a" * 64,
        "a" * 64 + "-changed-version",
        "x" * 64 + "-nuscenes-ods-v1",
    ],
)
def test_invalid_identity_never_invokes_container(identity, monkeypatch, tmp_path):
    invoke = Mock(side_effect=AssertionError("unexpected container invocation"))
    monkeypatch.setattr("subprocess.run", invoke)
    with pytest.raises(ConfigError):
        upload({}, tmp_path, identity, tmp_path / "secret.env")
    invoke.assert_not_called()


def test_missing_or_corrupt_local_snapshot_never_invokes_container(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    invoke = Mock(side_effect=AssertionError("unexpected container invocation"))
    monkeypatch.setattr("subprocess.run", invoke)
    with pytest.raises(IngestionError):
        upload(
            {"storage": {"output_root": str(output)}},
            repo,
            "a" * 64 + "-nuscenes-ods-v1",
            tmp_path / "secret.env",
        )
    invoke.assert_not_called()


def test_remote_readback_detects_corruption():
    remote = Hdfs(Path("/unused"), Path("/unused"))
    remote.execute = Mock(return_value=Mock(stdout=b"changed"))
    with pytest.raises(IngestionError, match="mismatch"):
        remote.verify("/immutable", {"raw.json": {"bytes": 7, "sha256": "0" * 64}})


def test_symlinked_snapshot_is_rejected_before_container(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    output = tmp_path / "output"
    snapshots = output / "ingestion/snapshots"
    snapshots.mkdir(parents=True)
    identity = "a" * 64 + "-nuscenes-ods-v1"
    (snapshots / identity).symlink_to(tmp_path / "other", target_is_directory=True)
    invoke = Mock(side_effect=AssertionError("unexpected container invocation"))
    monkeypatch.setattr("subprocess.run", invoke)
    with pytest.raises(ConfigError, match="symlink"):
        upload(
            {"storage": {"output_root": str(output)}},
            repo,
            identity,
            tmp_path / "secret.env",
        )
    invoke.assert_not_called()


@pytest.mark.parametrize("content", [b"broken", b"[]", b"null", b"\xff"])
def test_invalid_remote_completion_is_quality_failure(content):
    remote = Hdfs(Path("/unused"), Path("/unused"))
    remote.execute = Mock(return_value=Mock(stdout=content))
    with pytest.raises(IngestionError, match="manifest"):
        remote.manifest("/immutable/_COMPLETE.json")
