"""备份到仓库外并恢复到独立试验数据库，默认保留试验数据库。"""

import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path

from psycopg import sql

from driving_data_platform.common.identity import canonical
from driving_data_platform.ingestion.nuscenes import atomic_json
from driving_data_platform.publishing.store import connect
from driving_data_platform.warehouse.catalog import command as catalog_command
from driving_data_platform.warehouse.catalog import job

TABLES = {
    "control.active": "singleton,generation,revision",
    "control.generation": "generation,snapshot_id,processing_version,status,manifest_sha256,manifest,quality",
    "control.publication": "event_id,previous_generation,generation,revision,action",
    "control.task": "task_id",
    "control.attempt": "attempt_id,task_id,status,error_code",
    "serving.scene": "generation,entity_id,payload",
    "serving.frame": "generation,entity_id,timestamp_us,scene_id,location,payload",
    "serving.media": "generation,entity_id,frame_id,is_key_frame,channel,modality,payload",
    "serving.annotation": "generation,entity_id,frame_id,category,payload",
    "serving.frame_category": "generation,frame_id,category",
    "serving.frame_channel": "generation,frame_id,channel",
    "serving.statistics": "generation,dimension,value,payload",
    "serving.dimension": "generation,kind,identity,payload",
}


def fingerprint(conn):
    values = {}
    for table, columns in TABLES.items():
        schema, name = table.split(".")
        query = sql.SQL("SELECT {} FROM {}.{}").format(
            sql.SQL(columns), sql.Identifier(schema), sql.Identifier(name)
        )
        rows = conn.execute(query).fetchall()
        encoded = sorted(canonical(row) for row in rows)
        values[table] = {
            "count": len(rows),
            "sha256": hashlib.sha256(b"\n".join(encoded)).hexdigest(),
        }
    return values


def verify_backup(root: Path, repo: Path):
    identity = "restore_" + uuid.uuid4().hex
    destination = root / "backups" / (identity + ".dump")
    command = [
        "docker",
        "compose",
        "--env-file",
        str(root / "compose.env"),
        "-f",
        str(repo / "infra/docker/compose.yaml"),
        "exec",
        "-T",
        "postgres",
    ]
    with connect(root) as conn:
        expected = fingerprint(conn)
    with destination.open("wb") as stream:
        subprocess.run(
            [*command, "pg_dump", "-U", "ddp", "-d", "ddp", "-Fc"],
            stdout=stream,
            check=True,
        )
    with connect(root) as conn:
        conn.autocommit = True
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(identity)))
    with destination.open("rb") as stream:
        subprocess.run(
            [
                *command,
                "pg_restore",
                "-U",
                "ddp",
                "--exit-on-error",
                "--no-owner",
                "--no-acl",
                "-d",
                identity,
            ],
            stdin=stream,
            check=True,
        )
    with connect(root, database=identity) as conn:
        restored = fingerprint(conn)
    if restored != expected:
        raise ValueError("独立库恢复后版本、指针或服务产物与源库不一致")
    metastore = verify_metastore_backup(root, repo)
    result = {
        "status": "passed",
        "metastore": metastore,
        "database": identity,
        "backup": str(destination),
        "tables": restored,
        "retention": "保留试验数据库和备份；不自动删除 generation。",
    }
    destination.with_suffix(".json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False)
    )
    return result


def metastore_fingerprint(conn):
    conn.execute("SET TIME ZONE 'UTC'")
    tables = conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
    ).fetchall()
    result = {}
    for (name,) in tables:
        rows = conn.execute(
            sql.SQL("SELECT row_to_json(t)::text FROM public.{} t").format(
                sql.Identifier(name)
            )
        ).fetchall()
        encoded = sorted(row[0].encode() for row in rows)
        result[name] = {
            "count": len(rows),
            "sha256": hashlib.sha256(b"\n".join(encoded)).hexdigest(),
        }
    return result


def verify_metastore_backup(root: Path, repo: Path):
    # 同时保留正常 HMS 与只读恢复验证 HMS，为 Docker VM 留至少 1 GiB。
    memory = int(
        subprocess.check_output(["docker", "info", "--format", "{{.MemTotal}}"])
    )
    if memory < int(7.5 * 1024**3):
        raise ValueError("元数据恢复验证资源门禁不通过，需要 Docker VM 至少 7.5 GiB")
    identity = "restore_hms_" + uuid.uuid4().hex
    destination = root / "backups" / (identity + ".dump")
    base = [*catalog_command(root, repo), "exec", "-T", "postgres"]
    with connect(root, database="ddp_metastore_v410") as conn:
        expected = metastore_fingerprint(conn)
    with destination.open("wb") as stream:
        subprocess.run(
            [*base, "pg_dump", "-U", "ddp", "-d", "ddp_metastore_v410", "-Fc"],
            stdout=stream,
            check=True,
        )
    with connect(root) as conn:
        conn.autocommit = True
        conn.execute(
            sql.SQL("CREATE DATABASE {} OWNER ddp_metastore").format(
                sql.Identifier(identity)
            )
        )
    with destination.open("rb") as stream:
        subprocess.run(
            [
                *base,
                "pg_restore",
                "-U",
                "ddp",
                "--role",
                "ddp_metastore",
                "--exit-on-error",
                "--no-owner",
                "--no-acl",
                "-d",
                identity,
            ],
            stdin=stream,
            check=True,
        )
    with connect(root, database=identity) as conn:
        actual = metastore_fingerprint(conn)
    if actual != expected:
        raise ValueError("元数据库恢复后的全表指纹不一致")
    with connect(root) as conn:
        reference_generation, manifest = conn.execute(
            "SELECT generation,manifest FROM control.generation WHERE generation=(SELECT generation FROM control.active)"
        ).fetchone()
        if not isinstance(manifest.get("catalog_contract"), dict):
            # 旧 JSON 版本仍可回滚；元数据库恢复验证使用已完成的正式目录版本。
            reference = conn.execute(
                "SELECT generation,manifest FROM control.generation WHERE status='READY' AND jsonb_typeof(manifest->'catalog_contract')='object' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if reference is None:
                raise ValueError("没有可用于元数据恢复验证的正式目录版本")
            reference_generation, manifest = reference
    catalog_id = manifest["catalog"]["database"].removeprefix("ddp_hms_")
    request = json.loads(
        (root / "metastore/generations" / catalog_id / "request.json").read_text()
    )
    trial = root / "metastore/trials" / uuid.uuid4().hex
    trial.mkdir()
    atomic_json(
        trial / "request.json", request | {"uri": "thrift://metastore-restore:9083"}
    )
    env = dict(os.environ, DDP_HMS_RESTORE_DATABASE=identity)
    command = catalog_command(root, repo)
    log = destination.with_suffix(".log")
    with log.open("w") as output:
        try:
            subprocess.run(
                [*command, "up", "-d", "--wait", "--no-deps", "metastore-restore"],
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
                check=True,
            )
            job(root, repo, trial, "check")
        finally:
            subprocess.run(
                [*command, "stop", "metastore-restore"],
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
                check=True,
            )
            subprocess.run(
                [*command, "rm", "-f", "metastore-restore"],
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
                check=True,
            )
    result = {
        "database": identity,
        "backup": str(destination),
        "tables": actual,
        "restored_hms_table_read": True,
        "reference_generation": reference_generation,
        "probe_directory": str(trial),
    }
    atomic_json(destination.with_suffix(".json"), result)
    return result


if __name__ == "__main__":
    root = Path(
        os.environ.get(
            "DDP_OUTPUT_ROOT",
            "/Users/qiujiajun/Project/driving-data-platform-artifacts",
        )
    )
    print(
        json.dumps(
            verify_backup(root, Path(__file__).resolve().parents[1]), ensure_ascii=False
        )
    )
