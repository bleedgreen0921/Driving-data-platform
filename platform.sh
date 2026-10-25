#!/bin/sh
set -eu
cd "$(dirname "$0")"
DDP_PYTHON=${DDP_PYTHON:-/Users/qiujiajun/Python-env/driving-data-platform-py312/bin/python}
if [ ! -x "$DDP_PYTHON" ]; then
  echo 'Python 环境不存在；先用 Python 3.12 创建专用环境并安装 requirements.lock' >&2
  exit 2
fi
"$DDP_PYTHON" - <<'CHECK_PYTHON'
import sys
if sys.version_info[:2] != (3, 12):
    sys.exit("需要 Python 3.12；请检查 DDP_PYTHON。")
try:
    import yaml
    import fastapi
    import uvicorn
    import psycopg
except ImportError:
    sys.exit("依赖缺失或无法加载；请在专用环境安装 requirements.lock，不会自动安装。")
CHECK_PYTHON
exec "$DDP_PYTHON" scripts/manage_platform.py "$@"
