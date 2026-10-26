"""Hive Metastore 候选准备与真实 Parquet 消费；不自动发布版本。"""

import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path

from driving_data_platform.common.identity import canonical
from driving_data_platform.common.preflight import digest
from driving_data_platform.ingestion.nuscenes import atomic_json, verify_snapshot
from driving_data_platform.publishing.store import load
from driving_data_platform.warehouse.build import reconcile, verify


def command(root: Path, repo: Path):
    return [
        "docker",
        "compose",
        "--env-file",
        str(root / "compose.env"),
        "--env-file",
        str(root / "metastore/runtime.env"),
        "-f",
        str(repo / "infra/docker/compose.yaml"),
        "-f",
        str(repo / "infra/docker/compose.metastore.yaml"),
    ]


def check_image(reference):
    info = json.loads(
        subprocess.check_output(["docker", "image", "inspect", reference])
    )[0]
    if info["Architecture"] != "arm64":
        raise ValueError("Metastore 候选仅验证原生 arm64 镜像")
    return info


def job(root: Path, repo: Path, trial: Path, mode: str, *, fail_register=False):
    lock = json.loads((repo / "infra/metastore/runtime.lock.json").read_text())
    fields = dict(
        line.split("=", 1)
        for line in (root / "metastore/runtime.env").read_text().splitlines()
    )
    if (
        fields.get("DDP_HMS_IMAGE") != lock["hive"]["image"]
        or fields.get("DDP_HMS_SPARK_IMAGE") != lock["spark_client"]["image"]
        or digest(root / "metastore/dependencies/postgresql.jar")
        != lock["jdbc"]["sha256"]
    ):
        raise ValueError("候选配置或 JDBC 与锁文件不一致")
    for name in ("hive", "spark_client"):
        check_image(lock[name]["image"])
    log = trial / (mode + "-" + uuid.uuid4().hex + ".log")
    args = [
        *command(root, repo),
        "run",
        "--rm",
        "--no-deps",
        "spark-catalog",
        "/opt/spark/bin/spark-submit",
        "--master",
        "local[2]",
        "--driver-memory",
        "1g",
        "/app/src/driving_data_platform/warehouse/catalog_job.py",
        exchange_path(root, trial) + "/request.json",
        mode,
    ]
    if fail_register:
        args.append("--fail-register")
    with log.open("w") as stream:
        result = subprocess.run(
            args, stdout=stream, stderr=subprocess.STDOUT, check=False
        )
    if result.returncode:
        raise ValueError("Metastore " + mode + " 失败，查看 " + str(log))
    return log


def new_trial(root: Path, source: Path):
    manifest = verify(source)
    if source.parent.resolve() != (root / "warehouse").resolve():
        raise ValueError("候选仅消费正式数仓目录内已完成的版本")
    ods = root / "ingestion/snapshots" / (manifest["snapshot_id"] + "-nuscenes-ods-v1")
    verify_snapshot(ods)
    if digest(ods / "complete.json") != manifest["input_sha256"]:
        raise ValueError("数仓输入与 ODS 身份不一致")
    trial = root / "metastore/trials" / uuid.uuid4().hex
    trial.mkdir()
    request = {
        "database": "ddp_hms_" + trial.name,
        "source": "/warehouse/" + source.name,
        "source_sha256": digest(source / "complete.json"),
        "tables": manifest["tables"],
        "input_manifest": manifest,
        "uri": "thrift://metastore:9083",
    }
    atomic_json(trial / "request.json", request)
    return trial


def register(root: Path, repo: Path, trial: Path, *, fail=False):
    request = json.loads((trial / "request.json").read_text())
    source = root / "warehouse" / Path(request["source"]).name
    verify(source)
    if digest(source / "complete.json") != request["source_sha256"]:
        raise ValueError("登记输入版本已改变")
    job(root, repo, trial, "register", fail_register=fail)
    receipt = json.loads((trial / "registered.json").read_text())
    if receipt["source_sha256"] != request["source_sha256"]:
        raise ValueError("登记回执身份不一致")
    return receipt


def consume(root: Path, repo: Path, trial: Path, *, load_fail=False, ack_fail=False):
    request = json.loads((trial / "request.json").read_text())
    source = root / "warehouse" / Path(request["source"]).name
    verify(source)
    receipt = json.loads((trial / "registered.json").read_text())
    if (
        receipt["source_sha256"] != digest(source / "complete.json")
        or receipt["database"] != request["database"]
    ):
        raise ValueError("元数据登记未完成或版本身份不同")
    output = trial / "consumed"
    if output.exists():
        job(root, repo, trial, "check")
        verify(output)
        return load(root, output, fail=load_fail, fail_after_commit=ack_fail)
    staging = trial / ("attempt-" + uuid.uuid4().hex)
    request["output"] = exchange_path(root, trial) + "/" + staging.name
    atomic_json(trial / "request.json", request)
    job(root, repo, trial, "consume")
    output = staging
    manifest = request["input_manifest"]
    ods = root / "ingestion/snapshots" / (manifest["snapshot_id"] + "-nuscenes-ods-v1")
    counts = reconcile(ods, output)
    result = manifest | {
        "processing_version": manifest["processing_version"]
        if request.get("production")
        else "hms-" + trial.name,
        "catalog_contract": request.get("contract"),
        "tables": counts,
        "catalog": receipt,
        "consumer_code_sha256": digest(
            repo / "src/driving_data_platform/warehouse/catalog_job.py"
        ),
        "artifacts": {
            str(p.relative_to(output)): digest(p)
            for p in output.rglob("*")
            if p.is_file()
        },
    }
    atomic_json(output / "complete.json", result)
    verify(output)
    for path in output.rglob("*"):
        if path.is_file():
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
    for directory in sorted(
        [p for p in output.rglob("*") if p.is_dir()],
        key=lambda p: len(p.parts),
        reverse=True,
    ) + [output]:
        fd = os.open(directory, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    output.rename(trial / "consumed")
    fd = os.open(trial, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return load(root, trial / "consumed", fail=load_fail, fail_after_commit=ack_fail)


def exchange_path(root: Path, directory: Path) -> str:
    relative = directory.resolve().relative_to((root / "metastore").resolve())
    if relative.parts[0] not in {"trials", "generations"} or len(relative.parts) != 2:
        raise ValueError("非法目录消费路径")
    return "/exchange/" + relative.as_posix()


def production_entry(root: Path, repo: Path, source: Path) -> Path:
    """同一生产产物复用目录身份；代码或运行环境变化必须使用新处理版本。"""
    manifest = verify(source)
    if source.parent.resolve() != (root / "warehouse").resolve():
        raise ValueError("生产消费必须引用正式数仓目录")
    checksum = digest(source / "complete.json")
    runtime = json.loads((repo / "infra/metastore/runtime.lock.json").read_text())
    contract = {
        "version": "parquet-hms-v1",
        "source_sha256": checksum,
        "code": {
            name: digest(repo / "src/driving_data_platform" / name)
            for name in (
                "warehouse/catalog.py",
                "warehouse/catalog_job.py",
                "warehouse/catalog_contract.py",
            )
        },
        "runtime": {name: runtime[name]["image"] for name in ("hive", "spark_client")},
        "jdbc": runtime["jdbc"]["sha256"],
    }
    identity = hashlib.sha256(canonical(["parquet-hms-v1", checksum])).hexdigest()[:32]
    entry = root / "metastore/generations" / identity
    entry.mkdir(parents=True, exist_ok=True)
    request_file = entry / "request.json"
    if request_file.exists():
        request = json.loads(request_file.read_text())
        if (
            request.get("contract") != contract
            or request.get("source") != "/warehouse/" + source.name
            or not request.get("production")
        ):
            raise ValueError("同一处理版本的消费代码或环境已变化，请使用新的 --version")
    else:
        if any(entry.iterdir()):
            raise ValueError("目录身份不完整，拒绝覆盖已有产物")
        atomic_json(
            request_file,
            {
                "database": "ddp_hms_" + identity,
                "source": "/warehouse/" + source.name,
                "source_sha256": checksum,
                "tables": manifest["tables"],
                "input_manifest": manifest,
                "uri": "thrift://metastore:9083",
                "production": True,
                "contract": contract,
            },
        )
    return entry


def materialize(
    root: Path, repo: Path, source: Path, *, load_fail=False, ack_fail=False
) -> dict:
    entry = production_entry(root, repo, source)
    register(root, repo, entry)
    result = consume(root, repo, entry, load_fail=load_fail, ack_fail=ack_fail)
    return result | {"catalog_id": entry.name, "catalog_directory": str(entry)}
