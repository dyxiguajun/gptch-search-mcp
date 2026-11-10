#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if ! command -v uv >/dev/null 2>&1; then
  echo '请先安装 uv：https://docs.astral.sh/uv/getting-started/installation/' >&2
  exit 1
fi
uv sync --frozen --python 3.12
.venv/bin/python -m personal_search.cli --data-dir "$PWD/storage" doctor
.venv/bin/python -m personal_search.cli --data-dir "$PWD/storage" config
