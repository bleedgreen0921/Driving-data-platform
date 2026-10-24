"""Prepare and manage only this project's verified stage 1 infrastructure."""

import argparse
import json
import os
import secrets
import subprocess
from pathlib import Path

from driving_data_platform.common.config import (
    ConfigError,
    external_path,
    load_config,
    required,
)
from driving_data_platform.common.resources import inspect

PROJECT = "driving-data-platform"
SECRET_KEYS = (
    "DDP_ADMIN_PASSWORD",
    "DDP_CONTROL_PASSWORD",
    "DDP_HIVE_PASSWORD",
    "DDP_SCHEDULER_PASSWORD",
)


def manage(action: str, platform: dict, data: dict, repository: Path) -> dict:
    root = external_path(
        required(platform, "deployment", "persistence_root"),
        repository,
        "persistence_root",
    )
    output = external_path(
        required(data, "storage", "output_root"), repository, "output_root"
    )
    dataset = external_path(
        required(data, "dataset", "root"), repository, "dataset_root"
    )
    if root == output or root.is_relative_to(dataset) or dataset.is_relative_to(root):
        raise ConfigError(
            "persistence root must be dedicated and disjoint from dataset"
        )
    if platform.get("deployment", {}).get("project_name") != PROJECT:
        raise ConfigError("only the fixed project name is supported")
    identity = root / ".ddp-project.json"
    env_file = root / "compose.env"
    lock = json.loads((repository / "infra/docker/versions.lock.json").read_text())
    expected = {
        "DDP_POSTGRES_IMAGE": lock["postgres"]["image"],
        "DDP_HADOOP_IMAGE": lock["hadoop"]["image"],
        "DDP_PERSISTENCE_ROOT": str(root),
        "DDP_ODS_ROOT": str(output / "ingestion/snapshots"),
    }
    if action == "prepare" and not identity.exists():
        root.mkdir(parents=True, exist_ok=True)
        if any(root.iterdir()):
            raise ConfigError("nonempty unowned persistence directory")
        if inspect(platform, repository)["status"] != "passed":
            raise ConfigError("initial resource gate blocked")
        root.chmod(0o700)
        identity.write_text(json.dumps({"project": PROJECT}))
    if identity.is_symlink() or json.loads(identity.read_text()) != {
        "project": PROJECT
    }:
        raise ConfigError("project persistence ownership mismatch")
    if action == "prepare" and not env_file.exists():
        for name in ("namenode", "datanode"):
            (root / name).mkdir(exist_ok=True)
            if (root / name).stat().st_uid != 1000:
                raise ConfigError("Hadoop bind directories require uid 1000 ownership")
        values = {
            **expected,
            **{
                key: os.environ.get(key) or secrets.token_hex(32) for key in SECRET_KEYS
            },
        }
        if any(
            "\n" in value or "\r" in value or "'" in value for value in values.values()
        ):
            raise ConfigError("deployment environment contains unsupported characters")
        fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            for key, value in values.items():
                stream.write(f"{key}='{value}'\n")
    if env_file.is_symlink() or env_file.stat().st_mode & 0o077:
        raise ConfigError("environment file requires private permissions")
    # Validate interpolation without printing the resolved environment or credentials.
    command = [
        "docker",
        "compose",
        "--env-file",
        str(env_file),
        "-f",
        str(repository / "infra/docker/compose.yaml"),
        "--profile",
        "infra",
    ]
    result = subprocess.run(
        [*command, "config", "--format", "json"],
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode:
        raise ConfigError("Compose environment validation failed")
    resolved = json.loads(result.stdout)
    if any(
        resolved["services"][service]["image"] != expected[key]
        for service, key in (
            ("postgres", "DDP_POSTGRES_IMAGE"),
            ("namenode", "DDP_HADOOP_IMAGE"),
            ("datanode", "DDP_HADOOP_IMAGE"),
        )
    ):
        raise ConfigError("environment images do not match verified lock")
    mounts = {
        "postgres": {
            "/var/lib/postgresql/data": str(root / "postgres"),
            "/docker-entrypoint-initdb.d/10-ddp.sh": str(
                repository / "infra/docker/postgres/initialize.sh"
            ),
        },
        "namenode": {
            "/etc/ddp-hadoop": str(repository / "infra/hadoop/config"),
            "/data/name": str(root / "namenode"),
            "/input-ods": str(output / "ingestion/snapshots"),
        },
        "datanode": {
            "/etc/ddp-hadoop": str(repository / "infra/hadoop/config"),
            "/data/data": str(root / "datanode"),
        },
    }
    for service, allowed in mounts.items():
        actual = {
            item["target"]: item["source"]
            for item in resolved["services"][service]["volumes"]
        }
        if actual != allowed:
            raise ConfigError("resolved bind mounts differ from project-owned paths")
    if resolved["networks"]["platform"]["name"] != PROJECT:
        raise ConfigError("resolved network name differs from project identity")
    network = subprocess.run(
        ["docker", "network", "inspect", PROJECT],
        capture_output=True,
        check=False,
        timeout=30,
    )
    if (
        network.returncode == 0
        and json.loads(network.stdout)[0]
        .get("Labels", {})
        .get("com.docker.compose.project")
        != PROJECT
    ):
        raise ConfigError("existing network is not owned by this project")
    if action == "prepare":
        return {
            "status": "passed",
            "action": action,
            "credentials": "external-private-file",
        }
    operations = {
        "start": ["up", "-d", "--pull", "never", "--wait", "--wait-timeout", "120"],
        "recreate": [
            "up",
            "-d",
            "--pull",
            "never",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            "120",
        ],
        "stop": ["stop", "--timeout", "30"],
        "status": ["ps", "--all", "--format", "json"],
    }
    result = subprocess.run(
        [*command, *operations[action]], capture_output=True, check=False, timeout=180
    )
    if action == "status" and result.returncode == 0:
        services = [
            json.loads(line) for line in result.stdout.splitlines() if line.strip()
        ]
        healthy = len(services) == 3 and all(
            item["State"] == "running" and item.get("Health") == "healthy"
            for item in services
        )
        return {
            "status": "passed" if healthy else "blocked",
            "action": action,
            "services": [
                {
                    "service": item["Service"],
                    "state": item["State"],
                    "health": item.get("Health"),
                }
                for item in services
            ],
        }
    # Keep service output and resolved config out of public JSON, especially on failure.
    return {
        "status": "passed" if result.returncode == 0 else "blocked",
        "action": action,
        "exit_code": result.returncode,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("prepare", "start", "recreate", "stop", "status")
    )
    parser.add_argument("--platform-config", type=Path, required=True)
    parser.add_argument("--data-config", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = manage(
            args.action,
            load_config(args.platform_config),
            load_config(args.data_config),
            Path(__file__).resolve().parents[1],
        )
    except (ConfigError, OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        report = {
            "status": "blocked",
            "error": "deployment configuration, ownership or dependency check failed",
        }
    report.setdefault("scope", "stage-1-infrastructure-only")
    print(json.dumps(report))
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
