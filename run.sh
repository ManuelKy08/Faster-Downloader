#!/usr/bin/env bash
# IDMLike — launcher
set -euo pipefail
cd "$(dirname "$0")"

# Pakai venv lokal
PY="$(pwd)/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "[IDMLike] Membuat venv..." >&2
  python3 -m venv .venv
  ./.venv/bin/pip install -r requirements.txt
fi

export PYTHONPATH="$(pwd)/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$PY" -m idmlike.app "$@"