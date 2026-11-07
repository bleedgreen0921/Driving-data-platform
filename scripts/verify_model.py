"""真实模型文件及 llama 多模态编码最小验收，不注册空间或写入数据库。"""

import argparse
import base64
import hashlib
import json
import math
import socket
import struct
import subprocess
import threading
import time
import uuid
from itertools import pairwise
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from driving_data_platform.common.config import (
    application_config,
    output_root,
    port,
)
from driving_data_platform.ingestion.nuscenes import atomic_json


def integrity(model, metadata):
    records = []
    for entry in metadata["Data"]["Files"]:
        if entry["Type"] != "blob":
            continue
        relative = Path(entry["Path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("上游清单包含越界路径")
        file = model / relative
        if file.is_symlink() or not file.is_file():
            raise ValueError("模型文件缺失或为符号链接：" + entry["Path"])
        with file.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if file.stat().st_size != entry["Size"] or actual != entry["Sha256"]:
            raise ValueError("模型文件缺失或摘要不匹配：" + entry["Path"])
        records.append(
            {
                "file": entry["Path"],
                "bytes": file.stat().st_size,
                "sha256": actual,
                "revision": entry["Revision"],
            }
        )
    if not records or not any(x["file"] == "model.safetensors" for x in records):
        raise ValueError("上游模型权重清单不完整")
    path = model / "model.safetensors"
    with path.open("rb") as stream:
        header_size = struct.unpack("<Q", stream.read(8))[0]
        if not 0 < header_size < 16 * 1024**2:
            raise ValueError("Safetensors 头部长度非法")
        header = json.loads(stream.read(header_size))
    sizes = {"BF16": 2, "F16": 2, "F32": 4, "I64": 8, "I32": 4, "BOOL": 1}
    spans = []
    for name, tensor in header.items():
        if name == "__metadata__":
            continue
        start, end = tensor["data_offsets"]
        if end - start != math.prod(tensor["shape"]) * sizes[tensor["dtype"]]:
            raise ValueError("张量形状与字节数不一致")
        spans.append((start, end))
    spans.sort()
    if (
        not spans
        or spans[0][0] != 0
        or any(x[1] != y[0] for x, y in pairwise(spans))
        or spans[-1][1] + header_size + 8 != path.stat().st_size
    ):
        raise ValueError("张量区间缺失、重叠或文件被截断")
    return {"files": records, "tensor_count": len(spans), "tensor_structure": "matched"}


def cosine(a, b):
    return math.fsum(x * y for x, y in zip(a, b)) / (
        math.sqrt(math.fsum(x * x for x in a)) * math.sqrt(math.fsum(x * x for x in b))
    )


def probe(args, root, report_dir, report):
    from driving_data_platform.api.app import image_bytes

    with socket.socket() as check:
        check.bind(("127.0.0.1", args.port))
    commands = [
        "llama-server",
        "-m",
        str(args.gguf),
        "--mmproj",
        str(args.mmproj),
        "--embedding",
        "--pooling",
        "last",
        "--ctx-size",
        "4096",
        "--batch-size",
        "1024",
        "--ubatch-size",
        "1024",
        "--parallel",
        "1",
        "--image-min-tokens",
        "64",
        "--image-max-tokens",
        "256",
        "--host",
        "127.0.0.1",
        "--port",
        str(args.port),
    ]
    report["command"] = commands
    report["runtime_version"] = subprocess.check_output(
        ["llama-server", "--version"], stderr=subprocess.STDOUT, text=True
    ).strip()
    report["converted_files"] = [
        {
            "name": p.name,
            "bytes": p.stat().st_size,
            "sha256": hashlib.file_digest(p.open("rb"), "sha256").hexdigest(),
        }
        for p in (args.gguf, args.mmproj)
    ]
    opener = build_opener(ProxyHandler({}))
    endpoint = f"http://127.0.0.1:{args.port}"
    samples = []
    stopped = threading.Event()
    process = None

    def request(path, body=None):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        req = Request(
            endpoint + path, data=data, headers={"Content-Type": "application/json"}
        )
        with opener.open(req, timeout=120) as response:
            return json.load(response)

    def monitor():
        while not stopped.wait(0.5):
            if process is not None:
                result = subprocess.run(
                    ["ps", "-p", str(process.pid), "-o", "rss="],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if result.stdout.strip():
                    samples.append(int(result.stdout.strip()) * 1024)

    vectors = {}
    latencies = {}
    token_usage = {}

    def embed(name, value):
        started = time.monotonic()
        response = request(
            "/v1/embeddings", {"input": value, "encoding_format": "float"}
        )
        token_usage[name] = response.get("usage", {})
        vector = response["data"][0]["embedding"]
        if len(vector) != 2048 or not all(
            type(v) in (int, float) and math.isfinite(v) for v in vector
        ):
            raise ValueError("输出不是 2048 维有限向量")
        norm = math.sqrt(math.fsum(v * v for v in vector))
        if abs(norm - 1) > 1e-4:
            raise ValueError("输出向量未 L2 归一化")
        vectors[name] = vector
        latencies[name] = time.monotonic() - started
        print(
            name,
            "dimension",
            len(vector),
            "norm",
            round(norm, 7),
            "seconds",
            round(latencies[name], 3),
            flush=True,
        )
        return vector

    def template(text):
        return request(
            "/apply-template",
            {
                "messages": [
                    {"role": "system", "content": "Represent the user's input."},
                    {"role": "user", "content": text},
                ]
            },
        )["prompt"]

    try:
        with (report_dir / "llama-server.log").open("w") as log:
            process = subprocess.Popen(
                commands, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
            )
        sampler = threading.Thread(target=monitor, daemon=True)
        sampler.start()
        start = time.monotonic()
        for _ in range(180):
            if process.poll() is not None:
                raise ValueError("llama-server 加载失败，请查看本轮日志")
            try:
                if request("/health").get("status") == "ok":
                    break
            except (OSError, URLError, TimeoutError):
                time.sleep(0.5)
        else:
            raise ValueError("llama-server 加载超时")
        report["load_seconds"] = time.monotonic() - start
        props = request("/props")
        report["modalities"] = props.get("modalities")
        report["models"] = request("/v1/models")
        texts = {
            "text_a": "路口有行人正在过马路",
            "text_similar": "有人正在横穿道路",
            "text_unrelated": "厨房里正在制作巧克力蛋糕",
        }
        for name, text in texts.items():
            embed(name, template(text))
        embed("text_repeat", template(texts["text_a"]))
        same = cosine(vectors["text_a"], vectors["text_repeat"])
        related = cosine(vectors["text_a"], vectors["text_similar"])
        unrelated = cosine(vectors["text_a"], vectors["text_unrelated"])
        if args.text_reference:
            reference = json.loads(args.text_reference.read_text())["vectors"]
            report["native_text_cosines"] = {
                name: cosine(vectors[name], vector)
                for name, vector in zip(texts, reference)
            }
            if any(value < 0.99 for value in report["native_text_cosines"].values()):
                raise ValueError("llama 文本输出与原始权重参考偏差过大")
        report["text_checks"] = {
            "repeat_cosine": same,
            "related_cosine": related,
            "unrelated_cosine": unrelated,
        }
        if same < 0.9999 or related <= unrelated:
            raise ValueError("文本重复性或中文相关性最小核对未通过")
        input_file = root / "embedding/inputs" / args.input_id / "input.json"
        entries = json.loads(input_file.read_text())["entries"]
        dataset = Path(application_config(root)["dataset"]["root"])
        selected = [entries[0], entries[len(entries) // 2]]
        report["images"] = [
            {"media_id": e["media_id"], "sha256": e["sha256"], "path": e["path"]}
            for e in selected
        ]
        image_prompt = template("IMAGE_PLACEHOLDER")
        prefix, suffix = image_prompt.split("IMAGE_PLACEHOLDER")
        for i, entry in enumerate(selected):
            data = image_bytes(dataset, entry)
            uri = "data:image/jpeg;base64," + base64.b64encode(data).decode()
            value = [
                {
                    "content": [
                        {"type": "text", "text": prefix},
                        {"type": "image_url", "image_url": {"url": uri}},
                        {"type": "text", "text": suffix},
                    ]
                }
            ]
            embed("image_" + str(i), value)
            if i == 0:
                embed("image_repeat", value)
        image_same = cosine(vectors["image_0"], vectors["image_repeat"])
        image_different = cosine(vectors["image_0"], vectors["image_1"])
        report["image_checks"] = {
            "repeat_cosine": image_same,
            "different_cosine": image_different,
        }
        if image_same < 0.9999 or image_different > 0.99999:
            raise ValueError("图片重复性或输入敏感性核对未通过，不能排除图片被忽略")
        invalid = [
            {
                "content": [
                    {"type": "text", "text": prefix},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/jpeg;base64,"
                            + base64.b64encode(b"not-an-image").decode()
                        },
                    },
                    {"type": "text", "text": suffix},
                ]
            }
        ]
        try:
            request("/v1/embeddings", {"input": invalid, "encoding_format": "float"})
        except HTTPError as error:
            report["invalid_image_status"] = error.code
            if error.code not in (400, 422):
                report["robustness_status"] = "failed"
                report["limitations"] = [
                    "损坏图片返回 HTTP "
                    + str(error.code)
                    + "；调用端必须先校验图片，当前后端错误分类不合格"
                ]
            else:
                report["robustness_status"] = "passed"
        else:
            raise ValueError("损坏图片被接受，不能确认图片真正被解码")
        if request("/health").get("status") != "ok":
            raise ValueError("损坏图片请求后服务不可用")
        embed("text_after_invalid_image", template(texts["text_a"]))
        if cosine(vectors["text_a"], vectors["text_after_invalid_image"]) < 0.9999:
            raise ValueError("错误请求后正常编码结果发生异常")
        report["functional_status"] = "passed"
        report["recovery_after_invalid_image"] = "passed"
        report["text_image_scores"] = {
            name: [cosine(vectors[name], vectors["image_" + str(i)]) for i in range(2)]
            for name in texts
        }
        report["scope"] = (
            "真实文本和图片最小功能核对，非检索质量/并发容量验收；无正式产物或数据库写入"
        )
    finally:
        stopped.set()
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            sampler.join(timeout=3)
        atomic_json(report_dir / "probe-vectors.json", vectors)
        report["token_usage"] = token_usage
        report["latency_seconds"] = latencies
        report["sampled_process_rss_peak_bytes"] = max(samples, default=0)
        report["resource_boundary"] = (
            "RSS 采样不是 GPU/统一内存完整峰值；没有启动 Spark/HMS/PostgreSQL 组合验收"
        )
        report["test_process_stopped"] = process is None or process.poll() is not None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument(
        "--metadata",
        type=Path,
        required=True,
        help="本轮保存的 ModelScope 官方文件清单",
    )
    parser.add_argument("--gguf", type=Path)
    parser.add_argument("--mmproj", type=Path)
    parser.add_argument("--text-reference", type=Path)
    parser.add_argument(
        "--input-id",
        required=False,
    )
    parser.add_argument("--port", type=int, default=port("diagnostic"))
    args = parser.parse_args()
    if args.gguf and not args.input_id:
        parser.error("模型图片探针要求 --input-id")
    if bool(args.gguf) != bool(args.mmproj):
        parser.error("--gguf 与 --mmproj 必须同时提供")
    root = output_root()
    output = root / "reports" / ("model-probe-" + uuid.uuid4().hex)
    output.mkdir()
    report = {
        "status": "running",
        "stage": "integrity",
        "model_dir": str(args.model_dir),
    }
    atomic_json(output / "report.json", report)
    try:
        report["integrity"] = integrity(
            args.model_dir, json.loads(args.metadata.read_text())
        )
        if args.gguf:
            report["stage"] = "llama-probe"
            probe(args, root, output, report)
        else:
            report["scope"] = "integrity-only"
        report["status"] = "partial" if report.get("limitations") else "passed"
    except (Exception, KeyboardInterrupt) as error:
        report["status"] = "failed"
        report["error_type"] = type(error).__name__
        report["error"] = str(error)
        raise
    finally:
        atomic_json(output / "report.json", report)
        print(
            json.dumps(
                {"status": report["status"], "report": str(output / "report.json")},
                ensure_ascii=False,
            )
        )

    if report["status"] == "partial":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
