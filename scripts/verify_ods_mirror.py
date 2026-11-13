"""Real PostgreSQL ODS reconciliation, rollback and immutability in an isolated database."""

import json
import os
import uuid
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from driving_data_platform.common.config import output_root
from driving_data_platform.ingestion.postgres import mirror
from driving_data_platform.publishing.store import connect


def main():
    root = output_root()
    repo = Path(__file__).resolve().parents[1]
    water = json.loads((root / "ingestion/watermark.json").read_text())
    source = (
        root
        / "ingestion/snapshots"
        / (water["snapshot_id"] + "-" + water["processing_version"])
    )
    identity = "ods_verify_" + uuid.uuid4().hex
    with connect(root) as conn:
        settings = conn.info.get_parameters()
        password = dict(
            line.split("=", 1)
            for line in (root / "compose.env").read_text().splitlines()
        )["DDP_POSTGRES_PASSWORD"]
        conn.autocommit = True
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(identity)))
    previous = os.environ.get("DDP_POSTGRES_DSN")
    os.environ["DDP_POSTGRES_DSN"] = make_conninfo(
        host=settings["host"],
        port=settings["port"],
        user=settings["user"],
        password=password,
        dbname=identity,
    )
    try:
        # Create only ODS schema first so interrupted DML can be observed after rollback.
        with connect(root) as conn:
            conn.execute((repo / "sql/postgres/ods.sql").read_text())
        try:
            mirror(root, repo, source, fail_after="sample_data")
        except ValueError as error:
            if str(error) != "injected ODS transaction interruption":
                raise
        else:
            raise ValueError("fault did not trigger")
        with connect(root) as conn:
            assert conn.execute("SELECT count(*) FROM ods.snapshot").fetchone() == (0,)
            for table in ["scene", "sample", "sample_data"]:
                assert conn.execute(
                    sql.SQL("SELECT count(*) FROM ods.{}").format(sql.Identifier(table))
                ).fetchone() == (0,)
        first = mirror(root, repo, source)
        second = mirror(root, repo, source)
        assert not first["reused"] and second["reused"]
        rejected = []
        for statement in [
            "UPDATE ods.sample SET next=next",
            "DELETE FROM ods.sample",
            "INSERT INTO ods.sample SELECT * FROM ods.sample LIMIT 1",
            "TRUNCATE ods.sample",
            "UPDATE ods.snapshot SET counts=counts",
            "DELETE FROM ods.snapshot",
        ]:
            try:
                with connect(root) as conn:
                    conn.execute(statement)
            except psycopg.errors.RaiseException:
                rejected.append(statement.split()[0])
            else:
                raise ValueError("READY mutation accepted")
        print(
            json.dumps(
                {
                    "status": "passed",
                    "database": identity,
                    "counts": first["counts"],
                    "full_expanded_reconciliation": True,
                    "interruption_rolled_back": True,
                    "recovery_and_reuse": True,
                    "ready_rejections": rejected,
                }
            )
        )
    finally:
        if previous is None:
            os.environ.pop("DDP_POSTGRES_DSN", None)
        else:
            os.environ["DDP_POSTGRES_DSN"] = previous


if __name__ == "__main__":
    main()
