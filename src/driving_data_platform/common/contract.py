"""数据语义契约引用；不替代实际质量检查。"""

import hashlib
import json
from pathlib import Path

from driving_data_platform.common.errors import PlatformError


def load_contract(repo: Path) -> dict:
    path = repo / "contracts/nuscenes-mini-v1.json"
    value = json.loads(path.read_text())
    if value.get("contract_id") != "nuscenes-mini" or value.get("version") != "1":
        raise PlatformError(
            "CONTRACT_CONFLICT", "unsupported-contract", "未知数据契约版本"
        )
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return {
        "contract_id": value["contract_id"],
        "version": value["version"],
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }
