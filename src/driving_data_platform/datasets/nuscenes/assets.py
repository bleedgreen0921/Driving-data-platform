"""收集 nuScenes 元数据与引用资产，并按策略校验。"""

import hashlib
from pathlib import Path

from driving_data_platform.common.preflight import TABLES
from driving_data_platform.core.errors import IngestionError
from driving_data_platform.core.model import Asset, AssetPolicy, AssetState


def _check(condition: bool, code: str) -> None:
    if not condition:
        raise IngestionError(code)


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _relative_path(value: object, label: str) -> str:
    _check(isinstance(value, str) and bool(value), f"{label}: missing filename")
    relative = Path(value)
    _check(
        not relative.is_absolute()
        and ".." not in relative.parts
        and "\x00" not in value
        and "\\" not in value
        and relative.as_posix() == value,
        f"{label}: path escape",
    )
    return value


def _verified(root: Path, relative: str, kind: str) -> Asset:
    path = root / relative
    resolved = path.resolve()
    _check(
        resolved.is_relative_to(root.resolve()) and path.is_file(),
        f"{kind}: missing or escaped file",
    )
    before = path.stat()
    checksum = _digest(path)
    after = path.stat()
    _check(
        (before.st_size, before.st_mtime_ns, before.st_ino)
        == (after.st_size, after.st_mtime_ns, after.st_ino),
        f"{kind}: input changed during hashing",
    )
    return Asset(
        path=relative,
        kind=kind,
        state=AssetState.VERIFIED,
        bytes=after.st_size,
        sha256=checksum,
    )


def _registered(root: Path, relative: str, kind: str) -> Asset:
    """不读取非元数据文件，只确认路径安全及是否存在。"""
    path = root / relative
    _check(
        path.resolve().is_relative_to(root.resolve()),
        f"{kind}: path escape",
    )
    if path.is_file():
        return Asset(path=relative, kind=kind, state=AssetState.UNVERIFIED)
    return Asset(path=relative, kind=kind, state=AssetState.MISSING)


def _sample_data_kind(
    calibrated: dict[str, dict], sensors: dict[str, dict], row: dict
) -> str:
    calibration = calibrated.get(row.get("calibrated_sensor_token"))
    _check(calibration is not None, "sample_data: dangling calibrated sensor")
    sensor = sensors.get(calibration.get("sensor_token"))
    _check(sensor is not None, "sample_data: dangling sensor")
    return "camera_image" if sensor.get("modality") == "camera" else "sensor_data"


def inventory(
    root: Path,
    version: str,
    tables: dict[str, list[dict]],
    policy: AssetPolicy = AssetPolicy.STRICT,
) -> list[Asset]:
    """登记 13 份元数据及 sample_data/map 引用的文件。

    元数据始终校验摘要；非元数据在 strict 下校验存在性和摘要，在
    registered 下只登记路径并区分 unverified/missing。
    """
    entries: dict[str, str] = {}
    for table in TABLES:
        entries[f"{version}/{table}.json"] = "metadata"

    def add(relative: str, kind: str) -> None:
        previous = entries.get(relative)
        _check(
            previous is None or previous == kind,
            f"{kind}: conflicting file roles",
        )
        entries[relative] = kind

    calibrated = {item["token"]: item for item in tables["calibrated_sensor"]}
    sensors = {item["token"]: item for item in tables["sensor"]}
    for row in tables["sample_data"]:
        relative = _relative_path(row.get("filename"), "sample_data")
        add(relative, _sample_data_kind(calibrated, sensors, row))
    for row in tables["map"]:
        relative = _relative_path(row.get("filename"), "map")
        add(relative, "map")

    result = []
    for relative, kind in sorted(entries.items()):
        if kind == "metadata" or policy == AssetPolicy.STRICT:
            result.append(_verified(root, relative, kind))
        else:
            result.append(_registered(root, relative, kind))
    return result
