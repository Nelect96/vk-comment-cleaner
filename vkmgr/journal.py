"""Private SQLite journal for attempted VK writes, scoped by authenticated user.

Stores comment identifiers and operation outcomes, NEVER tokens or comment texts.
A 'pending' or 'unknown' result is not safe to retry automatically.
"""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from contextlib import contextmanager

BLOCK_REPEAT = frozenset({'pending', 'outcome_unknown', 'api_accepted', 'user_marked_deleted', 'browser_pending', 'browser_unknown', 'browser_ui_removed', 'browser_user_confirmed', 'browser_not_found', 'browser_ui_deleted_marker'})


def journal_path():
    if sys.platform == 'darwin':
        root = Path.home() / 'Library' / 'Application Support'
    else:
        root = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local' / 'share')))
    return root / 'vk-comment-manager' / 'progress.sqlite3'


class Journal:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else journal_path()
        self.lock = threading.RLock()
        self._init()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.execute('PRAGMA busy_timeout=10000')
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise RuntimeError('Refusing symlinked journal')
        if not self.path.exists():
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        if self.path.is_symlink():
            raise RuntimeError('Refusing symlinked journal')
        os.chmod(self.path, 0o600)
        with self.lock, self._connect() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS latest (
                user_id INTEGER NOT NULL,
                comment_key TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, comment_key))''')
            conn.execute('''CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                comment_key TEXT NOT NULL,
                operation TEXT NOT NULL,
                status TEXT NOT NULL,
                timestamp TEXT NOT NULL)''')

    def record(self, user_id, comment_key, operation, status):
        if not isinstance(user_id, int) or user_id <= 0:
            raise ValueError('Verified numeric user ID is required')
        if operation not in ('delete', 'edit', 'verify', 'manual', 'browser'):
            raise ValueError('Unknown operation')
        if not comment_key or len(comment_key) > 180 or not status or len(status) > 60:
            raise ValueError('Invalid journal entry')
        now = datetime.now(timezone.utc).isoformat(timespec='seconds')
        with self.lock, self._connect() as conn:
            conn.execute('INSERT INTO events (user_id,comment_key,operation,status,timestamp) VALUES (?,?,?,?,?)',
                         (user_id, comment_key, operation, status, now))
            conn.execute('''INSERT INTO latest (user_id,comment_key,status,updated_at) VALUES (?,?,?,?)
                ON CONFLICT(user_id,comment_key) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at''',
                         (user_id, comment_key, status, now))

    def statuses(self, user_id, keys):
        if not user_id or not keys:
            return {}
        # Keyed lookup avoids SQLite parameter count limits for large archives.
        with self.lock, self._connect() as conn:
            rows = conn.execute('SELECT comment_key,status FROM latest WHERE user_id=?', (user_id,))
            wanted = set(keys)
            return {k: status for k, status in rows if k in wanted}

    def summary(self, user_id):
        if not user_id:
            return {}
        with self.lock, self._connect() as conn:
            return dict(conn.execute('SELECT status,COUNT(*) FROM latest WHERE user_id=? GROUP BY status', (user_id,)))
