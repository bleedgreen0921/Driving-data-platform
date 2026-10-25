import hashlib
import io
import json
import zipfile

import pytest
from fastapi import HTTPException

from driving_data_platform.api import app as api


def test_media_rejects_tampering_and_links(tmp_path):
    data = b"\xff\xd8\xfftest\xff\xd9"
    image = tmp_path / "sample.jpg"
    image.write_bytes(data)
    entry = {
        "path": "sample.jpg",
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    assert api.image_bytes(tmp_path, entry) == data
    image.write_bytes(b"\xff\xd8\xffevil\xff\xd9")
    with pytest.raises(ValueError):
        api.image_bytes(tmp_path, entry)
    image.unlink()
    target = tmp_path / "other.jpg"
    target.write_bytes(data)
    image.symlink_to(target)
    with pytest.raises(OSError):
        api.image_bytes(tmp_path, entry)
    with pytest.raises(ValueError):
        api.image_bytes(tmp_path, entry | {"path": "../sample.jpg"})


def test_export_limits_and_manifest(monkeypatch):
    meta = {"generation": "test", "snapshot_id": "source", "processing_version": "v1"}
    item = {"entity_id": "frame"}
    data = api.export_bundle([item], meta, {}, "jsonl")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert (
            manifest["sha256"]
            == hashlib.sha256(archive.read("frames.jsonl")).hexdigest()
        )
        assert manifest["count"] == 1
    assert api.MAX_ROWS == 100000
    assert api.MAX_BYTES == 100 * 1024**2
    with pytest.raises(HTTPException) as error:
        api.export_bundle([item] * 100001, meta, {}, "jsonl")
    assert error.value.status_code == 413
    monkeypatch.setattr(api, "MAX_BYTES", 32)
    with pytest.raises(HTTPException) as error:
        api.export_bundle([item], meta, {}, "jsonl")
    assert error.value.status_code == 413
