"""Unified diagnostics; protocol probes never imply retrieval acceptance."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from driving_data_platform.common.config import ConfigError, configure_runtime
from driving_data_platform.embedding import artifacts as a
from driving_data_platform.embedding.runtime import encoder
from driving_data_platform.ingestion.nuscenes import atomic_json


def main(argv=None):
    import uuid

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=["model", "encoding"], required=True)
    parser.add_argument(
        "--backend", choices=["local_llama", "external_api"], default="local_llama"
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--dataset-root", type=Path)
    parser.add_argument("--space-id")
    parser.add_argument("--image", type=Path)
    args, remaining = parser.parse_known_args(argv)
    try:
        root = configure_runtime(
            config=args.config, root=args.root, dataset=args.dataset_root
        )
        repo = Path(__file__).resolve().parents[3]
        from driving_data_platform.common.config import (
            application_config,
            external_path,
        )

        cfg = (
            application_config(root)
            if (os.environ.get("DDP_CONFIG") or (root / "application.yaml").exists())
            else {}
        )
        dataset = cfg.get("dataset", {}).get("root", os.environ.get("DDP_DATASET_ROOT"))
        if dataset:
            source = external_path(dataset, repo, "dataset.root")
            if root.is_relative_to(source) or source.is_relative_to(root):
                raise ConfigError("产物目录不得与源数据重叠")
        if args.backend == "local_llama":
            if args.image or args.space_id:
                parser.error(
                    "本地诊断沿用 --input-id/--model-dir 参数，不接受外部探针参数"
                )
            tool = "verify_model.py" if args.target == "model" else "verify_encoding.py"
            command = [sys.executable, str(repo / "scripts" / tool), *remaining]
            if args.target == "encoding":
                command += ["--root", str(root)]
            return subprocess.run(command, check=False).returncode
        if remaining:
            parser.error("外部模式不接受本地模型、投影文件或故障参数")
        if not args.space_id:
            parser.error("外部诊断要求 --space-id")
        if args.target == "encoding" and not args.image:
            parser.error("外部图文编码诊断必须显式提供 --image")
        client = encoder(root, args.space_id)
        if client.capabilities()["backend"] != "external_api":
            raise ValueError("诊断后端与空间不一致")
        path = a.safe_parent(root, "reports") / (
            "diagnostic-" + uuid.uuid4().hex + ".json"
        )
        report = {
            "status": "running",
            "scope": "protocol-only",
            "backend": args.backend,
            "space_id": args.space_id,
            "acceptance": "not_verified",
        }
        atomic_json(path, report)
        # Serialize with all control/verification operations, and with online encoders.
        import fcntl

        from driving_data_platform.embedding.gate import acquire

        try:
            with (root / ".operation.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with acquire(root):
                    report["text"] = client.probe()
                    if args.target == "encoding":
                        if not args.image:
                            raise ValueError("外部图文编码诊断必须显式提供 --image")
                        image = args.image.absolute()
                        data = a.read_bytes(image.parent, image.name, 25 * 1024**2)
                        client.encode_image(data)
                        report["image"] = "passed"
            report["status"] = "passed"
        except BaseException:
            report["status"] = "failed"
            raise
        finally:
            atomic_json(path, report)
        print(json.dumps({**report, "report": str(path)}))
        return 0
    except (ValueError, OSError, ConfigError):
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": "诊断失败；核对配置、依赖及本轮脱敏报告。",
                }
            )
        )
        return 2
