"""PostgreSQL 控制与不可变服务版本；所有查询参数化。"""

import json
import os
import uuid
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from driving_data_platform.common.errors import PlatformError
from driving_data_platform.common.preflight import digest
from driving_data_platform.warehouse.build import reconcile, rows, verify


def connect(root: Path, *, database=None):
    dsn = os.environ.get("DDP_POSTGRES_DSN")
    if dsn and database is None:
        return psycopg.connect(dsn, connect_timeout=5)
    env = dict(
        line.split("=", 1) for line in (root / "compose.env").read_text().splitlines()
    )
    return psycopg.connect(
        host="127.0.0.1",
        port=55433,
        dbname=database or "ddp",
        user="ddp",
        password=env["DDP_POSTGRES_PASSWORD"],
        connect_timeout=5,
    )


def initialize(root: Path, repo: Path):
    with connect(root) as conn:
        conn.execute((repo / "sql/postgres/schema.sql").read_text())


def active(conn):
    generation, revision = conn.execute(
        "SELECT generation, revision FROM control.active WHERE singleton=true"
    ).fetchone()
    return {"generation": generation, "revision": revision}


def load(root: Path, directory: Path, *, fail=False, fail_after_commit=False):
    manifest = verify(directory)
    source = (
        root / "ingestion/snapshots" / (manifest["snapshot_id"] + "-nuscenes-ods-v1")
    )
    if digest(source / "complete.json") != manifest["input_sha256"]:
        raise ValueError("装载输入摘要不匹配")
    reconcile(source, directory)
    checksum = digest(directory / "complete.json")
    generation = "g_" + checksum
    attempt = uuid.uuid4().hex
    with connect(root) as conn:
        conn.execute(
            "INSERT INTO control.task(task_id) VALUES(%s) ON CONFLICT DO NOTHING",
            (checksum,),
        )
        conn.execute(
            "INSERT INTO control.attempt(attempt_id,task_id,status) VALUES(%s,%s,'RUNNING')",
            (attempt, checksum),
        )
    try:
        with connect(root) as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (int(checksum[:15], 16),))
            found = conn.execute(
                "SELECT manifest_sha256,status FROM control.generation WHERE generation=%s",
                (generation,),
            ).fetchone()
            if found:
                if found != (checksum, "READY"):
                    raise ValueError("版本身份冲突或尚未就绪")
                reused = True
            else:
                reused = False
                quality = json.loads((source / "quality.json").read_text())
                conn.execute(
                    "INSERT INTO control.generation(generation,snapshot_id,processing_version,status,manifest_sha256,manifest,quality) VALUES(%s,%s,%s,'LOADING',%s,%s,%s)",
                    (
                        generation,
                        manifest["snapshot_id"],
                        manifest["processing_version"],
                        checksum,
                        Jsonb(manifest),
                        Jsonb(quality),
                    ),
                )
                files = {
                    f["path"]: f
                    for f in json.loads((source / "file-manifest.json").read_text())
                }
                datasets = [
                    ("dim_scene", "scene", ("entity_id",)),
                    (
                        "fact_frame",
                        "frame",
                        ("entity_id", "timestamp_us", "scene_id", "location"),
                    ),
                    (
                        "fact_media",
                        "media",
                        (
                            "entity_id",
                            "frame_id",
                            "is_key_frame",
                            "channel",
                            "modality",
                        ),
                    ),
                    (
                        "fact_annotation",
                        "annotation",
                        ("entity_id", "frame_id", "category"),
                    ),
                    ("frame_category", "frame_category", ("frame_id", "category")),
                    ("frame_channel", "frame_channel", ("frame_id", "channel")),
                    ("statistics", "statistics", ("dimension", "value")),
                ]
                for name, table, fields in datasets:
                    values = rows(directory, name)
                    has_payload = table not in {"frame_category", "frame_channel"}
                    columns = [
                        "generation",
                        *fields,
                        *(["payload"] if has_payload else []),
                    ]
                    statement = sql.SQL(
                        "INSERT INTO serving.{} ({}) VALUES ({})"
                    ).format(
                        sql.Identifier(table),
                        sql.SQL(",").join(map(sql.Identifier, columns)),
                        sql.SQL(",").join(sql.Placeholder() for _ in columns),
                    )
                    entries = []
                    for row in values:
                        if table == "media":
                            row = row | {
                                "bytes": files[row["path"]]["bytes"],
                                "sha256": files[row["path"]]["sha256"],
                            }
                        entries.append(
                            (
                                generation,
                                *(row[f] for f in fields),
                                *([Jsonb(row)] if has_payload else []),
                            )
                        )
                    with conn.cursor() as cursor:
                        cursor.executemany(statement, entries)
                    if conn.execute(
                        sql.SQL(
                            "SELECT count(*) FROM serving.{} WHERE generation=%s"
                        ).format(sql.Identifier(table)),
                        (generation,),
                    ).fetchone()[0] != len(values):
                        raise PlatformError(
                            "RECONCILIATION_FAILED",
                            "database-row-count",
                            "数据库行数不一致",
                        )
                    # 同一事务内从数据库回读，核对全部业务值，不能只核对行数。
                    if has_payload:
                        returned = [
                            r[0]
                            for r in conn.execute(
                                sql.SQL(
                                    "SELECT payload FROM serving.{} WHERE generation=%s"
                                ).format(sql.Identifier(table)),
                                (generation,),
                            )
                        ]
                        expected = [entry[-1].obj for entry in entries]
                        if sorted(
                            returned, key=lambda r: json.dumps(r, sort_keys=True)
                        ) != sorted(
                            expected, key=lambda r: json.dumps(r, sort_keys=True)
                        ):
                            raise PlatformError(
                                "RECONCILIATION_FAILED",
                                "database-rows",
                                "数据库明细不一致",
                            )
                    if fail and table == "frame":
                        raise ValueError("fault: load-after-frame")
                for name in ("dim_category", "dim_sensor", "dim_location", "metrics"):
                    for index, row in enumerate(rows(directory, name)):
                        conn.execute(
                            "INSERT INTO serving.dimension VALUES(%s,%s,%s,%s)",
                            (
                                generation,
                                name,
                                row.get("entity_id", row.get("location", str(index))),
                                Jsonb(row),
                            ),
                        )
                conn.execute(
                    "UPDATE control.generation SET status='READY' WHERE generation=%s",
                    (generation,),
                )
            conn.execute(
                "UPDATE control.attempt SET status='SUCCEEDED',finished_at=now() WHERE attempt_id=%s",
                (attempt,),
            )
        if fail_after_commit:
            raise psycopg.OperationalError("fault: acknowledgement-lost-after-commit")
    except (ValueError, psycopg.Error):
        # 提交结果未知时先查询确定版本是否存在；不盲目追加数据。
        with connect(root) as conn:
            known = conn.execute(
                "SELECT status FROM control.generation WHERE generation=%s",
                (generation,),
            ).fetchone()
            conn.execute(
                "UPDATE control.attempt SET status=%s,error_code=%s,finished_at=now() WHERE attempt_id=%s",
                (
                    "SUCCEEDED" if known == ("READY",) else "FAILED",
                    "load-interrupted",
                    attempt,
                ),
            )
        if known != ("READY",):
            raise
        reused = True
    return {
        "status": "passed",
        "generation": generation,
        "reused": reused,
        "attempt_id": attempt,
    }


def publish(root: Path, generation: str, expected: dict, *, rollback=False):
    from driving_data_platform.publishing.difference import difference

    summary = difference(root, generation, expected)
    with connect(root) as conn:
        if conn.execute(
            "SELECT status FROM control.generation WHERE generation=%s", (generation,)
        ).fetchone() != ("READY",):
            raise ValueError("只能发布 READY 版本")
        result = conn.execute(
            "UPDATE control.active SET generation=%s,revision=revision+1 WHERE singleton=true AND generation IS NOT DISTINCT FROM %s AND revision=%s RETURNING revision",
            (generation, expected["generation"], expected["revision"]),
        ).fetchone()
        if result is None:
            raise PlatformError(
                "PUBLICATION_CONFLICT",
                "active-pointer-changed",
                "活动指针已改变；本次发布竞争失败",
            )
        conn.execute(
            "INSERT INTO control.publication(event_id,previous_generation,generation,revision,action) VALUES(%s,%s,%s,%s,%s)",
            (
                uuid.uuid4().hex,
                expected["generation"],
                generation,
                result[0],
                "rollback" if rollback else "publish",
            ),
        )
    return {
        "status": "passed",
        "generation": generation,
        "revision": result[0],
        "difference_report": summary["report"],
    }
