"""Offline staging of own VK comment links before an archive is available.

- No VK API calls, browser login, Playwright, network or deletes.
- TXT/CSV/VK ZIP read locally; retain only recognized exact permalinks.
- Private, explicit, atomic queue snapshots; no auto-overwrite.
- Optional read-only per-user SQLite journal filtering for progress.
- Synthetic stress test NEVER writes URLs or starts the live batch.

Actual deletion always uses vkmgr.playwright_batch with a separately
authenticated account, fresh author-proof, a 1..5 cap, explicit consent,
and its own no-repeat journal checks.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import os
from pathlib import Path
import tempfile

from .journal import Journal
from .parser import MAX_ZIP_BYTES, parse_comment_link, parse_zip
from .playwright_batch import MAX_FILE_BYTES, MAX_FILE_LINKS, read_url_file

MAX_QUEUE = 5000


@dataclass
class QueuePlan:
    """Summary and ordered exact VK permalinks. No comment texts/tokens."""
    links: list
    imported: int
    duplicates: int
    skipped_status: int
    unsupported: int
    by_type: dict
    source: str


def load_source(path):
    """Read local TXT, native VKCM CSV or VK data ZIP; never extract to disk."""
    src = Path(path).expanduser()
    if not src.is_file() or src.is_symlink():
        raise ValueError("Source file is missing or is a symlink")
    suffix = src.suffix.casefold()
    if suffix == ".zip":
        if src.stat().st_size > MAX_ZIP_BYTES:
            raise ValueError("ZIP exceeds the 128 MiB limit")
        data, meta = parse_zip(src.read_bytes())
        if meta.get("files", 0) == 0:
            raise ValueError("ZIP contains no comments*.html/txt. Did you select the Comments export category?")
        return [item.url for item in data]
    if suffix in (".txt", ".csv"):
        return read_url_file(str(src))
    raise ValueError("Only VK ZIP, CSV, or TXT is supported")


def build_plan(urls, user_id=None, journal=None, wall_only=True, source="source"):
    """Build a local queue without any VK writes. Account ID only filters status.

    Supplying user_id here is NOT proof of identity or authorization to delete.
    The interactive browser batch verifies identity again independently.
    """
    if user_id is not None and (type(user_id) is not int or user_id <= 0):
        raise ValueError("Journal viewing requires a positive numeric VK user ID")
    if journal is not None and user_id is None:
        raise ValueError("Filtering by journal requires --user-id")
    seen = set()
    unique = []
    duplicates = 0
    for raw in urls:
        parsed = parse_comment_link(str(raw))
        if parsed is None:
            raise ValueError("Source contains an unsupported comment URL")
        kind, owner, item, cid, clean = parsed
        key = f"{kind}:{owner}:{item}:{cid}"
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        unique.append((key, kind, clean))
        if len(unique) > MAX_QUEUE:
            raise ValueError("Maximum 5,000 unique comments per plan")

    latest = journal.statuses(user_id, [item[0] for item in unique]) if journal else {}
    # Never silently drop uncertain/pending entries: explicitly account for them.
    from .journal import BLOCK_REPEAT
    kept = []
    unsupported = 0
    skipped_status = 0
    by_type = Counter()
    for key, kind, clean in unique:
        by_type[kind] += 1
        if latest.get(key) in BLOCK_REPEAT:
            skipped_status += 1
        elif wall_only and kind != "wall":
            unsupported += 1
        else:
            kept.append(clean)
    return QueuePlan(
        links=kept, imported=len(unique) + duplicates,
        duplicates=duplicates, skipped_status=skipped_status,
        unsupported=unsupported, by_type=dict(by_type), source=source,
    )


def write_snapshot(path, urls):
    """Create private immutable-at-creation snapshot; never overwrite user data."""
    file = Path(path).expanduser()
    if not urls:
        raise ValueError("No links to save")
    if len(urls) > MAX_QUEUE:
        raise ValueError("Queue plan limit exceeded")
    if file.exists() or file.is_symlink():
        raise FileExistsError("File already exists. Choose another name; overwriting is blocked.")
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".vkcm-queue-", suffix=".tmp",
                               dir=str(file.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            for url in urls:
                # Security: generated snapshots contain only normalized VK
                # permalinks. No scripts, HTML, comment texts or secrets.
                if parse_comment_link(url) is None:
                    raise ValueError("Attempted to save an unsupported URL")
                stream.write(url + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Hard link fails atomically if destination exists, unlike os.replace.
        os.link(tmp, file)
        os.chmod(file, 0o600)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return file


def synthetic_demo(count):
    """Offline-only synthetic stress test; never output these as real VK links."""
    if type(count) is not int or not 1 <= count <= MAX_QUEUE:
        raise ValueError("Demo supports 1..5000 comments")
    rows = [
        "https://vk.com/wall-998877665_444?reply=" + str(100000 + n)
        for n in range(count)
    ]
    rows += rows[:min(15, len(rows))]
    plan = build_plan(rows, source="DEMO_ONLY")
    assert len(plan.links) == count and plan.duplicates == min(15, count)
    return plan


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="VKCM: offline queue planning from ZIP/CSV/TXT (no VK writes)"
    )
    sel = ap.add_mutually_exclusive_group(required=True)
    sel.add_argument("--source", help="Path to VK ZIP, TXT or CSV")
    sel.add_argument("--demo", type=int, metavar="N",
                     help="Run N synthetic comments (no file created)")
    ap.add_argument("--save", help="Save a new private TXT queue; never overwrite")
    ap.add_argument("--user-id", type=int, help="Existing journal filtering only; not authentication")
    ap.add_argument("--include-media", action="store_true",
                    help="Include photo/video links in the list (browser deletion not supported)")
    args = ap.parse_args(argv)
    try:
        if args.demo is not None:
            if args.save or args.user_id or args.include_media:
                raise ValueError("Demo never saves a queue or reads the real journal")
            plan = synthetic_demo(args.demo)
        else:
            urls = load_source(args.source)
            journal = Journal() if args.user_id is not None else None
            plan = build_plan(
                urls, user_id=args.user_id, journal=journal,
                wall_only=not args.include_media,
                source=Path(args.source).name
            )
        print("SOURCE: " + plan.source)
        print("Input links: " + str(plan.imported))
        print("Duplicates: " + str(plan.duplicates))
        print("Already handled/uncertain: " + str(plan.skipped_status))
        print("Unsupported for browser deletion: " +
              str(plan.unsupported))
        print("Queued: " + str(len(plan.links)))
        for kind in sorted(plan.by_type):
            print("  " + kind + ": " + str(plan.by_type[kind]))
        if args.demo is not None:
            print("DEMO_OK: synthetic records; VK was not contacted, "
                  "no file was written and nothing was deleted.")
            return 0
        if args.save:
            if not plan.links:
                print("Queue is empty. Nothing to save.")
                return 0
            saved = write_snapshot(args.save, plan.links)
            print("Private TXT saved: " + str(saved))
            print("Start a separate browser batch only after "
                  "verified VK ID login, using --file.")
        else:
            print("PREVIEW_OK: no changes. Add --save PATH to write a queue.")
        return 0
    except (OSError, ValueError, AssertionError) as exc:
        print("QUEUE_STOP: " + str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
