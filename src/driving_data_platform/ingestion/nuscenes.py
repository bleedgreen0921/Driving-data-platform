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
    asset_policy,
    external_path,
    load_config,
    required,
)
from driving_data_platform.common.contract import load_contract_document
from driving_data_platform.common.identity import (
    canonical,
    entity_id,
    manifest_revision,
)
from driving_data_platform.common.preflight import TABLES, digest, preflight
from driving_data_platform.core.errors import IngestionError
from driving_data_platform.core.model import (
    AssetPolicy,
    asset_summary,
    derive_capabilities,
)
from driving_data_platform.datasets.nuscenes.assets import inventory

# 本模块只生成本地 ODS：保留元数据原文、包装统一记录、登记引用文件，不复制媒体。
# 摄取完成不代表 Spark 加工、数据库装载或在线发布完成。
# 处理版本描述 ODS 格式，与配置中的数据集版本（如 v1.0-mini）分别管理。
PROCESSING_VERSION = "nuscenes-ods-v2"


def write_json(path: Path, value: object) -> None:
    # 固定键序、分隔符及编码，并拒绝非标准数值，使相同内容具有稳定字节表示。
    # 直接写入暂存文件；对外可见水位的替换使用下面的 atomic_json。
    path.write_bytes(canonical(value) + b"\n")


def atomic_json(path: Path, value: object) -> None:
    # 在目标同目录写临时文件，确保 replace 不跨文件系统，避免暴露半写入的 JSON。
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(canonical(value) + b"\n")
            stream.flush()
            # flush 清空 Python 缓冲；fsync 请求持久化文件内容，两者职责不同。
            os.fsync(stream.fileno())
        temporary.replace(path)
        # 文件替换后同步父目录，持久化目录项；文件 fsync 不能替代这一步。
        fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        # 失败时尽力清理临时文件；replace 成功后原临时路径已不存在。
        temporary.unlink(missing_ok=True)


def read_tables(root: Path, version: str) -> dict[str, list[dict]]:
    # TABLES 为固定的 13 张源表。元数据全部读入内存，适用于当前 mini 单机范围。
    # raw_record 后续保留完整原行，不依赖 nuScenes SDK 进行转换。
    return {
        table: json.loads((root / version / f"{table}.json").read_text())
        for table in TABLES
    }


def _check(condition: bool, code: str) -> None:
    # 质量门禁遇错立即停止，不跳过坏行或把未通过的数据标记为 valid。
    if not condition:
        raise IngestionError(code)


def validate(tables: dict[str, list[dict]], contract: dict) -> dict:
    # 1. 核心表非空，全部表的 token 是非空字符串且同表唯一。
    # 本函数校验已定义的关系和关键字段，并非所有原始字段的完整类型校验器。
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
        # 建立 token 索引，后续跨表引用可直接查找，避免每个外键都全表扫描。
        indexes[table] = {row["token"]: row for row in rows}
    # 2. 检查契约声明的核心外键，不允许悬空引用。
    for table, fields in contract["relations"].items():
        for row in tables[table]:
            for field, target in fields.items():
                _check(
                    isinstance(row.get(field), str) and row[field] in indexes[target],
                    f"{table}.{field}: dangling relation",
                )
    # 3. 检查链表局部一致性：端点可空，非空引用必须存在且前后互相指向。
    # sample 按 scene 分链，标注按 instance 分链，媒体另按 sensor 检查。
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
                    # 空字符串是合法链表端点；缺失字段或非字符串已在上面拒绝。
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
                    # 时间必须沿 next 严格递增，不允许相邻记录同一时间或逆序。
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
                    # 比较标定记录所指向的 sensor，而不是要求标定 token 本身相同。
                    a = indexes["calibrated_sensor"][row["calibrated_sensor_token"]][
                        "sensor_token"
                    ]
                    b = indexes["calibrated_sensor"][other["calibrated_sensor_token"]][
                        "sensor_token"
                    ]
                    _check(a == b, "sample_data: cross-sensor chain")
    # 4. 时间戳必须是整数且符合微秒量级；这不是完整的业务日期范围校验。
    # type(value) is int 同时排除 bool（Python 中 bool 是 int 的子类）。
    for table in ("sample", "sample_data", "ego_pose"):
        for row in tables[table]:
            value = row.get("timestamp")
            _check(
                type(value) is int and 10**14 <= value < 10**17,
                f"{table}: invalid microsecond timestamp",
            )
    # 5. 标注属性是可为空的列表；列表中每个 token 必须存在于 attribute 表。
    for row in tables["sample_annotation"]:
        attributes = row.get("attribute_tokens")
        _check(
            isinstance(attributes, list)
            and all(
                isinstance(x, str) and x in indexes["attribute"] for x in attributes
            ),
            "sample_annotation: dangling attribute",
        )
    # 6. 按 sample 收集关键帧媒体的 sensor；非关键帧 sweep 不计入覆盖。
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
    # 预期集合来自本数据集 sensor 表，不硬编码通道数量。
    # 数量与集合同时相等，确保每个 sensor 恰好一次，拒绝缺失和重复覆盖。
    expected_sensors = set(indexes["sensor"])
    _check(
        all(
            len(values) == len(expected_sensors) and set(values) == expected_sensors
            for values in keyframes.values()
        ),
        "sample: keyframe sensor coverage mismatch",
    )
    # 7. 核对场景声明的帧数、首尾所属关系及链表端点。
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
    # 8. 对象可跨多帧出现：检查其标注数及首尾，而非把一次标注当成独立对象。
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
    # 9. 局部互指正确仍可能有断开的子链；从声明的首节点遍历整个场景/对象链。
    # visited 同时用于发现环，以及与该 owner 全部子记录比较，发现遗漏或断链。
    # 这里仅遍历 sample、sample_annotation；sample_data 只有前面的局部检查。
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
    # 10. mini 子集的地图可引用未包含的 log，明确区分本地与范围外引用。
    # 该例外只适用于这里的 map.log_tokens，不放宽其他核心外键。
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
    # 每个本地 log 必须恰好关联一张地图，缺失或多张地图覆盖都拒绝。
    _check(all(len(maps) == 1 for maps in mapped.values()), "log: map must be unique")
    # 只有全部门禁通过才返回报告；下面的零值是通过结论，不是坏行累计计数。
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


def validate_source_types(tables: dict[str, list[dict]], contract: dict) -> None:
    """按契约校验源字段名称和基础类型；缺失字段由业务校验继续拦截。"""
    types = {"TEXT": str, "BIGINT": int, "BOOLEAN": bool, "JSONB": list}
    for table, rows in tables.items():
        fields = contract["source_fields"][table]
        for row in rows:
            _check(set(row).issubset(fields), f"{table}: unknown source fields")
            for key, value in row.items():
                _check(
                    type(value) is types[fields[key]],
                    f"{table}.{key}: invalid source type",
                )


def verify_snapshot(directory: Path) -> dict:
    # 验证 ODS 内部产物与完成清单，不重新读取原始媒体或执行全部关系校验。
    try:
        complete = json.loads((directory / "complete.json").read_text())
        _check(
            complete.get("profile_id") == "nuscenes-mini"
            and complete.get("profile_version") == "2",
            "snapshot: invalid profile",
        )
        _check(
            complete.get("asset_policy") in {"strict", "registered"},
            "snapshot: invalid asset policy",
        )
        _check(
            complete.get("processing_version") == PROCESSING_VERSION,
            "snapshot: processing version mismatch",
        )
        _check(
            isinstance(complete["artifacts"], dict),
            "snapshot: invalid artifact mapping",
        )
        _check(
            isinstance(complete.get("assets"), dict)
            and isinstance(complete.get("capabilities"), dict),
            "snapshot: missing asset or capability summary",
        )
        expected_artifacts = {
            f"{directory}/{table}.{extension}"
            for directory, extension in (("raw", "json"), ("records", "jsonl"))
            for table in TABLES
        } | {"file-manifest.json", "quality.json"}
        # 清单必须恰好登记 13 份 raw、13 份 records 和两份报告。
        # complete.json 不包含自身摘要；本函数也不扫描拒绝目录内额外的未登记文件。
        _check(
            set(complete["artifacts"]) == expected_artifacts,
            "snapshot: incomplete artifact manifest",
        )
        for relative, checksum in complete["artifacts"].items():
            # 即使读取既有快照，也要重新核对产物路径和内容，不能只看目录是否存在。
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
        # 保留明确的摄取错误；其他清单结构、JSON 或 I/O 错误转换为统一错误。
        if isinstance(error, IngestionError):
            raise
        raise IngestionError("snapshot: invalid completion manifest") from None


def ingest(config: dict, repository: Path, *, fail_at: str | None = None) -> dict:
    # 两个故障检查点供验证调用：正式目录重命名前，以及重命名后但水位更新前。
    if fail_at not in {None, "before_rename", "after_rename"}:
        raise IngestionError("fault: unsupported checkpoint")
    # 先确认配置、目录与来源证据；路径必须在仓库外，不能包含仓库本身。
    preflight(config, repository, "dataset")
    policy = AssetPolicy(asset_policy(config))
    processing_version = PROCESSING_VERSION
    contract = load_contract_document(repository)
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
        # 同一输出目录内遵守该锁的摄取任务串行；退出上下文关闭文件即释放锁。
        # 该锁不限制其他程序改写原始数据，也不是分布式锁。
        fcntl.flock(lock, fcntl.LOCK_EX)
        tables = read_tables(root, version)
        quality = validate(tables, contract)
        validate_source_types(tables, contract)
        assets = inventory(root, version, tables, policy)
        capabilities = derive_capabilities(assets)
        quality.update(
            profile_id=contract["contract_id"],
            profile_version=contract["version"],
            asset_policy=policy.value,
            assets=asset_summary(assets),
            capabilities=capabilities.as_dict(),
        )
        table_checksums = {
            f"{version}/{table}.json": digest(root / version / f"{table}.json")
            for table in TABLES
        }
        # Verify in-memory records against the exact raw bytes fingerprinted above.
        # 再读元数据并比较解析结果，检查读取、校验和文件哈希期间是否出现变化。
        for table in TABLES:
            relative = f"{version}/{table}.json"
            _check(
                json.loads((root / relative).read_text()) == tables[table],
                "metadata changed during validation",
            )
        # 输入身份覆盖表摘要与资产实际状态，不把“模式”写进身份。
        identity = {
            "profile": f"{contract['contract_id']}@{contract['version']}",
            "dataset_version": version,
            "tables": table_checksums,
            "assets": [asset.identity() for asset in assets],
        }
        snapshot_id = manifest_revision(identity).removeprefix("sha256:")
        # entity_id 还会包含该命名空间：输入快照变化时，同源 token 的内部身份也会变。
        namespace = canonical(["nuscenes", version, snapshot_id]).decode()
        snapshots = output / "snapshots"
        _check(not snapshots.is_symlink(), "output: snapshots directory is a symlink")
        snapshots.mkdir(exist_ok=True)
        target = snapshots / f"{snapshot_id}-{processing_version}"
        _check(not target.is_symlink(), "output: snapshot is a symlink")
        reused = target.exists()
        # 重跑不覆盖正式快照；即使复用，也已完成上面的源校验和文件清单计算。
        if not reused:
            run_id = uuid.uuid4().hex
            ingested_at = datetime.now(UTC).isoformat()
            staging = snapshots / f".staging-{run_id}"
            # 新产物仅写暂存目录，全部验证通过后再整体重命名为正式目录。
            # 异常遗留的暂存目录不自动清理；重跑会使用新的 UUID，不从半成品续写。
            staging.mkdir()
            (staging / "raw").mkdir()
            (staging / "records").mkdir()
            for table in TABLES:
                relative = f"{version}/{table}.json"
                destination = staging / "raw" / f"{table}.json"
                shutil.copyfile(root / relative, destination)
                # 保留元数据原始字节，并与本轮已登记摘要核对，检测复制阶段变化。
                _check(
                    digest(destination) == table_checksums[relative],
                    "metadata changed during copy",
                )
                with (staging / "records" / f"{table}.jsonl").open("wb") as stream:
                    # 每行包装完整原记录，保留源身份、摄取身份及时间，供 Spark 显式读入。
                    # source_checksum 是整份源表文件摘要；无 timestamp 的表记录为 null。
                    # quality_status=valid 表示前置门禁已通过，不代表验证了原记录所有字段。
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
                                    "source_checksum": table_checksums[relative],
                                    "run_id": run_id,
                                    "ingested_at": ingested_at,
                                    "timestamp_us": row.get("timestamp"),
                                    "schema_version": processing_version,
                                    "quality_status": "valid",
                                    "raw_record": row,
                                }
                            )
                            + b"\n"
                        )
            # 文件清单指向仓库外原始媒体；只复制元数据，不复制图片、点云或地图文件。
            write_json(
                staging / "file-manifest.json",
                [asset.as_dict() for asset in assets],
            )
            write_json(staging / "quality.json", quality)
            # 完成清单固定本次创建身份并列出产物摘要；不把自身纳入 artifacts。
            complete = {
                "snapshot_id": snapshot_id,
                "dataset_version": version,
                "namespace": namespace,
                "profile_id": contract["contract_id"],
                "profile_version": contract["version"],
                "processing_version": processing_version,
                "asset_policy": policy.value,
                "assets": asset_summary(assets),
                "capabilities": capabilities.as_dict(),
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
            # 先同步所有文件，再同步子目录及暂存根目录；重命名后还需同步父目录。
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
            if fail_at == "before_rename":
                raise IngestionError("fault: before_rename")
            staging.rename(target)
            # 此时正式目录可见，但水位未前移；失败后重跑可验证并复用该目录。
            if fail_at == "after_rename":
                raise IngestionError("fault: after_rename")
            fd = os.open(snapshots, os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        # 新建和复用都走相同校验；损坏的正式快照会阻断，不自动覆盖修复。
        complete = verify_snapshot(target)
        _check(
            complete["snapshot_id"] == snapshot_id
            and complete["namespace"] == namespace,
            "snapshot: identity mismatch",
        )
        if config.get("postgres", {}).get("mirror_ods", False):
            from driving_data_platform.ingestion.postgres import mirror

            mirror(Path(config["storage"]["output_root"]), repository, target)
        # No published watermark exists here: ingestion cannot publish online data.
        # 仅在正式快照通过校验后原子更新摄取水位；这里不改 PostgreSQL 活动版本。
        # 若水位写失败，下次可复用已提交快照；复用时保留首次创建的 run_id。
        atomic_json(
            output / "watermark.json",
            {
                "snapshot_id": snapshot_id,
                "profile_id": contract["contract_id"],
                "profile_version": contract["version"],
                "processing_version": processing_version,
                "asset_policy": policy.value,
                "capabilities": capabilities.as_dict(),
                "run_id": complete["run_id"],
                "completion_sha256": digest(target / "complete.json"),
            },
        )
        # 返回摄取摘要；详细地图关联仍保留在 quality.json，响应仅返回数量。
        return {
            "status": "passed",
            "scope": "local_ods_ingestion_only",
            "profile_id": contract["contract_id"],
            "profile_version": contract["version"],
            "processing_version": processing_version,
            "asset_policy": policy.value,
            "assets": asset_summary(assets),
            "capabilities": capabilities.as_dict(),
            "snapshot_id": snapshot_id,
            "run_id": complete["run_id"],
            "reused": reused,
            "counts": quality["counts"],
            "files": len(assets),
            "map_links": {
                key: len(value) if isinstance(value, list) else value
                for key, value in quality["map_links"].items()
            },
        }


def main(argv: list[str] | None = None) -> int:
    # CLI 只接受配置入口；fail_at 不对外暴露为本模块的命令行选项。
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = ingest(load_config(args.config), Path(__file__).resolve().parents[3])
    except (ConfigError, IngestionError, OSError, ValueError) as error:
        # 配置/摄取错误保留约定信息，其他已捕获异常不直接回显底层路径细节。
        message = (
            str(error)
            if isinstance(error, (ConfigError, IngestionError))
            else "ingestion: I/O or JSON failure"
        )
        print(json.dumps({"status": "blocked", "error": message}))
        return 2
    # 输出结构化结果供上层入口读取：0 成功，捕获的摄取失败为 2。
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
