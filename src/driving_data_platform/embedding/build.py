"""单图串行加工、不可变内容缓存与完整提交。"""

import hashlib
import signal
import time
import uuid
from pathlib import Path

from driving_data_platform.common.config import (
    application_config,
)
from driving_data_platform.common.errors import PlatformError, describe
from driving_data_platform.common.run_query import identity
from driving_data_platform.embedding import artifacts as a
from driving_data_platform.embedding import model
from driving_data_platform.embedding.images import validate_image
from driving_data_platform.ingestion.nuscenes import atomic_json

VERSION = "embedding-real-v3"


def source(root, input_id):
    value = a.read(a.directory(root, "inputs", input_id), "input.json")
    a.validate_input(value)
    if a.checksum(value) != input_id:
        raise ValueError("输入清单摘要不一致")
    return value


def image(dataset, entry):
    data = a.read_bytes(dataset, entry["path"], 25 * 1024**2)
    if (
        len(data) != entry["bytes"]
        or hashlib.sha256(data).hexdigest() != entry["sha256"]
    ):
        raise ValueError("图片大小或摘要不符合输入清单")
    validate_image(data)
    return data


def show(root, run_id):
    identity(run_id, r"[a-f0-9]{32}")
    return a.read(root, "embedding/build-runs/" + run_id + ".json")


def execute(root, input_id, space_id):
    input_value = source(root, input_id)
    contract = model.space(root, space_id)
    from driving_data_platform.embedding.runtime import selection

    selected = selection(root, space_id)
    if selected.get("space_id") != space_id or selected.get("status") != "passed":
        raise ValueError("缺少已通过评测的选中空间记录")
    evaluation = a.read(root, selected["report"])
    if (
        a.checksum(evaluation) != selected["report_sha256"]
        or evaluation.get("status") != "passed"
        or evaluation.get("selected_space_id") != space_id
    ):
        raise ValueError("选参报告缺失或摘要冲突")
    from driving_data_platform.embedding.runtime import encoder

    client = encoder(root, space_id)
    dataset = Path(application_config(root)["dataset"]["root"])
    run_id = uuid.uuid4().hex
    path = a.safe_parent(root, "build-runs") / (run_id + ".json")
    code = a.checksum(
        {
            name: model.digest(Path(__file__).with_name(name))
            for name in (
                "build.py",
                "artifacts.py",
                "providers.py",
                "images.py",
                "model.py",
                "external.py",
                "runtime.py",
                "transport.py",
            )
        }
    )
    completed = {}
    for candidate in (root / "embedding/artifacts").glob("*/complete.json"):
        value = a.read(candidate.parent, candidate.name)
        if (
            value.get("input_id"),
            value.get("space_id"),
            value.get("processing_version"),
            value.get("code_sha256"),
        ) == (input_id, space_id, VERSION, code):
            if completed:
                raise ValueError("同一加工身份存在多个完成产物，需核对后再复用")
            _, _, _, rows = a.verify_artifact(root, candidate.parent.name)
            completed = {row["media_id"]: row for row in rows}
    report = {
        "task_identity": a.checksum(
            {
                "input_id": input_id,
                "space_id": space_id,
                "processing_version": VERSION,
                "code_sha256": code,
            }
        ),
        "run_id": run_id,
        "input_id": input_id,
        "space_id": space_id,
        "processing_version": VERSION,
        "code_sha256": code,
        "status": "running",
        "total": input_value["count"],
        "success": 0,
        "reused": 0,
        "failed": 0,
        "blocked": 0,
        "items": [],
    }
    started = time.monotonic()
    last_write = started
    vectors = []
    blocked = None
    consecutive = 0
    previous = signal.getsignal(signal.SIGTERM)

    def interrupt(signum, frame):
        raise KeyboardInterrupt("收到 SIGTERM，已落盘结果可显式重跑复用")

    def save():
        report["seconds"] = time.monotonic() - started
        atomic_json(path, report)

    signal.signal(signal.SIGTERM, interrupt)
    save()
    try:
        for index, entry in enumerate(input_value["entries"]):
            report["current_media_id"] = entry["media_id"]
            item = {"media_id": entry["media_id"]}
            try:
                data = image(dataset, entry)
                cache_id = a.checksum({"sha256": entry["sha256"], "space_id": space_id})
                cache_dir = a.safe_parent(root, "cache") / cache_id
                if cache_dir.exists() or cache_dir.is_symlink():
                    cached = a.read(cache_dir, "result.json")
                    if (
                        set(cached) != {"space_id", "sha256", "vector", "result_sha256"}
                        or cached["result_sha256"]
                        != a.checksum(
                            {k: v for k, v in cached.items() if k != "result_sha256"}
                        )
                        or cached["space_id"] != space_id
                        or cached["sha256"] != entry["sha256"]
                    ):
                        raise ValueError("缓存摘要或身份冲突，保留原缓存且不复用")
                    vector = a.vector32(cached["vector"], contract["dimension"])
                    status = "reused"
                elif entry["media_id"] in completed:
                    vector = completed[entry["media_id"]]["vector"]
                    status = "reused"
                elif blocked:
                    item["error"] = blocked
                    status = "blocked"
                    vector = None
                else:
                    vector = client.encode_image(data)
                    cached = {
                        "space_id": space_id,
                        "sha256": entry["sha256"],
                        "vector": vector,
                    }
                    cached["result_sha256"] = a.checksum(cached)
                    a.commit(
                        root, "cache", cache_id, {"result.json": a.json_bytes(cached)}
                    )
                    consecutive = 0
                    status = "success"
                if vector is not None:
                    vectors.append(
                        {
                            "media_id": entry["media_id"],
                            "sha256": entry["sha256"],
                            "vector": vector,
                        }
                    )
                item["status"] = status
                report[status] += 1
            except (OSError, ValueError) as error:
                item.update(status="failed", error=describe(error, "embedding-build"))
                report["failed"] += 1
                if isinstance(error, PlatformError) and error.category in (
                    "CONTRACT_CONFLICT",
                    "RESOURCE_BLOCKED",
                ):
                    blocked = item["error"]
                elif (
                    isinstance(error, PlatformError)
                    and error.category == "DEPENDENCY_UNAVAILABLE"
                ):
                    consecutive += 1
                    if consecutive >= 3:
                        try:
                            client.probe()
                            client.check_identity()
                        except (ValueError, OSError) as health_error:
                            blocked = describe(health_error, "embedding-health")
                        else:
                            consecutive = 0
                else:
                    consecutive = 0
            report["items"].append(item)
            if (index + 1) % 16 == 0 or time.monotonic() - last_write >= 5:
                save()
                last_write = time.monotonic()
        if report["failed"] or report["blocked"]:
            report["status"] = "failed"
        else:
            if not completed:
                client.check_identity()
            # 提交前再次核对完整文件，避免加工期间媒体发生变化。
            for entry in input_value["entries"]:
                image(dataset, entry)
            report["artifact"] = a.create_artifact(
                root,
                input_value,
                contract,
                vectors,
                processing_version=VERSION,
                code_sha256=code,
            )
            a.verify_artifact(root, report["artifact"]["artifact_id"])
            report["status"] = "passed"
        return report
    except BaseException as error:
        report["status"] = (
            "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        )
        report["error"] = describe(error, "embedding-build")
        raise
    finally:
        save()
        signal.signal(signal.SIGTERM, previous)
