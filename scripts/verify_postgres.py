"""真实 PostgreSQL 读写与容器重建持久化，显式调用。"""

import argparse
import json
import os
import subprocess
import uuid
from pathlib import Path

import psycopg

ROOT = Path(
    os.environ.get(
        "DDP_OUTPUT_ROOT", "/Users/qiujiajun/Project/driving-data-platform-artifacts"
    )
)
REPO = Path(__file__).resolve().parents[1]


def connection():
    env = dict(
        line.split("=", 1) for line in (ROOT / "compose.env").read_text().splitlines()
    )
    return psycopg.connect(
        host="127.0.0.1",
        port=55433,
        dbname="ddp",
        user="ddp",
        password=env["DDP_POSTGRES_PASSWORD"],
        connect_timeout=5,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recreate", action="store_true")
    args = parser.parse_args()
    identity = "verify_" + uuid.uuid4().hex
    with connection() as conn:
        has_versions = (
            conn.execute("SELECT to_regclass('control.active')").fetchone()[0]
            is not None
        )
        pointer = (
            conn.execute("SELECT generation,revision FROM control.active").fetchall()
            if has_versions
            else []
        )
        generations = (
            conn.execute(
                "SELECT generation,manifest_sha256 FROM control.generation ORDER BY generation"
            ).fetchall()
            if has_versions
            else []
        )
        conn.execute("CREATE SCHEMA IF NOT EXISTS verification")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS verification.persistence (id text PRIMARY KEY, value bigint NOT NULL)"
        )
        conn.execute(
            "INSERT INTO verification.persistence VALUES (%s, %s)",
            (identity, 1500000000000001),
        )
    if args.recreate:
        subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                str(ROOT / "compose.env"),
                "-f",
                str(REPO / "infra/docker/compose.yaml"),
                "up",
                "-d",
                "--wait",
                "--force-recreate",
                "postgres",
            ],
            check=True,
        )
    with connection() as conn:
        row = conn.execute(
            "SELECT value FROM verification.persistence WHERE id=%s", (identity,)
        ).fetchone()
        if row != (1500000000000001,):
            raise ValueError("重建后持久化值不一致")
        if has_versions and (
            conn.execute("SELECT generation,revision FROM control.active").fetchall()
            != pointer
            or conn.execute(
                "SELECT generation,manifest_sha256 FROM control.generation ORDER BY generation"
            ).fetchall()
            != generations
        ):
            raise ValueError("重建后版本或指针不一致")
        version = conn.execute("SHOW server_version").fetchone()[0]
    print(
        json.dumps(
            {
                "status": "passed",
                "identity": identity,
                "version": version,
                "value": row[0],
            }
        )
    )


if __name__ == "__main__":
    main()
