#!/usr/bin/env bash
# Each user supplies THEIR OWN archive. Explicit VK ID confirmation required.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="${VKCM_VENV:-$ROOT/.venv}/bin/python"
[[ -x "$PY" ]] || { echo "Run bash scripts/install.sh first" >&2; exit 1; }
SOURCE="${1:-}"
[[ -f "$SOURCE" ]] || { echo "Usage: bash scripts/cleanup.sh /path/to/YOUR/Archive.zip" >&2; exit 1; }
export VKCM_PORT="${VKCM_PORT:-18765}"
echo "Use only your own exported comments and own VK account. Requires explicit confirmation."
exec "$PY" -m vkmgr.playwright_batch --auto --execute --limit 1000 --source "$SOURCE"
