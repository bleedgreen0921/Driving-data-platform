"""项目空间占用观察；只报告实际大小。"""

import json
import re
import subprocess
from pathlib import Path

from driving_data_platform.publishing.store import connect


def output(*args):
    return subprocess.check_output(args, text=True)


def inventory(root: Path, repo: Path, growth: int = 0):
    """保留历史调用点的观测摘要。"""
    measured = usage(root, repo)
    measured["planned_growth_bytes"] = growth
    return measured


def usage(root: Path, repo: Path):
    seen = set()
    files = 0
    source_files = root / "embedding/model-source-files.json"
    referenced = []
    if source_files.exists():
        referenced = [Path(p) for p in json.loads(source_files.read_text())["files"]]
    model_config = root / "embedding/model.json"
    if model_config.exists():
        referenced += [
            Path(v["path"]) for v in json.loads(model_config.read_text()).values()
        ]
    for path in [*root.rglob("*"), *referenced]:
        if path.is_file() and not path.is_symlink():
            info = path.stat()
            key = (info.st_dev, info.st_ino)
            if key not in seen:
                files += info.st_blocks * 512
                seen.add(key)

    images = json.loads((repo / "infra/docker/versions.lock.json").read_text())
    image_bytes = sum(item["size_bytes"] for item in images.values())
    candidate_lock = repo / "infra/metastore/runtime.lock.json"
    if candidate_lock.exists():
        candidate = json.loads(candidate_lock.read_text())
        image_bytes += sum(
            candidate[key]["size_bytes"] for key in ("hive", "spark_client")
        )

    volume = 0
    base = [
        "docker",
        "compose",
        "--env-file",
        str(root / "compose.env"),
        "-f",
        str(repo / "infra/docker/compose.yaml"),
        "exec",
        "-T",
        "postgres",
    ]
    try:
        volume = (
            int(
                subprocess.check_output(
                    [*base, "du", "-sk", "/var/lib/postgresql/data"], text=True
                ).split()[0]
            )
            * 1024
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        volume = None

    with connect(root) as conn:
        databases = conn.execute(
            "SELECT datname,pg_database_size(oid) FROM pg_database WHERE datname='ddp' OR datname='ddp_metastore_v410' OR datname LIKE 'restore_%' ORDER BY datname"
        ).fetchall()
        generations = conn.execute(
            "SELECT generation,processing_version,status FROM control.generation ORDER BY created_at"
        ).fetchall()

    embedding_images = 0
    embedding_volumes = 0
    embedding_cache = 0
    embedding_lock = repo / "infra/embedding/runtime.lock.json"
    from driving_data_platform.common.config import embedding_enabled

    if embedding_enabled(root) and embedding_lock.exists():
        runtime = json.loads(embedding_lock.read_text())
        embedding_images = runtime["size_bytes"]
        names = subprocess.check_output(
            ["docker", "volume", "ls", "--format", "{{.Name}}"], text=True
        ).splitlines()
        for name in names:
            if re.fullmatch(r"ddp-(?:embedding|deploy)-[a-f0-9]{32}", name):
                embedding_volumes += (
                    int(
                        subprocess.check_output(
                            [
                                "docker",
                                "run",
                                "--rm",
                                "--network",
                                "none",
                                "--entrypoint",
                                "du",
                                "--volume",
                                name + ":/usage:ro",
                                runtime["image"],
                                "-sk",
                                "/usage",
                            ],
                            text=True,
                        ).split()[0]
                    )
                    * 1024
                )
        raw = subprocess.check_output(
            [
                "docker",
                "buildx",
                "du",
                "--filter",
                "description~=pgvector",
                "--format",
                "{{.Size}}",
            ],
            text=True,
        )
        for size in raw.splitlines():
            match = re.fullmatch(r"([0-9.]+)([kMGT]?B)", size.strip())
            if not match:
                raise ValueError("无法计量向量镜像构建缓存")
            embedding_cache += int(
                float(match[1])
                * {"B": 1, "kB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4}[
                    match[2]
                ]
            )

    total = (
        files
        + image_bytes
        + (volume or 0)
        + embedding_images
        + embedding_volumes
        + embedding_cache
    )
    return {
        "project_bytes_conservative": total,
        "external_artifact_allocated_bytes": files,
        "model_source_references_count": len(referenced),
        "images_bytes_conservative": image_bytes,
        "postgres_volume_bytes": volume,
        "embedding_images_bytes": embedding_images,
        "embedding_trial_volumes_bytes": embedding_volumes,
        "embedding_build_cache_bytes": embedding_cache,
        "databases": [{"database": name, "bytes": size} for name, size in databases],
        "generations": [
            dict(zip(("generation", "processing_version", "status"), row))
            for row in generations
        ],
        "retention": "不自动删除任何 generation、试验库或备份；删除前先人工核对活动指针与恢复需求。",
    }
