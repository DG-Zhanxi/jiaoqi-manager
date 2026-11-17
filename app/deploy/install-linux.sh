#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
mkdir -p "$ROOT/runtime"
python3 -m venv "$ROOT/runtime/linux"
"$ROOT/runtime/linux/bin/python" -m pip install --upgrade pip
"$ROOT/runtime/linux/bin/python" -m pip install -r "$ROOT/app/deploy/requirements-linux.txt"
export JIAOQI_ROOT="$ROOT"
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
cd "$ROOT/app"
"$ROOT/runtime/linux/bin/python" -m pytest -q
