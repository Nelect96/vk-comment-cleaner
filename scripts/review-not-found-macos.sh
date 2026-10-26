#!/usr/bin/env bash
# Dedicated follow-up for comments that were NOT found BEFORE any delete click.
# Never includes browser_unknown, pending, or previously clicked/confirmed IDs.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VENV="${VKCM_VENV:-$ROOT/.venv}"
SOURCE="${1:-$HOME/Downloads/Archive.zip}"
export VKCM_PORT="${VKCM_PORT:-18765}"

if [[ ! -x "$VENV/bin/python" ]]; then
  printf 'Playwright Python not found: %s\n' "$VENV/bin/python" >&2
  exit 1
fi
if [[ ! -f "$SOURCE" ]]; then
  printf 'VK archive not found: %s\n' "$SOURCE" >&2
  exit 1
fi

printf 'Recheck only browser_not_found entries from %s\n' "$SOURCE"
printf 'Previously clicked or uncertain deletion attempts will not be retried.\n'
printf 'A separate DELETE BATCH N confirmation is required before new deletions.\n'
printf 'Press Ctrl+C to stop processing.\n\n'
exec "$VENV/bin/python" -m vkmgr.playwright_batch \
  --auto --execute --review-not-found --limit 1000 --source "$SOURCE"
