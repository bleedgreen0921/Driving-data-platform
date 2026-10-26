"""真实全流程与固定五客户端观测；默认 1800 秒，不启停现有服务。"""

import argparse
import json
import math
import os
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

import psycopg
from api_process import status as api_status
from usage_report import usage
from verify_backup import verify_backup

from driving_data_platform.pipeline import DEFAULT_VERSION, run
from driving_data_platform.publishing.store import active, connect, publish
from driving_data_platform.warehouse.baseline import baseline


def get(path):
    with urlopen("http://127.0.0.1:18080" + path, timeout=5) as response:
        return json.load(response)


def execute(args, root):
    repo = Path(__file__).resolve().parents[1]
    report = root / "reports" / ("integration-" + uuid.uuid4().hex)
    report.mkdir()
    health = get("/api/health")
    pinned = health["generation"]
    watermark = json.loads((root / "ingestion/watermark.json").read_text())
    source = (
        root
        / "ingestion/snapshots"
        / (watermark["snapshot_id"] + "-" + watermark["processing_version"])
    )
    expected = baseline(source)
    ordered = sorted(
        expected["fact_frame"], key=lambda r: (r["timestamp_us"], r["entity_id"])
    )
    expected_total = len(ordered)
    expected_ids = [r["entity_id"] for r in ordered]
    filtered_ids = [
        r["entity_id"] for r in ordered if r["location"] == "boston-seaport"
    ]
    state_lock = threading.Lock()
    state = {"in_flight": 0, "peak_in_flight": 0, "samples": [], "errors": []}
    stop = threading.Event()
    known = {pinned}
    queries = [
        ("/api/frames?" + urlencode({"generation": pinned}), 50),
        ("/api/frames?" + urlencode({"generation": pinned, "limit": 200}), 200),
        (
            "/api/frames?"
            + urlencode(
                {
                    "generation": pinned,
                    "channel": ["CAM_FRONT", "LIDAR_TOP"],
                    "category": "vehicle.car",
                    "location": "boston-seaport",
                    "limit": 200,
                },
                doseq=True,
            ),
            200,
        ),
        (
            "/api/frames?"
            + urlencode({"generation": pinned, "channel": "invalid-channel"}),
            0,
        ),
        ("/api/frames?limit=50", 50),
    ]
    start = time.monotonic()

    def client(index):
        iteration = 0
        while not stop.is_set():
            number = (index + iteration) % len(queries)
            path, limit = queries[number]
            with state_lock:
                state["in_flight"] += 1
                state["peak_in_flight"] = max(
                    state["peak_in_flight"], state["in_flight"]
                )
            began = time.monotonic()
            error = None
            try:
                data = get(path)
                with state_lock:
                    valid_generation = data["generation"] in known
                ids = [r["entity_id"] for r in data["items"]]
                if (
                    not valid_generation
                    or (data["generation"] != pinned and number != 4)
                    or data["total"]
                    != (
                        len(filtered_ids)
                        if number == 2
                        else (expected_total if limit else 0)
                    )
                    or ids != (filtered_ids if number == 2 else expected_ids[:limit])
                ):
                    raise ValueError("版本、数量或实体顺序不一致")
            except (OSError, ValueError, KeyError) as exc:
                error = type(exc).__name__ + ": " + str(exc)
            elapsed = time.monotonic() - began
            with state_lock:
                state["in_flight"] -= 1
                state["samples"].append(
                    {
                        "client": index,
                        "query": number,
                        "seconds": elapsed,
                        "ok": error is None,
                    }
                )
                if error:
                    state["errors"].append(error)
            iteration += 1
            stop.wait(0.5)

    container = "driving-data-platform-mac-postgres-1"

    def inspect_container():
        result = {}
        for name in (container, "driving-data-platform-mac-metastore-1"):
            item = json.loads(subprocess.check_output(["docker", "inspect", name]))[0]
            result[name] = {
                "restart_count": item["RestartCount"],
                "started_at": item["State"]["StartedAt"],
                "oom": item["State"]["OOMKilled"],
            }
        return result

    before = inspect_container()
    api_before = api_status(root)
    usage_before = usage(root, repo)
    if usage_before["status"] != "passed":
        raise ValueError("项目空间门禁未通过")
    resources = []

    def sample():
        while not stop.is_set():
            try:
                # 不传易消失的临时任务名称；采集后仅保留本项目容器。
                stats = subprocess.check_output(
                    ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
                    text=True,
                    timeout=10,
                )
                resources.append(
                    {
                        "elapsed": time.monotonic() - start,
                        "docker": [
                            item
                            for line in stats.splitlines()
                            if (item := json.loads(line))["Name"].startswith(
                                "driving-data-platform-mac-"
                            )
                        ],
                        "host_vm_stat": subprocess.check_output(["vm_stat"], text=True),
                        "disk_free": shutil.disk_usage(root).free,
                        "api_cpu_memory": subprocess.check_output(
                            [
                                "ps",
                                "-p",
                                str(api_before["pid"]),
                                "-o",
                                "%cpu=",
                                "-o",
                                "rss=",
                            ],
                            text=True,
                        ).strip(),
                    }
                )
            except (OSError, subprocess.SubprocessError, ValueError) as error:
                resources.append({"error": str(error)})
            stop.wait(5)

    rounds = []
    failure = None
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(client, index) for index in range(5)]
        sampler = pool.submit(sample)
        try:

            def ready(loaded):
                with state_lock:
                    known.add(loaded["generation"])

            for version in (DEFAULT_VERSION, "acceptance-" + uuid.uuid4().hex):
                result = run(root, repo, version, on_ready=ready)
                rounds.append(result)
                (report / "rounds.json").write_text(json.dumps(rounds, indent=2))
            # 同版本再次执行，服务身份与目录身份必须完全复用。
            repeated = run(root, repo, DEFAULT_VERSION, on_ready=ready)
            if (
                not repeated["steps"]["load"]["reused"]
                or repeated["steps"]["load"]["generation"]
                != rounds[0]["steps"]["load"]["generation"]
            ):
                raise ValueError("默认链路重跑产生了重复服务版本")
            rounds.append(repeated)
            (report / "rounds.json").write_text(json.dumps(rounds, indent=2))
            with connect(root) as conn:
                pointer = active(conn)
            rollback = publish(root, pinned, pointer, rollback=True)
            backup = verify_backup(root, repo)
            while time.monotonic() - start < args.duration:
                # 进度文件每 5 秒刷新；不阻塞其他用户交互。
                with state_lock:
                    progress = {
                        "elapsed": time.monotonic() - start,
                        "requests": len(state["samples"]),
                        "errors": len(state["errors"]),
                    }
                (report / "progress.json").write_text(json.dumps(progress))
                time.sleep(min(5, max(0, args.duration - (time.monotonic() - start))))
        except (
            OSError,
            ValueError,
            KeyError,
            psycopg.Error,
            subprocess.SubprocessError,
        ) as error:
            failure = type(error).__name__ + ": " + str(error)
        finally:
            stop.set()
            for future in futures:
                future.result()
            sampler.result()
    after = inspect_container()
    api_after = api_status(root)
    usage_after = usage(root, repo)
    elapsed = time.monotonic() - start
    samples = state["samples"]
    durations = sorted(s["seconds"] for s in samples)
    p95 = durations[max(0, math.ceil(len(durations) * 0.95) - 1)] if durations else None
    (report / "requests.json").write_text(json.dumps(samples))
    (report / "resources.json").write_text(json.dumps(resources, indent=2))
    summary = {
        "status": "passed"
        if failure is None
        and not state["errors"]
        and p95 is not None
        and p95 <= 2
        and before == after
        and all(not value["oom"] for value in after.values())
        and usage_after["status"] == "passed"
        and resources
        and all("error" not in r for r in resources)
        and api_before == api_after
        and api_after["running"]
        else "failed",
        "scope": "final-30-minute"
        if args.duration >= 1800 and elapsed >= 1800
        else "short-rehearsal-only",
        "failure": failure,
        "duration_seconds": elapsed,
        "virtual_clients": 5,
        "peak_in_flight": state["peak_in_flight"],
        "requests": len(samples),
        "errors": state["errors"],
        "error_rate": len(state["errors"]) / len(samples) if samples else 1,
        "throughput_rps": len(samples) / elapsed,
        "p95_seconds": p95,
        "usage_before": usage_before,
        "usage_after": usage_after,
        "api_before": api_before,
        "api_after": api_after,
        "container_before": before,
        "container_after": after,
        "rounds": rounds,
        "rollback": rollback if failure is None else None,
        "backup": backup if failure is None else None,
        "same_machine_contention": True,
        "report_directory": str(report),
    }
    (report / "report.json").write_text(json.dumps(summary, indent=2))
    print(
        json.dumps(
            {
                k: v
                for k, v in summary.items()
                if k not in {"rounds", "backup", "errors"}
            }
        )
    )
    return 0 if summary["status"] == "passed" else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=int, default=1800)
    args = parser.parse_args()
    if args.duration < 20:
        parser.error("最短试运行 20 秒，正式验收至少 1800 秒")
    root = Path(
        os.environ.get(
            "DDP_OUTPUT_ROOT",
            "/Users/qiujiajun/Project/driving-data-platform-artifacts",
        )
    )
    from driving_data_platform.common.operations import operation_lock

    try:
        with operation_lock(root):
            return execute(args, root)
    except (OSError, ValueError) as error:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "error": "无法取得串行验证锁或读取运行证据: " + str(error),
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
