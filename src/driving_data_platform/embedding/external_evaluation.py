"""Real external image/text evaluation against labels frozen before inference."""

import math
import time
import uuid

from driving_data_platform.embedding import artifacts as a
from driving_data_platform.embedding import build, model
from driving_data_platform.embedding.runtime import encoder
from driving_data_platform.ingestion.nuscenes import atomic_json


def execute(root, input_id, space_id):
    from driving_data_platform.embedding.evaluate import cosine, sample

    contract = model.space(root, space_id)
    if contract["schema_version"] != 3:
        raise ValueError("外部评测要求 v3 独立空间")
    manifest, dataset = sample(root, input_id)
    labels = a.read(root, "embedding/evaluation-labels.json")
    ids = {e["media_id"] for e in manifest["entries"]}
    queries = [q for q in labels["queries"] if q["positive_media_ids"]]
    if (
        labels.get("sample_sha256") != a.checksum(manifest)
        or not labels.get("review_method")
        or labels.get("reviewed_before_results") is not True
        or set(labels.get("reviewed_media_ids", [])) != ids
        or len(queries) < 6
        or any(not set(q["positive_media_ids"]) <= ids for q in queries)
    ):
        raise ValueError("必须先固定全部图片审阅与至少六条有效查询正例")
    lid = a.checksum(labels)
    a.commit(root, "evaluation-labels", lid, {"labels.json": a.json_bytes(labels)})
    path = a.safe_parent(root, "evaluations") / (uuid.uuid4().hex + ".json")
    report = {
        "status": "running",
        "backend": "external_api",
        "space_id": space_id,
        "input_id": input_id,
        "labels_sha256": lid,
        "sample": manifest,
        "scope": "real-external-labeled-sample",
        "limitations": "样本相关性与重复性，不代表全量质量或服务容量",
    }
    atomic_json(path, report)
    try:
        client = encoder(root, space_id)
        client.check_identity()
        rounds, elapsed = [], []
        for _ in range(2):
            vectors = []
            for entry in manifest["entries"]:
                start = time.monotonic()
                vectors.append(client.encode_image(build.image(dataset, entry)))
                elapsed.append(time.monotonic() - start)
            rounds.append(vectors)
        hits = []
        for q in queries:
            vector = client.encode_text(q["text"])
            ranked = sorted(
                zip(manifest["entries"], rounds[0]),
                key=lambda pair: (-cosine(vector, pair[1]), pair[0]["media_id"]),
            )
            top = [e["media_id"] for e, _ in ranked[:5]]
            hits.append(
                {"top5": top, "hit": bool(set(top) & set(q["positive_media_ids"]))}
            )
        repeat = min(cosine(x, y) for x, y in zip(*rounds))
        score = sum(h["hit"] for h in hits) / len(hits)
        passed = repeat >= 0.9999 and score >= 0.8
        report.update(
            status="passed" if passed else "failed",
            repeat_cosine_min=repeat,
            hit_at_5=score,
            queries=hits,
            image_p95=sorted(elapsed)[math.ceil(len(elapsed) * 0.95) - 1],
        )
        if passed:
            report["selected_space_id"] = space_id
            atomic_json(path, report)
            atomic_json(
                a.safe_parent(root, "selections") / (space_id + ".json"),
                {
                    "status": "passed",
                    "input_id": input_id,
                    "space_id": space_id,
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
