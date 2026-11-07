"""用真实小文件验证校验器，不模拟模型编码成功。"""

import hashlib
import json
import runpy
import struct
from pathlib import Path

import pytest

MODULE = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts/verify_model.py")
)


def fixture_file(root, *, offsets=(0, 4), shape=(2,), data=b"\x00" * 4):
    header = json.dumps(
        {
            "weight": {
                "dtype": "BF16",
                "shape": list(shape),
                "data_offsets": list(offsets),
            }
        }
    ).encode()
    content = struct.pack("<Q", len(header)) + header + data
    (root / "model.safetensors").write_bytes(content)
    return {
        "Data": {
            "Files": [
                {
                    "Type": "blob",
                    "Path": "model.safetensors",
                    "Size": len(content),
                    "Sha256": hashlib.sha256(content).hexdigest(),
                    "Revision": "test-only",
                }
            ]
        }
    }


def test_weight_file_and_tensor_coverage(tmp_path):
    metadata = fixture_file(tmp_path)
    result = MODULE["integrity"](tmp_path, metadata)
    assert result["tensor_count"] == 1 and result["tensor_structure"] == "matched"
    assert result["files"][0]["revision"] == "test-only"


@pytest.mark.parametrize("fault", ["digest", "truncated", "shape", "missing", "path"])
def test_weight_failures_are_real(tmp_path, fault):
    metadata = fixture_file(tmp_path)
    if fault == "digest":
        metadata["Data"]["Files"][0]["Sha256"] = "0" * 64
    elif fault == "truncated":
        metadata = fixture_file(tmp_path, data=b"\x00" * 2)
    elif fault == "shape":
        metadata = fixture_file(tmp_path, shape=(4,))
    elif fault == "missing":
        (tmp_path / "model.safetensors").unlink()
    elif fault == "path":
        metadata["Data"]["Files"][0]["Path"] = "../model.safetensors"
    with pytest.raises(ValueError):
        MODULE["integrity"](tmp_path, metadata)
