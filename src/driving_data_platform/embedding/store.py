"""pgvector 增量迁移、事务装载及按业务版本隔离的发布与精确检索。"""

import hashlib
import json
import os
import re
import uuid
from contextlib import contextmanager

import psycopg
from psycopg.types.json import Jsonb

from driving_data_platform.common.config import (
    application_config,
)
from driving_data_platform.common.errors import PlatformError
from driving_data_platform.common.run_query import identity
from driving_data_platform.embedding.artifacts import (
    media_input,
    trial_config,
    validate_space,
    vector32,
    verify_artifact,
)


@contextmanager
def connect(root, *, trial=None, deployment=None):
    trial = trial or os.environ.get("DDP_EMBEDDING_TRIAL")
    deployment = deployment or os.environ.get("DDP_EMBEDDING_DEPLOYMENT")
    if trial and deployment:
        raise ValueError("试验与部署环境不得同时指定")
    if deployment:
        from driving_data_platform.embedding import deployment as d

        state = d.registration(root, deployment)
        d.container(state)
        fields = dict(
            line.split("=", 1)
            for line in (root / "compose.env").read_text().splitlines()
        )
        with psycopg.connect(
            host=state["host"],
            port=state["port"],
            dbname=state["database"],
            user=state["user"],
            password=fields["DDP_POSTGRES_PASSWORD"],
            connect_timeout=5,
            autocommit=True,
        ) as conn:
            d.validate_database(conn, state)
            token = d.CURRENT.set(state)
            try:
                yield conn
            finally:
                d.CURRENT.reset(token)
        return
    if trial:
        state = trial_config(root, trial)
        with psycopg.connect(
            host=state["host"],
            port=state["port"],
            dbname=state["database"],
            user=state["user"],
            password=state["password"],
            connect_timeout=5,
            autocommit=True,
        ) as conn:
            if trial_database(conn) != state["database"]:
                raise ValueError("试验连接身份不一致")
            yield conn
        return
    dsn = os.environ.get("DDP_EMBEDDING_DSN")
    if not dsn:
        dsn = application_config(root).get("embedding", {}).get("database_dsn")
    if not isinstance(dsn, str) or not dsn.strip():
        raise ValueError("请配置独立的 DDP_EMBEDDING_DSN；不回退到正式服务数据库")
    with psycopg.connect(dsn, connect_timeout=5, autocommit=True) as conn:
        yield conn


def trial_database(conn):
    name = conn.execute("SELECT current_database()").fetchone()[0]
    if not re.fullmatch(r"ddp_embedding_trial_[a-f0-9]{32}", name):
        raise ValueError("本轮仅允许登记的独立 embedding 试验数据库")
    return name


def migrate(conn, repo, *, test_database=False, restore_from_database=None):
    if not test_database:
        raise ValueError("本轮迁移必须显式指定 --test-database")
    name = trial_database(conn)
    script = (repo / "sql/postgres/migrations/001_embedding.sql").read_bytes()
    checksum = hashlib.sha256(script).hexdigest()
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(877801)")
        if conn.execute("SELECT to_regclass('serving.media')").fetchone()[0] is None:
            raise ValueError("请先初始化或恢复既有业务 schema")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS control.embedding_migration(version integer PRIMARY KEY,sha256 text NOT NULL)"
        )
        previous = conn.execute(
            "SELECT sha256 FROM control.embedding_migration WHERE version=1"
        ).fetchone()
        if previous and previous[0] != checksum:
            raise ValueError("已执行的向量迁移 SQL 摘要漂移")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS control.embedding_environment(singleton boolean PRIMARY KEY CHECK(singleton),database_name text NOT NULL,test_only_allowed boolean NOT NULL)"
        )
        registered = conn.execute(
            "SELECT database_name FROM control.embedding_environment WHERE singleton"
        ).fetchone()
        if registered and registered[0] != name:
            if restore_from_database != registered[0] or not re.fullmatch(
                r"ddp_embedding_trial_[a-f0-9]{32}", restore_from_database
            ):
                raise ValueError(
                    "恢复库尚未登记为当前独立试验环境，须显式指定原试验数据库"
                )
            conn.execute(
                "UPDATE control.embedding_environment SET database_name=%s WHERE singleton",
                (name,),
            )
        conn.execute(
            "INSERT INTO control.embedding_environment VALUES(true,%s,true) ON CONFLICT DO NOTHING",
            (name,),
        )
        if not previous:
            conn.execute(script.decode())
            conn.execute(
                "INSERT INTO control.embedding_migration VALUES(1,%s)", (checksum,)
            )
        if conn.execute(
            "SELECT extversion FROM pg_extension WHERE extname='vector'"
        ).fetchone() != ("0.8.7",):
            raise ValueError("pgvector 版本必须为 0.8.7")
    return {
        "status": "passed",
        "migration": 1,
        "sha256": checksum,
        "reused": previous is not None,
    }


def check_environment(conn, test_only):
    from driving_data_platform.embedding import deployment as d

    state = d.CURRENT.get()
    if state is not None:
        d.validate_database(conn, state)
        if test_only:
            raise ValueError("正式和恢复演练环境禁止人工测试空间")
        return
    name = trial_database(conn)
    row = conn.execute(
        "SELECT test_only_allowed FROM control.embedding_environment WHERE singleton AND database_name=%s",
        (name,),
    ).fetchone()
    if row is None or (test_only and row != (True,)):
        raise ValueError("向量操作只能用于当前明确登记的独立试验数据库")


def load(root, conn, item_id, *, fail=False, ack_fail=False):
    complete, source, space, rows = verify_artifact(root, item_id)
    space_id = validate_space(space)
    check_environment(conn, space["test_only"])
    actual = media_input(conn, source["generation"])
    if source != actual:
        raise ValueError("输入清单与数据库图片登记不一致")
    eg = "e_" + item_id
    # 外层调用应使用 autocommit；确保方法返回前事务真实提交。
    if not conn.autocommit:
        raise ValueError("向量写操作要求独立 autocommit 连接，由业务函数管理事务")
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (int(item_id[:15], 16),))
        prior = conn.execute(
            "SELECT status,manifest FROM control.embedding_generation WHERE embedding_generation=%s",
            (eg,),
        ).fetchone()
        if prior:
            if prior != ("READY", complete):
                raise ValueError("向量版本身份冲突或未就绪")
            return {"status": "passed", "embedding_generation": eg, "reused": True}
        conn.execute(
            "INSERT INTO control.embedding_space VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (space_id, Jsonb(space), space["dimension"], space["test_only"]),
        )
        if conn.execute(
            "SELECT contract FROM control.embedding_space WHERE space_id=%s",
            (space_id,),
        ).fetchone() != (space,):
            raise ValueError("向量空间契约冲突")
        conn.execute(
            "INSERT INTO control.embedding_generation VALUES(%s,%s,%s,%s,%s,%s,'LOADING',%s)",
            (
                eg,
                source["generation"],
                space_id,
                item_id,
                Jsonb(complete),
                Jsonb(source),
                len(rows),
            ),
        )
        with conn.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO serving.image_embedding VALUES(%s,%s,%s,%s,%s,%s::vector)",
                [
                    (
                        eg,
                        source["generation"],
                        space_id,
                        r["media_id"],
                        r["sha256"],
                        json.dumps(r["vector"], allow_nan=False),
                    )
                    for r in rows
                ],
            )
        if fail:
            raise ValueError("fault: embedding-before-ready")
        returned = conn.execute(
            "SELECT media_id,image_sha256,embedding::text FROM serving.image_embedding WHERE embedding_generation=%s ORDER BY media_id",
            (eg,),
        ).fetchall()
        if [(r["media_id"], r["sha256"], r["vector"]) for r in rows] != [
            (i, s, vector32(json.loads(v), space["dimension"])) for i, s, v in returned
        ]:
            raise ValueError("数据库向量回读不一致")
        conn.execute(
            "UPDATE control.embedding_generation SET status='READY' WHERE embedding_generation=%s",
            (eg,),
        )
    if ack_fail:
        raise psycopg.OperationalError(
            "injected commit acknowledgement lost; verify by identity"
        )
    return {"status": "passed", "embedding_generation": eg, "reused": False}


def show(conn, eg):
    identity(eg, r"e_[a-f0-9]{64}")
    row = conn.execute(
        "SELECT generation,space_id,artifact_id,status,count,manifest FROM control.embedding_generation WHERE embedding_generation=%s",
        (eg,),
    ).fetchone()
    if row is None:
        raise ValueError("向量版本不存在")
    return {
        "status": "passed",
        "embedding_generation": eg,
        **dict(
            zip(
                (
                    "generation",
                    "space_id",
                    "artifact_id",
                    "version_status",
                    "count",
                    "manifest",
                ),
                row,
            )
        ),
    }


def publish(conn, eg, expected_revision, *, rollback=False):
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("必须指定非负 expected-revision")
    if not conn.autocommit:
        raise ValueError("向量发布要求独立 autocommit 连接")
    value = show(conn, eg)
    if value["version_status"] != "READY":
        raise ValueError("只能发布 READY 向量版本")
    trial = conn.execute(
        "SELECT test_only FROM control.embedding_space WHERE space_id=%s",
        (value["space_id"],),
    ).fetchone()[0]
    check_environment(conn, trial)
    with conn.transaction():
        conn.execute(
            "INSERT INTO control.embedding_active(generation) VALUES(%s) ON CONFLICT DO NOTHING",
            (value["generation"],),
        )
        old = conn.execute(
            "SELECT embedding_generation,revision FROM control.embedding_active WHERE generation=%s FOR UPDATE",
            (value["generation"],),
        ).fetchone()
        if old[1] != expected_revision:
            raise PlatformError(
                "PUBLICATION_CONFLICT",
                "embedding-pointer-changed",
                "向量活动指针已变化，禁止自动刷新预期 revision",
            )
        conn.execute(
            "UPDATE control.embedding_active SET embedding_generation=%s,revision=revision+1 WHERE generation=%s",
            (eg, value["generation"]),
        )
        conn.execute(
            "INSERT INTO control.embedding_publication VALUES(%s,%s,%s,%s,%s,%s)",
            (
                uuid.uuid4().hex,
                value["generation"],
                old[0],
                eg,
                old[1] + 1,
                "rollback" if rollback else "publish",
            ),
        )
    return {
        "status": "passed",
        "embedding_generation": eg,
        "generation": value["generation"],
        "revision": old[1] + 1,
    }


def search(
    conn,
    generation,
    space_id,
    query,
    *,
    embedding_generation=None,
    conditions=None,
    channel=None,
    limit=20,
):
    """内部函数：向量必须来自已确认空间；先按业务条件限定，再精确排序。"""
    from driving_data_platform.api.app import where

    identity(generation, r"g_[a-f0-9]{64}")
    identity(space_id, r"[a-f0-9]{64}")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit 必须在 1–100")
    if embedding_generation is None:
        pointer = conn.execute(
            "SELECT embedding_generation FROM control.embedding_active WHERE generation=%s",
            (generation,),
        ).fetchone()
        if not pointer or pointer[0] is None:
            raise ValueError("该业务版本尚未建立检索数据")
        embedding_generation = pointer[0]
    meta = show(conn, embedding_generation)
    if (
        meta["generation"] != generation
        or meta["space_id"] != space_id
        or meta["version_status"] != "READY"
    ):
        raise ValueError("业务版本、向量版本或空间不兼容")
    contract = conn.execute(
        "SELECT contract FROM control.embedding_space WHERE space_id=%s", (space_id,)
    ).fetchone()[0]
    check_environment(conn, contract["test_only"])
    query = vector32(query, contract["dimension"])
    conditions = conditions or {
        "location": None,
        "start_us": None,
        "end_us": None,
        "category": None,
        "channels": [],
    }
    clause, values = where(generation, conditions)
    extra = " AND m.channel=%s" if channel is not None else ""
    rows = conn.execute(
        "SELECT v.media_id,m.frame_id,m.channel,f.scene_id,m.payload->>'path',f.timestamp_us,f.location,1-(v.embedding <=> %s::vector) AS score FROM serving.image_embedding v JOIN serving.media m ON m.generation=v.generation AND m.entity_id=v.media_id JOIN serving.frame f ON f.generation=m.generation AND f.entity_id=m.frame_id WHERE v.embedding_generation=%s AND v.space_id=%s AND "
        + clause
        + extra
        + " ORDER BY v.embedding <=> %s::vector,v.media_id LIMIT %s",
        [
            json.dumps(query),
            embedding_generation,
            space_id,
            *values,
            *([channel] if channel is not None else []),
            json.dumps(query),
            limit,
        ],
    ).fetchall()
    return {
        "generation": generation,
        "embedding_generation": embedding_generation,
        "space_id": space_id,
        "items": [
            dict(
                zip(
                    (
                        "media_id",
                        "frame_id",
                        "channel",
                        "scene_id",
                        "path",
                        "timestamp_us",
                        "location",
                        "score",
                    ),
                    r,
                )
            )
            for r in rows
        ],
    }


def migrate_deployment(conn, repo, state):
    """新部署使用原始001和增量002/003；迁移与登记在同一事务提交。"""
    from driving_data_platform.embedding import deployment as d

    d.validate_database(conn, state, registered=False)
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(877801)")
        if conn.execute("SELECT to_regclass('serving.media')").fetchone()[0] is None:
            raise ValueError("部署数据库缺少业务表")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS control.embedding_migration(version integer PRIMARY KEY,sha256 text NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS control.embedding_environment(singleton boolean PRIMARY KEY CHECK(singleton),database_name text NOT NULL,test_only_allowed boolean NOT NULL)"
        )
        environment = conn.execute(
            "SELECT database_name,test_only_allowed FROM control.embedding_environment WHERE singleton"
        ).fetchone()
        if environment is not None and environment != (state["database"], False):
            raise ValueError("禁止接管试验登记或其他数据库的恢复副本")
        conn.execute(
            "INSERT INTO control.embedding_environment VALUES(true,%s,false) ON CONFLICT DO NOTHING",
            (state["database"],),
        )
        for version, name in (
            (1, "001_embedding.sql"),
            (2, "002_embedding_deployment.sql"),
            (3, "003_embedding_deployment_guard.sql"),
        ):
            content = (repo / "sql/postgres/migrations" / name).read_bytes()
            checksum = hashlib.sha256(content).hexdigest()
            previous = conn.execute(
                "SELECT sha256 FROM control.embedding_migration WHERE version=%s",
                (version,),
            ).fetchone()
            if previous and previous != (checksum,):
                raise ValueError("已执行的向量迁移 SQL 摘要漂移")
            if not previous:
                conn.execute(content.decode())
                conn.execute(
                    "INSERT INTO control.embedding_migration VALUES(%s,%s)",
                    (version, checksum),
                )
        expected = (
            state["deployment_id"],
            state["kind"],
            state["database"],
            state["database_oid"],
            state["system_identifier"],
        )
        old = conn.execute(
            "SELECT deployment_id,kind,database_name,database_oid,system_identifier FROM control.embedding_deployment WHERE singleton"
        ).fetchone()
        if old is not None and old != expected:
            raise ValueError("部署身份已被其他环境登记")
        conn.execute(
            "INSERT INTO control.embedding_deployment VALUES(true,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            expected,
        )
        if conn.execute(
            "SELECT extversion FROM pg_extension WHERE extname='vector'"
        ).fetchone() != ("0.8.7",):
            raise ValueError("pgvector版本必须为0.8.7")
        d.validate_database(conn, state)
    return {"status": "passed", "migration": 3, "deployment_id": state["deployment_id"]}
