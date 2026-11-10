#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python -m personal_search.cli --data-dir "$PWD/storage" stop
