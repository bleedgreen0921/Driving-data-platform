"""Read configuration without connecting to services or creating directories."""

import os
from collections.abc import Mapping
from pathlib import Path

import yaml


class ConfigError(ValueError):
    """Invalid configuration; messages never include connection values."""


ENV_FIELDS = {
    "DDP_DATASET_ROOT": ("dataset", "root"),
    "DDP_DATASET_VERSION": ("dataset", "version"),
    "DDP_OUTPUT_ROOT": ("storage", "output_root"),
    "DDP_ASSET_EVIDENCE": ("assets", "evidence_file"),
    "DDP_POSTGRES_DSN": ("postgres", "dsn"),
    "DDP_CLICKHOUSE_URL": ("clickhouse", "url"),
    "DDP_HDFS_URI": ("hadoop", "hdfs_uri"),
    "DDP_YARN_RESOURCE_MANAGER": ("hadoop", "yarn_resource_manager"),
    "DDP_HIVE_METASTORE_URI": ("hive", "metastore_uri"),
    "DDP_HIVE_DATABASE": ("hive", "database"),
    "DDP_SPARK_MASTER": ("spark", "master"),
    "DDP_DATAX_HOME": ("datax", "home"),
    "DDP_DOLPHINSCHEDULER_URL": ("dolphinscheduler", "url"),
}


def load_config(path: Path, environ: Mapping[str, str] | None = None) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        raise ConfigError("configuration: unreadable or invalid YAML") from None
    if not isinstance(data, dict) or any(
        not isinstance(k, str) or not isinstance(v, dict) for k, v in data.items()
    ):
        raise ConfigError("configuration: expected section mappings")
    env = os.environ if environ is None else environ
    for name, (section, field) in ENV_FIELDS.items():
        if name in env:
            data.setdefault(section, {})[field] = env[name]
    return data


def required(config: dict, section: str, field: str) -> str:
    value = config.get(section, {}).get(field)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{section}.{field}: required nonempty string")
    return value.strip()


def external_path(value: str, repository: Path, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ConfigError(f"{label}: absolute path required")
    path = path.resolve()
    repository = repository.resolve()
    if path.is_relative_to(repository) or repository.is_relative_to(path):
        raise ConfigError(f"{label}: must be outside and not contain repository")
    return path
