"""真实图片HTTP验收：只查询已有试验库，独立余弦核对及显式故障恢复。"""

import argparse
import hashlib
import io
import json
import os
import signal
import socket
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from http.client import HTTPConnection
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import embedding_api_process as api
from embedding_ops import trial_up
from embedding_search_resources import inventory
from PIL import Image
from verify_backup import fingerprint
from verify_embedding_search import MAIN, baseline

from driving_data_platform.common.config import (
    application_config,
    output_root,
    port,
)
from driving_data_platform.common.operations import operation_lock
from driving_data_platform.embedding import artifacts as a
from driving_data_platform.embedding import build, gate, model, store
from driving_data_platform.ingestion.nuscenes import atomic_json

BASE = "http://127.0.0.1:" + str(port("trial_api"))
EMPTY = {
    "location": None,
    "start_us": None,
    "end_us": None,
    "category": None,
    "channels": [],
}


def http(path, data=None, mime="image/jpeg", expected=200):
    request = Request(BASE + path, data=data, headers={"Content-Type": mime})
    try:
        with urlopen(request, timeout=90) as response:
            code, raw = response.status, response.read()
    except HTTPError as error:
        code, raw = error.code, error.read()
    if expected is not None and code != expected:
        raise ValueError(f"HTTP状态不一致：{code}/{expected}")
    return code, raw if path.startswith("/api/media/") else json.loads(raw)


def fixtures(root, conn, source, folder):
    """按相机和场景选两张原图；衍生PNG只作为明确的验收输入。"""
    scenes = dict(
        conn.execute(
            "SELECT entity_id,scene_id FROM serving.frame WHERE generation=%s",
            (source["generation"],),
        )
    )
    dataset = Path(application_config(root)["dataset"]["root"])
    selected = []
    for channel in sorted({e["channel"] for e in source["entries"]}):
        candidates = [e for e in source["entries"] if e["channel"] == channel]
        first = candidates[0]
        second = next(
            e for e in candidates if scenes[e["frame_id"]] != scenes[first["frame_id"]]
        )
        selected.extend((first, second))
    result = []
    folder.mkdir(exist_ok=True)
    for index, entry in enumerate(selected):
        data = build.image(dataset, entry)
        result.append(
            {"media_id": entry["media_id"], "data": data, "mime": "image/jpeg"}
        )
        if index < 6:
            with Image.open(io.BytesIO(data)) as image:
                stream = io.BytesIO()
                image.save(stream, format="PNG")
            png = stream.getvalue()
            (folder / f"derived-{index}.png").write_bytes(png)
            result.append({"media_id": None, "data": png, "mime": "image/png"})
    atomic_json(
        folder / "manifest.json",
        {
            "input_id": a.checksum(source),
            "fixtures": [
                {
                    "media_id": r["media_id"],
                    "sha256": hashlib.sha256(r["data"]).hexdigest(),
                    "bytes": len(r["data"]),
                    "mime": r["mime"],
                }
                for r in result
            ],
        },
    )
    return result


def compare(result, expected, scores):
    if len(result["items"]) != len(expected):
        raise ValueError("结果数量不一致")
    ties = []
    for rank, (item, (mid, score)) in enumerate(
        zip(result["items"], expected, strict=True), 1
    ):
        if abs(item["score"] - scores[item["media_id"]]) > 1e-5:
            raise ValueError("独立余弦分数不一致")
        if item["media_id"] != mid:
            if abs(item["score"] - score) > 1e-5:
                raise ValueError("独立排序不一致")
            ties.append(rank)
    return ties


def verify(root, repo, trial, artifact, faults):
    if artifact != MAIN:
        raise ValueError("本轮要求已验收主产物")
    folder = root / "reports" / ("embedding-image-" + uuid.uuid4().hex)
    folder.mkdir()
    report = {
        "status": "running",
        "checks": [],
        "near_ties": [],
        "directory": str(folder),
    }
    state = a.trial_config(root, trial)
    initial = json.loads(
        subprocess.check_output(["docker", "inspect", state["container"]])
    )[0]
    mr = ar = None
    try:
        report["resources_before"] = inventory(root, repo, 1024**3)
        if report["resources_before"]["status"] != "passed":
            raise ValueError("资源门禁未通过")
        trial_up(root, repo, trial)
        api.validate(root, trial)
        _, source, contract, rows = a.verify_artifact(root, artifact)
        sid = a.validate_space(contract)
        with store.connect(root, trial=trial) as conn:
            before = fingerprint(conn)
            if a.media_input(conn, source["generation"]) != source:
                raise ValueError("产物绑定业务清单不一致")
            cases = fixtures(root, conn, source, folder / "fixtures")
        mr = model.up(root, sid)
        ar = api.start(root, repo, trial)
        body = {"generation": source["generation"]}
        path = "/api/embedding/search/image?"
        observations = []
        for index, fixture in enumerate(cases):
            vector = model.encoder(root, sid).encode_image(fixture["data"])
            with store.connect(root, trial=trial) as conn:
                all_expected = baseline(conn, source, rows, vector, EMPTY, None, 2424)
            scores = dict(all_expected)
            if (
                fixture["media_id"]
                and scores[fixture["media_id"]] < all_expected[0][1] - 1e-5
            ):
                raise ValueError("自身原图不在最高分组")
            for limit in (1, 20, 100):
                result = http(
                    path + urlencode(body | {"limit": limit}),
                    fixture["data"],
                    fixture["mime"],
                )[1]
                if (
                    result["embedding_generation"] != "e_" + MAIN
                    or result["space_id"] != sid
                    or result["generation"] != source["generation"]
                ):
                    raise ValueError("响应版本身份不一致")
                report["near_ties"].extend(
                    compare(result, all_expected[:limit], scores)
                )
            again = http(
                path + urlencode(body | {"limit": 100}),
                fixture["data"],
                fixture["mime"],
            )[1]
            if result["items"] != again["items"]:
                raise ValueError("同图重复排序不稳定")
            top = again["items"][0]
            media = http(top["media_url"])[1]
            entry = next(
                e for e in source["entries"] if e["media_id"] == top["media_id"]
            )
            if hashlib.sha256(media).hexdigest() != entry["sha256"]:
                raise ValueError("返回图片摘要错误")
            detail = http(top["detail_url"])[1]
            if (
                detail["generation"] != source["generation"]
                or detail["frame"]["entity_id"] != top["frame_id"]
            ):
                raise ValueError("详情版本错误")
            observations.append(
                {"fixture": index, "timings": again["timings"], "count": again["count"]}
            )
            atomic_json(folder / "observations.json", observations)
        report["checks"].append(
            "12原图与6PNG真实编码、Top1/20/100、独立余弦、自身最高分组、稳定性和图片详情"
        )
        fixture = cases[0]
        vector = model.encoder(root, sid).encode_image(fixture["data"])
        with store.connect(root, trial=trial) as conn:
            location, ts = conn.execute(
                "SELECT location,timestamp_us FROM serving.frame WHERE generation=%s ORDER BY timestamp_us LIMIT 1",
                (source["generation"],),
            ).fetchone()
            category = conn.execute(
                "SELECT category FROM serving.frame_category WHERE generation=%s ORDER BY category LIMIT 1",
                (source["generation"],),
            ).fetchone()[0]
            channel = source["entries"][0]["channel"]
            boundary = datetime.fromtimestamp(ts / 1e6, UTC).isoformat()
            variants = [
                ({"location": location}, {"location": location}, None),
                ({"category": category}, {"category": category}, None),
                (
                    {"start": boundary, "end": boundary},
                    {"start_us": ts, "end_us": ts},
                    None,
                ),
                (
                    {"required_channels": [channel, channel]},
                    {"channels": [channel]},
                    None,
                ),
                ({"image_channel": channel}, {}, channel),
                (
                    {
                        "location": location,
                        "category": category,
                        "required_channels": [channel],
                    },
                    {"location": location, "category": category, "channels": [channel]},
                    None,
                ),
                ({"location": "no-such-place"}, {"location": "no-such-place"}, None),
            ]
            for changes, conditions, image_channel in variants:
                result = http(
                    path + urlencode(body | changes, doseq=True), fixture["data"]
                )[1]
                expected = baseline(
                    conn, source, rows, vector, EMPTY | conditions, image_channel, 20
                )
                scores = dict(
                    baseline(
                        conn,
                        source,
                        rows,
                        vector,
                        EMPTY | conditions,
                        image_channel,
                        2424,
                    )
                )
                report["near_ties"].extend(compare(result, expected, scores))
        for changes, expected in [
            ({"limit": 0}, 422),
            ({"limit": 101}, 422),
            ({"start": "bad"}, 422),
            ({"generation": "g_" + "0" * 64}, 404),
            ({"embedding_generation": "e_" + "0" * 64}, 404),
        ]:
            http(path + urlencode(body | changes), fixture["data"], expected=expected)
        for data, mime, status in [
            (b"", "image/jpeg", 422),
            (b"broken-private-image", "image/jpeg", 422),
            (fixture["data"], "image/png", 422),
            (fixture["data"], "image/gif", 415),
        ]:
            http(path + urlencode(body), data, mime, status)
        oversized = HTTPConnection("127.0.0.1", port("trial_api"), timeout=5)
        try:
            oversized.putrequest("POST", path + urlencode(body))
            oversized.putheader("Content-Type", "application/octet-stream")
            oversized.putheader("Content-Length", str(25 * 1024**2 + 1))
            oversized.endheaders()
            if oversized.getresponse().status != 413:
                raise ValueError("超限声明未提前拒绝")
        finally:
            oversized.close()
        with gate.acquire(root):
            http(path + urlencode(body), fixture["data"], expected=429)
            http(
                "/api/embedding/search",
                json.dumps(body | {"text": "路口有汽车"}).encode(),
                "application/json",
                429,
            )
        report["checks"].append("筛选/UTC边界/空结果/非法参数与图片、共享锁")
        if faults:
            if mr["reused"] or ar["reused"] or initial["State"]["Running"]:
                raise ValueError("故障只允许操作本轮启动的依赖")
            # 模型故障及恢复；不触碰正式数据库。
            model.stop(root)
            http(path + urlencode(body), fixture["data"], expected=503)
            mr = model.up(root, sid)
            http(path + urlencode(body), fixture["data"])
            subprocess.run(
                ["docker", "stop", state["container"]], check=True, capture_output=True
            )
            try:
                http(path + urlencode(body), fixture["data"], expected=503)
            finally:
                trial_up(root, repo, trial)
            http(path + urlencode(body), fixture["data"])
            # 在模型调用阻塞后改变真实活动指针，验证请求已固定原版本。
            from verify_embedding_search import AUX

            pid = model.checked(root)["pid"]
            os.kill(pid, signal.SIGSTOP)
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(http, path + urlencode(body), fixture["data"])
                try:
                    with store.connect(root, trial=trial) as conn:
                        deadline = time.monotonic() + 5
                        while time.monotonic() < deadline:
                            if conn.execute(
                                "SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND query LIKE 'SELECT contract FROM control.embedding_space%' LIMIT 1"
                            ).fetchone():
                                break
                            time.sleep(0.05)
                        else:
                            raise ValueError("未观察到固定版本后的模型等待")
                        conn.execute(
                            "UPDATE control.embedding_active SET embedding_generation=%s WHERE generation=%s",
                            ("e_" + AUX, source["generation"]),
                        )
                    os.kill(pid, signal.SIGCONT)
                    pinned = pending.result()[1]
                    if pinned["embedding_generation"] != "e_" + MAIN:
                        raise ValueError("查询中途切换了向量版本")
                finally:
                    os.kill(pid, signal.SIGCONT)
                    with store.connect(root, trial=trial) as conn:
                        conn.execute(
                            "UPDATE control.embedding_active SET embedding_generation=%s WHERE generation=%s",
                            ("e_" + MAIN, source["generation"]),
                        )
            # 编码中客户端断开仍持锁，实际工作完成后才可再次检索。
            os.kill(pid, signal.SIGSTOP)
            client = socket.create_connection(
                ("127.0.0.1", port("trial_api")), timeout=5
            )
            try:
                client.sendall(
                    (
                        "POST "
                        + path
                        + urlencode(body)
                        + " HTTP/1.1\r\nHost: localhost\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + str(len(fixture["data"]))
                        + "\r\n\r\n"
                    ).encode()
                    + fixture["data"]
                )
                time.sleep(0.5)
                client.close()
                http(
                    "/api/embedding/search",
                    json.dumps(body | {"text": "路口有汽车"}).encode(),
                    "application/json",
                    429,
                )
            finally:
                client.close()
                os.kill(pid, signal.SIGCONT)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                code, _ = http(path + urlencode(body), fixture["data"], expected=None)
                if code == 200:
                    break
                if code != 429:
                    raise ValueError("断开后的恢复状态异常")
                time.sleep(0.1)
            else:
                raise ValueError("断开后未释放工作锁")
            report["checks"].append(
                "活动指针变化仍固定原版本、编码中断开持锁及完成后恢复"
            )
            # 上传中断随后可重查。
            client = socket.create_connection(
                ("127.0.0.1", port("trial_api")), timeout=5
            )
            client.sendall(
                (
                    "POST "
                    + path
                    + urlencode(body)
                    + " HTTP/1.1\r\nHost: localhost\r\nContent-Type: image/jpeg\r\nContent-Length: 10000\r\n\r\n"
                ).encode()
                + b"partial"
            )
            client.close()
            time.sleep(0.2)
            http(path + urlencode(body), fixture["data"])
            report["checks"].append("本轮模型/试验库故障与恢复、真实上传中断恢复")
        samples = []

        def client(index):
            deadline = time.monotonic() + 60
            iteration = 0
            while time.monotonic() < deadline:
                began = time.monotonic()
                if (index + iteration) % 2:
                    code, _ = http(
                        path + urlencode(body), fixture["data"], expected=None
                    )
                else:
                    code, _ = http(
                        "/api/embedding/search",
                        json.dumps(body | {"text": "路口有汽车"}).encode(),
                        "application/json",
                        None,
                    )
                samples.append(
                    {
                        "client": index,
                        "status": code,
                        "seconds": time.monotonic() - began,
                    }
                )
                iteration += 1
                time.sleep(max(0, 1 - (time.monotonic() - began)))

        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(client, range(5)))
        atomic_json(folder / "load.json", samples)
        if any(x["status"] not in (200, 429) for x in samples) or not any(
            x["status"] == 200 for x in samples
        ):
            raise ValueError("混合固定负载出现非预期状态")
        report["load"] = {
            "virtual_clients": 5,
            "seconds": 60,
            "requests": len(samples),
            "success": sum(x["status"] == 200 for x in samples),
            "busy": sum(x["status"] == 429 for x in samples),
        }
        with store.connect(root, trial=trial) as conn:
            if fingerprint(conn) != before:
                raise ValueError("查询改变数据库指纹")
        report["status"] = "passed"
    except BaseException as error:
        report.update(status="failed", error=type(error).__name__)
        raise
    finally:
        try:
            if ar and not ar["reused"]:
                api.stop(root, trial)
        finally:
            if mr and not mr["reused"]:
                model.stop(root)
            if not initial["State"]["Running"]:
                subprocess.run(
                    ["docker", "stop", state["container"]],
                    check=True,
                    capture_output=True,
                )
            report["resources_after"] = inventory(root, repo)
            if report["resources_after"]["status"] != "passed":
                report["status"] = "failed"
            atomic_json(folder / "report.json", report)
            print(
                json.dumps({"status": report["status"], "directory": str(folder)}),
                flush=True,
            )
    if report["status"] != "passed":
        raise ValueError("最终资源门禁失败")


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
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 — 独立验收边界
        print(json.dumps({"status": "failed", "error": type(error).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
