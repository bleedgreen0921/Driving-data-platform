"""真实 Spark 加工、独立对账与不可变数仓提交。"""

import json
import os
import re
import subprocess
import uuid
from pathlib import Path

from driving_data_platform.common.preflight import digest
from driving_data_platform.ingestion.nuscenes import atomic_json, verify_snapshot
from driving_data_platform.warehouse.baseline import baseline


def rows(directory: Path, name: str) -> list[dict]:
    return [
        json.loads(line)
        for p in sorted((directory / name / "json").glob("part-*"))
        for line in p.read_text().splitlines()
    ]


def verify(directory: Path) -> dict:
    complete = json.loads((directory / "complete.json").read_text())
    for relative, checksum in complete["artifacts"].items():
        path = directory / relative
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(directory.resolve())
            or digest(path) != checksum
        ):
            raise ValueError("数仓产物缺失、越界或摘要不一致")
    actual = {
        str(p.relative_to(directory))
        for p in directory.rglob("*")
        if p.is_file() and p.name != "complete.json"
    }
    if actual != set(complete["artifacts"]):
        raise ValueError("数仓清单不完整")
    return complete


def reconcile(source: Path, directory: Path):
    expected = baseline(source)
    counts = json.loads((directory / "schema-counts.json").read_text())
    fields = (
        "entity_id",
        "frame_id",
        "category",
        "channel",
        "dimension",
        "value",
        "location",
    )

    def key(row):
        return tuple(str(row.get(field, "")) for field in fields)

    for name, values in expected.items():
        actual = rows(directory, name)
        if sorted(actual, key=key) != sorted(values, key=key) or counts[name][
            "count"
        ] != len(values):
            raise ValueError("独立对账不一致: " + name)
    return counts


def build(root: Path, repo: Path, version="warehouse-v3", *, fail=False) -> dict:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", version):
        raise ValueError("处理版本只能包含字母、数字、下划线与短横线")
    water = json.loads((root / "ingestion/watermark.json").read_text())
    source = (
        root
        / "ingestion/snapshots"
        / (water["snapshot_id"] + "-" + water["processing_version"])
    )
    if digest(source / "complete.json") != water["completion_sha256"]:
        raise ValueError("ODS 水位摘要不一致")
    complete = verify_snapshot(source)
    code = {
        str(p.relative_to(repo)): digest(p)
        for p in [
            repo / "src/driving_data_platform/warehouse/spark_job.py",
            repo / "src/driving_data_platform/warehouse/baseline.py",
            *sorted((repo / "sql/spark").glob("*.sql")),
        ]
    }
    target = root / "warehouse" / (complete["snapshot_id"] + "-" + version)
    if target.exists():
        result = verify(target)
        if (
            result["code"] != code
            or result["input_sha256"] != water["completion_sha256"]
        ):
            raise ValueError("已存在版本的代码或输入不同，请使用新处理版本")
        reconcile(source, target)
        return {
            "status": "passed",
            "reused": True,
            "directory": str(target),
            "counts": result["tables"],
        }
    staging = root / "warehouse" / (".staging-" + uuid.uuid4().hex)
    log = root / "reports" / ("spark-" + uuid.uuid4().hex + ".log")
    command = [
        "docker",
        "compose",
        "--env-file",
        str(root / "compose.env"),
        "-f",
        str(repo / "infra/docker/compose.yaml"),
        "run",
        "--rm",
        "--no-deps",
        "--pull",
        "never",
        "spark",
        "/opt/spark/bin/spark-submit",
        "--master",
        "local[2]",
        "--driver-memory",
        "1g",
        "/app/src/driving_data_platform/warehouse/spark_job.py",
        "/ods/snapshots/" + source.name + ("-missing" if fail else ""),
        "/warehouse/" + staging.name,
    ]
    with log.open("w") as stream:
        result = subprocess.run(
            command, check=False, stdout=stream, stderr=subprocess.STDOUT
        )
    if result.returncode:
        raise ValueError("Spark 加工失败，查看 " + str(log))
    counts = reconcile(source, staging)
    manifest = {
        "snapshot_id": complete["snapshot_id"],
        "processing_version": version,
        "schema_version": "warehouse-schema-v1",
        "input_sha256": water["completion_sha256"],
        "code": code,
        "tables": counts,
        "artifacts": {
            str(p.relative_to(staging)): digest(p)
            for p in staging.rglob("*")
            if p.is_file()
        },
    }
    atomic_json(staging / "complete.json", manifest)
    verify(staging)
    # 提交前同步所有文件；完成清单也必须持久化。
    for path in staging.rglob("*"):
        if path.is_file():
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
    for path in sorted(
        [p for p in staging.rglob("*") if p.is_dir()],
        key=lambda p: len(p.parts),
        reverse=True,
    ) + [staging]:
        fd = os.open(path, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    staging.rename(target)
    fd = os.open(target.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return {
        "status": "passed",
        "reused": False,
        "directory": str(target),
        "counts": counts,
    }
