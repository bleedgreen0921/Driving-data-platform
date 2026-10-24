"""Commit verified local ODS into project HDFS using the fixed Compose service."""

import argparse
import fcntl
import hashlib
import json
import re
import subprocess
import uuid
from pathlib import Path

from driving_data_platform.common.config import (
    ConfigError,
    external_path,
    load_config,
    required,
)
from driving_data_platform.common.identity import canonical
from driving_data_platform.ingestion.nuscenes import IngestionError, verify_snapshot


class HdfsError(RuntimeError):
    """A remote operation did not complete; its output is not safe to consume."""


class Hdfs:
    def __init__(self, env_file: Path, repository: Path):
        self.command = [
            "docker",
            "compose",
            "--env-file",
            str(env_file),
            "-f",
            str(repository / "infra/docker/compose.yaml"),
            "exec",
            "-T",
            "namenode",
            "hdfs",
            "dfs",
        ]

    def execute(self, *args: str, data: bytes | None = None, allowed=(0,)):
        try:
            result = subprocess.run(
                [*self.command, *args],
                input=data,
                capture_output=True,
                timeout=180,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise HdfsError("HDFS invocation unavailable or timed out") from None
        if result.returncode not in allowed:
            raise HdfsError("HDFS operation failed; remote completion not confirmed")
        return result

    def exists(self, path: str) -> bool:
        # A missing path returns 1, but so can a connection error: never interpret
        # this check alone as evidence that a remote operation succeeded.
        return self.execute("-test", "-e", path, allowed=(0, 1)).returncode == 0

    def manifest(self, path: str) -> dict:
        try:
            value = json.loads(self.execute("-cat", path).stdout)
        except (ValueError, UnicodeError):
            raise IngestionError("HDFS completion manifest invalid") from None
        if not isinstance(value, dict):
            raise IngestionError("HDFS completion manifest must be a mapping")
        return value

    def verify(self, root: str, artifacts: dict) -> None:
        for relative, item in sorted(artifacts.items()):
            content = self.execute("-cat", f"{root}/{relative}").stdout
            if (
                len(content) != item["bytes"]
                or hashlib.sha256(content).hexdigest() != item["sha256"]
            ):
                raise IngestionError("HDFS readback: size or SHA-256 mismatch")


def upload(config: dict, repository: Path, snapshot: str, env_file: Path) -> dict:
    if not re.fullmatch(r"[a-f0-9]{64}-nuscenes-ods-v1", snapshot):
        raise ConfigError("snapshot: expected fixed ODS snapshot identity")
    output = external_path(
        required(config, "storage", "output_root"), repository, "output_root"
    )
    source = output / "ingestion" / "snapshots" / snapshot
    if any(path.is_symlink() for path in (source, source.parent, source.parent.parent)):
        raise ConfigError("snapshot: symlinked ODS path is not allowed")
    complete = verify_snapshot(source)
    if source.name != f"{complete['snapshot_id']}-{complete['processing_version']}":
        raise IngestionError("snapshot: identity mismatch")
    artifacts = {}
    for relative in [*sorted(complete["artifacts"]), "complete.json"]:
        path = source / relative
        content = path.read_bytes()
        artifacts[relative] = {
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    expected = {
        "input_snapshot": complete["snapshot_id"],
        "processing_version": "hdfs-ods-v1",
        "ods_processing_version": complete["processing_version"],
        "run_id": complete["run_id"],
        "artifacts": artifacts,
    }
    remote = Hdfs(env_file, repository)
    base = "/ddp/ods/snapshots"
    target = f"{base}/{snapshot}"
    marker = f"{target}/_COMPLETE.json"
    # Locks live outside the repository; same-host project executors serialize.
    with (output / "ingestion" / ".hdfs-upload.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        reused = remote.exists(marker)
        recovered = False
        if reused:
            actual = remote.manifest(marker)
            if actual != expected:
                raise IngestionError(
                    "HDFS completion identity or artifact manifest mismatch"
                )
            remote.verify(target, artifacts)
        else:
            recovered = remote.exists(target)
            staging = target if recovered else f"{base}/.staging-{uuid.uuid4().hex}"
            if not recovered:
                remote.execute("-mkdir", "-p", base)
                remote.execute("-mkdir", staging)
                # Only known ODS artifacts, never source camera or point cloud files.
                remote.execute(
                    "-put",
                    *[
                        f"/input-ods/{snapshot}/{p}"
                        for p in (
                            "raw",
                            "records",
                            "file-manifest.json",
                            "quality.json",
                            "complete.json",
                        )
                    ],
                    staging,
                )
            # An uncommitted final directory is recoverable only after full readback.
            remote.verify(staging, artifacts)
            # Re-verify local immutable input before committing remote output.
            if verify_snapshot(source) != complete:
                raise IngestionError("local completion changed during upload")
            if not recovered:
                remote.execute("-mv", staging, target)
            remote.execute(
                "-put",
                "-f",
                "-",
                f"{target}/._COMPLETE.upload",
                data=canonical(expected),
            )
            remote.execute("-mv", f"{target}/._COMPLETE.upload", marker)
            if remote.manifest(marker) != expected:
                raise IngestionError("HDFS final completion could not be verified")
        return {
            "status": "passed",
            "exit_code": 0,
            "reused": reused,
            "recovered": recovered,
            **expected,
            "remote_path": target,
            "verified_artifact_count": len(artifacts),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = upload(
            load_config(args.config),
            Path(__file__).resolve().parents[3],
            args.snapshot,
            args.env_file,
        )
        code = 0
    except (ConfigError, IngestionError):
        report, code = (
            {"status": "blocked", "error": "configuration or quality failure"},
            2,
        )
    except HdfsError:
        # Unknown failures are not automatically retryable.
        report, code = (
            {
                "status": "blocked",
                "error": "HDFS operation unconfirmed; inspect remote state",
            },
            1,
        )
    except OSError:
        report, code = {"status": "blocked", "error": "local artifact unavailable"}, 2
    report.setdefault("exit_code", code)
    print(json.dumps(report, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
