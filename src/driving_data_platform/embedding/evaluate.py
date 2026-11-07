"""预先固定标注、串行双轮参数比较；不提交全量产物。"""

import math
import os
import statistics
import subprocess
import threading
import time
import uuid
from pathlib import Path

from driving_data_platform.common.config import (
    application_config,
    port,
)
from driving_data_platform.embedding import artifacts as a
from driving_data_platform.embedding import build, model
from driving_data_platform.ingestion.nuscenes import atomic_json


def sample(root, input_id):
    source = build.source(root, input_id)
    cfg = application_config(root)
    dataset = Path(cfg["dataset"]["root"])
    metadata = dataset / cfg["dataset"]["version"]
    import json

    samples = {
        r["token"]: r for r in json.loads((metadata / "sample.json").read_text())
    }
    media = {
        r["filename"]: samples[r["sample_token"]]
        for r in json.loads((metadata / "sample_data.json").read_text())
        if r["is_key_frame"]
    }
    first = {}
    for entry in source["entries"]:
        row = media[entry["path"]]
        key = (row["timestamp"], row["token"])
        first[row["scene_token"]] = min(first.get(row["scene_token"], key), key)
    selected = []
    for scene in sorted(first):
        entries = [
            e for e in source["entries"] if media[e["path"]]["token"] == first[scene][1]
        ]
        if len(entries) != 6:
            raise ValueError("抽样关键帧不是六路相机")
        selected.extend(sorted(entries, key=lambda e: e["channel"]))
    return {
        "input_id": input_id,
        "rule": "scene_token ascending; earliest (timestamp, sample_token); channel ascending; six cameras",
        "entries": selected,
    }, dataset


def cosine(a_, b):
    return math.fsum(x * y for x, y in zip(a_, b))


def execute(root, input_id, *, space_id=None):
    if space_id is not None:
        from driving_data_platform.embedding.external_evaluation import (
            execute as external,
        )

        return external(root, input_id, space_id)
    primary = model.state_path(root)
    if primary.exists():
        state = a.read(primary.parent, primary.name)
        try:
            os.kill(state["pid"], 0)
        except ProcessLookupError:
            pass
        else:
            raise ValueError(
                "正常端口已有项目模型；比较会额外占用模型内存，请先显式停止或在资源重新核验后比较"
            )
    manifest, dataset = sample(root, input_id)
    folder = a.safe_parent(root, "evaluations")
    a.commit(
        root,
        "evaluation-samples",
        a.checksum(manifest),
        {"sample.json": a.json_bytes(manifest)},
    )
    labels = a.read(root, "embedding/evaluation-labels.json")
    if (
        labels.get("sample_sha256") != a.checksum(manifest)
        or not labels.get("review_method")
        or not labels.get("reviewed_before_results")
    ):
        raise ValueError("必须先逐图审阅并固定文本标签及不确定项")
    queries = [q for q in labels["queries"] if q["positive_media_ids"]]
    ids = {e["media_id"] for e in manifest["entries"]}
    if len(queries) < 6 or any(
        not set(q["positive_media_ids"]) <= ids for q in queries
    ):
        raise ValueError("至少需要六条有已审阅正例的查询；不足则补充下一关键帧")
    if set(labels.get("reviewed_media_ids", [])) != ids:
        raise ValueError("全部抽样图片都必须完成审阅")
    # 首个结果出现前将原始标签不可变落盘，避免结果反向影响标注。
    labels_id = a.checksum(labels)
    a.commit(
        root, "evaluation-labels", labels_id, {"labels.json": a.json_bytes(labels)}
    )
    report = {
        "status": "running",
        "input_id": input_id,
        "sample": manifest,
        "labels_sha256": labels_id,
        "candidates": [],
        "thresholds": {"repeat_cosine": 0.9999, "hit_at_5": 0.8},
        "limitations": "小样本基本相关性检查；RSS 与统一内存日志分开，不代表完整峰值或全套服务容量",
    }
    path = folder / (uuid.uuid4().hex + ".json")
    candidates = {}
    for file in (root / "embedding/spaces").glob("*/space.json"):
        value = a.read(file.parent, file.name)
        if (
            value.get("schema_version") == 2
            and value["implementation_sha256"] == model.implementation()
        ):
            limit = value["runtime_parameters"]["image_max_tokens"]
            sid = a.validate_space(value)
            if limit in candidates and candidates[limit] != sid:
                raise ValueError("同一 token 配置有多个候选身份，需先核对登记")
            candidates[limit] = sid
    if set(candidates) != {256, 512, 1024}:
        from driving_data_platform.embedding.prepare import execute as prepare

        for sid in prepare(root):
            contract = model.space(root, sid)
            candidates[contract["runtime_parameters"]["image_max_tokens"]] = sid
    atomic_json(path, report)
    try:
        for limit in (256, 512, 1024):
            if limit == 1024 and any(c["passed"] for c in report["candidates"]):
                break
            if limit not in candidates:
                raise ValueError(f"缺少 {limit} token 的已核验候选空间")
            space_id = candidates[limit]
            state = model.up(root, space_id, port("diagnostic"))
            stop = threading.Event()
            rss = []
            pressure = []

            def monitor(stop=stop, state=state, rss=rss):
                while not stop.wait(0.5):
                    result = subprocess.run(
                        ["ps", "-p", str(state["pid"]), "-o", "rss="],
                        capture_output=True,
                        check=False,
                        text=True,
                    )
                    if result.stdout.strip():
                        rss.append(int(result.stdout.strip()) * 1024)

            thread = threading.Thread(target=monitor)
            thread.start()
            try:
                client = model.encoder(root, space_id, port("diagnostic"))
                client.encode_image(build.image(dataset, manifest["entries"][0]))
                texts = [
                    client.encode_text(t)
                    for t in ("路口有一辆汽车", "十字路口有一台车", "厨房里正在切水果")
                ]
                query_vectors = [client.encode_text(q["text"]) for q in queries]
                rounds = []
                latencies = []
                for _ in range(2):
                    pressure.append(
                        subprocess.check_output(["memory_pressure", "-Q"], text=True)
                    )
                    vectors = []
                    for entry in manifest["entries"]:
                        started = time.monotonic()
                        vector = client.encode_image(build.image(dataset, entry))
                        latencies.append(time.monotonic() - started)
                        vectors.append(vector)
                    rounds.append(vectors)
                repeat = min(cosine(x, y) for x, y in zip(*rounds))
                hits = []
                for query, vector in zip(queries, query_vectors):
                    ranked = sorted(
                        zip(manifest["entries"], rounds[0]),
                        key=lambda pair: (
                            -cosine(vector, pair[1]),
                            pair[0]["media_id"],
                        ),
                    )
                    top = [e["media_id"] for e, _ in ranked[:5]]
                    hits.append(
                        {
                            "text": query["text"],
                            "top5": top,
                            "hit": bool(set(top) & set(query["positive_media_ids"])),
                        }
                    )
                score = sum(h["hit"] for h in hits) / len(hits)
                result = {
                    "space_id": space_id,
                    "image_max_tokens": limit,
                    "repeat_cosine_min": repeat,
                    "hit_at_5": score,
                    "queries": hits,
                    "latencies": latencies,
                    "image_p95": sorted(latencies)[
                        math.ceil(len(latencies) * 0.95) - 1
                    ],
                    "image_median": statistics.median(latencies),
                    "rss_peak_sampled": max(rss, default=0),
                    "system_memory_pressure": pressure,
                    "server_log": state["log"],
                    "passed": repeat >= 0.9999
                    and score >= 0.8
                    and cosine(texts[0], texts[1]) > cosine(texts[0], texts[2]),
                }
                if result["rss_peak_sampled"] > 6 * 1024**3:
                    result["passed"] = False
                    result["resource_blocked"] = True
                report["candidates"].append(result)
                atomic_json(path, report)
            finally:
                stop.set()
                thread.join()
                if not state["reused"]:
                    model.stop(root, port("diagnostic"))
        passed = [c for c in report["candidates"] if c["passed"]]
        if not passed:
            report["status"] = "failed"
            return report
        fastest = min(c["image_p95"] for c in passed)
        selected = min(
            [c for c in passed if c["image_p95"] <= fastest * 1.1],
            key=lambda c: (c["rss_peak_sampled"], c["image_max_tokens"]),
        )
        report.update(status="passed", selected_space_id=selected["space_id"])
        atomic_json(path, report)
        atomic_json(
            root / "embedding/selection.json",
            {
                "status": "passed",
                "input_id": input_id,
                "space_id": selected["space_id"],
                "report": str(path.relative_to(root)),
                "report_sha256": a.checksum(report),
            },
        )
        return report
    except BaseException:
        report["status"] = "failed"
        raise
    finally:
        atomic_json(path, report)
