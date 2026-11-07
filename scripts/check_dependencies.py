"""Read-only compatibility check for the selected Python, with pip or uv."""

import importlib.util
import shutil
import subprocess
import sys


def main():
    if importlib.util.find_spec("pip") is not None:
        command = [sys.executable, "-m", "pip", "check"]
    elif executable := shutil.which("uv"):
        command = [executable, "pip", "check", "--python", sys.executable]
    else:
        print("依赖检查不可执行：当前环境没有pip且PATH没有uv；不会自动安装。")
        return 2
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
