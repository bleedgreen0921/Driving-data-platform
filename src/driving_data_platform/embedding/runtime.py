"""Backend-neutral space selection and runtime construction."""

import json
from pathlib import Path

from driving_data_platform.common.config import (
    application_config,
    port,
)
from driving_data_platform.embedding import artifacts as a
from driving_data_platform.embedding import model
from driving_data_platform.embedding.providers import provider


def selection(root, space_id):
    contract = model.space(root, space_id)
    path = (
        f"embedding/selections/{space_id}.json"
        if contract.get("schema_version") == 3
        else "embedding/selection.json"
    )
    return a.read(root, path)


def external_config(root):
    config = application_config(root).get("embedding", {})
    path = config.get("provider_file")
    if not path:
        raise ValueError("embedding.provider_file: required for external backend")
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("外部服务配置文件必须是普通文件")
    # Config contains endpoint and credential variable name, never credential values.
    value = json.loads(path.read_text(encoding="utf-8"))
    allowed = {
        "backend",
        "endpoint",
        "model",
        "timeout_seconds",
        "api_key_env",
        "auth_header",
        "max_text_characters",
        "max_image_bytes",
    }
    if (
        not isinstance(value, dict)
        or set(value) - allowed
        or value.get("backend") != "external_api"
    ):
        raise ValueError("外部配置字段非法；密钥只能通过环境变量注入")
    return value


def encoder(root, space_id):
    contract = model.space(root, space_id)
    if contract["schema_version"] == 3:
        return provider(external_config(root) | {"space": contract})
    return model.encoder(root, space_id, port("model"))


def register(root, file, *, test_only=False):
    file = Path(file)
    if file.is_symlink():
        raise ValueError("空间文件不得为符号链接")
    contract = json.loads(file.read_text(encoding="utf-8"))
    # Adapter identity is computed, never copied from another machine or invented.
    from driving_data_platform.embedding.external import implementation

    if (
        isinstance(contract, dict)
        and contract.get("schema_version") == 3
        and contract.get("implementation_sha256") is None
    ):
        contract["implementation_sha256"] = implementation()
    sid = a.validate_space(contract)
    if contract.get("schema_version") != 3 or contract["test_only"] != test_only:
        raise ValueError("仅登记外部 v3 空间；test_only 必须与显式选项一致")
    # Registration is not evaluation or publication; no network request is made here.
    reused = a.commit(root, "spaces", sid, {"space.json": a.json_bytes(contract)})
    return {
        "status": "passed",
        "scope": "space-registration-only",
        "space_id": sid,
        "reused": reused,
        "acceptance": "not_verified",
    }


def up(root, space_id, *args, **kwargs):
    contract = model.space(root, space_id)
    if contract["schema_version"] != 3:
        return model.up(root, space_id, *args, **kwargs)
    result = encoder(root, space_id).probe()
    return {**result, "space_id": space_id, "reused": True, "managed_process": False}


def stop(root, *args, space_id=None, **kwargs):
    # Stop only the registered local process. An external provider is never managed here.
    if space_id is not None and model.space(root, space_id)["schema_version"] == 3:
        return {"status": "passed", "managed_process": False}
    if not model.state_path(root).exists():
        return {"status": "passed", "managed_process": False}
    return model.stop(root, *args, **kwargs)


def active_space(root, deployment):
    from driving_data_platform.embedding import store

    with store.connect(root, deployment=deployment) as conn:
        row = conn.execute(
            "SELECT a.embedding_generation FROM control.embedding_active a JOIN control.active b ON a.generation=b.generation WHERE b.singleton"
        ).fetchone()
        if not row or not row[0]:
            raise ValueError("活动业务版本没有向量版本")
        return store.show(conn, row[0])


def checked(root, expected):
    client = encoder(root, expected)
    if client.capabilities()["backend"] == "external_api":
        client.check_identity()
        return {
            "space_id": expected,
            "backend": "external_api",
            "managed_process": False,
        }
    return model.checked(root, expected=expected)
