"""Read-only configuration and asset gate. No service connections or source ingestion."""

import argparse
import hashlib
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlsplit

from .config import ConfigError, external_path, load_config, required

TABLES = (
    "scene",
    "sample",
    "sample_data",
    "sensor",
    "calibrated_sensor",
    "ego_pose",
    "sample_annotation",
    "instance",
    "category",
    "log",
    "map",
    "attribute",
    "visibility",
)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _evidence(config: dict, repository: Path) -> dict:
    path = external_path(
        required(config, "assets", "evidence_file"), repository, "assets.evidence_file"
    )
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise ConfigError("assets.evidence_file: unreadable or invalid JSON") from None
    if not isinstance(evidence, dict):
        raise ConfigError("assets.evidence_file: expected object")
    return evidence


def _verify(
    asset: dict, root: Path, expected_version: str, label: str, required_files: set[str]
) -> dict:
    if not isinstance(asset, dict):
        raise ConfigError(f"{label}: missing evidence")
    for field in ("source_url", "license", "license_basis", "acquired_at", "version"):
        if not isinstance(asset.get(field), str) or not asset[field].strip():
            raise ConfigError(f"{label}.{field}: missing evidence")
        if asset[field].strip().casefold() in {
            "unknown",
            "todo",
            "tbd",
            "未知",
            "待确认",
            "待补充",
        }:
            raise ConfigError(f"{label}.{field}: unconfirmed evidence")
    try:
        source = urlsplit(asset["source_url"])
        valid_source = source.scheme in {"http", "https"} and bool(source.hostname)
    except ValueError:
        valid_source = False
    if not valid_source:
        raise ConfigError(f"{label}.source_url: expected HTTP(S) source")
    try:
        acquired = date.fromisoformat(asset["acquired_at"])
    except ValueError:
        raise ConfigError(f"{label}.acquired_at: expected ISO date") from None
    if acquired > datetime.now(UTC).date():
        raise ConfigError(f"{label}.acquired_at: future date")
    if asset["version"] != expected_version:
        raise ConfigError(f"{label}.version: configuration mismatch")
    checksums = asset.get("checksums")
    if not isinstance(checksums, dict) or not checksums:
        raise ConfigError(f"{label}.checksums: nonempty mapping required")
    if not required_files.issubset(checksums):
        raise ConfigError(f"{label}.checksums: required files not covered")
    for relative, expected in checksums.items():
        if not isinstance(relative, str) or not isinstance(expected, str):
            raise ConfigError(f"{label}.checksums: invalid entry")
        path = root / relative
        if Path(relative).is_absolute() or not path.resolve().is_relative_to(root):
            raise ConfigError(f"{label}.checksums: path escape")
        if not re.fullmatch("[0-9a-f]{64}", expected):
            raise ConfigError(f"{label}.checksums: invalid SHA-256")
        if not path.is_file() or digest(path) != expected:
            raise ConfigError(f"{label}.checksums: missing or changed file")
    return {"version": expected_version, "verified_files": len(checksums)}


def preflight(config: dict, repository: Path, task: str = "dataset") -> dict:
    if task != "dataset":
        raise ConfigError("task: unsupported value; only dataset is supported")
    root = external_path(
        required(config, "dataset", "root"), repository, "dataset.root"
    )
    output = external_path(
        required(config, "storage", "output_root"), repository, "storage.output_root"
    )
    if not root.is_dir():
        raise ConfigError("dataset.root: directory missing")
    if output.is_relative_to(root) or root.is_relative_to(output):
        raise ConfigError("storage.output_root: overlaps input")
    if output.exists() and not output.is_dir():
        raise ConfigError("storage.output_root: not a directory")
    evidence = _evidence(config, repository)
    version = required(config, "dataset", "version")
    if Path(version).name != version or version in {".", ".."}:
        raise ConfigError("dataset.version: expected directory name")
    files = {f"{version}/{table}.json" for table in TABLES}
    for directory in (version, "samples", "sweeps", "maps"):
        path = root / directory
        if not path.resolve().is_relative_to(root) or not path.is_dir():
            raise ConfigError("dataset: required directory missing or escaped")
    for relative in files:
        path = root / relative
        if not path.resolve().is_relative_to(root):
            raise ConfigError("dataset: table path escape")
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            raise ConfigError("dataset: unreadable or invalid table") from None
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
            raise ConfigError("dataset: expected record arrays")
    result = _verify(evidence.get("dataset"), root, version, "dataset", files)
    return {
        "status": "passed",
        "scope": "asset_preflight_only",
        "assets": {"dataset": result},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--task", choices=("dataset",), default="dataset")
    args = parser.parse_args(argv)
    repository = Path(__file__).resolve().parents[3]
    try:
        report = preflight(load_config(args.config), repository, args.task)
    except (ConfigError, OSError) as error:
        # ConfigError contains only field labels; OSError can expose private paths.
        message = str(error) if isinstance(error, ConfigError) else "asset: I/O failure"
        print(json.dumps({"status": "blocked", "error": message}))
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
