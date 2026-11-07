"""CLI 筛选错误及编码前版本绑定边界。"""

from types import SimpleNamespace

import pytest

from driving_data_platform.common.errors import PlatformError
from driving_data_platform.embedding import search, store


def args(**changes):
    return SimpleNamespace(
        **(
            {
                "limit": 20,
                "location": None,
                "start": None,
                "end": None,
                "category": None,
                "required_channel": [],
            }
            | changes
        )
    )


def test_filter_boundaries():
    assert search.conditions(args(required_channel=["CAM_FRONT", "CAM_FRONT"]))[
        "channels"
    ] == ["CAM_FRONT"]
    assert (
        search.conditions(
            args(start="1970-01-01T00:00:00Z", end="1970-01-01T00:00:00.000001+00:00")
        )["end_us"]
        == 1
    )
    for changes in (
        {"start": "bad"},
        {"start": "2026-01-01"},
        {"start": "2026-01-01T00:00:00+08:00"},
        {"start": "2026-01-02T00:00:00Z", "end": "2026-01-01T00:00:00Z"},
    ):
        with pytest.raises(PlatformError) as caught:
            search.conditions(args(**changes))
        assert caught.value.category == "INPUT_INVALID"
    for limit in (0, 101, True):
        with pytest.raises(ValueError):
            search.conditions(args(limit=limit))


class Connection:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.queries = []

    def execute(self, query, parameters=None):
        self.queries.append((query, parameters))
        return SimpleNamespace(fetchone=lambda: next(self.responses))


def test_real_space_requires_registered_database():
    for test_only in (False, True):
        conn = Connection([("ddp_embedding_trial_" + "1" * 32,), None])
        with pytest.raises(ValueError, match="登记"):
            store.check_environment(conn, test_only)
    conn = Connection([("ddp",)])
    with pytest.raises(ValueError, match="独立"):
        store.check_environment(conn, False)


def test_missing_pointer_does_not_read_model(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "check_environment", lambda *_: None)
    monkeypatch.setattr(
        search.model, "space", lambda *_: pytest.fail("缺失指针时不得读取模型空间")
    )
    with pytest.raises(ValueError, match="尚未建立检索数据"):
        search.resolve(tmp_path, Connection([None]), "g_" + "1" * 64)


def test_explicit_version_never_reads_pointer(monkeypatch, tmp_path):
    generation = "g_" + "1" * 64
    eg = "e_" + "2" * 64
    sid = "3" * 64
    contract = {"test_only": False}
    evaluation = {"status": "passed", "selected_space_id": sid}
    selected = {
        "space_id": sid,
        "status": "passed",
        "report": "evaluation.json",
        "report_sha256": search.a.checksum(evaluation),
    }
    monkeypatch.setattr(store, "check_environment", lambda *_: None)
    monkeypatch.setattr(
        store,
        "show",
        lambda _, version: {
            "generation": generation,
            "embedding_generation": version,
            "version_status": "READY",
            "space_id": sid,
        },
    )
    monkeypatch.setattr(search.model, "space", lambda *_: contract)
    monkeypatch.setattr(
        search.a,
        "read",
        lambda _, path: selected if path.endswith("selection.json") else evaluation,
    )
    conn = Connection([(contract,)])
    meta, _ = search.resolve(tmp_path, conn, generation, eg)
    assert meta["embedding_generation"] == eg
    assert all("embedding_active" not in query for query, _ in conn.queries)
    selected["report_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="选参"):
        search.resolve(tmp_path, Connection([(contract,)]), generation, eg)


def test_encoding_keeps_resolved_version(monkeypatch, tmp_path):
    from contextlib import contextmanager

    original = "e_" + "1" * 64
    pointer = {"current": original}
    conn = Connection([])

    @contextmanager
    def connect(*_, **__):
        yield conn

    monkeypatch.setattr(store, "connect", connect)
    monkeypatch.setattr(
        search,
        "resolve",
        lambda *_: (
            {
                "embedding_generation": pointer["current"],
                "space_id": "2" * 64,
                "artifact_id": "3" * 64,
            },
            {},
        ),
    )

    class Encoder:
        def __init__(self, config):
            pass

        def encode_text(self, text):
            assert text == " 路口\n有汽车 "
            pointer["current"] = "e_" + "4" * 64
            return [1.0]

    from driving_data_platform.embedding import runtime

    monkeypatch.setattr(runtime, "encoder", lambda *_: Encoder({}))

    def query(_, generation, space_id, vector, **kwargs):
        assert kwargs["embedding_generation"] == original
        assert pointer["current"] != original
        return {
            "generation": generation,
            "embedding_generation": original,
            "space_id": space_id,
            "items": [],
        }

    monkeypatch.setattr(store, "search", query)
    requested = args()
    requested.__dict__.update(
        trial="5" * 32,
        generation="g_" + "6" * 64,
        text=" 路口\n有汽车 ",
        embedding_generation=None,
        image_channel=None,
    )
    result = search.execute(tmp_path, requested)
    assert result["count"] == 0 and result["embedding_generation"] == original
