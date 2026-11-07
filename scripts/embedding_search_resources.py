"""服务停机时也可执行的项目实测盘点；只读挂载已存在的项目卷。"""

import json
import re
import shutil
import subprocess

from driving_data_platform.common.config import (
    load_config,
    resolve_path,
    storage_directory,
)
from driving_data_platform.common.resources import PROJECT_DISK_GIB, inspect

GIB = 1024**3


def output(*args):
    return subprocess.check_output(args, text=True)


def inventory(root, repo, growth=0):
    config = load_config(repo / "configs/platform.example.yaml")
    config["deployment"]["persistence_root"] = str(root)
    config["resources"]["postgres_mib"] += 1024
    static = inspect(config, repo)
    locks = [
        json.loads((repo / p).read_text())
        for p in (
            "infra/docker/versions.lock.json",
            "infra/metastore/runtime.lock.json",
            "infra/embedding/runtime.lock.json",
        )
    ]
    ids = [
        v["image"]
        for lock in locks[:2]
        for v in lock.values()
        if isinstance(v, dict) and "image" in v
    ]
    ids.extend([locks[2]["image"], locks[2]["base_image"]])
    images = {}
    for image in ids:
        info = json.loads(output("docker", "image", "inspect", image))[0]
        images[info["Id"]] = info["Size"]
    volumes = {}
    free = None
    for name in output("docker", "volume", "ls", "--format", "{{.Name}}").splitlines():
        if name.startswith("driving-data-platform-mac_") or re.fullmatch(
            r"ddp-(?:embedding|deploy)-[a-f0-9]{32}", name
        ):
            base = [
                "docker",
                "run",
                "--rm",
                "--pull",
                "never",
                "--network",
                "none",
                "--memory",
                "128m",
                "--volume",
                name + ":/usage:ro",
                "--entrypoint",
            ]
            volumes[name] = (
                int(output(*base, "du", locks[2]["image"], "-sk", "/usage").split()[0])
                * 1024
            )
            free = (
                int(
                    output(*base, "df", locks[2]["image"], "-k", "/usage")
                    .splitlines()[-1]
                    .split()[3]
                )
                * 1024
            )
    cache = output(
        "docker",
        "buildx",
        "du",
        "--filter",
        "description~=pgvector|hms|hive|spark",
        "--format",
        "{{json .}}",
    )
    cache_rows = [json.loads(line) for line in cache.splitlines()]
    cache_bytes = 0
    for row in cache_rows:
        match = re.fullmatch(r"([0-9.]+)([kMGT]?B)", row["Size"])
        if not match:
            raise ValueError("无法计量构建缓存")
        cache_bytes += (
            float(match[1])
            * {"B": 1, "kB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4}[
                match[2]
            ]
        )
    references = []
    source_files = root / "embedding/model-source-files.json"
    if source_files.exists():
        references.extend(
            resolve_path(p, source_files.parent)
            for p in json.loads(source_files.read_text())["files"]
        )
    registered = root / "embedding/model.json"
    if registered.exists():
        references.extend(
            resolve_path(v["path"], registered.parent)
            for v in json.loads(registered.read_text()).values()
        )
    backup = storage_directory(root, "backup")
    if backup.exists() and not backup.is_relative_to(root):
        references.extend(backup.rglob("*"))
    seen = set()
    files = 0
    for p in [*root.rglob("*"), *references]:
        if p.is_file() and not p.is_symlink():
            st = p.stat()
            if (st.st_dev, st.st_ino) not in seen:
                seen.add((st.st_dev, st.st_ino))
                files += st.st_blocks * 512
    total = files + sum(images.values()) + sum(volumes.values()) + int(cache_bytes)
    host_free = min(shutil.disk_usage(p).free for p in (root, backup) if p.exists())
    passed = (
        static["status"] == "passed"
        and total + growth <= PROJECT_DISK_GIB * GIB
        and host_free - growth >= 12 * GIB
        and free is not None
        and free - growth >= 12 * GIB
    )
    return {
        "status": "passed" if passed else "blocked",
        "static": static,
        "project_bytes_conservative": total,
        "planned_growth_bytes": growth,
        "disk_budget_gib": PROJECT_DISK_GIB,
        "files_allocated_bytes": files,
        "images": images,
        "volumes": volumes,
        "build_cache": cache_rows,
        "build_cache_bytes": int(cache_bytes),
        "host_free_bytes": host_free,
        "docker_free_bytes": free,
        "scope": "实测锁定镜像、项目命名卷、相关构建缓存、外部产物及登记文件；镜像共享层保守重复计量",
    }
