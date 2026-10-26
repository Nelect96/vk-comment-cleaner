#!/usr/bin/env bash
# Isolated local setup. No account credentials or VK actions.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
ENV_PATH="${VKCM_VENV:-$ROOT/.venv}"

python_ok() {
  "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

find_python() {
  local candidate
  if [[ -n "${PYTHON:-}" ]]; then
    if python_ok "$PYTHON"; then
      PYTHON_CMD="$PYTHON"
      return 0
    fi
    echo "PYTHON=$PYTHON is not supported. Python 3.10+ is required." >&2
    return 1
  fi

  # macOS system python3 can be 3.9 despite newer versions installed.
  # Prefer Homebrew's versioned packages over unversioned Python.
  for candidate in \
    /opt/homebrew/opt/python@3.13/bin/python3.13 \
    /usr/local/opt/python@3.13/bin/python3.13 \
    /opt/homebrew/opt/python@3.12/bin/python3.12 \
    /Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13 \
    /Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 \
    /usr/local/opt/python@3.12/bin/python3.12 \
    python3.13 python3.14 python3.12 python3.11 python3.10 python3 \
    /usr/local/bin/python3 /opt/homebrew/bin/python3; do
    if python_ok "$candidate"; then
      PYTHON_CMD="$candidate"
      return 0
    fi
  done
  return 1
}

brew_executable() {
  if command -v brew >/dev/null 2>&1; then
    command -v brew
  elif [[ -x /opt/homebrew/bin/brew ]]; then
    printf '%s\n' /opt/homebrew/bin/brew
  elif [[ -x /usr/local/bin/brew ]]; then
    printf '%s\n' /usr/local/bin/brew
  else
    return 1
  fi
}

if ! find_python; then
  echo "Python 3.10+ was not found. Your macOS system Python will not be changed."
  if [[ "$(uname -s)" == "Darwin" ]]; then
    if BREW="$(brew_executable)"; then
      printf 'Homebrew found: %s\n' "$BREW"
      if [[ "${VKCM_NONINTERACTIVE:-0}" == "1" ]]; then
        echo "Non-interactive mode: install Python 3.13 using Homebrew first." >&2
        exit 2
      fi
      printf 'Install Python 3.13 using Homebrew? [y/N]: '
      IFS= read -r answer || answer="n"
      case "$answer" in
        [Yy]|[Yy][Ee][Ss])
          echo "Homebrew may ask you to confirm package/dependency changes again. Type y and press Enter to approve, or n to cancel."
          "$BREW" install python@3.13
          PYTHON_CMD="$("$BREW" --prefix python@3.13)/bin/python3.13"
          python_ok "$PYTHON_CMD" || { echo "Homebrew did not install a supported Python version." >&2; exit 2; }
          ;;
        *) echo "Installation cancelled. You can download Python from https://www.python.org/downloads/"; exit 2 ;;
      esac
    else
      echo "Homebrew not found. Install Python 3.13 from https://www.python.org/downloads/macos/" >&2
      echo "Or install Homebrew from https://brew.sh and run this command again." >&2
      exit 2
    fi
  else
    echo "Install Python 3.10+ with venv using your distribution's package manager, then try again." >&2
    exit 2
  fi
fi

printf 'Selected Python: %s (%s)\n' "$PYTHON_CMD" "$("$PYTHON_CMD" --version)"
if [[ ! -x "$ENV_PATH/bin/python" ]] || ! "$ENV_PATH/bin/python" -c \
  'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' >/dev/null 2>&1; then
  "$PYTHON_CMD" -m venv "$ENV_PATH"
fi

echo "=== Installing Python dependencies ==="
"$ENV_PATH/bin/python" -m pip install --disable-pip-version-check "playwright>=1.50,<2"
echo "=== Installing Chromium ==="
"$ENV_PATH/bin/python" -m playwright install chromium
echo "=== Running offline tests (no deletions) ==="
"$ENV_PATH/bin/python" -m unittest discover -s tests -q

printf "ok\n" > "$ENV_PATH/.vkcm-ready-0.4.0"
echo "VKCM setup complete. Start it with: bash scripts/app.sh"
