"""真实产物与文本 CLI 独立验收；只重建指定且已有完整备份的试验库。"""

import argparse
import itertools
import json
import math
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import psycopg
from embedding_ops import trial_state, trial_up
from psycopg import sql
from psycopg.types.json import Jsonb
from usage_report import inventory, output
from verify_backup import fingerprint

from driving_data_platform.common.config import (
    application_config,
    output_root,
)
from driving_data_platform.common.errors import PlatformError, describe
from driving_data_platform.common.operations import operation_lock
from driving_data_platform.embedding import artifacts as a
from driving_data_platform.embedding import build, model, search, store
from driving_data_platform.ingestion.nuscenes import atomic_json
from driving_data_platform.publishing.store import connect
from driving_data_platform.warehouse.catalog import command

TRIAL = "126a9e17a1df40cb9bdb851f8677aba1"
MAIN = "1e0a6429f23abcf5e40ed6eb6cd4a02334e60b6a3fbd653f641c0a908363759e"
AUX = "24e73bf914f15a507eb3d88c32efa371447bf14a74e3d132a408f231497632cb"


def reject(function, *args, **kwargs):
    try:
        function(*args, **kwargs)
    except (ValueError, psycopg.Error):
        return
    raise AssertionError("预期拒绝却成功")


def dump(base, database, path, snapshot=None):
    with path.open("xb") as stream:
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
    listing = subprocess.check_output(
        [*base, "pg_restore", "--list"], input=path.read_bytes()
    )
    if not listing or path.stat().st_size == 0:
        raise ValueError("备份不可读，禁止重建")
    path.with_suffix(".list").write_bytes(listing)
    return {"sha256": model.digest(path), "bytes": path.stat().st_size}


def restore(base, database, path):
    with path.open("rb") as stream:
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


def rebuild(state, backup):
    # 数据库名来自已核对登记，绝不接收任意数据库名。
    assert (
        state["trial"] == TRIAL and state["database"] == "ddp_embedding_trial_" + TRIAL
    )
    with psycopg.connect(
        host=state["host"],
        port=state["port"],
        dbname="postgres",
        user=state["user"],
        password=state["password"],
        connect_timeout=5,
        autocommit=True,
    ) as admin:
        admin.execute(
            sql.SQL("DROP DATABASE {}").format(sql.Identifier(state["database"]))
        )
        admin.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(state["database"]))
        )
    restore(["docker", "exec", "-i", state["container"]], state["database"], backup)


def baseline(conn, source, rows, query, conditions, channel, limit):
    """预期结果独立读取业务表和不可变向量，不调用 SQL where/filters。"""
    frames = {
        r[0]: r[1:]
        for r in conn.execute(
            "SELECT entity_id,location,timestamp_us FROM serving.frame WHERE generation=%s",
            (source["generation"],),
        )
    }
    categories = set(
        conn.execute(
            "SELECT frame_id,category FROM serving.frame_category WHERE generation=%s",
            (source["generation"],),
        ).fetchall()
    )
    channels = set(
        conn.execute(
            "SELECT frame_id,channel FROM serving.frame_channel WHERE generation=%s",
            (source["generation"],),
        ).fetchall()
    )
    entries = {r["media_id"]: r for r in source["entries"]}
    norm = math.sqrt(sum(x * x for x in query))
    scores = []
    for row in rows:
        item = entries[row["media_id"]]
        frame = item["frame_id"]
        location, timestamp = frames[frame]
        if channel is not None and item["channel"] != channel:
            continue
        if conditions["location"] is not None and location != conditions["location"]:
            continue
        if conditions["start_us"] is not None and timestamp < conditions["start_us"]:
            continue
        if conditions["end_us"] is not None and timestamp > conditions["end_us"]:
            continue
        if (
            conditions["category"] is not None
            and (frame, conditions["category"]) not in categories
        ):
            continue
        if any((frame, c) not in channels for c in conditions["channels"]):
            continue
        vector = row["vector"]
        score = sum(x * y for x, y in zip(vector, query)) / (
            norm * math.sqrt(sum(x * x for x in vector))
        )
        scores.append((row["media_id"], score))
    return sorted(scores, key=lambda r: (-r[1], r[0]))[:limit]


def verify(root, repo, trial, artifact_id, faults):
    if trial != TRIAL or artifact_id != MAIN:
        raise ValueError("本轮仅允许计划中指定的试验库和主产物")
    directory = root / "reports" / ("embedding-search-" + uuid.uuid4().hex)
    directory.mkdir()
    report = {"status": "running", "checks": [], "directory": str(directory)}
    initial = [
        json.loads(line)
        for line in output("docker", "ps", "-a", "--format", "{{json .}}").splitlines()
    ]
    report["initial_containers"] = initial
    model_path = model.state_path(root)
    if model_path.exists():
        registered = a.read(model_path.parent, model_path.name)
        try:
            model.process_identity(registered["pid"])
        except subprocess.CalledProcessError:
            report["initial_model_status"] = "stopped"
        else:
            checked = model.checked(root)
            report["initial_model_status"] = "running"
            report["initial_model_identity"] = {
                k: checked[k] for k in ("pid", "process_identity", "space_id")
            }
    else:
        report["initial_model_status"] = "stopped"
    service_started = False
    trial_started = False
    receipt = None
    base = [*command(root, repo), "exec", "-T", "postgres"]
    state = trial_state(root, trial)
    source_fingerprint = None

    def check(label):
        report["checks"].append(label)
        atomic_json(directory / "report.json", report)
        print(label, flush=True)

    try:
        report["resources_before"] = inventory(root, repo, 2 * 1024**3)
        atomic_json(directory / "report.json", report)
        _complete, source, contract, rows = a.verify_artifact(root, artifact_id)
        _, aux_source, aux_contract, _ = a.verify_artifact(root, AUX)
        assert source == aux_source and contract == aux_contract
        assert (
            len(rows) == 2424 and len({r["frame_id"] for r in source["entries"]}) == 404
        )
        assert (
            contract["runtime_parameters"]["image_max_tokens"] == 256
            and not contract["test_only"]
        )
        dataset = Path(application_config(root)["dataset"]["root"])
        for entry in source["entries"]:
            build.image(dataset, entry)
        report.update(
            generation=source["generation"],
            space_id=a.validate_space(contract),
            artifact_id=artifact_id,
        )
        check("主/辅助真实产物完整校验及 2424 张原始图片核验")
        service_was_running = any(
            r["Names"] == "driving-data-platform-mac-postgres-1"
            and r["State"] == "running"
            for r in initial
        )
        if not service_was_running:
            subprocess.run(
                [
                    *command(root, repo),
                    "up",
                    "-d",
                    "--wait",
                    "--pull",
                    "never",
                    "postgres",
                ],
                check=True,
            )
            service_started = True
        trial_was_running = any(
            r["Names"] == state["container"] and r["State"] == "running"
            for r in initial
        )
        trial_started = not trial_was_running
        trial_up(root, repo, trial)
        info = json.loads(output("docker", "container", "inspect", state["container"]))[
            0
        ]
        assert (
            info["Image"] == state["image"]
            and info["Config"]["Labels"]["ddp.embedding.trial"] == trial
        )
        assert info["NetworkSettings"]["Ports"]["5432/tcp"] == [
            {"HostIp": "127.0.0.1", "HostPort": str(state["port"])}
        ]
        target = ["docker", "exec", "-i", state["container"]]
        with store.connect(root, trial=trial) as conn:
            store.check_environment(conn, False)
            report["old_trial_fingerprint"] = fingerprint(conn)
        report["old_trial_backup"] = dump(
            target, state["database"], directory / "old-trial.dump"
        )
        with connect(root) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            snapshot = conn.execute("SELECT pg_export_snapshot()").fetchone()[0]
            source_fingerprint = fingerprint(conn)
            report["source_fingerprint"] = source_fingerprint
            report["source_extensions"] = conn.execute(
                "SELECT extname,extversion FROM pg_extension ORDER BY extname"
            ).fetchall()
            assert a.media_input(conn, source["generation"]) == source
            report["source_backup"] = dump(
                base, "ddp", directory / "service.dump", snapshot
            )
        check("旧试验库完整可读备份；正式库同一只读快照备份及全表指纹")
        rebuild(state, directory / "service.dump")
        with store.connect(root, trial=trial) as conn:
            assert fingerprint(conn) == source_fingerprint
            assert a.media_input(conn, source["generation"]) == source
            store.migrate(conn, repo, test_database=True)
            assert store.migrate(conn, repo, test_database=True)["reused"]
            if faults:
                reject(store.load, root, conn, artifact_id, fail=True)
                assert conn.execute(
                    "SELECT count(*) FROM control.embedding_generation"
                ).fetchone() == (0,)
                reject(store.load, root, conn, artifact_id, ack_fail=True)
            else:
                store.load(root, conn, artifact_id)
        with store.connect(root, trial=trial) as conn:
            loaded = store.load(root, conn, artifact_id)
            assert loaded["reused"]
            eg = loaded["embedding_generation"]
            returned = conn.execute(
                "SELECT media_id,image_sha256,embedding::text FROM serving.image_embedding WHERE embedding_generation=%s ORDER BY media_id",
                (eg,),
            ).fetchall()
            assert [(r["media_id"], r["sha256"], r["vector"]) for r in rows] == [
                (i, s, a.vector32(json.loads(v), contract["dimension"]))
                for i, s, v in returned
            ]
            aux = store.load(root, conn, AUX)["embedding_generation"]
            store.publish(conn, eg, 0)
            reject(store.publish, conn, aux, 0)

            def compete(_):
                with store.connect(root, trial=trial) as c:
                    try:
                        store.publish(c, aux, 1)
                        return "passed"
                    except PlatformError:
                        return "conflict"

            with ThreadPoolExecutor(max_workers=2) as pool:
                assert sorted(pool.map(compete, range(2))) == ["conflict", "passed"]
            store.publish(conn, eg, 2, rollback=True)
        check("真实向量全量逐值回读、复用、提交故障恢复、竞争发布及真实回滚")
        receipt = model.up(root, report["space_id"])
        report["model_reused"] = receipt["reused"]
        client = model.encoder(root, report["space_id"])
        queries = [
            q["text"]
            for q in a.read(root, "embedding/evaluation-labels.json")["queries"]
            if q["positive_media_ids"]
        ]
        assert len(queries) == 7
        query = client.encode_text(queries[0])
        empty = {
            "location": None,
            "start_us": None,
            "end_us": None,
            "category": None,
            "channels": [],
        }
        with store.connect(root, trial=trial) as conn:
            conn.execute("SET statement_timeout='10s'")
            frame, location, timestamp = conn.execute(
                "SELECT entity_id,location,timestamp_us FROM serving.frame WHERE generation=%s ORDER BY entity_id LIMIT 1",
                (source["generation"],),
            ).fetchone()
            category = conn.execute(
                "SELECT category FROM serving.frame_category WHERE generation=%s AND frame_id=%s LIMIT 1",
                (source["generation"], frame),
            ).fetchone()[0]
            cases = [
                ({}, None),
                ({"location": location}, None),
                ({"start_us": timestamp, "end_us": timestamp}, None),
                ({"category": category}, None),
                ({"channels": ["CAM_FRONT"]}, None),
                ({}, "CAM_FRONT"),
                (
                    {
                        "location": location,
                        "category": category,
                        "channels": ["CAM_FRONT"],
                        "start_us": timestamp,
                    },
                    "CAM_FRONT",
                ),
                ({"location": "not-a-location"}, None),
            ]
            report["near_ties"] = []
            for changes, channel in cases:
                condition = empty | changes
                for limit in (1, 20, 100):
                    expected = baseline(
                        conn, source, rows, query, condition, channel, limit
                    )
                    result = store.search(
                        conn,
                        source["generation"],
                        report["space_id"],
                        query,
                        embedding_generation=eg,
                        conditions=condition,
                        channel=channel,
                        limit=limit,
                    )
                    assert result == store.search(
                        conn,
                        source["generation"],
                        report["space_id"],
                        query,
                        embedding_generation=eg,
                        conditions=condition,
                        channel=channel,
                        limit=limit,
                    )
                    actual = result["items"]
                    for left, right in itertools.pairwise(actual):
                        assert left["score"] >= right["score"]
                        if left["score"] == right["score"]:
                            assert left["media_id"] < right["media_id"]
                    assert len(actual) == len(expected)
                    # 候选边界近似并列单独记录；所有返回分数与独立计算核对。
                    all_expected = dict(
                        baseline(conn, source, rows, query, condition, channel, 2424)
                    )
                    for index, item in enumerate(actual):
                        assert (
                            abs(item["score"] - all_expected[item["media_id"]]) <= 1e-5
                        )
                        if item["media_id"] != expected[index][0]:
                            assert abs(item["score"] - expected[index][1]) <= 1e-5
                            report["near_ties"].append(
                                {"rank": index + 1, "limit": limit}
                            )
            pinned, _ = search.resolve(root, conn, source["generation"])
            store.publish(conn, aux, 3)
            assert (
                store.search(
                    conn,
                    source["generation"],
                    report["space_id"],
                    query,
                    embedding_generation=pinned["embedding_generation"],
                )["embedding_generation"]
                == eg
            )
            store.publish(conn, eg, 4, rollback=True)
            reject(
                store.search,
                conn,
                source["generation"],
                "0" * 64,
                query,
                embedding_generation=eg,
            )
            reject(
                store.search,
                conn,
                source["generation"],
                report["space_id"],
                query,
                embedding_generation="e_" + "0" * 64,
            )
            other = conn.execute(
                "SELECT generation FROM control.generation WHERE generation<>%s LIMIT 1",
                (source["generation"],),
            ).fetchone()[0]
            reject(search.resolve, root, conn, other)
            reject(
                store.search,
                conn,
                other,
                report["space_id"],
                query,
                embedding_generation=eg,
            )
        with store.connect(root, trial=trial) as conn:
            # 只在回滚事务中创建不同的同维空间契约，不生成或保存替代向量。
            with conn.transaction():
                alternate = contract | {"test_only": True}
                alternate_id = a.validate_space(alternate)
                conn.execute(
                    "INSERT INTO control.embedding_space VALUES(%s,%s,%s,true)",
                    (alternate_id, Jsonb(alternate), contract["dimension"]),
                )
                reject(
                    store.search,
                    conn,
                    source["generation"],
                    alternate_id,
                    query,
                    embedding_generation=eg,
                )
                raise psycopg.Rollback()
            with conn.transaction():
                conn.execute(
                    "UPDATE control.embedding_environment SET database_name=%s WHERE singleton",
                    ("ddp_embedding_trial_" + "0" * 32,),
                )
                reject(store.check_environment, conn, False)
                reject(store.check_environment, conn, True)
                reject(
                    store.search,
                    conn,
                    source["generation"],
                    report["space_id"],
                    query,
                    embedding_generation=eg,
                )
                raise psycopg.Rollback()
            assert store.search(
                conn,
                source["generation"],
                report["space_id"],
                query,
                embedding_generation=eg,
            )["items"]
        check("同为2048维的不同空间拒绝；真实/测试空间均拒绝错误环境登记且回滚恢复")
        check("独立 Python 余弦/业务筛选基准、Top1/20/100、重复稳定及请求版本固定")
        args = SimpleNamespace(
            trial=trial,
            generation=source["generation"],
            embedding_generation=None,
            text=queries[0],
            limit=20,
            location=None,
            start=None,
            end=None,
            category=None,
            required_channel=[],
            image_channel=None,
        )
        assert search.conditions(
            SimpleNamespace(
                **(vars(args) | {"required_channel": ["CAM_FRONT", "CAM_FRONT"]})
            )
        )["channels"] == ["CAM_FRONT"]
        for changes in (
            {"start": "2026-01-01"},
            {"limit": 0},
            {"limit": 101},
            {"start": "bad"},
        ):
            reject(search.conditions, SimpleNamespace(**(vars(args) | changes)))
        for text in (" ", "\ud800", "中" * 4097, "\U0001f680" * 4096):
            reject(client.encode_text, text)
        observations = []
        client.encode_text(queries[0])
        for round_id in range(3):
            for text in queries:
                result = search.execute(
                    root, SimpleNamespace(**(vars(args) | {"text": text}))
                )
                assert (
                    result["embedding_generation"] == eg
                    and result["artifact_id"] == MAIN
                )
                observations.append(
                    {
                        "round": round_id,
                        "model_rss_kib": int(
                            output(
                                "ps",
                                "-p",
                                str(model.checked(root)["pid"]),
                                "-o",
                                "rss=",
                            ).strip()
                        ),
                        "query_sha256": result["query_sha256"],
                        "count": result["count"],
                        "timings": result["timings"],
                        "resources": output(
                            "docker",
                            "stats",
                            "--no-stream",
                            "--format",
                            "{{json .}}",
                            state["container"],
                        ),
                    }
                )
        report["observations"] = observations
        from driving_data_platform.embedding.cli import execute as cli_execute

        result = cli_execute(
            root,
            repo,
            SimpleNamespace(**(vars(args) | {"command": "embedding-search"})),
        )
        task = json.loads(Path(result["report"]).read_text())
        assert task["status"] == "passed" and task["result"]["count"] == 20
        assert (
            queries[0] not in Path(result["report"]).read_text()
            and '"vector"' not in Path(result["report"]).read_text()
        )
        atomic_json(
            directory / "top10.json",
            {"query_sha256": result["query_sha256"], "items": result["items"][:10]},
        )
        from PIL import Image, ImageDraw

        sheet = Image.new("RGB", (1000, 5 * 220), "white")
        draw = ImageDraw.Draw(sheet)
        for index, item in enumerate(result["items"][:10]):
            with Image.open(dataset / item["path"]) as image:
                image.thumbnail((490, 185))
                x, y = (index % 2) * 500, (index // 2) * 220
                sheet.paste(image, (x, y))
                draw.text(
                    (x, y + 187),
                    f"{index + 1} {item['channel']} {item['score']:.6f}",
                    fill="black",
                )
        sheet.save(directory / "top10.jpg")
        check("7 条固定中文真实编码、输入边界、预热后三轮串行观察和 Top10 联系表")
        if faults:
            if receipt["reused"]:
                raise ValueError("模型原本运行，禁止停止；本轮故障覆盖未完成")
            model.checked(root)
            model.stop(root)
            reject(search.execute, root, args)
            receipt = model.up(root, report["space_id"])
            search.execute(root, args)
            subprocess.run(
                ["docker", "stop", state["container"]], check=True, capture_output=True
            )
            try:
                reject(search.execute, root, args)
            finally:
                trial_up(root, repo, trial)
            search.execute(root, args)
            check("本轮模型和指定数据库停机失败、恢复后真实查询")
        with store.connect(root, trial=trial) as conn:
            vector_fingerprint = fingerprint(conn)
        report["vector_backup"] = dump(
            target, state["database"], directory / "with-vectors.dump"
        )
        rebuild(state, directory / "with-vectors.dump")
        with store.connect(root, trial=trial) as conn:
            assert fingerprint(conn) == vector_fingerprint
        subprocess.run(
            ["docker", "restart", state["container"]], check=True, capture_output=True
        )
        for attempt in range(60):
            try:
                with store.connect(root, trial=trial) as conn:
                    assert fingerprint(conn) == vector_fingerprint
                break
            except psycopg.OperationalError:
                time.sleep(1)
        else:
            raise ValueError("隔离库重启恢复超时")
        search.execute(root, args)
        report["cli_checks"] = cli_checks(root, repo, source["generation"], directory)
        check("含真实向量完整备份、串行重建恢复、全表指纹和容器重启持久化")
        with connect(root) as conn:
            assert fingerprint(conn) == source_fingerprint
            assert conn.execute(
                "SELECT extname,extversion FROM pg_extension ORDER BY extname"
            ).fetchall() == [tuple(row) for row in report["source_extensions"]]
        report["resources_after"] = inventory(root, repo)
        check("正式全表/指针/扩展保持不变")
        report["status"] = "passed"
    except BaseException as error:
        report["status"] = "failed"
        report["error"] = describe(error, "embedding-search-verification")
        raise
    finally:
        try:
            if receipt is not None and not receipt["reused"]:
                model.stop(root)
            if trial_started:
                subprocess.run(
                    ["docker", "stop", state["container"]],
                    check=True,
                    capture_output=True,
                )
            if service_started:
                subprocess.run(
                    [*command(root, repo), "stop", "postgres"],
                    check=True,
                    capture_output=True,
                )
        except BaseException as error:
            report["status"] = "failed"
            report["cleanup_error"] = describe(error, "restore-service-state")
            raise
        finally:
            report["backup_files"] = [
                {"name": p.name, "bytes": p.stat().st_size, "sha256": model.digest(p)}
                for p in directory.glob("*.dump")
            ]
            atomic_json(directory / "report.json", report)
            print(
                json.dumps(
                    {"status": report["status"], "directory": str(directory)},
                    ensure_ascii=False,
                ),
                flush=True,
            )
    return report


def cli_checks(root, repo, generation, directory):
    """直接启动命令行进程，核对退出码、脱敏报告和空结果。调用方持有验证锁。"""
    invocation = [
        sys.executable,
        str(repo / "scripts/manage_platform.py"),
        "embedding-search",
        "--trial",
        TRIAL,
        "--generation",
        generation,
        "--text",
        " 路口\n有汽车 ",
    ]
    results = []
    for options, expected_code, expected_count in (
        ([], 0, 20),
        (["--location", "not-a-location"], 0, 0),
        (["--start", "invalid"], 2, None),
        (["--limit", "101"], 2, None),
        (["--limit", "invalid"], 2, None),
    ):
        process = subprocess.run(
            [*invocation, *options], capture_output=True, text=True, check=False
        )
        assert process.returncode == expected_code, process.stdout
        payload = json.loads(process.stdout)
        if expected_count is not None:
            assert len(payload["items"]) == expected_count
            assert payload["artifact_id"] == MAIN
            evidence = json.loads(Path(payload["report"]).read_text())
            assert evidence["status"] == "passed" and evidence["input"][
                "query_characters"
            ] == len(" 路口\n有汽车 ")
            assert " 路口\n有汽车 " not in Path(payload["report"]).read_text()
            assert "vector" not in payload and "text" not in payload
        else:
            assert payload["error"]["category"] == "INPUT_INVALID"
        results.append(
            {"options": options, "exit_code": process.returncode, "result": payload}
        )
    atomic_json(directory / "cli-checks.json", {"status": "passed", "checks": results})
    return {"status": "passed", "count": len(results)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial", required=True)
    parser.add_argument("--artifact-id", required=True)
    parser.add_argument("--faults", action="store_true")
    args = parser.parse_args()
    root = output_root()
    try:
        with operation_lock(root):
            verify(
                root,
                Path(__file__).resolve().parents[1],
                args.trial,
                args.artifact_id,
                args.faults,
            )
        return 0
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 — 入口统一结构化错误
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": describe(error, "embedding-search-verification"),
                },
                ensure_ascii=False,
            )
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
