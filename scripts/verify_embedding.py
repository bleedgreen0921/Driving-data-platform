"""真实 pgvector 隔离验收；人工测试向量绝不用于正式检索。"""

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import psycopg
from embedding_ops import trial_state, trial_stop, trial_up
from psycopg import sql
from verify_backup import TABLES, fingerprint, metastore_fingerprint

from driving_data_platform.common.config import (
    application_config,
    output_root,
    port,
)
from driving_data_platform.common.errors import PlatformError
from driving_data_platform.common.operations import operation_lock
from driving_data_platform.common.resources import PROJECT_DISK_GIB
from driving_data_platform.embedding import artifacts, store
from driving_data_platform.ingestion.nuscenes import atomic_json
from driving_data_platform.publishing.store import active, connect
from driving_data_platform.warehouse.catalog import command


def test_space(dimension=3, suffix="v1"):
    """只供独立数据库测试；不是实际 Qwen 推理结果。"""
    return {
        "schema_version": 1,
        "model": "synthetic-test-only",
        "revision": suffix,
        "model_sha256": "1" * 64,
        "projector_sha256": "2" * 64,
        "runtime": "synthetic-test",
        "runtime_version": "1",
        "quantization": "none",
        "image_preprocessing": {"synthetic": True},
        "text_instruction": "test",
        "image_instruction": "test",
        "pooling": "test",
        "dimension": dimension,
        "dtype": "float32",
        "normalization": "l2",
        "distance": "cosine",
        "test_only": True,
    }


def reject(function, *args, **kwargs):
    try:
        function(*args, **kwargs)
    except (ValueError, psycopg.Error, PlatformError):
        return
    raise AssertionError("预期拒绝却成功")


def verify_constraints(conn, root, repo, item_id):
    """用真实数据库逐项核验拒绝行为；所有试验行在外层事务回滚。"""
    import hashlib

    from psycopg.types.json import Jsonb

    complete, source, space, rows = artifacts.verify_artifact(root, item_id)
    space_id = artifacts.validate_space(space)
    unique = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    eg = "e_" + unique

    def attempt(statement, params=None):
        with conn.transaction():
            conn.execute(statement, params)

    with conn.transaction(force_rollback=True):
        statement = (
            "INSERT INTO control.embedding_generation VALUES(%s,%s,%s,%s,%s,%s,%s,%s)"
        )
        fields = (
            eg,
            source["generation"],
            space_id,
            unique,
            Jsonb(complete),
            Jsonb(source),
        )
        reject(attempt, statement, (*fields, "READY", len(rows)))
        attempt(statement, (*fields, "LOADING", len(rows)))
        first = rows[0]
        insert = "INSERT INTO serving.image_embedding VALUES(%s,%s,%s,%s,%s,%s::vector)"
        prefix = (eg, source["generation"], space_id)
        for media_id, sha, vector in (
            (first["media_id"], "0" * 64, "[1,0,0]"),
            ("f" * 64, first["sha256"], "[1,0,0]"),
            (first["media_id"], first["sha256"], "[1,0]"),
            (first["media_id"], first["sha256"], "[2,0,0]"),
            (first["media_id"], first["sha256"], "[NaN,0,0]"),
        ):
            reject(attempt, insert, (*prefix, media_id, sha, vector))
        attempt(insert, (*prefix, first["media_id"], first["sha256"], "[0.6,0.8,0]"))
        reject(
            attempt,
            "UPDATE control.embedding_generation SET status='READY' WHERE embedding_generation=%s",
            (eg,),
        )
        conn.execute(
            "UPDATE control.embedding_environment SET test_only_allowed=false WHERE singleton"
        )
        reject(store.load, root, conn, item_id)
        reject(store.publish, conn, "e_" + item_id, 0)
    # 漂移测试只修改仓库外复制的 SQL，不修改实际迁移。
    drift = root / "reports" / ("embedding-migration-drift-" + uuid.uuid4().hex)
    (drift / "sql/postgres/migrations").mkdir(parents=True)
    (drift / "sql/postgres/migrations/001_embedding.sql").write_bytes(
        (repo / "sql/postgres/migrations/001_embedding.sql").read_bytes()
        + b"\n-- drift\n"
    )
    reject(store.migrate, conn, drift, test_database=True)
    return [
        "数据库拒绝 READY 直接插入、缺向量、非法维度/归一化/NaN、图片摘要及媒体身份冲突",
        "未登记环境拒绝测试产物装载与发布，迁移 SQL 摘要漂移阻断",
    ]


def verify(root, repo, faults=False, trial=None):
    trial = trial or uuid.uuid4().hex
    directory = root / "reports" / ("embedding-" + uuid.uuid4().hex)
    directory.mkdir()
    report = {
        "status": "running",
        "trial": trial,
        "checks": [],
        "not_executed": [
            "真实模型编码",
            "多模态图文检索效果",
            "搜索HTTP与页面",
            "生产数据库切换",
            "持续并发观察",
        ],
    }
    process = None
    started = False

    def check(label):
        report["checks"].append(label)
        atomic_json(directory / "report.json", report)
        print(label, flush=True)

    def dump(base, database, destination, snapshot=None):
        with destination.open("wb") as stream:
            subprocess.run(
                [
                    *base,
                    "pg_dump",
                    "-U",
                    "ddp",
                    "-d",
                    database,
                    "-Fc",
                    *(["--snapshot", snapshot] if snapshot else []),
                ],
                stdout=stream,
                check=True,
            )

    def restore(base, database, source):
        with source.open("rb") as stream:
            subprocess.run(
                [
                    *base,
                    "pg_restore",
                    "-U",
                    "ddp",
                    "--exit-on-error",
                    "--no-owner",
                    "--no-acl",
                    "-d",
                    database,
                ],
                stdin=stream,
                check=True,
            )

    try:
        with connect(root) as conn:
            baseline = fingerprint(conn)
            pointer = active(conn)
            source = artifacts.media_input(conn, pointer["generation"])
            old = conn.execute(
                "SELECT generation FROM control.generation WHERE status='READY' AND generation<>%s ORDER BY created_at LIMIT 1",
                (pointer["generation"],),
            ).fetchone()[0]
            captured = artifacts.capture(
                root,
                conn,
                pointer["generation"],
                verify_files=True,
                dataset_root=application_config(root)["dataset"]["root"],
            )
            assert captured["count"] == 2424
            assert artifacts.capture(root, conn, pointer["generation"])["reused"]
        report["input"] = captured
        check("真实 2424 张图片清单、全量文件核验与身份复用")
        from usage_report import usage

        before_usage = usage(root, repo)
        if before_usage["status"] != "passed":
            report["resources"] = before_usage
            raise ValueError("项目占用已达到预算，停止扩大隔离验证")
        report["resources_before"] = before_usage
        # 已保留的试验卷复用；新建完整恢复库先为数据库、WAL 和备份预留空间。
        existing = (root / "embedding/trials" / trial / "connection.json").exists()
        with connect(root) as source_conn:
            database_bytes = source_conn.execute(
                "SELECT pg_database_size(current_database())"
            ).fetchone()[0]
        planned_growth = 256 * 1024**2 if existing else database_bytes + 1024**3
        report["planned_growth_bytes"] = planned_growth
        if (
            before_usage["project_bytes_conservative"] + planned_growth
            > PROJECT_DISK_GIB * 1024**3
        ):
            raise ValueError(
                "预计新增恢复数据超过项目预算，请复用已登记 trial 或调整资源后再验证"
            )
        started = True
        trial_up(root, repo, trial)
        state = trial_state(root, trial)
        service = [*command(root, repo), "exec", "-T", "postgres"]
        target = ["docker", "exec", "-i", state["container"]]
        # 只读正式库；重验可以复用隔离卷，不累积完整数据库副本。
        with store.connect(root, trial=trial) as conn:
            exists = (
                conn.execute("SELECT to_regclass('control.generation')").fetchone()[0]
                is not None
            )
        if not exists:
            dump(service, "ddp", directory / "service.dump")
            restore(target, state["database"], directory / "service.dump")
        hms_database = "ddp_embedding_hms_" + trial
        with store.connect(root, trial=trial) as conn:
            assert {
                k: v for k, v in fingerprint(conn).items() if k in TABLES
            } == baseline
            has_hms = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname=%s", (hms_database,)
            ).fetchone()
            if has_hms:
                conn.execute(
                    sql.SQL("DROP DATABASE {}").format(sql.Identifier(hms_database))
                )
            conn.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(hms_database))
            )
        # HMS 会更新锁及 housekeeping 表；导出快照确保备份和指纹对应同一时点。
        with connect(root, database="ddp_metastore_v410") as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            snapshot = conn.execute("SELECT pg_export_snapshot()").fetchone()[0]
            hms_baseline = metastore_fingerprint(conn)
            dump(service, "ddp_metastore_v410", directory / "metastore.dump", snapshot)
        restore(target, hms_database, directory / "metastore.dump")
        with psycopg.connect(
            host=state["host"],
            port=state["port"],
            dbname=hms_database,
            user="ddp",
            password=state["password"],
        ) as conn:
            assert metastore_fingerprint(conn) == hms_baseline
        check("真实服务库与 HMS 库隔离恢复，全表指纹一致")
        with store.connect(root, trial=trial) as conn:
            store.migrate(conn, repo, test_database=True)
            assert store.migrate(conn, repo, test_database=True)["reused"]
            reject(store.migrate, conn, repo)
            assert (
                conn.execute(
                    "SELECT %s::vector <=> %s::vector", ("[1,0,0]", "[0,1,0]")
                ).fetchone()[0]
                == 1
            )
            v = json.dumps([1.0] + [0.0] * 2047)
            assert conn.execute(
                "SELECT vector_dims(%s::vector), %s::vector <=> %s::vector", (v, v, v)
            ).fetchone() == (2048, 0.0)
        check("迁移、幂等、三维距离和 2048 维精确计算")
        space = test_space()
        vectors = [
            {
                "media_id": e["media_id"],
                "sha256": e["sha256"],
                "vector": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]][i % 3],
            }
            for i, e in enumerate(source["entries"])
        ]
        artifact = artifacts.create_artifact(
            root,
            source,
            space,
            vectors,
            processing_version="verify-" + directory.name[-32:],
        )["artifact_id"]
        report["test_artifact_id"] = artifact
        other = artifacts.create_artifact(
            root, source, test_space(suffix="v2"), vectors
        )["artifact_id"]
        with store.connect(root, trial=trial) as conn:
            count_before = conn.execute(
                "SELECT count(*) FROM control.embedding_generation"
            ).fetchone()[0]
            if faults:
                reject(store.load, root, conn, artifact, fail=True)
                assert (
                    conn.execute(
                        "SELECT count(*) FROM control.embedding_generation"
                    ).fetchone()[0]
                    == count_before
                )
                reject(store.load, root, conn, artifact, ack_fail=True)
                # 重新连接核实提交确认丢失，而不是推测事务是否成功。
            else:
                store.load(root, conn, artifact)
        with store.connect(root, trial=trial) as conn:
            loaded = store.load(root, conn, artifact)
            assert loaded["reused"]
            eg = loaded["embedding_generation"]
            assert store.show(conn, eg)["version_status"] == "READY"
            report["constraint_checks"] = verify_constraints(conn, root, repo, artifact)
            check(
                "完整测试产物事务装载、逐值回读、重复复用及确认丢失恢复"
                if faults
                else "完整测试产物事务装载、逐值回读与重复复用"
            )
            pointer_row = conn.execute(
                "SELECT revision FROM control.embedding_active WHERE generation=%s",
                (source["generation"],),
            ).fetchone()
            revision = pointer_row[0] if pointer_row else 0
            store.publish(conn, eg, revision)
            reject(store.publish, conn, eg, revision)
            second = store.load(root, conn, other)["embedding_generation"]

            def compete(_):
                with store.connect(root, trial=trial) as client:
                    try:
                        store.publish(client, second, revision + 1)
                        return "passed"
                    except PlatformError:
                        return "conflict"

            with ThreadPoolExecutor(max_workers=2) as pool:
                assert sorted(pool.map(compete, range(2))) == ["conflict", "passed"]
            assert (
                store.publish(conn, eg, revision + 2, rollback=True)["revision"]
                == revision + 3
            )
            result = store.search(
                conn,
                source["generation"],
                artifacts.validate_space(space),
                [1.0, 0.0, 0.0],
                limit=100,
            )
            ids = [r["media_id"] for r in result["items"]]
            assert ids == [v["media_id"] for v in vectors if v["vector"][0] == 1][:100]
            assert all(r["score"] == 1 for r in result["items"])
            reject(
                store.search,
                conn,
                old,
                artifacts.validate_space(space),
                [1.0, 0.0, 0.0],
            )
            reject(
                store.search,
                conn,
                source["generation"],
                artifacts.validate_space(test_space(suffix="v2")),
                [1.0, 0.0, 0.0],
            )
            reject(
                store.search,
                conn,
                source["generation"],
                artifacts.validate_space(space),
                [0.0, 0.0, 0.0],
            )
            from driving_data_platform.api.app import filters

            selected = conn.execute(
                "SELECT f.location,v.media_id,m.channel FROM serving.image_embedding v JOIN serving.media m ON m.generation=v.generation AND m.entity_id=v.media_id JOIN serving.frame f ON f.generation=m.generation AND f.entity_id=m.frame_id WHERE v.embedding_generation=%s ORDER BY v.media_id LIMIT 1",
                (eg,),
            ).fetchone()
            filtered = store.search(
                conn,
                source["generation"],
                artifacts.validate_space(space),
                [1.0, 0.0, 0.0],
                conditions=filters(selected[0], None, None, None, []),
                channel=selected[2],
                limit=100,
            )
            assert all(r["channel"] == selected[2] for r in filtered["items"])
            assert (
                store.search(
                    conn,
                    source["generation"],
                    artifacts.validate_space(space),
                    [1.0, 0.0, 0.0],
                    conditions=filters("not-a-location", None, None, None, []),
                )["items"]
                == []
            )
            reject(
                conn.execute,
                "UPDATE serving.image_embedding SET image_sha256=%s WHERE embedding_generation=%s",
                ("0" * 64, eg),
            )
            reject(
                conn.execute,
                "DELETE FROM control.embedding_generation WHERE embedding_generation=%s",
                (eg,),
            )
            reject(conn.execute, "UPDATE control.embedding_space SET dimension=4")
            # 即使维度相同也必须通过空间身份检查。
            reject(
                store.search,
                conn,
                source["generation"],
                artifacts.validate_space(space),
                [1.0, 0.0, 0.0],
                embedding_generation=second,
            )
            backup_fingerprint = fingerprint(conn)
            report["vector_fingerprints"] = backup_fingerprint
        check("发布竞争、回滚、不可变保护、同维不同空间拒绝、业务筛选与稳定精确排序")
        dump(target, state["database"], directory / "with-embeddings.dump")
        # 串行重建本轮独立试验库。已留完整备份，不清理任何正式库或其他试验卷。
        with psycopg.connect(
            host=state["host"],
            port=state["port"],
            dbname="postgres",
            user="ddp",
            password=state["password"],
            autocommit=True,
        ) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE {}").format(sql.Identifier(state["database"]))
            )
            admin.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(state["database"]))
            )
        restore(target, state["database"], directory / "with-embeddings.dump")
        with store.connect(root, trial=trial) as conn:
            assert fingerprint(conn) == backup_fingerprint
            old_name = "ddp_embedding_trial_" + "0" * 32
            conn.execute(
                "UPDATE control.embedding_environment SET database_name=%s WHERE singleton",
                (old_name,),
            )
            reject(
                store.search,
                conn,
                source["generation"],
                artifacts.validate_space(space),
                [1.0, 0.0, 0.0],
            )
            store.migrate(
                conn, repo, test_database=True, restore_from_database=old_name
            )
            assert store.search(
                conn,
                source["generation"],
                artifacts.validate_space(space),
                [1.0, 0.0, 0.0],
            )["items"]
            conn.execute("CHECKPOINT")
            for _ in range(64):
                if (
                    conn.execute("SELECT count(*) FROM pg_ls_waldir()").fetchone()[0]
                    <= 8
                ):
                    break
                for _ in range(8):
                    conn.execute("SELECT pg_switch_wal()")
                conn.execute("CHECKPOINT")
        subprocess.run(
            ["docker", "restart", state["container"]], check=True, capture_output=True
        )
        for _ in range(60):
            try:
                with store.connect(root, trial=trial) as conn:
                    assert fingerprint(conn) == backup_fingerprint
                    break
            except psycopg.OperationalError:
                time.sleep(1)
        else:
            raise ValueError("重启后连接未恢复")
        check("含向量备份恢复、显式恢复环境登记、重启持久化")
        from psycopg.conninfo import make_conninfo

        dsn = make_conninfo(
            host=state["host"],
            port=state["port"],
            dbname=state["database"],
            user=state["user"],
            password=state["password"],
        )
        env = dict(
            os.environ,
            DDP_POSTGRES_DSN=dsn,
            DDP_OUTPUT_ROOT=str(root),
            DDP_API_BASE=("http://127.0.0.1:" + str(port("trial_api"))),
        )
        with (directory / "api-runtime.log").open("w") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "driving_data_platform.api.app:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    (str(port("trial_api"))),
                ],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        for _ in range(60):
            try:
                with urlopen(
                    ("http://127.0.0.1:" + str(port("trial_api")) + "/api/health"),
                    timeout=1,
                ) as response:
                    assert json.load(response)["generation"] == pointer["generation"]
                break
            except (URLError, TimeoutError):
                if process.poll() is not None:
                    raise ValueError("隔离 API 启动失败") from None
                time.sleep(0.5)
        else:
            raise ValueError("隔离 API 健康检查超时")
        with (directory / "api-regression.log").open("w") as log:
            subprocess.run(
                [sys.executable, "scripts/verify_api.py"],
                env=env,
                check=True,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        check("pgvector 隔离恢复数据上的既有真实 API 回归")
        with connect(root) as conn:
            assert fingerprint(conn) == baseline and active(conn) == pointer
            assert (
                conn.execute(
                    "SELECT extname FROM pg_extension WHERE extname='vector'"
                ).fetchone()
                is None
            )
        with connect(root, database="ddp_metastore_v410") as conn:
            after_hms = metastore_fingerprint(conn)
            changed = [k for k in after_hms if after_hms[k] != hms_baseline.get(k)]
            if set(changed) - {"AUX_TABLE", "HIVE_LOCKS"}:
                raise ValueError("HMS 静态元数据在验证期间变化")
            report["hms_runtime_tables_changed"] = changed
        check("正式业务指纹、HMS 静态元数据、活动指针和扩展状态保持不变")
        report["resources_after"] = usage(root, repo)
        if report["resources_after"]["status"] != "passed":
            raise ValueError("隔离验证后资源门禁未通过，停止扩大部署")
        report["status"] = "passed"
    except BaseException:
        report["status"] = "failed"
        raise
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if started:
            trial_stop(root, trial)
        report["retention"] = "保留隔离卷、镜像、备份与报告；移除本轮隔离容器和 API"
        atomic_json(directory / "report.json", report)
        print(
            json.dumps(
                {"status": report["status"], "directory": str(directory)},
                ensure_ascii=False,
            )
        )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--faults", action="store_true")
    parser.add_argument("--trial")
    args = parser.parse_args()
    root = output_root()
    with operation_lock(root):
        verify(root, Path(__file__).resolve().parents[1], args.faults, args.trial)


if __name__ == "__main__":
    main()
