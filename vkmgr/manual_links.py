"""Safely import individual VK comment permalinks for read-only preflight.

This module NEVER makes a network call. Ambiguous links are rejected because
guessing a comment ID is unsafe before an irreversible deletion.
"""
from __future__ import annotations

from .parser import Comment, parse_comment_link

MAX_LINKS = 50
MAX_LINE_CHARS = 2048


def parse_manual_links(text):
    if not isinstance(text, str):
        raise ValueError("Expected text with one VK comment link per line")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or len(lines) > MAX_LINKS:
        raise ValueError("Enter 1–50 VK comment links, one link per line")
    found = {}
    for pos, line in enumerate(lines, 1):
        if len(line) > MAX_LINE_CHARS:
            raise ValueError(f"Link on line {pos} is too long")
        parsed = parse_comment_link(line)
        if not parsed:
            raise ValueError(
                f"Line {pos}: not a recognized VK comment permalink. "
                "Include a comment ID (reply, comment, comment_id, cid or _rID)."
            )
        kind, owner, item, cid, url = parsed
        key = f"{kind}:{owner}:{item}:{cid}"
        found[key] = Comment(key, kind, owner, item, cid, url, "", "", "manual")
    return list(found.values()), {"submitted": len(lines), "recognized": len(found), "duplicates": len(lines) - len(found)}
