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
