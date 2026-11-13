"""图片输入及不可变向量产物；文件仅在登记目录内定位。"""

import hashlib
import json
import math
import os
import stat
import struct
import uuid
from pathlib import Path

from driving_data_platform.common.config import port
from driving_data_platform.common.identity import canonical
from driving_data_platform.common.run_query import identity
from driving_data_platform.ingestion.nuscenes import atomic_json

MAX_BYTES = 256 * 1024**2
SPACE_FIELDS = {
    "schema_version",
    "model",
    "revision",
    "model_sha256",
    "projector_sha256",
    "runtime",
    "runtime_version",
    "quantization",
    "image_preprocessing",
    "text_instruction",
    "image_instruction",
    "pooling",
    "dimension",
    "dtype",
    "normalization",
    "distance",
    "test_only",
}


def checksum(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def read_bytes(root, relative, maximum=MAX_BYTES):
    """逐级 NOFOLLOW 打开；拒绝路径替换、链接、特殊文件和超限文件。"""
    root, relative = Path(root), Path(relative)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("向量证据路径越界")
    if any(p.is_symlink() for p in [root, *root.parents]):
        raise ValueError("向量产物根包含符号链接")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = child
        file = os.open(
            relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
        )
        with os.fdopen(file, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
                raise ValueError("向量证据类型非法或文件过大")
            data = stream.read(maximum + 1)
            if len(data) > maximum:
                raise ValueError("向量证据文件过大")
            return data
    finally:
        os.close(fd)


def read(root, relative):
    try:
        value = json.loads(read_bytes(root, relative))
        if not isinstance(value, dict):
            raise TypeError("向量 JSON 必须为对象")
        return value
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("向量 JSON 证据损坏") from None


def directory(root, collection, item_id):
    identity(item_id, r"[a-f0-9]{64}")
    if collection not in ("inputs", "artifacts"):
        raise ValueError("未知向量产物目录")
    return Path(root) / "embedding" / collection / item_id


def safe_parent(root, collection):
    root = Path(root)
    if any(p.is_symlink() for p in [root, *root.parents]):
        raise ValueError("产物根包含符号链接")
    for part in ("embedding", collection):
        root = root / part
        if root.is_symlink():
            raise ValueError("产物目录包含符号链接")
        root.mkdir(exist_ok=True)
    return root


def commit(root, collection, item_id, files):
    """同身份只能保存相同字节，不覆盖已有目录；提交前同步文件。"""
    identity(item_id, r"[a-f0-9]{64}")
    parent = safe_parent(root, collection)
    target = parent / item_id
    if target.exists() or target.is_symlink():
        if target.is_symlink() or {p.name for p in target.iterdir()} != set(files):
            raise ValueError("不可变产物目录冲突")
        for name, data in files.items():
            if read_bytes(target, name) != data:
                raise ValueError("不可变产物内容冲突")
        return True
    staging = parent / (".staging-" + uuid.uuid4().hex)
    staging.mkdir()
    for name, data in files.items():
        with (staging / name).open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    for folder in (staging,):
        fd = os.open(folder, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    staging.rename(target)
    fd = os.open(parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return False


def json_bytes(value):
    return canonical(value) + b"\n"


def media_input(conn, generation):
    identity(generation, r"g_[a-f0-9]{64}")
    row = conn.execute(
        "SELECT manifest_sha256, manifest->'capabilities'->>'original_media' FROM control.generation WHERE generation=%s AND status='READY'",
        (generation,),
    ).fetchone()
    if row is None:
        raise ValueError("指定业务 generation 不存在或未就绪")
    if row[1] != "true":
        raise ValueError("指定业务 generation 没有已校验的原始媒体")
    entries = []
    for media_id, frame_id, channel, payload in conn.execute(
        "SELECT entity_id,frame_id,channel,payload FROM serving.media WHERE generation=%s AND modality='camera' AND is_key_frame ORDER BY entity_id",
        (generation,),
    ):
        entries.append(
            {
                "media_id": media_id,
                "frame_id": frame_id,
                "channel": channel,
                "path": payload["path"],
                "bytes": payload["bytes"],
                "sha256": payload["sha256"],
            }
        )
    if not entries:
        raise ValueError("指定版本没有关键帧相机图片")
    value = {
        "schema_version": 1,
        "generation": generation,
        "generation_manifest_sha256": row[0],
        "selection": "keyframe_camera",
        "count": len(entries),
        "entries": entries,
    }
    validate_input(value)
    return value


def validate_input(value):
    if (
        type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or value.get("selection") != "keyframe_camera"
    ):
        raise ValueError("不支持的图片输入契约")
    identity(value.get("generation"), r"g_[a-f0-9]{64}")
    identity(value.get("generation_manifest_sha256"), r"[a-f0-9]{64}")
    entries = value.get("entries")
    if (
        not isinstance(entries, list)
        or not entries
        or type(value.get("count")) is not int
        or value.get("count") != len(entries)
    ):
        raise ValueError("图片清单数量不一致或为空")
    ids = []
    for entry in entries:
        if set(entry) != {"media_id", "frame_id", "channel", "path", "bytes", "sha256"}:
            raise ValueError("图片输入字段不支持")
        for key in ("media_id", "frame_id", "sha256"):
            identity(entry[key], r"[a-f0-9]{64}")
        identity(entry["channel"], r"[A-Z0-9_]{1,64}")
        path = Path(entry["path"])
        if (
            path.is_absolute()
            or ".." in path.parts
            or path.suffix.lower() not in (".jpg", ".jpeg")
        ):
            raise ValueError("非法图片相对路径")
        if type(entry["bytes"]) is not int or not 0 < entry["bytes"] <= 25 * 1024**2:
            raise ValueError("图片大小不合法")
        ids.append(entry["media_id"])
    if ids != sorted(set(ids)):
        raise ValueError("图片 ID 重复或未排序")


def capture(root, conn, generation, *, verify_files=False, dataset_root=None):
    value = media_input(conn, generation)
    item_id = checksum(value)
    reused = commit(root, "inputs", item_id, {"input.json": json_bytes(value)})
    result = {
        "status": "passed",
        "input_id": item_id,
        "count": value["count"],
        "reused": reused,
        "file_verification": "not_executed",
    }
    if verify_files:
        from driving_data_platform.api.app import image_bytes

        if dataset_root is None:
            raise ValueError("文件核验必须配置图片根目录")
        report = (
            Path(root) / "reports" / ("embedding-files-" + uuid.uuid4().hex + ".json")
        )
        details = {
            "status": "running",
            "input_id": item_id,
            "verified": 0,
            "scope": "真实图片大小、格式与摘要核验",
        }
        try:
            for entry in value["entries"]:
                image_bytes(Path(dataset_root), entry)
                details["verified"] += 1
            details["status"] = "passed"
            result["file_verification"] = "passed"
        except (OSError, ValueError):
            details["status"] = "failed"
            details["error"] = "图片缺失、路径、类型或摘要校验失败"
            raise
        finally:
            report.parent.mkdir(exist_ok=True)
            atomic_json(report, details)
    return result


SPACE_V2_FIELDS = {
    "binary_sha256",
    "template_sha256",
    "implementation_sha256",
    "runtime_parameters",
}


def validate_space(value):
    if isinstance(value, dict) and value.get("schema_version") == 3:
        from driving_data_platform.embedding.external import validate_contract

        validate_contract(value)
        canonical(value)
        return checksum(value)
    if (
        set(value)
        != (
            SPACE_FIELDS | SPACE_V2_FIELDS
            if value.get("schema_version") == 2
            else SPACE_FIELDS
        )
        or value["schema_version"] not in (1, 2)
        or type(value["test_only"]) is not bool
    ):
        raise ValueError("空间契约字段不完整或不支持")
    if type(value["dimension"]) is not int or not 1 <= value["dimension"] <= 16000:
        raise ValueError("向量维度尚未确认或不支持")
    for key in ("model_sha256", "projector_sha256"):
        identity(value[key], r"[a-f0-9]{64}")
    for key in (
        "model",
        "revision",
        "runtime",
        "runtime_version",
        "quantization",
        "pooling",
    ):
        if not isinstance(value[key], str) or not value[key].strip():
            raise ValueError("空间身份尚未确认")
    for key in ("text_instruction", "image_instruction"):
        if not isinstance(value[key], str):
            raise TypeError("任务指令必须是已确认字符串")
    if (
        not isinstance(value["image_preprocessing"], dict)
        or not value["image_preprocessing"]
    ):
        raise ValueError("图片预处理尚未确认")
    if (value["dtype"], value["normalization"], value["distance"]) != (
        "float32",
        "l2",
        "cosine",
    ):
        raise ValueError("首版仅支持 float32/L2 归一化/余弦距离")
    if value["schema_version"] == 2:
        for key in ("binary_sha256", "template_sha256", "implementation_sha256"):
            identity(value[key], r"[a-f0-9]{64}")
        from driving_data_platform.embedding.images import RULES

        if value["image_preprocessing"] != RULES or value["runtime_parameters"] not in [
            {
                "context": 4096,
                "batch": 1024,
                "ubatch": 1024,
                "parallel": 1,
                "image_min_tokens": 64,
                "image_max_tokens": limit,
            }
            for limit in (256, 512, 1024)
        ]:
            raise ValueError("空间运行参数或图片策略不支持")
        if any(
            value[k] != "Represent the user's input."
            for k in ("text_instruction", "image_instruction")
        ):
            raise ValueError("空间任务指令不匹配")
    canonical(value)
    return checksum(value)


def vector32(values, dimension):
    if not isinstance(values, list) or len(values) != dimension:
        raise ValueError("向量维度不匹配")
    try:
        if any(type(x) not in (int, float) or not math.isfinite(x) for x in values):
            raise ValueError()
        values = [struct.unpack("f", struct.pack("f", x))[0] for x in values]
        norm = math.sqrt(math.fsum(x * x for x in values))
        if not all(math.isfinite(x) for x in values) or abs(norm - 1) > 1e-4:
            raise ValueError()
    except (ValueError, OverflowError, struct.error):
        raise ValueError("向量必须有限、非零并已 L2 归一化") from None
    return values


def create_artifact(
    root,
    input_value,
    space,
    vectors,
    *,
    processing_version="embedding-v1",
    code_sha256=None,
):
    """接受真实推理输出；测试调用必须使用 test_only 空间，绝不生成伪推理。"""
    validate_input(input_value)
    space_id = validate_space(space)
    identity(processing_version, r"[a-zA-Z0-9_-]{1,64}")
    if code_sha256 is not None:
        identity(code_sha256, r"[a-f0-9]{64}")
    expected = {e["media_id"]: e for e in input_value["entries"]}
    rows = []
    for row in vectors:
        if (
            set(row) != {"media_id", "sha256", "vector"}
            or row["media_id"] not in expected
            or row["sha256"] != expected[row["media_id"]]["sha256"]
        ):
            raise ValueError("向量与图片身份不匹配")
        rows.append(row | {"vector": vector32(row["vector"], space["dimension"])})
    rows.sort(key=lambda r: r["media_id"])
    if [r["media_id"] for r in rows] != sorted(expected):
        raise ValueError("向量集合缺失、多余或重复")
    files = {
        "input.json": json_bytes(input_value),
        "space.json": json_bytes(space),
        "vectors.jsonl": b"".join(json_bytes(r) for r in rows),
    }
    complete = {
        "schema_version": 1,
        "input_id": checksum(input_value),
        "space_id": space_id,
        "generation": input_value["generation"],
        "count": len(rows),
        "test_only": space["test_only"],
        "processing_version": processing_version,
        "code_sha256": code_sha256
        or hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "artifacts": {k: hashlib.sha256(v).hexdigest() for k, v in files.items()},
    }
    item_id = checksum(complete)
    files["complete.json"] = json_bytes(complete)
    reused = commit(root, "artifacts", item_id, files)
    return {"artifact_id": item_id, "reused": reused}


def _verify_artifact(root, item_id):
    target = directory(root, "artifacts", item_id)
    complete = read(target, "complete.json")
    if checksum(complete) != item_id or complete.get("schema_version") != 1:
        raise ValueError("向量完成清单身份不匹配")
    if set(complete.get("artifacts", {})) != {
        "input.json",
        "space.json",
        "vectors.jsonl",
    }:
        raise ValueError("向量产物文件集合不支持")
    if {p.name for p in target.iterdir()} != {"complete.json", *complete["artifacts"]}:
        raise ValueError("向量产物文件缺失或包含多余文件")
    files = {k: read_bytes(target, k) for k in complete["artifacts"]}
    if any(
        hashlib.sha256(v).hexdigest() != complete["artifacts"][k]
        for k, v in files.items()
    ):
        raise ValueError("向量产物文件摘要不匹配")
    source = json.loads(files["input.json"])
    space = json.loads(files["space.json"])
    validate_input(source)
    if (
        validate_space(space) != complete["space_id"]
        or checksum(source) != complete["input_id"]
        or source["generation"] != complete["generation"]
        or space["test_only"] != complete["test_only"]
    ):
        raise ValueError("向量产物契约身份冲突")
    rows = [json.loads(line) for line in files["vectors.jsonl"].splitlines()]
    entries = {e["media_id"]: e for e in source["entries"]}
    if len(rows) != complete["count"] or [r["media_id"] for r in rows] != sorted(
        entries
    ):
        raise ValueError("向量覆盖范围不完整")
    for row in rows:
        if (
            row["sha256"] != entries[row["media_id"]]["sha256"]
            or vector32(row["vector"], space["dimension"]) != row["vector"]
        ):
            raise ValueError("向量内容、图片摘要或精度不一致")
    return complete, source, space, rows


def trial_config(root, trial):
    identity(trial)
    state = read(root, "embedding/trials/" + trial + "/connection.json")
    if (
        state.get("trial") != trial
        or state.get("container") != "ddp-embedding-" + trial
        or state.get("database") != "ddp_embedding_trial_" + trial
        or state.get("host") != "127.0.0.1"
        or state.get("port") != port("trial")
        or state.get("user") != "ddp"
    ):
        raise ValueError("隔离连接登记身份冲突")
    identity(state.get("image"), r"sha256:[a-f0-9]{64}")
    if (
        not isinstance(state.get("password"), str)
        or not state["password"]
        or len(state["password"]) > 128
    ):
        raise ValueError("隔离数据库凭据登记无效")
    return state


def verify_artifact(root, item_id):
    try:
        return _verify_artifact(root, item_id)
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError):
        raise ValueError("向量产物结构不完整或格式不支持") from None
