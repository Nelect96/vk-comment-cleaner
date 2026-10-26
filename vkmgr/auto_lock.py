"""Single local worker for irreversible Chromium archive batches.

Rejects parallel runs instead of relying only on a stale journal snapshot.
Does not kill other processes; lock releases on exit or crash.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path

from .journal import journal_path


@contextmanager
def automatic_process_lock(path=None):
    root = Path(path) if path is not None else journal_path().parent / "automatic-batch.lock"
    root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise ValueError("Unsafe symlink to process lock file")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(root), flags, 0o600)
    try:
        if not root.is_file() or root.is_symlink():
            raise ValueError("Process lock is not a regular file")
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            raise ValueError(
                "Another automatic VKCM batch is already running. "
                "Do not start concurrent batches."
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
