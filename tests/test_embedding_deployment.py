"""部署登记拒绝混用、数据库重建及试验空间越界。"""

from types import SimpleNamespace

import pytest

from driving_data_platform.embedding import deployment as d
from driving_data_platform.embedding import store


def state():
    return {
        "deployment_id": "1" * 32,
        "kind": "production",
        "database": "ddp",
        "database_oid": "42",
        "system_identifier": "123",
    }


def connection(values):
    responses = iter(values)
    return SimpleNamespace(
        execute=lambda *_: SimpleNamespace(fetchone=lambda: next(responses))
    )


def test_database_recreated_or_wrong_cluster_rejected():
    for actual in (("ddp", "43", "123"), ("ddp", "42", "456"), ("other", "42", "123")):
        with pytest.raises(ValueError, match="集群"):
            d.validate_database(connection([actual]), state(), registered=False)


def test_missing_or_mixed_database_registration_rejected():
    for registered in (None, ("2" * 32, "production", "ddp", "42", "123")):
        with pytest.raises(ValueError, match="内部部署登记"):
            d.validate_database(connection([("ddp", "42", "123"), registered]), state())


def test_deployment_rejects_test_space_even_with_matching_identity():
    registration = ("1" * 32, "production", "ddp", "42", "123")
    token = d.CURRENT.set(state())
    try:
        with pytest.raises(ValueError, match="禁止人工"):
            store.check_environment(
                connection([("ddp", "42", "123"), registration, (False,)]), True
            )
    finally:
        d.CURRENT.reset(token)
    assert d.CURRENT.get() is None


def test_trial_and_deployment_cannot_be_combined(tmp_path):
    with (
        pytest.raises(ValueError, match="不得同时"),
        store.connect(tmp_path, trial="1" * 32, deployment="2" * 32),
    ):
        pytest.fail("混用身份不得连接数据库")


def test_runtime_rejects_trial_and_deployment_together(monkeypatch, tmp_path):
    from driving_data_platform.embedding import web

    monkeypatch.setenv("DDP_EMBEDDING_TRIAL", "1" * 32)
    monkeypatch.setenv("DDP_EMBEDDING_DEPLOYMENT", "2" * 32)
    with pytest.raises(ValueError, match="不得同时"):
        web.validate_runtime(tmp_path)


def test_default_api_refuses_unknown_port_listener(monkeypatch, tmp_path):
    import socket
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import api_process

    (tmp_path / "reports").mkdir()
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", 18080))
        except OSError:
            pytest.skip("18080正在使用")
        listener.listen()
        with pytest.raises(OSError):
            api_process.start(tmp_path, Path.cwd())
        assert not (tmp_path / "api-process.json").exists()
