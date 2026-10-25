"""不依赖 Spark 的逐实体、逐指标对账基准。"""

import json
from collections import defaultdict
from pathlib import Path


def baseline(source: Path) -> dict:
    records = {
        p.stem: [json.loads(line) for line in p.read_text().splitlines()]
        for p in (source / "records").glob("*.jsonl")
    }
    raw = {
        name: {
            r["source_token"]: dict(r["raw_record"], entity_id=r["entity_id"])
            for r in rows
        }
        for name, rows in records.items()
    }
    scenes = []
    for s in raw["scene"].values():
        scenes.append(
            {k: s[k] for k in ("entity_id", "name", "description")}
            | {
                "source_token": s["token"],
                "location": raw["log"][s["log_token"]]["location"],
            }
        )
    scene_index = {s["source_token"]: s for s in scenes}
    frames = []
    for f in raw["sample"].values():
        s = scene_index[f["scene_token"]]
        frames.append(
            {
                "entity_id": f["entity_id"],
                "source_token": f["token"],
                "timestamp_us": f["timestamp"],
                "scene_id": s["entity_id"],
                "scene_name": s["name"],
                "location": s["location"],
            }
        )
    media = []
    for m in raw["sample_data"].values():
        sensor = raw["sensor"][
            raw["calibrated_sensor"][m["calibrated_sensor_token"]]["sensor_token"]
        ]
        media.append(
            {
                "entity_id": m["entity_id"],
                "source_token": m["token"],
                "frame_id": raw["sample"][m["sample_token"]]["entity_id"],
                "timestamp_us": m["timestamp"],
                "path": m["filename"],
                "is_key_frame": m["is_key_frame"],
                "channel": sensor["channel"],
                "modality": sensor["modality"],
                **{
                    k: m[k]
                    for k in (
                        "width",
                        "height",
                        "calibrated_sensor_token",
                        "ego_pose_token",
                    )
                },
            }
        )
    annotations = []
    for a in raw["sample_annotation"].values():
        instance = raw["instance"][a["instance_token"]]
        annotations.append(
            {
                "entity_id": a["entity_id"],
                "source_token": a["token"],
                "frame_id": raw["sample"][a["sample_token"]]["entity_id"],
                "instance_id": instance["entity_id"],
                "category": raw["category"][instance["category_token"]]["name"],
                **{
                    k: a[k]
                    for k in (
                        "translation",
                        "size",
                        "rotation",
                        "num_lidar_pts",
                        "num_radar_pts",
                        "visibility_token",
                        "attribute_tokens",
                    )
                },
            }
        )
    groups = defaultdict(list)
    for f in frames:
        groups[("location", f["location"])].append(f)
    for a in annotations:
        groups[("category", a["category"])].append(a)
    for m in media:
        if m["is_key_frame"]:
            groups[("channel", m["channel"])].append(m)
    statistics = []
    for (dimension, value), rows in groups.items():
        statistics.append(
            {
                "dimension": dimension,
                "value": value,
                "frame_count": len(
                    {
                        r["entity_id"] if dimension == "location" else r["frame_id"]
                        for r in rows
                    }
                ),
                "annotation_count": len(rows) if dimension == "category" else 0,
                "object_count": len({r["instance_id"] for r in rows})
                if dimension == "category"
                else 0,
            }
        )
    return {
        "dim_scene": scenes,
        "dim_category": [
            {
                **{k: c[k] for k in ("entity_id", "name", "description")},
                "source_token": c["token"],
            }
            for c in raw["category"].values()
        ],
        "dim_sensor": [
            {
                **{k: s[k] for k in ("entity_id", "channel", "modality")},
                "source_token": s["token"],
            }
            for s in raw["sensor"].values()
        ],
        "dim_location": [
            {"location": loc} for loc in sorted({s["location"] for s in scenes})
        ],
        "fact_frame": frames,
        "fact_media": media,
        "fact_annotation": annotations,
        "frame_category": [
            {"frame_id": f, "category": c}
            for f, c in sorted({(a["frame_id"], a["category"]) for a in annotations})
        ],
        "frame_channel": [
            {"frame_id": f, "channel": c}
            for f, c in sorted(
                {(m["frame_id"], m["channel"]) for m in media if m["is_key_frame"]}
            )
        ],
        "metrics": [
            {
                "frame_count": len(frames),
                "annotation_count": len(annotations),
                "object_count": len({a["instance_id"] for a in annotations}),
                "sweep_count": sum(not m["is_key_frame"] for m in media),
                "keyframe_media_count": sum(m["is_key_frame"] for m in media),
            }
        ],
        "statistics": statistics,
    }
