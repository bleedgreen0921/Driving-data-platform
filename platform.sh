#!/bin/sh
set -eu
DDP_REPO=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$DDP_REPO/scripts/python-entry.sh"
export PYTHONPATH="$DDP_REPO/src:$DDP_REPO/scripts${PYTHONPATH:+:$PYTHONPATH}"
exec "$DDP_PYTHON" "$DDP_REPO/scripts/manage_platform.py" "$@"
