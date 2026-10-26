"""Opt-in, local-only discovery of VK comment archives in Downloads.

Archive requests and VK login always happen on VK's own website. This module
does NOT use browser cookies, inspect the user's browser profile, or contact VK.
Scanning checks ZIP directory entries only, without extracting or reading
comment contents. Contents are read only when the user selects a specific ZIP.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
import zipfile

from .parser import (
    MAX_ARCHIVE_ENTRIES, MAX_FILES, MAX_HTML_BYTES, MAX_TOTAL_UNPACKED,
    MAX_ZIP_BYTES, _is_comment_page, parse_zip,
)

MAX_DOWNLOAD_ENTRIES = 500
MAX_MATCHES = 25


def default_downloads():
    return Path.home() / "Downloads"


def _is_archive(path):
    """Only regular, non-symlink ZIP files within the selected directory."""
    if path.suffix.lower() != ".zip" or path.is_symlink():
        return False
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and 0 < info.st_size <= MAX_ZIP_BYTES


def _manifest(path):
    """Return number of recognizable archive pages, or zero when invalid."""
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ARCHIVE_ENTRIES:
                return 0
            count = 0
            total = 0
            for entry in entries:
                if entry.is_dir() or not _is_comment_page(entry.filename):
                    continue
                if count >= MAX_FILES:
                    return 0
                if entry.file_size > MAX_HTML_BYTES:
                    return 0
                total += entry.file_size
                if total > MAX_TOTAL_UNPACKED:
                    return 0
                count += 1
            return count
    except (OSError, zipfile.BadZipFile, RuntimeError, ValueError):
        return 0


def find_archives(folder=None):
    """List promising ZIP files from the direct Downloads folder, newest first.

    Does not send ZIP contents or names to VK or to any remote service.
    """
    root = Path(folder) if folder is not None else default_downloads()
    if not root.is_dir():
        return []
    try:
        candidates = []
        with os.scandir(root) as scan:
            for index, entry in enumerate(scan):
                if index >= MAX_DOWNLOAD_ENTRIES:
                    break
                try:
                    path = Path(entry.path)
                    if not _is_archive(path):
                        continue
                    info = entry.stat(follow_symlinks=False)
                    candidates.append((info.st_mtime_ns, path, info.st_size))
                except OSError:
                    continue
        candidates.sort(reverse=True, key=lambda x: x[0])
        matches = []
        for _, path, size in candidates:
            pages = _manifest(path)
            if not pages:
                continue
            matches.append({"name": path.name, "size": size, "pages": pages})
            if len(matches) == MAX_MATCHES:
                break
        return matches
    except (OSError, PermissionError) as exc:
        raise ValueError("Cannot access Downloads folder; check macOS permissions") from exc


def read_selected_archive(name, folder=None):
    """Read exactly the selected ZIP with symlink and traversal protection.

    Rechecks file type and size after opening it, preventing path swaps.
    """
    if (not isinstance(name, str) or not name or len(name) > 255
            or name in (".", "..") or "/" in name or "\\" in name
            or not name.lower().endswith(".zip")):
        raise ValueError("Choose a ZIP file from the discovered list")
    root = Path(folder) if folder is not None else default_downloads()
    path = root / name
    if not _is_archive(path):
        raise ValueError("Selected archive is unavailable, too large or unsafe")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_ZIP_BYTES:
                raise ValueError("Archive changed since discovery")
            data = source.read(MAX_ZIP_BYTES + 1)
    except OSError as exc:
        raise ValueError("Unable to read the selected archive") from exc
    if len(data) > MAX_ZIP_BYTES:
        raise ValueError("ZIP is larger than the supported 128 MiB limit")
    records, meta = parse_zip(data)
    if meta["files"] < 1:
        raise ValueError("No recognized comment files in the selected ZIP")
    return records, meta
