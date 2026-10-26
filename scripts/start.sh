#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="${VKCM_VENV:-$ROOT/.venv}/bin/python"
[[ -x "$PY" ]] || { echo "Run bash scripts/install.sh first" >&2; exit 1; }
export VKCM_PORT="${VKCM_PORT:-18765}"
exec "$PY" -m vkmgr
