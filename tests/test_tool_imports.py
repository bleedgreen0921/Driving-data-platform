import subprocess
import sys


def test_direct_tool_imports():
    result = subprocess.run(
        [sys.executable, "scripts/manage_platform.py", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
