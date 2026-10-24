"""Read-only host resource gate; a pass does not certify full-stack capacity."""

import argparse
import json
import shutil
from pathlib import Path

from driving_data_platform.common.config import ConfigError, external_path, load_config

GIB = 1024**3
MIB = 1024**2
MEMORY_FIELDS = (
    "hdfs_mib",
    "yarn_spark_mib",
    "metastore_mib",
    "postgres_mib",
    "clickhouse_mib",
    "scheduler_mib",
    "api_executor_mib",
)
DISK_FIELDS = (
    "images_and_build",
    "hdfs_and_warehouse",
    "databases_and_generations",
    "backups_and_temporary",
)


def positive(section: dict, key: str) -> int:
    value = section.get(key)
    if type(value) is not int or value <= 0:
        raise ConfigError(f"resource budget.{key}: positive integer required")
    return value


def evaluate(
    config: dict, available_memory: int, root_free: int, target_free: int
) -> dict:
    resources = config.get("resources", {})
    disk = config.get("disk_budget_gib", {})
    memory = sum(positive(resources, key) for key in MEMORY_FIELDS) * MIB
    growth = sum(positive(disk, key) for key in DISK_FIELDS) * GIB
    maximum = positive(resources, "max_growth_gib") * GIB
    reserve = positive(resources, "root_reserve_gib") * GIB
    if maximum > 24 * GIB or reserve < 12 * GIB:
        raise ConfigError("resource budget: cannot relax 24 GiB growth/12 GiB reserve")
    checks = {
        "memory": available_memory >= memory,
        "growth": growth <= maximum,
        # Conservatively budget all growth on both filesystems. Never sum their free space.
        "root_disk": root_free >= growth + reserve,
        "target_disk": target_free >= growth + reserve,
    }
    return {
        "status": "passed" if all(checks.values()) else "blocked",
        "scope": "static-host-budget-only",
        "checks": checks,
        "memory_budget_bytes": memory,
        "growth_budget_bytes": growth,
        "available_memory_bytes": available_memory,
        "root_free_bytes": root_free,
        "target_free_bytes": target_free,
        "shortfall_bytes": {
            "memory": max(0, memory - available_memory),
            "root_disk": max(0, growth + reserve - root_free),
            "target_disk": max(0, growth + reserve - target_free),
            "growth": max(0, growth - maximum),
        },
    }


def inspect(config: dict, repository: Path) -> dict:
    value = config.get("deployment", {}).get("persistence_root")
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("deployment.persistence_root: required absolute path")
    target = external_path(value, repository, "deployment.persistence_root")
    if not target.is_dir():
        raise ConfigError("deployment.persistence_root: existing directory required")
    fields = dict(
        line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines()
    )
    available = int(fields["MemAvailable"].split()[0]) * 1024
    return evaluate(
        config, available, shutil.disk_usage("/").free, shutil.disk_usage(target).free
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = inspect(load_config(args.config), Path(__file__).resolve().parents[3])
    except (ConfigError, OSError, ValueError, KeyError):
        report = {
            "status": "blocked",
            "error": "invalid budget or unavailable host evidence",
        }
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
