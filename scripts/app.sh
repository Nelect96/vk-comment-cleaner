#!/usr/bin/env bash
# Beginner-friendly local menu; only executes VK deletion after separate explicit confirmation.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="${VKCM_VENV:-$ROOT/.venv}/bin/python"
export VKCM_PORT="${VKCM_PORT:-18765}"

if [[ ! -x "$PY" ]] || [[ ! -f "${VKCM_VENV:-$ROOT/.venv}/.vkcm-ready-0.4.0" ]]; then
  printf "\nVK Comment Manager: first-time setup.\n"
  bash "$ROOT/scripts/install.sh"
fi

choose_archive() {
  local file=""
  if [[ "${1:-}" != "" ]]; then printf "%s\n" "$1"; return; fi
  if [[ "$(uname -s)" == "Darwin" ]] && command -v osascript >/dev/null 2>&1; then
    file="$(osascript -e 'POSIX path of (choose file with prompt "Select your VK comments ZIP archive")' 2>/dev/null || true)"
  fi
  if [[ -z "$file" ]]; then
    printf "Drag your ZIP archive into Terminal and press Enter: " >&2
    IFS= read -r file
    file="${file%\"}"; file="${file#\"}"
  fi
  [[ -f "$file" ]] || { echo "File not found. Nothing was started." >&2; return 1; }
  printf "%s\n" "$file"
}

printf "\n=======================================\n"
printf " VK Comment Manager | Local edition\n"
printf "=======================================\n"
printf "1. Open application (import, search, sign in)\n"
printf "2. Delete my own comments from an archive\n"
printf "3. Run offline tests\n"
printf "0. Exit\n"
printf "Choose an option [1]: "
IFS= read -r choice || choice=0
choice="${choice:-1}"
case "$choice" in
  1) exec bash "$ROOT/scripts/start.sh" ;;
  2)
    echo "Deletion requires a verified VK ID account and explicit confirmation."
    file="$(choose_archive "${1:-}")"
    exec bash "$ROOT/scripts/cleanup.sh" "$file"
    ;;
  3) exec "$PY" -m unittest discover -s tests -q ;;
  0) echo "Goodbye." ;;
  *) echo "Invalid option. Nothing was started."; exit 1 ;;
esac
