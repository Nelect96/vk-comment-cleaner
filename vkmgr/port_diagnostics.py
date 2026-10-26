"""Read-only diagnosis of VK Comment Manager's fixed localhost port.

This module NEVER stops processes, edits system proxy settings, contacts VK,
opens browser windows, or changes the comment journal.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

from .browser_assistant import _open_local
from .local_config import PORT, BASE


def port_owners(runner=subprocess.run):
    """Collect only process name and PID. Avoid printing full command lines."""
    if not shutil.which("lsof"):
        return ["lsof is unavailable; check the port owner manually"]
    try:
        result = runner(
            ["lsof", "-nP", "-iTCP:" + str(PORT), "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=4, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return ["Could not collect lsof output"]
    if result.returncode == 1 and not result.stdout.strip():
        return ["No listening process found"]
    if result.returncode != 0:
        return ["lsof could not enumerate processes"]
    seen = set()
    answer = []
    for line in result.stdout.splitlines()[1:]:
        columns = line.split()
        if len(columns) < 2 or not columns[1].isdigit():
            continue
        item = "PID " + columns[1] + ", process " + columns[0][:60]
        if item not in seen:
            seen.add(item)
            answer.append(item)
    return answer or ["Process information not found"]


def check_http(opener=_open_local):
    """Inspect one GET with no proxy; no cookies/tokens or request body."""
    request = urllib.request.Request(
        BASE + "/api/comments?page=1",
        headers={"Accept": "application/json"},
    )
    try:
        with opener(request, timeout=5) as response:
            server = response.headers.get("Server", "")
            raw = response.read(65537)
            if len(raw) > 65536:
                return False, "Response exceeds the safe diagnostic size"
            try:
                body = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeError):
                return False, "HTTP 200, but response is not VKCM JSON"
            if (isinstance(body, dict) and isinstance(body.get("items"), list)
                    and "auth_user" in body and "total" in body):
                return True, "VKCM_LOCAL_OK: API responded correctly (Server: " + server[:65] + ")"
            return False, "HTTP 200, but JSON structure does not match VKCM"
    except urllib.error.HTTPError as exc:
        server = str(exc.headers.get("Server", ""))[:65]
        return False, "HTTP " + str(exc.code) + (
            ", server " + server if server else ""
        ) + "; this does NOT confirm a VKCM server"
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (ConnectionRefusedError, TimeoutError, OSError)):
            return False, "No connection to " + BASE
        return False, "Local connection error: " + type(reason).__name__
    except (OSError, TimeoutError, ValueError) as exc:
        return False, "Diagnostic error: " + type(exc).__name__


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(
        description="Diagnose local VKCM port without deletion")
    parser.parse_args(argv)
    ok, message = check_http()
    print("=== VKCM LOCALHOST " + str(PORT) + " ===")
    print(message)
    print("=== PROCESSES LISTENING ON THE PORT ===")
    for item in port_owners():
        print(item)
    print("No processes were terminated, and no comments or journals were changed.")
    if ok:
        print("Port is available. You can safely retry opening your batch file.")
        return 0
    print("If another app is using this port, do not terminate it blindly. "
          "Use this output to diagnose the conflict.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
