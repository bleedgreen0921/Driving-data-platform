"""隔离文本 HTTP 闭环：独立余弦基准、真实媒体、负载及显式故障恢复。"""

import argparse
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import embedding_api_process as api
import psycopg
from embedding_ops import trial_state, trial_up
from embedding_search_resources import inventory, output
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from verify_backup import fingerprint
from verify_embedding_search import AUX, MAIN, TRIAL, baseline

from driving_data_platform.common.config import output_root, port
from driving_data_platform.common.errors import PlatformError, describe
from driving_data_platform.common.operations import operation_lock
from driving_data_platform.embedding import artifacts as a
from driving_data_platform.embedding import gate, model, store
from driving_data_platform.ingestion.nuscenes import atomic_json
from driving_data_platform.publishing.store import connect
from driving_data_platform.warehouse.catalog import command

BASE = "http://127.0.0.1:" + str(port("trial_api"))


def http(path, body=None, expected=200, raw=None):
    request = Request(
        BASE + path,
        data=raw
        if raw is not None
        else (
            json.dumps(body, ensure_ascii=True).encode() if body is not None else None
        ),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=90) as response:
            code, data, headers = (
                response.status,
                response.read(),
                {k.lower(): v for k, v in response.headers.items()},
            )
    except HTTPError as error:
        code, data, headers = (
            error.code,
            error.read(),
            {k.lower(): v for k, v in error.headers.items()},
        )
    if expected is not None and code != expected:
        raise ValueError(f"HTTP {code}, expected {expected}: {path}; {data[:400]!r}")
    if path.startswith("/api/media/"):
        return code, data, headers
    return code, json.loads(data), headers


def require(condition, label):
    if not condition:
        raise ValueError(label)


def disconnect_check(root, body):
    """真实客户端断开后仍保持工作锁，完成后释放并留存报告。"""
    request_body = {**body, "text": "断开连接恢复查询", "limit": 1}
    previous = set((root / "embedding/reports").glob("http-*.json"))
    pid = model.checked(root)["pid"]
    client = None
    os.kill(pid, signal.SIGSTOP)
    try:
        raw = json.dumps(request_body).encode()
        client = socket.create_connection(("127.0.0.1", port("trial_api")), timeout=5)
        client.sendall(
            b"POST /api/embedding/search HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(raw)).encode()
            + b"\r\n\r\n"
            + raw
        )
        with store.connect(root, trial=TRIAL) as conn:
            deadline = time.monotonic() + 1.3
            while time.monotonic() < deadline:
                if conn.execute(
                    "SELECT 1 FROM pg_stat_activity WHERE pid<>pg_backend_pid() AND datname=current_database() AND query LIKE 'SELECT contract FROM control.embedding_space%' LIMIT 1"
                ).fetchone():
                    break
                time.sleep(0.01)
            else:
                raise ValueError("未观察到断开测试真实请求执行")
        client.shutdown(socket.SHUT_RDWR)
        client.close()
        client = None
        require(
            http("/api/embedding/search", request_body, 429)[1]["error"]["code"]
            == "search-busy",
            "断开后提前释放锁",
        )
    finally:
        os.kill(pid, signal.SIGCONT)
        if client is not None:
            client.close()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with gate.acquire(root):
                break
        except PlatformError:
            time.sleep(0.05)
    else:
        raise ValueError("断开后锁未恢复")
    result = http("/api/embedding/search", request_body)[1]
    reports = [
        json.loads(path.read_text())
        for path in set((root / "embedding/reports").glob("http-*.json")) - previous
    ]
    require(
        len([r for r in reports if r["status"] == "passed"]) == 2, "断开请求报告未完成"
    )
    for report in reports:
        require(
            request_body["text"] not in json.dumps(report, ensure_ascii=False),
            "报告保存了查询原文",
        )
        if report["status"] == "passed":
            require(
                report["resolved"]["embedding_generation"]
                == result["embedding_generation"],
                "失败前固定版本未记录",
            )
    return {"status": "passed", "request_ids": [r["request_id"] for r in reports]}


def verify(root, repo, trial, artifact_id, faults):
    if trial != TRIAL or artifact_id != MAIN:
        raise ValueError("只允许已验收的指定试验库与主产物")
    directory = root / "reports" / ("embedding-web-" + uuid.uuid4().hex)
    directory.mkdir()
    report = {
        "status": "running",
        "scope": "真实HTTP、模型、数据库、媒体及固定负载；浏览器另行验收",
        "browser_validation": "not_executed_by_script",
        "faults_requested": faults,
        "checks": [],
        "directory": str(directory),
        "initial_containers": [
            json.loads(line)
            for line in output(
                "docker", "ps", "-a", "--format", "{{json .}}"
            ).splitlines()
        ],
    }
    state = trial_state(root, trial)
    trial_started = False
    service_started = False
    api_receipt = None
    model_receipt = None
    default_process = None
    main_eg = "e_" + MAIN
    source_fp = None
    primary_generation = None

    def check(label):
        report["checks"].append(label)
        atomic_json(directory / "report.json", report)
        print(label, flush=True)

    try:
        report["resources_before"] = inventory(root, repo, 128 * 1024**2)
        if report["resources_before"]["status"] != "passed":
            raise PlatformError(
                "RESOURCE_BLOCKED", "web-resource-gate", "资源门禁未通过"
            )
        _, source, contract, rows = a.verify_artifact(root, MAIN)
        primary_generation = source["generation"]
        sid = a.validate_space(contract)
        report.update(generation=primary_generation, space_id=sid, artifact_id=MAIN)
        report["initial_api"] = api.status(root, trial)
        running = {
            r["Names"] for r in report["initial_containers"] if r["State"] == "running"
        }
        trial_started = state["container"] not in running
        trial_up(root, repo, trial)
        api.validate(root, trial)
        with store.connect(root, trial=trial) as conn:
            before = fingerprint(conn)
            require(
                store.show(conn, main_eg)["version_status"] == "READY", "主产物未 READY"
            )
            require(a.media_input(conn, primary_generation) == source, "业务清单不匹配")
            require(
                conn.execute(
                    "SELECT embedding_generation FROM control.embedding_active WHERE generation=%s",
                    (primary_generation,),
                ).fetchone()
                == (main_eg,),
                "主产物不是活动版本",
            )
            require(
                store.show(conn, "e_" + AUX)["version_status"] == "READY",
                "辅助真实版本不存在",
            )
            other = conn.execute(
                "SELECT generation FROM control.generation WHERE generation<>%s AND status='READY' LIMIT 1",
                (primary_generation,),
            ).fetchone()[0]
        if "driving-data-platform-mac-postgres-1" not in running:
            service_started = True
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
        with connect(root) as conn:
            source_fp = fingerprint(conn)
            extensions = conn.execute(
                "SELECT extname,extversion FROM pg_extension ORDER BY extname"
            ).fetchall()
        model_receipt = model.up(root, sid)
        report["initial_model_running"] = model_receipt["reused"]
        api_receipt = api.start(root, repo, trial)
        client = model.encoder(root, sid)
        body = {"generation": primary_generation, "text": "路口有汽车", "limit": 20}
        require(
            http(
                "/api/embedding/status?" + urlencode({"generation": primary_generation})
            )[1]["status"]
            == "ready",
            "状态未就绪",
        )
        require(
            http("/api/embedding/status?" + urlencode({"generation": other}))[1][
                "status"
            ]
            == "no_data",
            "无向量状态错误",
        )
        queries = [
            q["text"]
            for q in a.read(root, "embedding/evaluation-labels.json")["queries"]
            if q["positive_media_ids"]
        ]
        expected_conditions = {
            "location": None,
            "start_us": None,
            "end_us": None,
            "category": None,
            "channels": [],
        }
        report["near_ties"] = []
        with store.connect(root, trial=trial) as conn:
            frame, location, timestamp = conn.execute(
                "SELECT entity_id,location,timestamp_us FROM serving.frame WHERE generation=%s ORDER BY entity_id LIMIT 1",
                (primary_generation,),
            ).fetchone()
            category = conn.execute(
                "SELECT category FROM serving.frame_category WHERE generation=%s AND frame_id=%s LIMIT 1",
                (primary_generation, frame),
            ).fetchone()[0]
            from datetime import UTC, datetime, timedelta

            boundary = (
                datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=timestamp)
            ).isoformat()
            cases = [
                ({}, {}),
                ({"location": location}, {"location": location}),
                (
                    {"start": boundary, "end": boundary},
                    {"start_us": timestamp, "end_us": timestamp},
                ),
                ({"category": category}, {"category": category}),
                (
                    {"required_channels": ["CAM_FRONT", "CAM_FRONT"]},
                    {"channels": ["CAM_FRONT"]},
                ),
                ({"image_channel": "CAM_FRONT"}, {}),
                (
                    {
                        "location": location,
                        "category": category,
                        "required_channels": ["CAM_FRONT"],
                        "image_channel": "CAM_FRONT",
                        "start": boundary,
                    },
                    {
                        "location": location,
                        "category": category,
                        "channels": ["CAM_FRONT"],
                        "start_us": timestamp,
                    },
                ),
                ({"location": "not-a-location"}, {"location": "not-a-location"}),
            ]
            for text in queries:
                with gate.acquire(root):
                    vector = client.encode_text(text)
                for changes, condition in cases if text == queries[0] else [({}, {})]:
                    for limit in (1, 20, 100) if text == queries[0] else (20,):
                        request_body = {**body, "text": text, "limit": limit, **changes}
                        result = http("/api/embedding/search", request_body)[1]
                        expected = baseline(
                            conn,
                            source,
                            rows,
                            vector,
                            expected_conditions | condition,
                            changes.get("image_channel"),
                            limit,
                        )
                        all_scores = dict(
                            baseline(
                                conn,
                                source,
                                rows,
                                vector,
                                expected_conditions | condition,
                                changes.get("image_channel"),
                                2424,
                            )
                        )
                        require(
                            result["embedding_generation"] == main_eg
                            and len(result["items"]) == len(expected),
                            "版本或结果数不一致",
                        )
                        for index, item in enumerate(result["items"]):
                            require(
                                abs(item["score"] - all_scores[item["media_id"]])
                                <= 1e-5,
                                "独立余弦分数不一致",
                            )
                            if item["media_id"] != expected[index][0]:
                                require(
                                    abs(item["score"] - expected[index][1]) <= 1e-5,
                                    "独立排序不一致",
                                )
                                report["near_ties"].append(
                                    {"rank": index + 1, "limit": limit}
                                )
            result = http("/api/embedding/search", body)[1]
            again = http("/api/embedding/search", body)[1]
            require(
                [i["media_id"] for i in result["items"]]
                == [i["media_id"] for i in again["items"]],
                "重复排序不稳定",
            )
            atomic_json(directory / "sample-result.json", result)
            entries = {e["media_id"]: e for e in source["entries"]}
            for item in result["items"]:
                data = http(item["media_url"])[1]
                require(
                    hashlib.sha256(data).hexdigest()
                    == entries[item["media_id"]]["sha256"],
                    "真实媒体摘要不一致",
                )
                detail = http(item["detail_url"])[1]
                require(
                    detail["generation"] == primary_generation
                    and detail["frame"]["entity_id"] == item["frame_id"]
                    and detail["frame"]["scene_id"] == item["scene_id"],
                    "详情串版",
                )
            with conn.transaction():
                alternate = contract | {"test_only": True}
                alt_id = a.validate_space(alternate)
                conn.execute(
                    "INSERT INTO control.embedding_space VALUES(%s,%s,%s,true)",
                    (alt_id, Jsonb(alternate), contract["dimension"]),
                )
                from verify_embedding_search import reject

                reject(
                    store.search,
                    conn,
                    primary_generation,
                    alt_id,
                    vector,
                    embedding_generation=main_eg,
                )
                raise psycopg.Rollback()
        check(
            "7条真实中文与独立Python基准、单项/组合筛选、UTC边界、Top1/20/100、稳定排序及真实媒体/详情一致"
        )
        for changes, expected in (
            ({"text": " "}, 422),
            ({"text": "\ud800"}, 422),
            ({"text": "中" * 4097}, 422),
            ({"text": "\U0001f680" * 4096}, 422),
            ({"start": "bad"}, 422),
            ({"start": "2026-01-01"}, 422),
            ({"limit": 0}, 422),
            ({"limit": 101}, 422),
            ({"limit": "20"}, 422),
            ({"limit": True}, 422),
            ({"trial": trial}, 422),
            ({"generation": "g_" + "0" * 64}, 404),
            ({"embedding_generation": "e_" + "0" * 64}, 404),
            ({"generation": other}, 409),
            ({"generation": other, "embedding_generation": main_eg}, 409),
        ):
            http("/api/embedding/search", {**body, **changes}, expected)
        http("/api/embedding/search", expected=413, raw=b"x" * 65537)
        http("/api/embedding/search", expected=422, raw=b"bad")
        with gate.acquire(root):
            require(
                http("/api/embedding/search", body, 429)[2].get("retry-after") == "1",
                "繁忙头缺失",
            )
            cli = subprocess.run(
                [
                    sys.executable,
                    "scripts/manage_platform.py",
                    "embedding-search",
                    "--trial",
                    trial,
                    "--generation",
                    primary_generation,
                    "--text",
                    "锁竞争",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            require(
                cli.returncode == 2
                and json.loads(cli.stdout)["error"]["code"] == "search-busy",
                "CLI互斥未生效",
            )
        http("/api/embedding/search", body)
        check(
            "严格参数、Unicode/上下文、版本拒绝、请求体限额、HTTP/CLI共用互斥与释放恢复"
        )

        # 真实五客户端固定速率短测；429明确计为拒绝，不混入成功样本。
        counter = {"inflight": 0, "peak": 0}
        counter_lock = threading.Lock()

        def load(client_id):
            values = []
            for index in range(60):
                deadline = load_start + index
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
                started = time.monotonic()
                with counter_lock:
                    counter["inflight"] += 1
                    counter["peak"] = max(counter["peak"], counter["inflight"])
                try:
                    code, payload, _ = http(
                        "/api/embedding/search", body, expected=None
                    )
                    search_seconds = time.monotonic() - started
                finally:
                    with counter_lock:
                        counter["inflight"] -= 1
                require(code in (200, 429), "负载出现非预期状态")
                if code == 200:
                    require(payload["embedding_generation"] == main_eg, "负载串版")
                    http(result["items"][0]["media_url"])
                    http(result["items"][0]["detail_url"])
                values.append(
                    {
                        "client": client_id,
                        "status": code,
                        "seconds": search_seconds,
                        "chain_seconds": time.monotonic() - started,
                        "request_id": payload["request_id"],
                    }
                )
            return values

        load_start = time.monotonic()
        samples = []
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(load, i) for i in range(5)]
            while not all(f.done() for f in futures):
                samples.append(
                    {
                        "elapsed": time.monotonic() - load_start,
                        "model_rss_kib": int(
                            output(
                                "ps",
                                "-p",
                                str(model.checked(root)["pid"]),
                                "-o",
                                "rss=",
                            ).strip()
                        ),
                        "api_rss_kib": int(
                            output(
                                "ps", "-p", str(api_receipt["pid"]), "-o", "rss="
                            ).strip()
                        ),
                        "docker": output(
                            "docker",
                            "stats",
                            "--no-stream",
                            "--format",
                            "{{json .}}",
                            state["container"],
                        ),
                    }
                )
                time.sleep(1)
            values = [v for f in futures for v in f.result()]
        successes = [v["seconds"] for v in values if v["status"] == 200]
        require(bool(successes), "并发没有成功请求")
        report["load"] = {
            "users": 5,
            "observed_peak_inflight": counter["peak"],
            "success_throughput_per_second": len(successes)
            / (time.monotonic() - load_start),
            "max_inflight_per_user": 1,
            "requested_seconds": 60,
            "elapsed_seconds": time.monotonic() - load_start,
            "requests": values,
            "successes": len(successes),
            "busy": len(values) - len(successes),
            "unexpected_errors": 0,
            "success_p95_seconds": sorted(successes)[
                min(len(successes) - 1, int(len(successes) * 0.95))
            ],
            "samples": samples,
            "scope": "固定五客户端、每客户端最多1请求/秒；同机资源争用，不代表最大容量",
        }
        check(
            "60秒真实五客户端、每客户端单在途混合媒体/详情读取，成功与429分开统计，无非预期错误"
        )
        if faults:
            if model_receipt["reused"] or api_receipt["reused"] or not trial_started:
                raise ValueError(
                    "故障覆盖要求本轮新启模型、API和指定数据库，不能停止原本运行的服务"
                )
            # 在真实请求已解析版本、等待模型身份响应时切换活动指针。
            pid = model.checked(root)["pid"]
            os.kill(pid, signal.SIGSTOP)
            try:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(http, "/api/embedding/search", body)
                    deadline = time.monotonic() + 1.5
                    with store.connect(root, trial=trial) as conn:
                        while time.monotonic() < deadline:
                            resolved = conn.execute(
                                "SELECT 1 FROM pg_stat_activity WHERE pid<>pg_backend_pid() AND datname=current_database() AND query LIKE 'SELECT contract FROM control.embedding_space%' LIMIT 1"
                            ).fetchone()
                            if resolved:
                                break
                            time.sleep(0.02)
                        require(bool(resolved), "未观察到真实请求固定版本")
                        revision = conn.execute(
                            "SELECT revision FROM control.embedding_active WHERE generation=%s",
                            (primary_generation,),
                        ).fetchone()[0]
                        store.publish(conn, "e_" + AUX, revision)
                    os.kill(pid, signal.SIGCONT)
                    require(
                        future.result()[1]["embedding_generation"] == main_eg,
                        "活动指针切换影响已固定请求",
                    )
            finally:
                os.kill(pid, signal.SIGCONT)
                with store.connect(root, trial=trial) as conn:
                    pointer = conn.execute(
                        "SELECT embedding_generation,revision FROM control.embedding_active WHERE generation=%s",
                        (primary_generation,),
                    ).fetchone()
                    if pointer[0] != main_eg:
                        store.publish(conn, main_eg, pointer[1], rollback=True)
            model.stop(root)
            http("/api/embedding/search", body, 503)
            http("/api/health")
            model_receipt = model.up(root, sid)
            http("/api/embedding/search", body)
            subprocess.run(
                ["docker", "stop", state["container"]], check=True, capture_output=True
            )
            try:
                http("/api/embedding/search", body, 503)
            finally:
                trial_up(root, repo, trial)
            http("/api/embedding/search", body)
            with store.connect(root, trial=trial) as conn:
                # 未提交的登记故障对其他连接不可见；显式提交、测试、finally原位恢复。
                conn.execute(
                    "UPDATE control.embedding_environment SET database_name=%s WHERE singleton",
                    ("ddp_embedding_trial_" + "0" * 32,),
                )
                try:
                    http("/api/embedding/search", body, 409)
                finally:
                    conn.execute(
                        "UPDATE control.embedding_environment SET database_name=%s WHERE singleton",
                        (state["database"],),
                    )
            http("/api/embedding/search", body)
            report["disconnect_check"] = disconnect_check(root, body)
            check(
                "真实请求固定版本后切换指针；模型/隔离库停机、环境故障、客户端断开持锁与恢复，主版本恢复"
            )
        # 既有业务HTTP回归使用同一隔离DSN；不使用故障参数以免操作正式组件。
        dsn = make_conninfo(
            host=state["host"],
            port=state["port"],
            dbname=state["database"],
            user=state["user"],
            password=state["password"],
        )
        with (directory / "api-regression.log").open("w") as log:
            subprocess.run(
                [sys.executable, "scripts/verify_api.py"],
                env=dict(
                    os.environ,
                    DDP_API_BASE=BASE,
                    DDP_POSTGRES_DSN=dsn,
                    DDP_OUTPUT_ROOT=str(root),
                ),
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
            )
        # 串行复用同一API端口验证未启用模式，不启动或修改正式API。
        if not api_receipt["reused"]:
            api.stop(root, trial)
            env = dict(os.environ, DDP_POSTGRES_DSN=dsn, DDP_OUTPUT_ROOT=str(root))
            env.pop("DDP_EMBEDDING_TRIAL", None)
            with (directory / "disabled-api.log").open("w") as log:
                default_process = subprocess.Popen(
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
                    http("/api/health")
                    break
                except OSError:
                    time.sleep(0.1)
            require(
                http(
                    "/api/embedding/status?"
                    + urlencode({"generation": primary_generation})
                )[1]["status"]
                == "disabled",
                "默认模式未禁用",
            )
            http("/api/embedding/search", body, 503)
            default_process.terminate()
            default_process.wait(timeout=15)
            default_process = None
            api_receipt = api.start(root, repo, trial)
        check("既有业务HTTP回归及默认未启用模式通过，专用API恢复")
        with store.connect(root, trial=trial) as conn:
            after = fingerprint(conn)
            for table in before:
                if table not in {
                    "control.embedding_active",
                    "control.embedding_publication",
                }:
                    require(
                        before[table] == after[table], "试验不可变数据变化：" + table
                    )
            require(
                conn.execute(
                    "SELECT embedding_generation FROM control.embedding_active WHERE generation=%s",
                    (primary_generation,),
                ).fetchone()
                == (main_eg,),
                "最终活动版本错误",
            )
        with connect(root) as conn:
            require(fingerprint(conn) == source_fp, "正式业务指纹变化")
            require(
                conn.execute(
                    "SELECT extname,extversion FROM pg_extension ORDER BY extname"
                ).fetchall()
                == extensions,
                "正式扩展变化",
            )
        report["resources_after"] = inventory(root, repo)
        require(report["resources_after"]["status"] == "passed", "末轮资源越界")
        report["status"] = "passed"
        check("业务/向量内容、正式库指纹与扩展未变，主活动版本及最终资源核对通过")
    except BaseException as error:
        report["status"] = "failed"
        report["error"] = describe(error, "embedding-web-verification")
        raise
    finally:
        try:
            if default_process is not None:
                default_process.terminate()
                default_process.wait(timeout=15)
            if api_receipt is not None and not api_receipt["reused"]:
                api.stop(root, trial)
            if model_receipt is not None and not model_receipt["reused"]:
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
            report["restore_error"] = describe(error, "restore-service-state")
            raise
        finally:
            atomic_json(directory / "report.json", report)
            print(
                json.dumps(
                    {"status": report["status"], "directory": str(directory)},
                    ensure_ascii=False,
                ),
                flush=True,
            )
    return report


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
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 — 独立入口返回结构化失败
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": describe(error, "embedding-web-verification"),
                },
                ensure_ascii=False,
            )
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
