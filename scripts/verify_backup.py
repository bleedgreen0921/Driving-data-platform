"""备份到仓库外并恢复到独立试验数据库，默认保留试验数据库。"""

import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path

from psycopg import sql

from driving_data_platform.common.identity import canonical
from driving_data_platform.publishing.store import connect

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
    result = {
        "status": "passed",
        "database": identity,
        "backup": str(destination),
        "tables": restored,
        "retention": "保留试验数据库和备份；不自动删除 generation。",
    }
    destination.with_suffix(".json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False)
    )
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
