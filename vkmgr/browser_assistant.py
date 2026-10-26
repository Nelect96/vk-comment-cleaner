"""User-controlled browser queue for removing own VK comments without VK API.

This tool opens exact comment permalinks in the user's DEFAULT web browser.
It NEVER executes JavaScript in VK, accesses Safari cookies, calls the VK
comment API, or clicks Delete. The user removes each comment on vk.com and
can explicitly record their own report in the existing local SQLite journal.
Run while VK Comment Manager's localhost UI is open.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from html import unescape

from .parser import parse_comment_link
from .local_config import PORT, BASE

MAX_QUEUE = 500
BLOCKED_STATUSES = frozenset({
    "api_accepted", "pending", "outcome_unknown", "user_marked_deleted",
    "browser_pending", "browser_unknown", "browser_ui_removed", "browser_user_confirmed", "browser_not_found", "browser_ui_deleted_marker",
})


def _open_local(request, timeout=5):
    """Always connect to 127.0.0.1 DIRECTLY, never via macOS/VPN proxies.

    urllib's default proxy auto-discovery may route loopback traffic through
    a proxy that returns HTTP 403/404 unrelated to VK Comment Manager.
    Explicit ProxyHandler({}) blocks any inherited HTTP(S) proxy for this
    *local-only* client, without changing global network settings.
    """
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({})
    ).open(request, timeout=timeout)


def _get(path):
    request = urllib.request.Request(BASE + path, headers={"Accept": "application/json"})
    with _open_local(request, timeout=5) as response:
        if response.status != 200:
            raise ValueError("Local application refused the request")
        body = response.read(5_000_001)
    if len(body) > 5_000_000:
        raise ValueError("Local response is too large")
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Local port " + str(PORT) + " did not return expected VKCM JSON. "
                         "Run: python -m vkmgr.port_diagnostics") from exc


def _local_server_running():
    """Check our application's expected response, not just an open port."""
    try:
        response = _get("/api/comments?page=1")
    except urllib.error.HTTPError as exc:
        raise ValueError(
            "Port " + str(PORT) + " responded with HTTP " + str(exc.code) +
            " on /api/comments; this is not confirmed to be VKCM. "
            "Check the port: python -m vkmgr.port_diagnostics. "
            "Other processes will not be terminated automatically."
        ) from exc
    except urllib.error.URLError as exc:
        # Only a failed connection is a reason to start the application.
        if isinstance(exc.reason, (ConnectionRefusedError, TimeoutError, OSError)):
            return False
        raise
    if not isinstance(response, dict) or not isinstance(response.get("items"), list) or "auth_user" not in response:
        raise ValueError("Port " + str(PORT) + " is in use by an unexpected service. "
                         "Run: python -m vkmgr.port_diagnostics")
    return True


def _start_server():
    """Start a private local child server; caller must stop it on exit."""
    if _local_server_running():
        return None
    print("VK Comment Manager is not running. Starting the local server...")
    process = subprocess.Popen(
        [sys.executable, "-m", "vkmgr"], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(40):
        if process.poll() is not None:
            raise ValueError("Local server exited. Check whether port " + str(PORT) + ".")
        time.sleep(0.2)
        try:
            if _local_server_running():
                print("Local server started: " + BASE)
                return process
        except ValueError:
            process.terminate()
            process.wait(timeout=3)
            raise
    process.terminate()
    process.wait(timeout=3)
    raise ValueError("Local server did not respond within 8 seconds")


def get_queue(limit=20):
    """Read the currently loaded VKCM comments, never a remote VK API."""
    if not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValueError("Allowed batch size is 1 to 50")
    collected = []
    seen = set()
    owner = None
    total = None
    for page in range(1, 12):
        data = _get("/api/comments?page=" + str(page))
        if total is None:
            total = data.get("total", 0)
            owner = data.get("auth_user")
        if not isinstance(data.get("items"), list):
            raise ValueError("Malformed local queue")
        for item in data["items"]:
            if not isinstance(item, dict):
                continue
            parsed = parse_comment_link(str(item.get("url") or ""))
            if parsed is None:
                continue
            kind, oid, iid, cid, clean = parsed
            identity = f"{kind}:{oid}:{iid}:{cid}"
            if (identity != item.get("id") or identity in seen
                    or item.get("status") in BLOCKED_STATUSES):
                continue
            seen.add(identity)
            collected.append({"id": identity, "kind": kind, "url": clean,
                              "preview": str(item.get("text") or "")[:120]})
            if len(collected) >= limit:
                return collected, owner
        if page * 50 >= data.get("total", 0) or page * 50 >= MAX_QUEUE:
            break
    return collected, owner


def _csrf():
    request = urllib.request.Request(BASE + "/", headers={"Accept": "text/html"})
    with _open_local(request, timeout=5) as response:
        html = response.read(250_001).decode("utf-8")
    match = re.search(r'<meta\s+name="csrf"\s+content="([^"]+)"', html)
    if not match:
        raise ValueError("CSRF token missing from the local app")
    return unescape(match.group(1))


def record_user_report(comment_id, csrf):
    """Sends explicit manual status to LOCAL app; never to VK or elsewhere."""
    body = json.dumps({"id": comment_id, "confirmation": "Я УДАЛИЛ В VK"}).encode()
    request = urllib.request.Request(
        BASE + "/api/manual-mark", data=body,
        headers={"Content-Type": "application/json", "X-VKCM-CSRF": csrf},
        method="POST",
    )
    with _open_local(request, timeout=5) as response:
        result = json.load(response)
    if result.get("status") != "user_marked_deleted":
        raise ValueError("Local journal did not accept the report")
    return result


def guide(queue, account, opener=webbrowser.open, ask=input, echo=print,
          recorder=record_user_report, csrf_factory=_csrf):
    if not queue:
        echo("No comments to process. Add links or import your archive in VKCM first.")
        return {"visited": 0, "marked": 0}
    if not isinstance(account, dict) or not isinstance(account.get("id"), int):
        echo("Sign into VK ID in the local app first. Deletions cannot be marked without verified identity.")
        return {"visited": 0, "marked": 0}
    echo("BROWSER MODE. VK ID account " + str(account["id"]))
    echo("Opening links in your regular browser. YOU must delete comments directly on VK.")
    echo("Commands: DELETED (you deleted it on VK), Enter (skip), STOP (exit).")
    visited = 0
    marked = 0
    csrf = None
    for number, item in enumerate(queue, 1):
        parsed = parse_comment_link(item["url"])
        if parsed is None:
            continue
        echo("\n[" + str(number) + "/" + str(len(queue)) + "] " + item["url"])
        if item.get("preview"):
            echo("Archive text: " + item["preview"])
        if ask("Press Enter to open this comment, or STOP: ").strip().upper() == "STOP":
            break
        opened = opener(item["url"])
        if not opened:
            echo("Browser could not confirm opening. Open the URL manually.")
        visited += 1
        response = ask("After deleting on vk.com type DELETED; Enter = skip; STOP = quit: ").strip().upper()
        if response == "STOP":
            break
        if response == "DELETED":
            if ask("Confirm you deleted YOUR OWN comment on VK (YES): ").strip().upper() != "YES":
                echo("Confirmation cancelled.")
                continue
            try:
                if csrf is None:
                    csrf = csrf_factory()
                recorder(item["id"], csrf)
                marked += 1
                echo("Saved as user-confirmed in the local journal, NOT verified by VK API.")
            except (OSError, ValueError, urllib.error.HTTPError) as exc:
                echo("Could not save the journal entry: " + str(exc))
    echo("\nOpened: " + str(visited) + ". Marked deleted by you: " + str(marked) + ".")
    return {"visited": visited, "marked": marked}


def main(argv=None):
    cli = argparse.ArgumentParser(description="VK Comment Manager: browser-assisted manual comment queue")
    cli.add_argument("--limit", type=int, default=20, help="Maximum comments per session (1–50)")
    cli.add_argument("--dry-run", action="store_true", help="List links without opening browser")
    args = cli.parse_args(argv)
    spawned = None
    try:
        spawned = None if args.dry_run else _start_server()
        queue, account = get_queue(args.limit)
        if not args.dry_run and (not queue or not account):
            print("VK Comment Manager will open in your default browser.")
            print("Sign in via VK ID and add links or import your ZIP archive.")
            webbrowser.open(BASE)
            input("When signed in and comments are visible, press Enter here: ")
            queue, account = get_queue(args.limit)
        if args.dry_run:
            print("Available to process: " + str(len(queue)))
            for item in queue:
                print(item["url"])
            return 0
        guide(queue, account)
        return 0
    except (OSError, ValueError, urllib.error.HTTPError) as exc:
        print("Unable to start or connect to the local app:", exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped by user.")
        return 130
    finally:
        if spawned is not None and spawned.poll() is None:
            print("Stopping the temporary local VKCM server.")
            spawned.terminate()
            try:
                spawned.wait(timeout=5)
            except subprocess.TimeoutExpired:
                spawned.kill()
                spawned.wait(timeout=3)


if __name__ == "__main__":
    raise SystemExit(main())
