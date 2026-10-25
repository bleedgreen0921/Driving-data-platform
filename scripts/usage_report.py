"""项目空间占用与保留报告，不删除旧版本或试验数据库。"""

import json
import shutil
import subprocess
from pathlib import Path

from driving_data_platform.publishing.store import connect

GIB = 1024**3


def usage(root: Path, repo: Path):
    files = sum(
        p.stat().st_blocks * 512
        for p in root.rglob("*")
        if p.is_file() and not p.is_symlink()
    )
    images = json.loads((repo / "infra/docker/versions.lock.json").read_text())
    image_bytes = sum(item["size_bytes"] for item in images.values())
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
    volume = (
        int(
            subprocess.check_output(
                [*base, "du", "-sk", "/var/lib/postgresql/data"], text=True
            ).split()[0]
        )
        * 1024
    )
    vm_free = (
        int(
            subprocess.check_output(
                [*base, "df", "-k", "/var/lib/postgresql/data"], text=True
            )
            .splitlines()[-1]
            .split()[3]
        )
        * 1024
    )
    with connect(root) as conn:
        databases = conn.execute(
            "SELECT datname,pg_database_size(oid) FROM pg_database WHERE datname='ddp' OR datname LIKE 'restore_%' ORDER BY datname"
        ).fetchall()
        generations = conn.execute(
            "SELECT generation,processing_version,status FROM control.generation ORDER BY created_at"
        ).fetchall()
    total = files + image_bytes + volume
    free = shutil.disk_usage(root).free
    return {
        "status": "passed"
        if total <= 24 * GIB and free >= 12 * GIB and vm_free >= 12 * GIB
        else "blocked",
        "project_bytes_conservative": total,
        "external_artifact_allocated_bytes": files,
        "images_bytes_conservative": image_bytes,
        "postgres_volume_bytes": volume,
        "host_free_bytes": free,
        "docker_volume_free_bytes": vm_free,
        "databases": [{"database": name, "bytes": size} for name, size in databases],
        "generations": [
            dict(zip(("generation", "processing_version", "status"), row))
            for row in generations
        ],
        "retention": "不自动删除任何 generation、试验库或备份；删除前先人工核对活动指针与恢复需求。",
    }
