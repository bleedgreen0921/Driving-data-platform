"""单一数据契约引用；不替代实际质量检查。"""

import hashlib
import json
from pathlib import Path

from driving_data_platform.common.errors import PlatformError

CONTRACT_ID = "nuscenes-mini"
CONTRACT_VERSION = "2"
CONTRACT_PATH = "contracts/nuscenes-mini-v2.json"


def load_contract_document(repo: Path) -> dict:
    """读取完整契约，供摄取类型检查和资产规则使用。"""
    path = repo / CONTRACT_PATH
    value = json.loads(path.read_text())
    if (
        value.get("contract_id") != CONTRACT_ID
        or value.get("version") != CONTRACT_VERSION
    ):
        raise PlatformError(
            "CONTRACT_CONFLICT", "unsupported-contract", "未知数据契约版本"
        )
    return value


def load_contract(repo: Path) -> dict:
    """返回规范化契约身份，写入数仓与 generation 清单。"""
    value = load_contract_document(repo)
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return {
        "contract_id": value["contract_id"],
        "version": value["version"],
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }
