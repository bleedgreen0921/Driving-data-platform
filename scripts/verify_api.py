"""真实 HTTP 与独立 ODS 基准核对；默认不启停服务。"""

import argparse
import csv
import hashlib
import io
import json
import os
import subprocess
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

from driving_data_platform.warehouse.baseline import baseline

BASE = "http://127.0.0.1:18080"


def get(path, expected=200):
    try:
        with urlopen(BASE + path, timeout=10) as response:
            data = response.read()
            status = response.status
    except HTTPError as error:
        status = error.code
        data = error.read()
    if status != expected:
        raise ValueError(f"HTTP {status}, expected {expected}: {path}")
    return data


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
    health = json.loads(get("/api/health"))
    generation = health["generation"]
    water = json.loads((root / "ingestion/watermark.json").read_text())
    source = (
        root
        / "ingestion/snapshots"
        / (water["snapshot_id"] + "-" + water["processing_version"])
    )
    expected = baseline(source)
    ordered = sorted(
        expected["fact_frame"], key=lambda r: (r["timestamp_us"], r["entity_id"])
    )
    collected = []
    for offset in range(0, len(ordered), 50):
        data = json.loads(
            get(
                "/api/frames?" + urlencode({"generation": generation, "offset": offset})
            )
        )
        if data["generation"] != generation or data["total"] != len(ordered):
            raise ValueError("分页版本或总数不一致")
        collected.extend(data["items"])
    if collected != ordered:
        raise ValueError("分页与独立基准不一致")
    cases = (
        [{}]
        + [{"location": r["location"]} for r in expected["dim_location"]]
        + [{"category": r["name"]} for r in expected["dim_category"]]
        + [
            {"category": "' OR 1=1 --"},
            {"channel": ["CAM_FRONT", "LIDAR_TOP"]},
            {"channel": ["not-a-channel"]},
            {
                "location": ordered[0]["location"],
                "category": "vehicle.car",
                "channel": ["CAM_FRONT", "RADAR_FRONT"],
            },
        ]
    )
    relations = {(r["frame_id"], r["category"]) for r in expected["frame_category"]}
    channels = {(r["frame_id"], r["channel"]) for r in expected["frame_channel"]}
    for case in cases:
        matches = [
            f
            for f in ordered
            if ("location" not in case or f["location"] == case["location"])
            and (
                "category" not in case
                or (f["entity_id"], case["category"]) in relations
            )
            and all((f["entity_id"], c) in channels for c in case.get("channel", []))
        ]
        query = urlencode({"generation": generation, **case, "limit": 200}, doseq=True)
        actual = json.loads(get("/api/frames?" + query))
        if actual["total"] != len(matches) or actual["items"] != matches[:200]:
            raise ValueError("筛选不一致: " + str(case))
    instant = datetime.fromtimestamp(
        ordered[0]["timestamp_us"] / 1000000, UTC
    ).isoformat()
    response = json.loads(
        get(
            "/api/frames?"
            + urlencode({"generation": generation, "start": instant, "end": instant})
        )
    )
    if len(response["items"]) != 1 or response["items"][0] != ordered[0]:
        raise ValueError("整数微秒时间边界不一致")
    for query in [
        "limit=201",
        "limit=0",
        "offset=-1",
        "start=2018-01-01",
        "start=2020-01-01T00:00:00Z&end=2018-01-01T00:00:00Z",
    ]:
        get("/api/frames?" + query, 422)
    get("/api/frames?generation=missing", 404)
    get("/api/media/missing?generation=" + generation, 404)
    get("/api/media/..%2F..%2Fetc%2Fpasswd?generation=" + generation, 404)
    detail = json.loads(
        get("/api/frames/" + ordered[0]["entity_id"] + "?generation=" + generation)
    )
    annotations = [
        a
        for a in expected["fact_annotation"]
        if a["frame_id"] == ordered[0]["entity_id"]
    ]
    if sorted(detail["annotations"], key=lambda r: r["entity_id"]) != sorted(
        annotations, key=lambda r: r["entity_id"]
    ):
        raise ValueError("标注不一致")
    pictures = 0
    for media in detail["media"]:
        if media["modality"] == "camera":
            picture = get(
                "/api/media/" + media["entity_id"] + "?generation=" + generation
            )
            if hashlib.sha256(picture).hexdigest() != media["sha256"]:
                raise ValueError("真实图片摘要不一致")
            pictures += 1
    analytics = json.loads(get("/api/analytics?generation=" + generation))
    if analytics["dimensions"]["metrics"] != expected["metrics"] or sorted(
        analytics["statistics"], key=lambda r: (r["dimension"], r["value"])
    ) != sorted(expected["statistics"], key=lambda r: (r["dimension"], r["value"])):
        raise ValueError("分析指标不一致")
    for format in ("jsonl", "csv"):
        bundle = get(
            "/api/export?" + urlencode({"generation": generation, "format": format})
        )
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            data = archive.read("frames." + format)
            manifest = json.loads(archive.read("manifest.json"))
        if (
            manifest["count"] != len(ordered)
            or manifest["generation"] != generation
            or manifest["sha256"] != hashlib.sha256(data).hexdigest()
        ):
            raise ValueError("导出摘要或数量不一致")
        exported = (
            [json.loads(line) for line in data.decode().splitlines()]
            if format == "jsonl"
            else list(csv.DictReader(io.StringIO(data.decode())))
        )
        if [r["entity_id"] for r in exported] != [r["entity_id"] for r in ordered]:
            raise ValueError("导出身份不一致")
    outage = False
    if args.faults:
        command = [
            "docker",
            "compose",
            "--env-file",
            str(root / "compose.env"),
            "-f",
            str(repo / "infra/docker/compose.yaml"),
        ]
        try:
            subprocess.run([*command, "stop", "postgres"], check=True)
            get("/api/health", 503)
            outage = True
        finally:
            subprocess.run([*command, "up", "-d", "--wait", "postgres"], check=True)
        if json.loads(get("/api/health"))["generation"] != generation:
            raise ValueError("恢复后版本改变")
    print(
        json.dumps(
            {
                "status": "passed",
                "generation": generation,
                "frames": len(ordered),
                "filter_cases": len(cases),
                "pictures": pictures,
                "exports": ["jsonl", "csv"],
                "dependency_outage_restored": outage,
            }
        )
    )


if __name__ == "__main__":
    main()
