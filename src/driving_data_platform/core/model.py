"""资产状态、校验策略与由实际输入推导出的能力。"""

from dataclasses import dataclass
from enum import Enum


class AssetState(str, Enum):
    """资产在本次快照中的实际状态。"""

    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    MISSING = "missing"


class AssetPolicy(str, Enum):
    """本次摄取对非元数据资产的校验策略。"""

    STRICT = "strict"
    REGISTERED = "registered"


@dataclass(frozen=True)
class Asset:
    """一个源文件在快照中的登记项。"""

    path: str
    kind: str
    state: AssetState
    bytes: int | None = None
    sha256: str | None = None
    error: str | None = None

    def as_dict(self) -> dict:
        value = {"path": self.path, "kind": self.kind, "state": self.state.value}
        if self.bytes is not None:
            value["bytes"] = self.bytes
        if self.sha256 is not None:
            value["sha256"] = self.sha256
        if self.error is not None:
            value["error"] = self.error
        return value

    def identity(self) -> dict:
        """只保留影响内容身份的字段；错误说明不参与身份。"""
        value = {"path": self.path, "kind": self.kind, "state": self.state.value}
        if self.bytes is not None:
            value["bytes"] = self.bytes
        if self.sha256 is not None:
            value["sha256"] = self.sha256
        return value


@dataclass(frozen=True)
class Capabilities:
    """下游可以依赖的能力；不是输入模式。"""

    structured_query: bool = True
    annotation_review: bool = True
    original_media: bool = False
    image_search: bool = False

    def as_dict(self) -> dict:
        return {
            "structured_query": self.structured_query,
            "annotation_review": self.annotation_review,
            "original_media": self.original_media,
            "image_search": self.image_search,
        }


def derive_capabilities(assets: list[Asset]) -> Capabilities:
    """媒体能力只在全部相机资产通过校验时为真。"""
    camera = [asset for asset in assets if asset.kind == "camera_image"]
    media_verified = bool(camera) and all(
        asset.state == AssetState.VERIFIED for asset in camera
    )
    return Capabilities(
        structured_query=True,
        annotation_review=True,
        original_media=media_verified,
        image_search=media_verified,
    )


def asset_summary(assets: list[Asset]) -> dict:
    """按种类和状态汇总，供完成清单与质量报告使用。"""
    summary: dict[str, dict[str, int]] = {}
    for asset in assets:
        counts = summary.setdefault(
            asset.kind,
            {"total": 0, "verified": 0, "unverified": 0, "missing": 0},
        )
        counts["total"] += 1
        counts[asset.state.value] += 1
    return summary
