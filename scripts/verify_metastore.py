"""真实 Hive 表名消费与隔离装载；故障注入必须显式选择。"""

import argparse
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.request import urlopen

import psycopg

from driving_data_platform.ingestion.nuscenes import atomic_json
from driving_data_platform.publishing.store import active, connect
from driving_data_platform.warehouse.build import verify
from driving_data_platform.warehouse.catalog import (
    command,
    consume,
    job,
    new_trial,
    register,
)


def expect_failure(action, label):
    try:
        action()
    except (ValueError, OSError, psycopg.Error):
        return {"case": label, "blocked": True}
    raise ValueError("故障未阻断: " + label)


def table_ids(root, database):
    with connect(root, database="ddp_metastore_v410") as conn:
        return conn.execute(
            'SELECT t."TBL_NAME",t."TBL_ID" FROM "TBLS" t JOIN "DBS" d ON t."DB_ID"=d."DB_ID" WHERE d."NAME"=%s ORDER BY t."TBL_NAME"',
            (database,),
        ).fetchall()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--faults", action="store_true")
    args = parser.parse_args()
    root = Path(
        os.environ.get(
            "DDP_OUTPUT_ROOT",
            "/Users/qiujiajun/Project/driving-data-platform-artifacts",
        )
    )
    repo = Path(__file__).resolve().parents[1]
    report_dir = root / "reports" / ("metastore-" + uuid.uuid4().hex)
    report_dir.mkdir()
    report = {
        "status": "running",
        "scope": "single-machine-catalog-consumption",
        "faults_requested": args.faults,
        "steps": [],
        "report_directory": str(report_dir),
    }
    atomic_json(report_dir / "report.json", report)
    stop = threading.Event()
    queries = []
    samples = []
    resource_errors = []
    threads = []
    before = None

    def step(name, value):
        report["steps"].append({"name": name, "result": value})
        atomic_json(report_dir / "report.json", report)

    try:
        with connect(root) as conn:
            before = active(conn)
            generation = conn.execute(
                "SELECT snapshot_id,processing_version FROM control.generation WHERE generation=%s",
                (before["generation"],),
            ).fetchone()
        source = root / "warehouse" / (generation[0] + "-" + generation[1])
        original = verify(source)
        with urlopen(
            "http://127.0.0.1:18080/api/frames?generation=" + before["generation"],
            timeout=5,
        ) as response:
            expected = json.load(response)

        def query_loop():
            while not stop.is_set():
                started = time.monotonic()
                try:
                    with urlopen(
                        "http://127.0.0.1:18080/api/frames?generation="
                        + before["generation"],
                        timeout=5,
                    ) as response:
                        result = json.load(response)
                    ok = (
                        result["generation"] == before["generation"]
                        and result["items"] == expected["items"]
                        and result["total"] == expected["total"]
                    )
                    queries.append({"ok": ok, "seconds": time.monotonic() - started})
                except (OSError, ValueError, KeyError) as error:
                    queries.append({"ok": False, "error": str(error)})
                stop.wait(0.5)

        def sample_loop():
            while not stop.is_set():
                try:
                    result = subprocess.check_output(
                        ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
                        text=True,
                        timeout=10,
                    )
                    samples.append(
                        [
                            v
                            for line in result.splitlines()
                            if (v := json.loads(line))["Name"].startswith(
                                "driving-data-platform-mac-"
                            )
                        ]
                    )
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    resource_errors.append(str(error))
                stop.wait(2)

        threads = [
            threading.Thread(target=query_loop),
            threading.Thread(target=sample_loop),
        ]
        for thread in threads:
            thread.start()
        with (report_dir / "schema-validation.log").open("w") as log:
            subprocess.run(
                [
                    *command(root, repo),
                    "run",
                    "--rm",
                    "--no-deps",
                    "metastore",
                    "validate",
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
            )
        step("metastore-schema-validation", {"passed": True})
        trial = new_trial(root, source)
        step("trial", str(trial))
        receipt = register(root, repo, trial)
        ids = table_ids(root, receipt["database"])
        if len(ids) != len(original["tables"]):
            raise ValueError("真实 PostgreSQL 元数据库的表数不一致")
        register(root, repo, trial)
        if table_ids(root, receipt["database"]) != ids:
            raise ValueError("重复登记改变了表身份")
        step("registration-idempotent", receipt)
        loaded = consume(root, repo, trial)
        step("table-name-parquet-to-postgres", loaded)
        if not consume(root, repo, trial)["reused"]:
            raise ValueError("重复消费没有复用独立装载结果")
        step("repeat-consumption", {"reused": True})
        if args.faults:
            partial = new_trial(root, source)
            step(
                "partial-registration",
                expect_failure(
                    lambda: register(root, repo, partial, fail=True),
                    "partial-registration",
                ),
            )
            if (partial / "registered.json").exists():
                raise ValueError("登记中断仍有完成回执")
            step(
                "partial-consumer-blocked",
                expect_failure(
                    lambda: consume(root, repo, partial), "partial-consumption"
                ),
            )
            register(root, repo, partial)
            step("partial-recovered", consume(root, repo, partial))
            job(root, repo, trial, "drop")
            verify(source)
            step("external-drop-preserves-files", {"unchanged": True})
            step(
                "missing-table-blocked",
                expect_failure(lambda: consume(root, repo, trial), "missing-table"),
            )
            register(root, repo, trial)
            consume(root, repo, trial)
            try:
                job(root, repo, trial, "redirect")
                step(
                    "wrong-location-blocked",
                    expect_failure(
                        lambda: consume(root, repo, trial), "wrong-location"
                    ),
                )
            finally:
                job(root, repo, trial, "drop")
                register(root, repo, trial)
            # 缺文件仅作用于新建副本，不修改既有正式产物。
            clone = root / "warehouse" / ("hms-file-" + uuid.uuid4().hex)
            shutil.copytree(source, clone)
            file_trial = new_trial(root, clone)
            register(root, repo, file_trial)
            data = next((clone / "fact_frame/parquet").glob("part-*.parquet"))
            saved = file_trial / data.name
            try:
                data.rename(saved)
                step(
                    "missing-file-preflight",
                    expect_failure(
                        lambda: consume(root, repo, file_trial),
                        "missing-file-preflight",
                    ),
                )
                step(
                    "missing-file-spark",
                    expect_failure(
                        lambda: job(root, repo, file_trial, "check"),
                        "missing-file-spark",
                    ),
                )
            finally:
                saved.rename(data)
            step("file-recovered", consume(root, repo, file_trial))
            try:
                subprocess.run([*command(root, repo), "stop", "metastore"], check=True)
                step(
                    "service-unavailable",
                    expect_failure(
                        lambda: consume(root, repo, trial), "service-unavailable"
                    ),
                )
            finally:
                subprocess.run(
                    [
                        *command(root, repo),
                        "up",
                        "-d",
                        "--wait",
                        "--no-deps",
                        "metastore",
                    ],
                    check=True,
                )
            old_ids = table_ids(root, receipt["database"])
            subprocess.run(
                [
                    *command(root, repo),
                    "up",
                    "-d",
                    "--wait",
                    "--no-deps",
                    "--force-recreate",
                    "metastore",
                ],
                check=True,
            )
            if table_ids(root, receipt["database"]) != old_ids:
                raise ValueError("元数据服务重建后表身份改变")
            step("service-recreated", consume(root, repo, trial))
        with connect(root) as conn:
            if active(conn) != before:
                raise ValueError("候选验证改变了活动指针")
        verify(source)
        with connect(root, database="ddp_metastore_v410") as conn:
            if conn.execute('SELECT "SCHEMA_VERSION" FROM "VERSION"').fetchall() != [
                ("4.1.0",)
            ]:
                raise ValueError("元数据库版本不符")
        step("active-pointer-and-source-preserved", before)
        report["status"] = "passed"
    except (
        OSError,
        ValueError,
        KeyError,
        psycopg.Error,
        subprocess.SubprocessError,
    ) as error:
        report["status"] = "failed"
        report["error"] = str(error)
    finally:
        stop.set()
        for thread in threads:
            thread.join()
        atomic_json(report_dir / "queries.json", queries)
        atomic_json(report_dir / "resources.json", samples)
        report["queries"] = {
            "count": len(queries),
            "errors": sum(not q["ok"] for q in queries),
        }
        report["resource_samples"] = len(samples)
        report["resource_errors"] = resource_errors
        if report["queries"]["errors"] or resource_errors:
            report["status"] = "failed"
        atomic_json(report_dir / "report.json", report)
    print(json.dumps(report))
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
