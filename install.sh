#!/bin/sh
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' 'Python 3.12以降を導入してから、もう一度実行してください。' >&2
  exit 1
fi
exec python3 -B "$script_dir/install.py" "$@"
