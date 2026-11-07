"""组合验收中的真实多模态图文检索探针；与五个业务客户端分别计量。"""

import hashlib
import json
import subprocess
import time
import uuid
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from verify_embedding_search import baseline

from driving_data_platform.common.config import port
from driving_data_platform.embedding import artifacts as a
from driving_data_platform.embedding import runtime as model
from driving_data_platform.embedding import store


class EmbeddingProbe:
    def __init__(self, root):
        self.root = root
        self.deployment = a.read(root, "embedding/production.json")["deployment_id"]
        self.artifact = model.active_space(root, self.deployment)["artifact_id"]
        _, self.source, contract, rows = a.verify_artifact(root, self.artifact)
        self.space = a.validate_space(contract)
        self.initial = model.checked(root, expected=self.space)
        self.samples = []
        self.errors = []
        self.queries = []
        conditions = {
            "location": None,
            "start_us": None,
            "end_us": None,
            "category": None,
            "channels": [],
        }
        # 预期值直接来自不可变向量和独立 Python 余弦计算。
        for item in a.read(root, "embedding/evaluation-labels.json")["queries"]:
            if not item["positive_media_ids"]:
                continue
            text = item["text"]
            vector = model.encoder(root, self.space).encode_text(text)
            with store.connect(root, deployment=self.deployment) as conn:
                expected = baseline(
                    conn, self.source, rows, vector, conditions, None, 20
                )
            self.queries.append(("text", text, "application/json", expected))
        if len(self.queries) < 6:
            raise ValueError("至少需要六条已审阅的固定文本查询")

        from verify_embedding_image import fixtures

        fixture_folder = (
            root / "reports" / ("integration-image-fixtures-" + uuid.uuid4().hex)
        )
        with store.connect(root, deployment=self.deployment) as conn:
            images = fixtures(root, conn, self.source, fixture_folder)
        image_queries = []
        for image in images:
            vector = model.encoder(root, self.space).encode_image(image["data"])
            with store.connect(root, deployment=self.deployment) as conn:
                expected = baseline(
                    conn, self.source, rows, vector, conditions, None, 20
                )
            image_queries.append(("image", image["data"], image["mime"], expected))
        texts = self.queries
        self.queries = []
        for index, image in enumerate(image_queries):
            self.queries.append(image)
            if index < len(texts):
                self.queries.append(texts[index])
        self.expected_counts = {"text": len(texts), "image": len(image_queries)}

    def resource(self):
        state = model.checked(self.root, expected=self.space)
        if state.get("backend") == "external_api":
            return {
                "backend": "external_api",
                "space_id": self.space,
                "managed_process": False,
            }
        if (state["pid"], state["process_identity"]) != (
            self.initial["pid"],
            self.initial["process_identity"],
        ):
            raise ValueError("模型进程身份在组合验收中变化")
        return {
            "pid": state["pid"],
            "cpu_rss": subprocess.check_output(
                ["ps", "-p", str(state["pid"]), "-o", "%cpu=", "-o", "rss="],
                text=True,
            ).strip(),
        }

    def run(self, stop, report, maximum=None):
        index = 0
        while not stop.is_set():
            kind, payload, mime, expected = self.queries[index % len(self.queries)]
            began = time.monotonic()
            sample = {
                "query_type": kind,
                "query_sha256": hashlib.sha256(
                    payload.encode() if kind == "text" else payload
                ).hexdigest(),
            }
            try:
                body = {
                    "generation": self.source["generation"],
                    "embedding_generation": "e_" + self.artifact,
                    "limit": 20,
                }
                if kind == "text":
                    body["text"] = payload
                    url = (
                        "http://127.0.0.1:" + str(port("api")) + "/api/embedding/search"
                    )
                    data = json.dumps(body).encode()
                else:
                    url = (
                        "http://127.0.0.1:"
                        + str(port("api"))
                        + "/api/embedding/search/image?"
                    ) + urlencode(body)
                    data = payload
                request = Request(url, data=data, headers={"Content-Type": mime})
                with urlopen(request, timeout=70) as response:
                    result = json.load(response)
                if (
                    result["generation"] != self.source["generation"]
                    or result["embedding_generation"] != "e_" + self.artifact
                    or result["space_id"] != self.space
                    or result["artifact_id"] != self.artifact
                    or [x["media_id"] for x in result["items"]]
                    != [x[0] for x in expected]
                    or any(
                        abs(x["score"] - score) > 1e-5
                        for x, (_, score) in zip(result["items"], expected, strict=True)
                    )
                ):
                    raise ValueError("多模态图文检索版本、独立排序或分数不一致")
                top = result["items"][0]
                with urlopen(
                    ("http://127.0.0.1:" + str(port("api"))) + top["media_url"],
                    timeout=10,
                ) as response:
                    digest = hashlib.sha256(response.read()).hexdigest()
                entry = next(
                    r
                    for r in self.source["entries"]
                    if r["media_id"] == top["media_id"]
                )
                if digest != entry["sha256"]:
                    raise ValueError("中文结果图片摘要不一致")
                sample.update(
                    ok=True, timings=result["timings"], count=len(result["items"])
                )
            except (OSError, ValueError, KeyError, StopIteration) as error:
                # 只保存异常类型，避免连接信息或查询正文进入报告。
                sample.update(ok=False, error=type(error).__name__)
                self.errors.append(sample["error"])
            sample["seconds"] = time.monotonic() - began
            self.samples.append(sample)
            (report / "embedding-requests.json").write_text(json.dumps(self.samples))
            index += 1
            if maximum is not None and index >= maximum:
                break
            stop.wait(5)

    def summary(self):
        return {
            "status": "passed"
            if all(
                len(
                    {
                        r["query_sha256"]
                        for r in self.samples
                        if r["query_type"] == kind and r["ok"]
                    }
                )
                >= count
                for kind, count in self.expected_counts.items()
            )
            and not self.errors
            else "failed",
            "deployment_id": self.deployment,
            "generation": self.source["generation"],
            "embedding_generation": "e_" + self.artifact,
            "space_id": self.space,
            "virtual_clients": 1,
            "requests": len(self.samples),
            "errors": self.errors,
            "requests_by_type": {
                kind: sum(r["query_type"] == kind for r in self.samples)
                for kind in self.expected_counts
            },
            "scope": "串行真实编码、独立排序分数及Top1图片摘要；与五个业务客户端并行",
        }
