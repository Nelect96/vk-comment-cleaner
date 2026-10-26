"""VK archive reader. Pure Python, no network calls or executable archive extraction."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from html.parser import HTMLParser
import io
import re
from urllib.parse import parse_qs, unquote, urlparse, urlunparse
import zipfile

MAX_ZIP_BYTES = 128 * 1024 * 1024
MAX_HTML_BYTES = 16 * 1024 * 1024
MAX_TOTAL_UNPACKED = 256 * 1024 * 1024
MAX_FILES = 1500
MAX_ARCHIVE_ENTRIES = 10000
COMMENT_FILE = re.compile(r"^comments\d*\.(?:html?|txt)$", re.I)
MEDIA_PATH = re.compile(r"(?:^|/)(wall|photo|video)(-?\d+)_(\d+)(?:_r(\d+))?(?:$|[^\d])", re.I)
DATE_PATTERN = re.compile(r"\b(?:[0-3]?\d)[./](?:[01]?\d)[./](?:20\d\d|19\d\d)\b|\b[0-3]?\d\s+(?:янв|фев|мар|апр|ма[йя]|июн|июл|авг|сен|окт|ноя|дек)[а-я]*\s+(?:20\d\d|19\d\d)(?:\s+в\s+\d{1,2}:\d{2})?", re.I)


@dataclass(frozen=True)
class Comment:
    id: str
    kind: str
    owner_id: int
    item_id: int
    comment_id: int
    url: str
    text: str
    date: str
    source: str

    def to_dict(self) -> dict:
        return asdict(self)


def parse_comment_link(raw: str):
    """Return (kind, owner_id, item_id, comment_id, canonical_url) or None.

    Do not guess comment ID from the media ID; ambiguous URLs are skipped.
    """
    raw = raw.strip().replace("&amp;", "&")
    if raw.startswith("//"):
        raw = "https:" + raw
    elif raw.startswith("/"):
        raw = "https://vk.com" + raw
    elif raw.startswith("vk.com/") or raw.startswith("vk.ru/"):
        raw = "https://" + raw
    p = urlparse(raw)
    if p.scheme not in ("https", "http") or (p.hostname or "").lower() not in {
        "vk.com", "www.vk.com", "m.vk.com", "vk.ru", "www.vk.ru", "m.vk.ru"
    }:
        return None
    query = parse_qs(p.query)
    probes = [unquote(p.path), unquote(p.fragment)]
    for field in ("w", "z"):
        probes.extend(unquote(v) for v in query.get(field, []))
    match = next((m for s in probes if (m := MEDIA_PATH.search(s))), None)
    if not match:
        return None
    kind, owner, item_id, r_id = match.groups()
    comment_id = r_id
    if not comment_id:
        for key in ("reply", "comment_id", "comment", "cid"):
            val = query.get(key, [""])[0]
            if re.fullmatch(r"\d+", val):
                comment_id = val
                break
    if not comment_id:
        frag = re.search(r"(?:reply|comment)(\d+)", unquote(p.fragment), re.I)
        if frag:
            comment_id = frag.group(1)
    if not comment_id or int(comment_id) == 0:
        return None
    kind = kind.lower()
    # Keep VK’s own archive link: videos/photos may use different query parameters.
    # Canonicalize the host to prevent malformed or credential-bearing links.
    original = urlunparse(("https", "vk.com", p.path, "", p.query, p.fragment))
    return kind, int(owner), int(item_id), int(comment_id), original


class _Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag="root", attrs=None, parent=None):
        self.tag = tag
        self.attrs = dict(attrs or [])
        self.children = []
        self.parent = parent

    def text(self, limit=7000):
        result = []
        length = 0
        todo = [self]
        while todo and length < limit:
            cur = todo.pop()
            if isinstance(cur, str):
                result.append(cur)
                length += len(cur)
            else:
                todo.extend(reversed(cur.children))
        return " ".join(" ".join(result).split())[:limit]


class _ArchiveParser(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node()
        self.current = self.root
        self.anchors = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            # Kept out of preview later by _Node.text trimming, but not executable.
            pass
        node = _Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag == "a" and node.attrs.get("href"):
            self.anchors.append(node)
        if tag not in self.VOID:
            self.current = node

    def handle_endtag(self, tag):
        node = self.current
        while node.parent is not None:
            if node.tag == tag:
                self.current = node.parent
                return
            node = node.parent

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if data.strip() and self.current.tag not in ("script", "style"):
            self.current.children.append(data.strip())


def _nearest_description(anchor):
    # Some archive layouts place the text inside a neighboring .item block.
    node = anchor
    candidate = anchor.text()
    for _ in range(5):
        node = node.parent
        if node is None or node.tag in ("body", "html", "root"):
            break
        content = node.text()
        if len(content) > 5000:
            break
        classes = node.attrs.get("class", "").lower()
        if any(key in classes for key in ("item", "comment", "entry", "row", "record")):
            candidate = content
            break
        if len(content) > len(candidate) and len(content) <= 1600:
            candidate = content
    return candidate.strip()[:1600]


def _decode(b):
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", errors="replace")


def _is_comment_page(path):
    """Identify actual comments-history pages, not photo/video/wall comments."""
    parts = path.replace("\\", "/").strip("/").split("/")
    if not parts or not COMMENT_FILE.fullmatch(parts[-1]):
        return False
    # Standalone comments0.html or .../comments/commentsN.html.
    # Legacy traversal-looking test paths are never extracted from ZIP.
    return (len(parts) == 1 or parts[-2].casefold() == "comments"
            or all(part == ".." for part in parts[:-1]))


def _parse_html(content, name):
    parser = _ArchiveParser()
    parser.feed(_decode(content))
    found = {}
    by_item = {}
    has_vk_rows = False

    # One 'div.item' is one authored comment. URLs quoted IN a comment
    # or attached media are NOT additional user-authored comments.
    for anchor in parser.anchors:
        parsed = parse_comment_link(anchor.attrs.get("href", ""))
        if parsed is None:
            continue
        node = anchor.parent
        while node is not None and node.tag != "root":
            if node.tag == "div" and "item" in node.attrs.get("class", "").split():
                break
            node = node.parent
        if (node is not None and node.tag == "div"
                and "item" in node.attrs.get("class", "").split()):
            by_item.setdefault(node, (anchor, parsed))
        else:
            kind, owner, item, cid, url = parsed
            key = "{}:{}:{}:{}".format(kind, owner, item, cid)
            found.setdefault(key, Comment(key, kind, owner, item, cid, url,
                                          _nearest_description(anchor), "", name))

    for row, (anchor, parsed) in by_item.items():
        kind, owner, item, cid, url = parsed
        key = "{}:{}:{}:{}".format(kind, owner, item, cid)
        mains = [child for child in row.children
                 if isinstance(child, _Node) and child.tag == "div"
                 and "item__main" in child.attrs.get("class", "").split()]
        if mains:
            has_vk_rows = True
        # Real VK export: first item__main contains authored text,
        # second item__main contains exact comment permalink.
        description = mains[0].text()[:1600] if mains else _nearest_description(anchor)
        match = DATE_PATTERN.search(row.text())
        date = match.group(0) if match else ""
        found.setdefault(key, Comment(key, kind, owner, item, cid, url,
                                      description, date, name))

    # Older exports may contain plain-text URLs outside structured rows.
    # NEVER scan RAW text when normal VK item__main rows are present: it
    # incorrectly treats URL citations in comment bodies as authored comments.
    if not has_vk_rows:
        raw = _decode(content)
        for m in re.finditer(r"(?:https?://)?(?:www\.)?(?:vk\.com|vk\.ru)/(?:wall|photo|video)-?\d+_\d+[^\s<>\"']*", raw, re.I):
            parsed = parse_comment_link(m.group(0))
            if not parsed:
                continue
            kind, owner, item, cid, url = parsed
            key = "{}:{}:{}:{}".format(kind, owner, item, cid)
            found.setdefault(key, Comment(key, kind, owner, item, cid, url,
                                          "", "", name))
    return list(found.values())

def parse_zip(blob: bytes):
    if len(blob) > MAX_ZIP_BYTES:
        raise ValueError("ZIP too large (128 MiB maximum)")
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError("Invalid ZIP archive") from exc
    found = {}
    files = 0
    total = 0
    with zf:
        entries = zf.infolist()
        if len(entries) > MAX_ARCHIVE_ENTRIES:
            raise ValueError("Too many ZIP entries (10000 maximum)")
        selected = [entry for entry in entries if not entry.is_dir()
                    and _is_comment_page(entry.filename)]
        if len(selected) > MAX_FILES:
            raise ValueError("Too many authored-comment pages (1500 maximum)")
        def page_sort(entry):
            basename = entry.filename.replace("\\", "/").split("/")[-1]
            number = re.search(r"comments(\d+)", basename, re.I)
            return (int(number.group(1)) if number else 0, entry.filename)
        for entry in sorted(selected, key=page_sort):
            filename = entry.filename.replace("\\", "/").split("/")[-1]
            if entry.file_size > MAX_HTML_BYTES:
                raise ValueError(f"Comment file too large: {filename}")
            total += entry.file_size
            if total > MAX_TOTAL_UNPACKED:
                raise ValueError("Uncompressed archive is too large")
            files += 1
            try:
                b = zf.read(entry)
            except (RuntimeError, zipfile.BadZipFile) as exc:
                raise ValueError(f"Cannot read: {filename}") from exc
            for comment in _parse_html(b, entry.filename):
                # Prefer the longest text available for duplicate references.
                prior = found.get(comment.id)
                if prior is None or len(comment.text) > len(prior.text):
                    found[comment.id] = comment
    return list(found.values()), {"files": files, "found": len(found), "notice": "Counts reflect only recognizable comment permalinks in comments*.html/txt."}
