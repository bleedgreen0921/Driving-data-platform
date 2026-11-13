"""Atomic, immutable PostgreSQL mirror of verified file ODS snapshots."""

import json
from pathlib import Path

from psycopg import sql
from psycopg.types.json import Jsonb

from driving_data_platform.common.preflight import TABLES, digest
from driving_data_platform.ingestion.nuscenes import verify_snapshot
from driving_data_platform.publishing.store import connect


def reconcile(conn, snapshot_id, tables):
    """Read every expanded column and source record back; reject any discrepancy."""
    counts = {}
    for table in TABLES:
        cursor = conn.execute(
            sql.SQL("SELECT * FROM ods.{} WHERE snapshot_id=%s ORDER BY token").format(
                sql.Identifier(table)
            ),
            (snapshot_id,),
        )
        columns = [c.name for c in cursor.description]
        actual = [dict(zip(columns, row)) for row in cursor.fetchall()]
        expected = sorted(tables[table], key=lambda r: r["token"])
        if len(actual) != len(expected):
            raise ValueError("ODS mirror count mismatch: " + table)
        for row, source in zip(actual, expected):
            if (
                row.pop("snapshot_id") != snapshot_id
                or row.pop("source_record") != source
            ):
                raise ValueError("ODS source record mismatch: " + table)
            if row != {key: source.get(key) for key in row}:
                raise ValueError("ODS expanded fields mismatch: " + table)
        counts[table] = len(actual)
    return counts


def mirror(root: Path, repository: Path, source: Path, *, fail_after=None):
    complete = verify_snapshot(source)
    snapshot = complete["snapshot_id"]
    checksum = digest(source / "complete.json")
    tables = {
        t: json.loads((source / "raw" / (t + ".json")).read_text()) for t in TABLES
    }
    with connect(root) as conn:
        conn.execute((repository / "sql/postgres/ods.sql").read_text())
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (int(snapshot[:15], 16),))
        existing = conn.execute(
            "SELECT manifest_sha256,status FROM ods.snapshot WHERE snapshot_id=%s",
            (snapshot,),
        ).fetchone()
        if existing:
            if existing != (checksum, "READY"):
                raise ValueError("ODS snapshot identity conflict")
            counts = reconcile(conn, snapshot, tables)
            return {"status": "passed", "reused": True, "counts": counts}
        counts = {t: len(r) for t, r in tables.items()}
        conn.execute(
            "INSERT INTO ods.snapshot(snapshot_id,manifest_sha256,profile_id,profile_version,asset_policy,status,counts,assets,capabilities) VALUES(%s,%s,%s,%s,%s,'LOADING',%s,%s,%s)",
            (
                snapshot,
                checksum,
                complete["profile_id"],
                complete["profile_version"],
                complete["asset_policy"],
                Jsonb(counts),
                Jsonb(complete["assets"]),
                Jsonb(complete["capabilities"]),
            ),
        )
        for table in TABLES:
            columns = [
                r[0]
                for r in conn.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema='ods' AND table_name=%s ORDER BY ordinal_position",
                    (table,),
                )
            ]
            types = dict(
                conn.execute(
                    "SELECT column_name,data_type FROM information_schema.columns WHERE table_schema='ods' AND table_name=%s",
                    (table,),
                ).fetchall()
            )
            statement = sql.SQL("INSERT INTO ods.{} ({}) VALUES ({})").format(
                sql.Identifier(table),
                sql.SQL(",").join(map(sql.Identifier, columns)),
                sql.SQL(",").join(sql.Placeholder() for _ in columns),
            )
            values = []
            for row in tables[table]:
                record = {**row, "snapshot_id": snapshot, "source_record": row}
                values.append(
                    tuple(
                        Jsonb(record.get(c)) if types[c] == "jsonb" else record.get(c)
                        for c in columns
                    )
                )
            with conn.cursor() as cursor:
                cursor.executemany(statement, values)
            if fail_after == table:
                raise ValueError("injected ODS transaction interruption")
        reconcile(conn, snapshot, tables)
        conn.execute(
            "UPDATE ods.snapshot SET status='READY' WHERE snapshot_id=%s", (snapshot,)
        )
    return {"status": "passed", "reused": False, "counts": counts}
