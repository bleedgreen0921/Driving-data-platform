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
    "DDP_EMBEDDING_PROVIDER_FILE": ("embedding", "provider_file"),
    "DDP_BACKUP_ROOT": ("storage", "backup_root"),
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
    # Paths in files are relative to that file; environment overrides use caller cwd.
    for section, field in PATH_FIELDS:
        value = data.get(section, {}).get(field)
        if isinstance(value, str) and value.strip():
            base = (
                Path.cwd()
                if any(
                    env.get(k) == value and v == (section, field)
                    for k, v in ENV_FIELDS.items()
                )
                else path.absolute().parent
            )
            data[section][field] = str(resolve_path(value, base))
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


PATH_FIELDS = (
    ("dataset", "root"),
    ("storage", "output_root"),
    ("assets", "evidence_file"),
    ("deployment", "persistence_root"),
    ("embedding", "provider_file"),
    ("storage", "backup_root"),
)
PORTS = {
    "postgres": 55433,
    "api": 18080,
    "model": 18090,
    "trial": 55434,
    "trial_api": 18081,
    "restore": 55435,
    "diagnostic": 18092,
}


def resolve_path(value, base=None):
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("path: nonempty value required")
    result = Path(value).expanduser()
    return (
        result
        if result.is_absolute()
        else (Path.cwd() if base is None else Path(base)) / result
    )


def selected_config():
    value = os.environ.get("DDP_CONFIG")
    return resolve_path(value) if value else None


def output_root(*, required=True):
    value = os.environ.get("DDP_OUTPUT_ROOT")
    if not value and (path := selected_config()):
        value = load_config(path).get("storage", {}).get("output_root")
    if not value:
        if not required:
            return None
        raise ConfigError(
            "storage.output_root: set --root, DDP_OUTPUT_ROOT or --config"
        )
    path = resolve_path(value)
    if path.is_symlink():
        raise ConfigError("产物根不得是符号链接")
    return external_path(
        str(path), Path(__file__).resolve().parents[3], "storage.output_root"
    )


def application_path(root=None):
    path = selected_config()
    if path and (root is None or root.resolve() == output_root().resolve()):
        return path
    return (root or output_root()) / "application.yaml"


def application_config(root):
    # An explicit independent verification root must never inherit the live dataset override.
    primary = output_root(required=False)
    return load_config(
        application_path(root),
        None if primary is None or root.resolve() == primary.resolve() else {},
    )


def port(name):
    value = os.environ.get("DDP_" + name.upper() + "_PORT")
    if value is None:
        path = selected_config()
        root = os.environ.get("DDP_OUTPUT_ROOT")
        if path is None and root:
            path = resolve_path(root) / "application.yaml"
        if path and path.exists():
            value = load_config(path).get("ports", {}).get(name)
    if value is None:
        root_value = os.environ.get("DDP_OUTPUT_ROOT")
        if not root_value and (config_path := selected_config()):
            root_value = load_config(config_path).get("storage", {}).get("output_root")
        runtime = resolve_path(root_value) / "compose.env" if root_value else None
        if runtime and runtime.is_file():
            fields = dict(
                line.split("=", 1)
                for line in runtime.read_text().splitlines()
                if "=" in line
            )
            value = fields.get("DDP_" + name.upper() + "_PORT")
    if value is None:
        return PORTS[name]
    try:
        if isinstance(value, bool) or str(int(value)) != str(value):
            raise ValueError()
        number = int(value)
        if not 1 <= number <= 65535:
            raise ValueError()
        return number
    except (ValueError, TypeError):
        raise ConfigError(f"ports.{name}: integer in 1..65535 required") from None


def configure_runtime(*, config=None, root=None, dataset=None):
    # Called before any chdir or filesystem mutation. Child processes inherit the same config.
    if config is not None:
        os.environ["DDP_CONFIG"] = str(resolve_path(str(config)))
    path = selected_config()
    data = load_config(path) if path else {}
    for name, section, field, explicit in (
        ("DDP_OUTPUT_ROOT", "storage", "output_root", root),
        ("DDP_DATASET_ROOT", "dataset", "root", dataset),
    ):
        value = (
            explicit
            if explicit is not None
            else os.environ.get(name, data.get(section, {}).get(field))
        )
        if value:
            os.environ[name] = str(resolve_path(str(value)))
    for name, field in ENV_FIELDS.items():
        if field in PATH_FIELDS and name in os.environ and os.environ[name]:
            os.environ[name] = str(resolve_path(os.environ[name]))
    values = {name: port(name) for name in PORTS}
    if len(set(values.values())) != len(values):
        raise ConfigError("ports: project host ports must be distinct")
    for name, value in values.items():
        os.environ["DDP_" + name.upper() + "_PORT"] = str(value)
    return output_root()


def storage_directory(root, name):
    if name != "backup":
        raise ConfigError("unknown storage directory")
    path = application_path(root)
    value = (
        application_config(root).get("storage", {}).get(name + "_root")
        if path.exists()
        else None
    )
    target = (
        resolve_path(value)
        if value
        else root / ("backups" if name == "backup" else "temporary")
    )
    if target.is_symlink():
        raise ConfigError("storage directory must not be a symlink")
    result = external_path(
        str(target), Path(__file__).resolve().parents[3], "storage." + name
    )
    if result == root or root.resolve().is_relative_to(result):
        raise ConfigError("backup directory must not equal or contain output root")
    dataset = (
        application_config(root).get("dataset", {}).get("root")
        if path.exists()
        else os.environ.get("DDP_DATASET_ROOT")
    )
    if dataset:
        source = resolve_path(dataset).resolve()
        if result.is_relative_to(source) or source.is_relative_to(result):
            raise ConfigError("backup directory must not overlap dataset")
    return result


def asset_policy(config):
    policy = config.get("dataset", {}).get("assets", {}).get("policy", "strict")
    if policy not in {"strict", "registered"}:
        raise ConfigError("dataset.assets.policy: expected strict or registered")
    return policy


def embedding_enabled(root):
    path = application_path(root)
    return (
        not path.exists()
        or application_config(root).get("embedding", {}).get("enabled", True)
        is not False
    )
