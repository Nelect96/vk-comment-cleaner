"""Read-only progress dashboard for the 4,693-link VK comment archive.

The preview NEVER starts the VK server, browser, API, or delete action.
Statuses come from the private, per-user journal and are not proof that a
comment still exists (or was removed) on the VK server.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sqlite3
import sys
from urllib.parse import quote

from .journal import BLOCK_REPEAT, journal_path
from .parser import parse_comment_link
from .queue_plan import load_source, write_snapshot

MAX_PREVIEW = 5
MAX_LINKS = 5000

# These are not interchangeable: UI observation/user testimony is not an
# independently confirmed VK backend deletion.
DONE_STATUSES = frozenset({
    "browser_ui_removed", "browser_ui_deleted_marker", "browser_user_confirmed", "user_marked_deleted",
})
UNKNOWN_STATUSES = frozenset({
    "browser_pending", "browser_unknown", "pending", "outcome_unknown",
    "api_accepted",
})
NOT_FOUND_STATUS = "browser_not_found"


@dataclass
class Progress:
    total: int
    duplicates: int
    eligible: int
    ui_removed: int
    user_confirmed: int
    manual_confirmed: int
    unknown: int
    not_found: int
    other_blocked: int
    not_supported: int
    source: str
    next_urls: list


def read_journal_statuses(user_id, path=None):
    """One SQLite read-only query. Do not create or modify journal or tokens."""
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("Specify a positive numeric VK user ID for your journal")
    file = Path(path) if path is not None else journal_path()
    if file.is_symlink():
        raise ValueError("Journal must not be a symlink")
    if not file.exists():
        return {}
    if not file.is_file():
        raise ValueError("Journal file is unavailable")
    # mode=ro guarantees that a progress report cannot alter durable journal
    # operations. SQLite itself owns reading the WAL when present.
    uri = "file:" + quote(str(file.absolute()), safe="/") + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as conn:
            rows = conn.execute(
                "SELECT comment_key, status FROM latest WHERE user_id=?",
                (user_id,)
            ).fetchall()
    except sqlite3.Error as exc:
        raise ValueError("Could not read the local SQLite journal") from exc
    return dict(rows)


def calculate_progress(urls, statuses, source, limit=5):
    """Pure read-only queue reduction with exact-key dedupe and no VK calls."""
    if type(limit) is not int or not 1 <= limit <= MAX_PREVIEW:
        raise ValueError("Next batch size must be 1..5")
    if not isinstance(statuses, dict):
        raise ValueError("Invalid journal")
    seen = set()
    eligible = []
    duplicates = 0
    ui_removed = user_confirmed = manual_confirmed = unknown = 0
    not_found = other_blocked = not_supported = 0
    all_rows = 0
    for raw in urls:
        parsed = parse_comment_link(str(raw))
        if parsed is None:
            raise ValueError("Source contains an invalid VK comment URL")
        kind, owner, item, comment_id, clean = parsed
        key = "{}:{}:{}:{}".format(kind, owner, item, comment_id)
        all_rows += 1
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        if len(seen) > MAX_LINKS:
            raise ValueError("Report supports at most 5000 unique URLs")
        if kind != "wall":
            not_supported += 1
            continue
        status = statuses.get(key, "")
        if status in ("browser_ui_removed", "browser_ui_deleted_marker"):
            ui_removed += 1
        elif status == "browser_user_confirmed":
            user_confirmed += 1
        elif status == "user_marked_deleted":
            manual_confirmed += 1
        elif status == NOT_FOUND_STATUS:
            not_found += 1
        elif status in UNKNOWN_STATUSES:
            unknown += 1
        elif status in BLOCK_REPEAT:
            other_blocked += 1
        else:
            eligible.append(clean)
    return Progress(
        total=len(seen), duplicates=duplicates, eligible=len(eligible),
        ui_removed=ui_removed, user_confirmed=user_confirmed,
        manual_confirmed=manual_confirmed, unknown=unknown,
        not_found=not_found, other_blocked=other_blocked,
        not_supported=not_supported, source=source,
        next_urls=eligible[:limit],
    )


def main(argv=None):
    cli = argparse.ArgumentParser(
        description="VKCM: read-only queue progress and next links"
    )
    cli.add_argument("--source", required=True,
                     help="VK ZIP archive, VKCM CSV or TXT containing comment links")
    cli.add_argument("--user-id", required=True, type=int,
                     help="User ID to read the journal; NOT deletion authorization")
    cli.add_argument("--next", type=int, default=5,
                     help="Show next batch of 1..5 links (default 5)")
    cli.add_argument("--save-next", default="",
                     help="Write a new private TXT containing the next 1..5 links")
    cli.add_argument("--json", action="store_true",
                     help="Machine-readable summary, without comment texts")
    args = cli.parse_args(argv)
    try:
        urls = load_source(args.source)
        statuses = read_journal_statuses(args.user_id)
        info = calculate_progress(
            urls, statuses, Path(args.source).name, limit=args.next,
        )
        if args.save_next and not info.next_urls:
            raise ValueError("No new links to save")
        if args.save_next:
            write_snapshot(args.save_next, info.next_urls)
        if args.json:
            payload = asdict(info)
            if not args.save_next:
                payload.pop("next_urls")
            if args.save_next:
                payload["saved_next"] = str(Path(args.save_next).expanduser())
            print(json.dumps(payload, ensure_ascii=False))
            return 0
        print("=== VK COMMENT MANAGER PROGRESS (READ ONLY) ===")
        print("Source: " + info.source)
        print("Unique links in archive: " + str(info.total))
        print("Duplicate input links: " + str(info.duplicates))
        print("VK UI deletion markers (not server verified): " + str(info.ui_removed))
        print("User-confirmed: " + str(info.user_confirmed))
        print("Manually marked: " + str(info.manual_confirmed))
        print("Unknown outcomes (no retries): " + str(info.unknown))
        print("Missing before click (eligible for review): " + str(info.not_found))
        print("Other blocked records: " + str(info.other_blocked))
        print("Unsupported types: " + str(info.not_supported))
        print("Eligible for inspection: " + str(info.eligible))
        if info.next_urls:
            print("Next " + str(len(info.next_urls)) + " links:")
            for number, url in enumerate(info.next_urls, 1):
                print("  " + str(number) + ". " + url)
        if args.save_next:
            print("Private batch saved: " + str(Path(args.save_next).expanduser()))
        print("PREVIEW_OK: VK was not opened and no deletion was attempted.")
        return 0
    except (OSError, ValueError) as exc:
        print("PROGRESS_STOP: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
