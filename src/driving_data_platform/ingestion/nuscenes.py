"""Independent nuScenes snapshot ingestion; local ODS, no SDK or service dependency."""

import argparse
import fcntl
import json
import os
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

from driving_data_platform.common.config import (
    ConfigError,
    external_path,
    load_config,
    required,
)
from driving_data_platform.common.identity import (
    canonical,
    entity_id,
    manifest_revision,
)
from driving_data_platform.common.preflight import TABLES, digest, preflight

PROCESSING_VERSION = "nuscenes-ods-v1"
# Only documented optional chain endpoints may be empty. Other FKs are required.
RELATIONS = {
    "calibrated_sensor": {"sensor_token": "sensor"},
    "instance": {
        "category_token": "category",
        "first_annotation_token": "sample_annotation",
        "last_annotation_token": "sample_annotation",
    },
    "sample": {"scene_token": "scene"},
    "sample_data": {
        "sample_token": "sample",
        "ego_pose_token": "ego_pose",
        "calibrated_sensor_token": "calibrated_sensor",
    },
    "sample_annotation": {
        "sample_token": "sample",
        "instance_token": "instance",
        "visibility_token": "visibility",
    },
    "scene": {
        "log_token": "log",
        "first_sample_token": "sample",
        "last_sample_token": "sample",
    },
}


class IngestionError(ValueError):
    """A source or artifact failed its ingestion contract."""


def write_json(path: Path, value: object) -> None:
    path.write_bytes(canonical(value) + b"\n")


def atomic_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(canonical(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def read_tables(root: Path, version: str) -> dict[str, list[dict]]:
    return {
        table: json.loads((root / version / f"{table}.json").read_text())
        for table in TABLES
    }


def _check(condition: bool, code: str) -> None:
    if not condition:
        raise IngestionError(code)


def validate(tables: dict[str, list[dict]]) -> dict:
    _check(
        all(bool(tables[name]) for name in ("scene", "sample", "sample_data")),
        "source: empty core tables",
    )
    indexes = {}
    for table, rows in tables.items():
        tokens = [row.get("token") for row in rows]
        _check(
            all(isinstance(token, str) and bool(token.strip()) for token in tokens),
            f"{table}: invalid token",
        )
        _check(len(set(tokens)) == len(tokens), f"{table}: duplicate token")
        indexes[table] = {row["token"]: row for row in rows}
    for table, fields in RELATIONS.items():
        for row in tables[table]:
            for field, target in fields.items():
                _check(
                    isinstance(row.get(field), str) and row[field] in indexes[target],
                    f"{table}.{field}: dangling relation",
                )
    for table, owner in [
        ("sample", "scene_token"),
        ("sample_annotation", "instance_token"),
        ("sample_data", None),
    ]:
        for row in tables[table]:
            for field, reciprocal in [("prev", "next"), ("next", "prev")]:
                target = row.get(field)
                _check(isinstance(target, str), f"{table}.{field}: missing chain field")
                if not target:
                    continue
                _check(target in indexes[table], f"{table}.{field}: dangling chain")
                other = indexes[table][target]
                _check(
                    other.get(reciprocal) == row["token"],
                    f"{table}: nonreciprocal chain",
                )
                if owner:
                    _check(other[owner] == row[owner], f"{table}: cross-owner chain")
                if table in {"sample", "sample_data"}:
                    timestamp, other_time = row.get("timestamp"), other.get("timestamp")
                    _check(
                        type(timestamp) is int and type(other_time) is int,
                        f"{table}: invalid timestamp",
                    )
                    _check(
                        other_time < timestamp
                        if field == "prev"
                        else other_time > timestamp,
                        f"{table}: nonmonotonic chain",
                    )
                if table == "sample_data":
                    a = indexes["calibrated_sensor"][row["calibrated_sensor_token"]][
                        "sensor_token"
                    ]
                    b = indexes["calibrated_sensor"][other["calibrated_sensor_token"]][
                        "sensor_token"
                    ]
                    _check(a == b, "sample_data: cross-sensor chain")
    for table in ("sample", "sample_data", "ego_pose"):
        for row in tables[table]:
            value = row.get("timestamp")
            _check(
                type(value) is int and 10**14 <= value < 10**17,
                f"{table}: invalid microsecond timestamp",
            )
    for row in tables["sample_annotation"]:
        attributes = row.get("attribute_tokens")
        _check(
            isinstance(attributes, list)
            and all(
                isinstance(x, str) and x in indexes["attribute"] for x in attributes
            ),
            "sample_annotation: dangling attribute",
        )
    keyframes = {token: [] for token in indexes["sample"]}
    for row in tables["sample_data"]:
        _check(
            type(row.get("is_key_frame")) is bool, "sample_data: invalid keyframe flag"
        )
        if row["is_key_frame"]:
            sensor = indexes["calibrated_sensor"][row["calibrated_sensor_token"]][
                "sensor_token"
            ]
            keyframes[row["sample_token"]].append(sensor)
    expected_sensors = set(indexes["sensor"])
    _check(
        all(
            len(values) == len(expected_sensors) and set(values) == expected_sensors
            for values in keyframes.values()
        ),
        "sample: keyframe sensor coverage mismatch",
    )
    for scene in tables["scene"]:
        frames = [r for r in tables["sample"] if r["scene_token"] == scene["token"]]
        _check(
            type(scene.get("nbr_samples")) is int
            and len(frames) == scene["nbr_samples"]
            and bool(frames),
            "scene: sample count mismatch",
        )
        first, last = (
            indexes["sample"][scene["first_sample_token"]],
            indexes["sample"][scene["last_sample_token"]],
        )
        _check(
            first["scene_token"] == last["scene_token"] == scene["token"]
            and not first["prev"]
            and not last["next"],
            "scene: invalid endpoints",
        )
    for instance in tables["instance"]:
        annotations = [
            r
            for r in tables["sample_annotation"]
            if r["instance_token"] == instance["token"]
        ]
        _check(
            type(instance.get("nbr_annotations")) is int
            and len(annotations) == instance["nbr_annotations"]
            and bool(annotations),
            "instance: annotation count mismatch",
        )
        first, last = (
            indexes["sample_annotation"][instance["first_annotation_token"]],
            indexes["sample_annotation"][instance["last_annotation_token"]],
        )
        _check(
            first["instance_token"] == last["instance_token"] == instance["token"]
            and not first["prev"]
            and not last["next"],
            "instance: invalid endpoints",
        )
    for owner_table, child_table, owner_field, first_field in (
        ("scene", "sample", "scene_token", "first_sample_token"),
        ("instance", "sample_annotation", "instance_token", "first_annotation_token"),
    ):
        for owner in tables[owner_table]:
            expected = {
                row["token"]
                for row in tables[child_table]
                if row[owner_field] == owner["token"]
            }
            visited = set()
            token = owner[first_field]
            while token:
                _check(token not in visited, f"{child_table}: chain cycle")
                visited.add(token)
                token = indexes[child_table][token]["next"]
            _check(visited == expected, f"{child_table}: disconnected chain")
    local, outside = [], []
    mapped = {token: [] for token in indexes["log"]}
    for row in tables["map"]:
        tokens = row.get("log_tokens")
        _check(
            isinstance(tokens, list)
            and all(isinstance(t, str) and bool(t) for t in tokens),
            "map: invalid log references",
        )
        _check(len(set(tokens)) == len(tokens), "map: duplicate log reference")
        for token in tokens:
            link = {"map_token": row["token"], "log_token": token}
            if token in mapped:
                local.append(link)
                mapped[token].append(row["token"])
            else:
                outside.append(link)
    _check(all(len(maps) == 1 for maps in mapped.values()), "log: map must be unique")
    return {
        "counts": {name: len(rows) for name, rows in tables.items()},
        "map_links": {
            "total": len(local) + len(outside),
            "local": local,
            "outside": outside,
        },
        "core_dangling_relations": 0,
        "duplicate_tokens": 0,
    }


def inventory(root: Path, version: str, tables: dict) -> list[dict]:
    files = {f"{version}/{table}.json": "metadata" for table in TABLES}
    for table in ("sample_data", "map"):
        for row in tables[table]:
            relative = row.get("filename")
            _check(
                isinstance(relative, str) and bool(relative),
                f"{table}: missing filename",
            )
            _check(
                not Path(relative).is_absolute() and ".." not in Path(relative).parts,
                f"{table}: path escape",
            )
            _check(
                relative not in files or files[relative] == table,
                f"{table}: conflicting file roles",
            )
            files[relative] = table
    result = []
    for relative, kind in sorted(files.items()):
        path = root / relative
        _check(
            path.resolve().is_relative_to(root) and path.is_file(),
            f"{kind}: missing or escaped file",
        )
        before = path.stat()
        checksum = digest(path)
        after = path.stat()
        _check(
            (before.st_size, before.st_mtime_ns, before.st_ino)
            == (after.st_size, after.st_mtime_ns, after.st_ino),
            f"{kind}: input changed during hashing",
        )
        result.append(
            {"path": relative, "kind": kind, "bytes": after.st_size, "sha256": checksum}
        )
    return result


def verify_snapshot(directory: Path) -> dict:
    try:
        complete = json.loads((directory / "complete.json").read_text())
        _check(
            complete["processing_version"] == PROCESSING_VERSION,
            "snapshot: processing version mismatch",
        )
        _check(
            isinstance(complete["artifacts"], dict),
            "snapshot: invalid artifact mapping",
        )
        expected_artifacts = {
            f"{directory}/{table}.{extension}"
            for directory, extension in (("raw", "json"), ("records", "jsonl"))
            for table in TABLES
        } | {"file-manifest.json", "quality.json"}
        _check(
            set(complete["artifacts"]) == expected_artifacts,
            "snapshot: incomplete artifact manifest",
        )
        for relative, checksum in complete["artifacts"].items():
            path = directory / relative
            _check(
                not Path(relative).is_absolute()
                and path.resolve().is_relative_to(directory.resolve())
                and path.is_file()
                and digest(path) == checksum,
                "snapshot: artifact corruption",
            )
        _check(bool(complete["artifacts"]), "snapshot: empty artifact manifest")
        return complete
    except (KeyError, TypeError, ValueError, OSError) as error:
        if isinstance(error, IngestionError):
            raise
        raise IngestionError("snapshot: invalid completion manifest") from None


def ingest(config: dict, repository: Path) -> dict:
    preflight(config, repository, "dataset")
    root = external_path(
        required(config, "dataset", "root"), repository, "dataset.root"
    )
    version = required(config, "dataset", "version")
    output = (
        external_path(
            required(config, "storage", "output_root"),
            repository,
            "storage.output_root",
        )
        / "ingestion"
    )
    _check(not output.is_symlink(), "output: ingestion directory is a symlink")
    output.mkdir(parents=True, exist_ok=True)
    _check(not (output / ".lock").is_symlink(), "output: lock is a symlink")
    with (output / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        tables = read_tables(root, version)
        quality = validate(tables)
        files = inventory(root, version, tables)
        # Verify in-memory records against the exact raw bytes fingerprinted above.
        for table in TABLES:
            relative = f"{version}/{table}.json"
            _check(
                json.loads((root / relative).read_text()) == tables[table],
                "metadata changed during validation",
            )
        source_checksums = {item["path"]: item["sha256"] for item in files}
        snapshot_id = manifest_revision(source_checksums).removeprefix("sha256:")
        namespace = canonical(["nuscenes", version, snapshot_id]).decode()
        snapshots = output / "snapshots"
        _check(not snapshots.is_symlink(), "output: snapshots directory is a symlink")
        snapshots.mkdir(exist_ok=True)
        target = snapshots / f"{snapshot_id}-{PROCESSING_VERSION}"
        _check(not target.is_symlink(), "output: snapshot is a symlink")
        reused = target.exists()
        if not reused:
            run_id = uuid.uuid4().hex
            ingested_at = datetime.now(UTC).isoformat()
            staging = snapshots / f".staging-{run_id}"
            staging.mkdir()
            (staging / "raw").mkdir()
            (staging / "records").mkdir()
            for table in TABLES:
                relative = f"{version}/{table}.json"
                destination = staging / "raw" / f"{table}.json"
                shutil.copyfile(root / relative, destination)
                _check(
                    digest(destination) == source_checksums[relative],
                    "metadata changed during copy",
                )
                with (staging / "records" / f"{table}.jsonl").open("wb") as stream:
                    for row in tables[table]:
                        stream.write(
                            canonical(
                                {
                                    "entity_id": entity_id(
                                        namespace, table, row["token"]
                                    ),
                                    "dataset_namespace": namespace,
                                    "entity_type": table,
                                    "source_token": row["token"],
                                    "source_path": relative,
                                    "source_checksum": source_checksums[relative],
                                    "run_id": run_id,
                                    "ingested_at": ingested_at,
                                    "timestamp_us": row.get("timestamp"),
                                    "schema_version": PROCESSING_VERSION,
                                    "quality_status": "valid",
                                    "raw_record": row,
                                }
                            )
                            + b"\n"
                        )
            write_json(staging / "file-manifest.json", files)
            write_json(staging / "quality.json", quality)
            complete = {
                "snapshot_id": snapshot_id,
                "dataset_version": version,
                "namespace": namespace,
                "processing_version": PROCESSING_VERSION,
                "run_id": run_id,
                "created_at": datetime.now(UTC).isoformat(),
                "artifacts": {
                    str(p.relative_to(staging)): digest(p)
                    for p in sorted(staging.rglob("*"))
                    if p.is_file()
                },
            }
            write_json(staging / "complete.json", complete)
            verify_snapshot(staging)
            # Persist all files and directory entries before advancing the watermark.
            for path in staging.rglob("*"):
                if path.is_file():
                    with path.open("rb") as stream:
                        os.fsync(stream.fileno())
            for path in [staging / "raw", staging / "records", staging]:
                fd = os.open(path, os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            staging.rename(target)
            fd = os.open(snapshots, os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        complete = verify_snapshot(target)
        _check(
            complete["snapshot_id"] == snapshot_id
            and complete["namespace"] == namespace,
            "snapshot: identity mismatch",
        )
        # No published watermark exists here: ingestion cannot publish online data.
        atomic_json(
            output / "watermark.json",
            {
                "snapshot_id": snapshot_id,
                "processing_version": PROCESSING_VERSION,
                "run_id": complete["run_id"],
                "completion_sha256": digest(target / "complete.json"),
            },
        )
        return {
            "status": "passed",
            "scope": "local_ods_ingestion_only",
            "snapshot_id": snapshot_id,
            "run_id": complete["run_id"],
            "reused": reused,
            "counts": quality["counts"],
            "files": len(files),
            "map_links": {
                key: len(value) if isinstance(value, list) else value
                for key, value in quality["map_links"].items()
            },
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = ingest(load_config(args.config), Path(__file__).resolve().parents[3])
    except (ConfigError, IngestionError, OSError, ValueError) as error:
        message = (
            str(error)
            if isinstance(error, (ConfigError, IngestionError))
            else "ingestion: I/O or JSON failure"
        )
        print(json.dumps({"status": "blocked", "error": message}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
