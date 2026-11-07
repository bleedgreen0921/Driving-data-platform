#!/bin/sh
set -eu
if [ -n "${DDP_PYTHON:-}" ]; then
  case "$DDP_PYTHON" in /*) ;; *) DDP_PYTHON="$(pwd)/$DDP_PYTHON" ;; esac
elif [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/python" ] && "$VIRTUAL_ENV/bin/python" -c 'import sys; sys.exit(sys.version_info[:2] != (3,12))'; then
  DDP_PYTHON="$VIRTUAL_ENV/bin/python"
else
  DDP_PYTHON=$(command -v python3.12 || true)
fi
if [ -z "$DDP_PYTHON" ] || [ ! -x "$DDP_PYTHON" ]; then
  echo '需要专用 Python 3.12 环境；请设置 DDP_PYTHON。' >&2
  exit 2
fi
"$DDP_PYTHON" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3,12) else "需要 Python 3.12；检查 DDP_PYTHON。")'
export DDP_PYTHON
export DDP_CALLER_CWD="$(pwd)"
