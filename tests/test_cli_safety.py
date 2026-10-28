import json
import os
import subprocess
import sys
from pathlib import Path


def test_prepare_rejects_dataset_overlap_before_writing(tmp_path):
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    marker = dataset / "original.txt"
    marker.write_text("原始数据不可修改")
    environment = dict(
        os.environ, DDP_OUTPUT_ROOT=str(dataset), DDP_DATASET_ROOT=str(dataset)
    )
    result = subprocess.run(
        [sys.executable, "scripts/manage_platform.py", "prepare"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "产物目录不得与源数据重叠" in result.stdout
    assert list(dataset.iterdir()) == [marker]
    assert marker.read_text() == "原始数据不可修改"


def test_prepare_rejects_symbolic_output(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(target, target_is_directory=True)
    environment = dict(os.environ, DDP_OUTPUT_ROOT=str(link))
    result = subprocess.run(
        [sys.executable, str(Path("scripts/manage_platform.py")), "prepare"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert not list(target.iterdir())


def test_catalog_consume_rejects_path_as_trial(tmp_path):
    environment = dict(os.environ, DDP_OUTPUT_ROOT=str(tmp_path / "artifacts"))
    result = subprocess.run(
        [
            sys.executable,
            "scripts/manage_platform.py",
            "hms-consume",
            "--trial",
            "../outside",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "32 位试验 ID" in result.stdout
    assert not (tmp_path / "outside").exists()


def test_direct_integration_cannot_bypass_serial_lock(tmp_path):
    import fcntl

    root = tmp_path / "artifacts"
    root.mkdir()
    environment = dict(os.environ, DDP_OUTPUT_ROOT=str(root))
    environment.pop("DDP_OPERATION_LOCK_FD", None)
    with (root / ".operation.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(
            [sys.executable, "scripts/verify_integration.py", "--duration", "20"],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
    assert result.returncode == 2
    assert "串行验证锁" in json.loads(result.stdout)["error"]
    assert not (root / "reports").exists()


def test_run_show_can_read_legacy_report_while_operation_locked(tmp_path):
    import fcntl

    root = tmp_path / "artifacts"
    (root / "reports").mkdir(parents=True)
    run_id = "a" * 32
    legacy = {"status": "failed", "steps": {}}
    (root / "reports" / ("pipeline-" + run_id + ".json")).write_text(json.dumps(legacy))
    with (root / ".operation.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(
            [
                sys.executable,
                "scripts/manage_platform.py",
                "run-show",
                "--run-id",
                run_id,
            ],
            env=dict(os.environ, DDP_OUTPUT_ROOT=str(root)),
            capture_output=True,
            text=True,
            check=False,
        )
    assert result.returncode == 0
    assert json.loads(result.stdout) == legacy
