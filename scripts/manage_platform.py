"""Mac 平台统一入口；准备、服务操作和验证分开。"""

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from driving_data_platform.common.config import ConfigError, external_path, load_config
from driving_data_platform.common.errors import PlatformError
from driving_data_platform.common.preflight import TABLES, digest
from driving_data_platform.common.resources import inspect
from driving_data_platform.ingestion.nuscenes import ingest

REPO = Path(__file__).resolve().parents[1]
ROOT = Path(
    os.environ.get(
        "DDP_OUTPUT_ROOT", "/Users/qiujiajun/Project/driving-data-platform-artifacts"
    )
)
CONFIG = ROOT / "application.yaml"
ENV = ROOT / "compose.env"


def compose(*args):
    if ENV.exists() and (ROOT / "metastore/runtime.env").exists():
        from driving_data_platform.warehouse.catalog import command

        return subprocess.run([*command(ROOT, REPO), *args], check=True)
    return subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(ENV),
            "-f",
            str(REPO / "infra/docker/compose.yaml"),
            *args,
        ],
        check=True,
    )


def prepare():
    if sys.version_info[:2] != (3, 12):
        raise ValueError("必须使用 Python 3.12")
    ROOT.mkdir(parents=True, exist_ok=True)
    for name in ("warehouse", "reports", "backups"):
        (ROOT / name).mkdir(exist_ok=True)
    dataset = Path(
        os.environ.get("DDP_DATASET_ROOT", "/Users/qiujiajun/Project/v1.0-mini")
    )
    if not CONFIG.exists():
        import yaml

        evidence = {
            "dataset": {
                "source_url": "https://www.nuscenes.org/nuscenes",
                "license": "nuScenes Terms of Use (2021-11-16); CC BY-NC-SA 4.0 with additional terms",
                "license_basis": "package LICENSE",
                "acquired_at": None,
                "observed_at": datetime.now(UTC).date().isoformat(),
                "version": "v1.0-mini",
                "checksums": {
                    f"v1.0-mini/{t}.json": digest(dataset / "v1.0-mini" / f"{t}.json")
                    for t in TABLES
                },
            }
        }
        evidence["dataset"]["checksums"]["LICENSE"] = digest(dataset / "LICENSE")
        (ROOT / "asset-evidence.json").write_text(json.dumps(evidence, indent=2))
        CONFIG.write_text(
            yaml.safe_dump(
                {
                    "dataset": {"root": str(dataset), "version": "v1.0-mini"},
                    "assets": {"evidence_file": str(ROOT / "asset-evidence.json")},
                    "storage": {"output_root": str(ROOT)},
                }
            )
        )
    if not ENV.exists():
        import secrets

        ENV.write_text(
            f"DDP_POSTGRES_PASSWORD={secrets.token_hex(24)}\nDDP_OUTPUT_ROOT={ROOT}\n"
        )
        ENV.chmod(0o600)
    return {
        "status": "passed",
        "scope": "local-preparation",
        "source_description": "local package; acquisition date unknown; observed_at records inventory date",
    }


def prepare_images():
    images = {}
    budget = load_config(REPO / "configs/platform.example.yaml")
    budget["deployment"]["persistence_root"] = str(ROOT)
    if inspect(budget, REPO)["status"] != "passed":
        raise PlatformError("RESOURCE_BLOCKED", "resource-gate", "资源门禁未通过")
    for key, tag in [
        ("POSTGRES", "postgres:16.15"),
        ("SPARK", "spark:4.2.0-java21-python3"),
    ]:
        local = subprocess.run(
            ["docker", "image", "inspect", tag], check=False, capture_output=True
        )
        source = tag
        if local.returncode:
            source = "mirror.gcr.io/library/" + tag
            subprocess.run(["docker", "pull", source], check=True)
            upstream = subprocess.check_output(
                ["docker", "buildx", "imagetools", "inspect", tag, "--raw"]
            )
            import hashlib

            expected = "sha256:" + hashlib.sha256(upstream).hexdigest()
            mirrored = json.loads(
                subprocess.check_output(["docker", "image", "inspect", source])
            )[0]
            if not any(
                item.endswith("@" + expected) for item in mirrored["RepoDigests"]
            ):
                raise ValueError("镜像缓存与官方摘要不一致")
        info = json.loads(
            subprocess.check_output(["docker", "image", "inspect", source])
        )[0]
        if info["Architecture"] != "arm64":
            raise ValueError("镜像必须为 arm64")
        images[key] = {
            "tag": tag,
            "image": info["RepoDigests"][0],
            "architecture": info["Architecture"],
            "size_bytes": info["Size"],
        }
    (REPO / "infra/docker/versions.lock.json").write_text(
        json.dumps(images, indent=2) + "\n"
    )
    lines = [
        line
        for line in ENV.read_text().splitlines()
        if not line.startswith(("DDP_POSTGRES_IMAGE=", "DDP_SPARK_IMAGE="))
    ]
    lines.extend(f"DDP_{key}_IMAGE={value['image']}" for key, value in images.items())
    ENV.write_text("\n".join(lines) + "\n")
    return {"status": "passed", "images": images}


def verify(selected, faults=False, *, duration=1800, lock_fd=None):
    directory = (
        ROOT
        / "reports"
        / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8])
    )
    directory.mkdir(parents=True)
    results = []
    commands = {
        "evidence": [sys.executable, "scripts/verify_evidence.py"],
        "pipeline": [
            sys.executable,
            "scripts/verify_pipeline.py",
            *(["--faults"] if faults else []),
        ],
        "metastore": [
            sys.executable,
            "scripts/verify_metastore.py",
            *(["--faults"] if faults else []),
        ],
        "backup": [sys.executable, "scripts/verify_backup.py"],
        "integration": [
            sys.executable,
            "scripts/verify_integration.py",
            "--duration",
            str(duration),
        ],
        "api": [
            sys.executable,
            "scripts/verify_api.py",
            *(["--faults"] if faults else []),
        ],
        "publication": [
            sys.executable,
            "scripts/verify_publication.py",
            *(["--faults"] if faults else []),
        ],
        "warehouse": [
            sys.executable,
            "scripts/verify_warehouse.py",
            *(["--faults"] if faults else []),
        ],
        "repository": [sys.executable, "scripts/check_repository.py"],
        "postgres": [
            sys.executable,
            "scripts/verify_postgres.py",
            *(["--recreate"] if faults else []),
        ],
        "regression": [sys.executable, "-m", "pytest", "-q"],
        "dependencies": [sys.executable, "-m", "pip", "check"],
        "preflight": [
            sys.executable,
            "-m",
            "driving_data_platform.common.preflight",
            "--config",
            str(CONFIG),
        ],
        "ods-contract": [
            sys.executable,
            "scripts/verify_ods.py",
            *(["--faults"] if faults else []),
        ],
        "ods": [
            sys.executable,
            "-m",
            "driving_data_platform.ingestion.nuscenes",
            "--config",
            str(CONFIG),
        ],
    }
    selected = list(dict.fromkeys(selected))
    report = {
        "status": "running",
        "results": results,
        "not_executed": sorted(commands),
        "directory": str(directory),
    }
    from driving_data_platform.ingestion.nuscenes import atomic_json

    atomic_json(directory / "report.json", report)
    interrupted = False
    for name in selected:
        with (directory / f"{name}.log").open("w") as log:
            try:
                result = subprocess.run(
                    commands[name],
                    check=False,
                    cwd=REPO,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=dict(
                        os.environ,
                        **(
                            {"DDP_OPERATION_LOCK_FD": str(lock_fd)}
                            if lock_fd is not None
                            else {}
                        ),
                    ),
                    pass_fds=(lock_fd,) if lock_fd is not None else (),
                )
                code = result.returncode
            except OSError:
                log.write("无法执行验证：解释器、脚本或依赖不可用。\n")
                code = 127
            except KeyboardInterrupt:
                log.write("验证被中断。\n")
                code = 130
                interrupted = True
        results.append({"name": name, "exit_code": code})
        report["not_executed"] = sorted(set(commands) - {r["name"] for r in results})
        atomic_json(directory / "report.json", report)
        if interrupted:
            break
    report["status"] = (
        "interrupted"
        if interrupted
        else ("passed" if all(r["exit_code"] == 0 for r in results) else "failed")
    )
    atomic_json(directory / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "run-show",
            "publish-diff",
            "prepare",
            "prepare-images",
            "hms-register",
            "hms-consume",
            "hms-validate",
            "hms-prepare",
            "hms-up",
            "hms-stop",
            "check",
            "up",
            "postgres-up",
            "stop",
            "status",
            "verify",
            "ods",
            "build",
            "usage",
            "run",
            "api-up",
            "api-stop",
            "init-db",
            "load",
            "publish",
            "rollback",
        ],
    )
    parser.add_argument(
        "checks",
        nargs="*",
        choices=[
            "evidence",
            "pipeline",
            "metastore",
            "regression",
            "dependencies",
            "preflight",
            "ods",
            "postgres",
            "repository",
            "ods-contract",
            "backup",
            "integration",
            "warehouse",
            "publication",
            "api",
        ],
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=1800,
        help="integration 观测秒数，正式验收至少 1800",
    )
    parser.add_argument("--trial", help="Hive 候选消费的 32 位试验 ID")
    parser.add_argument("--generation")
    parser.add_argument("--against-generation")
    parser.add_argument("--run-id")
    parser.add_argument("--version", default="warehouse-v4")
    parser.add_argument(
        "--faults",
        action="store_true",
        help="显式允许提交故障、缺输入与本项目数据库停机/重建验证",
    )
    args = parser.parse_args()
    validated_root = False
    try:
        if sys.version_info[:2] != (3, 12):
            raise ValueError("必须使用 Python 3.12")
        resolved_root = external_path(str(ROOT), REPO, "DDP_OUTPUT_ROOT")
        if ROOT.is_symlink():
            raise ValueError("产物根不得是符号链接")
        dataset_value = os.environ.get(
            "DDP_DATASET_ROOT", "/Users/qiujiajun/Project/v1.0-mini"
        )
        if CONFIG.exists():
            dataset_value = (
                load_config(CONFIG).get("dataset", {}).get("root", dataset_value)
            )
        if not isinstance(dataset_value, str):
            raise ConfigError("数据目录必须是绝对路径")
        dataset_root = external_path(dataset_value, REPO, "dataset.root")
        if resolved_root.is_relative_to(dataset_root) or dataset_root.is_relative_to(
            resolved_root
        ):
            raise ValueError("产物目录不得与源数据重叠；未创建任何文件")
        validated_root = True
        ROOT.mkdir(parents=True, exist_ok=True)
        with (ROOT / ".operation.lock").open("a") as lock:
            if args.command not in {"run-show", "publish-diff"}:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.command == "run-show":
                from driving_data_platform.common.runs import show

                report = show(ROOT, args.run_id or "")
            elif args.command == "publish-diff":
                from driving_data_platform.publishing.difference import difference
                from driving_data_platform.publishing.store import active, connect

                if not args.generation:
                    raise ValueError("必须指定 --generation")
                with connect(ROOT) as conn:
                    expected = active(conn)
                if args.against_generation:
                    expected = {"generation": args.against_generation, "revision": None}
                report = difference(ROOT, args.generation, expected)
            elif args.command == "prepare":
                report = prepare()
            elif args.command in {"hms-register", "hms-consume"}:
                from driving_data_platform.warehouse.catalog import (
                    consume,
                    new_trial,
                    register,
                )

                if args.command == "hms-register":
                    if args.trial:
                        if not re.fullmatch("[0-9a-f]{32}", args.trial):
                            raise ValueError("必须使用有效的 32 位试验 ID")
                        trial = ROOT / "metastore/trials" / args.trial
                    else:
                        water = json.loads(
                            (ROOT / "ingestion/watermark.json").read_text()
                        )
                        trial = new_trial(
                            ROOT,
                            ROOT
                            / "warehouse"
                            / (water["snapshot_id"] + "-" + args.version),
                        )
                    receipt = register(ROOT, REPO, trial)
                    report = {
                        "status": "passed",
                        "trial": trial.name,
                        "catalog": receipt,
                    }
                else:
                    if not args.trial or not re.fullmatch("[0-9a-f]{32}", args.trial):
                        raise ValueError("必须使用 --trial 指定有效的 32 位试验 ID")
                    report = consume(ROOT, REPO, ROOT / "metastore/trials" / args.trial)
            elif args.command.startswith("hms-"):
                action = args.command.removeprefix("hms-")
                log = (
                    ROOT
                    / "reports"
                    / ("metastore-" + action + "-" + uuid.uuid4().hex + ".log")
                )
                with log.open("w") as output:
                    result = subprocess.run(
                        [sys.executable, "scripts/metastore.py", action],
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        check=False,
                    )
                report = {
                    "status": "passed" if result.returncode == 0 else "blocked",
                    "exit_code": result.returncode,
                    "log": str(log),
                }
            elif args.command == "prepare-images":
                report = prepare_images()
            elif args.command == "check":
                budget = load_config(REPO / "configs/platform.example.yaml")
                budget["deployment"]["persistence_root"] = str(ROOT)
                report = inspect(budget, REPO)
                evidence = ROOT / "reports" / ("resource-" + uuid.uuid4().hex + ".json")
                evidence.parent.mkdir(exist_ok=True)
                evidence.write_text(json.dumps(report, indent=2))
            elif args.command == "verify":
                report = verify(
                    args.checks or ["dependencies", "regression", "preflight"],
                    args.faults,
                    duration=args.duration,
                    lock_fd=lock.fileno(),
                )
            elif args.command in {"init-db", "load", "publish", "rollback"}:
                from driving_data_platform.publishing.store import (
                    active,
                    connect,
                    initialize,
                    publish,
                )

                if args.command == "init-db":
                    initialize(ROOT, REPO)
                    report = {"status": "passed"}
                elif args.command == "load":
                    water = json.loads((ROOT / "ingestion/watermark.json").read_text())
                    from driving_data_platform.warehouse.catalog import materialize

                    report = materialize(
                        ROOT,
                        REPO,
                        ROOT
                        / "warehouse"
                        / (water["snapshot_id"] + "-" + args.version),
                    )
                else:
                    if not args.generation:
                        raise ValueError("必须指定 --generation")
                    with connect(ROOT) as conn:
                        expected = active(conn)
                    report = publish(
                        ROOT,
                        args.generation,
                        expected,
                        rollback=args.command == "rollback",
                    )
            elif args.command == "usage":
                from usage_report import usage

                report = usage(ROOT, REPO)
            elif args.command == "run":
                from driving_data_platform.pipeline import run

                report = run(ROOT, REPO, args.version)
            elif args.command in {"api-up", "api-stop"}:
                from api_process import start, stop

                report = start(ROOT, REPO) if args.command == "api-up" else stop(ROOT)
            elif args.command == "build":
                from driving_data_platform.warehouse.build import build

                report = build(ROOT, REPO, args.version)
            elif args.command == "ods":
                report = ingest(load_config(CONFIG), REPO)
            else:
                if args.command in {"up", "postgres-up"}:
                    budget = load_config(REPO / "configs/platform.example.yaml")
                    budget["deployment"]["persistence_root"] = str(ROOT)
                    if inspect(budget, REPO)["status"] != "passed":
                        raise PlatformError(
                            "RESOURCE_BLOCKED", "resource-gate", "资源门禁未通过"
                        )
                if args.command == "stop":
                    from api_process import stop

                    stop(ROOT)
                if (
                    args.command == "up"
                    and not (ROOT / "metastore/runtime.env").exists()
                ):
                    raise ValueError(
                        "请先执行 postgres-up、init-db、hms-prepare；启动不隐式准备依赖"
                    )
                compose(
                    *{
                        "up": [
                            "up",
                            "-d",
                            "--wait",
                            "--pull",
                            "never",
                            "postgres",
                            "metastore",
                        ],
                        "postgres-up": [
                            "up",
                            "-d",
                            "--wait",
                            "--pull",
                            "never",
                            "postgres",
                        ],
                        "stop": ["stop"],
                        "status": ["ps"],
                    }[args.command]
                )
                report = {"status": "passed", "operation": args.command}
                if args.command == "status":
                    from api_process import status

                    report["api"] = status(ROOT)
        report_dir = ROOT / "reports"
        report_dir.mkdir(exist_ok=True)
        (report_dir / (args.command + "-" + uuid.uuid4().hex + ".json")).write_text(
            json.dumps(report, indent=2)
        )
        print(json.dumps(report, ensure_ascii=False))
        return 0 if args.command == "run-show" or report["status"] == "passed" else 2
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 -- CLI 边界统一记录未知失败
        from driving_data_platform.common.errors import describe

        report = {
            "status": "blocked",
            "error": describe(error, args.command),
        }
        if validated_root:
            try:
                directory = ROOT / "reports"
                directory.mkdir(exist_ok=True)
                (
                    directory / (args.command + "-failed-" + uuid.uuid4().hex + ".json")
                ).write_text(json.dumps(report, indent=2, ensure_ascii=False))
            except OSError:
                pass
        print(json.dumps(report, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
