#!/bin/sh
set -eu
exec "$(dirname "$0")/platform.sh" verify "$@"
