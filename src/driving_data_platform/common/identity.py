"""Versioned canonical encodings for content and source identities."""

import hashlib
import json


def canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def manifest_revision(checksums: dict[str, object]) -> str:
    """A local content revision, explicitly distinct from a publisher commit."""
    return "sha256:" + hashlib.sha256(canonical(checksums)).hexdigest()


def entity_id(namespace: str, entity_type: str, source_token: str) -> str:
    return hashlib.sha256(canonical([namespace, entity_type, source_token])).hexdigest()
