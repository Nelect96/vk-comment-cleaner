"""Offline-first localhost server. Never listens on the public network."""
from __future__ import annotations
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import csv
import io
import json
import secrets
import threading
import time
import webbrowser

from .parser import MAX_ZIP_BYTES, parse_zip
from .manual_links import parse_manual_links
from .archive_discovery import find_archives, read_selected_archive
from .oauth import OAuthError, begin as begin_oauth, parse_return as parse_oauth_return, exchange as exchange_oauth
from .vk_api import action, VkApiError
from .verification import authorized_user, verify_comment
from .journal import Journal, BLOCK_REPEAT
from .local_config import PORT, BASE


class AppState:
    def __init__(self, journal=None):
        self.journal = journal if journal is not None else Journal()
        self.active_single = False
        self.stop_requested = threading.Event()
        self.csrf = secrets.token_urlsafe(30)
        self.comments = {}
        self.statuses = {}
        self.token = ""
        self.user = None
        self.oauth_attempt = None
        self.vkid = None
        self.vkid_token = None
        self.oauth_message = ""
        self.checks = {}
        # The VK ID token can identify a user but refuse classic comment methods.
        # Track 1051 per kind and stop repeating requests with that session.
        self.api_unavailable = set()
        self.import_meta = {"files": 0, "found": 0}
        self.lock = threading.RLock()
        self.job = {"running": False, "done": 0, "total": 0, "failed": 0, "message": ""}


STATE = AppState()
HTML = (Path(__file__).parent / "index.html").read_text(encoding="utf-8")


def _filter_comments(params):
    kind = params.get("kind", ["all"])[0]
    q = params.get("q", [""])[0].strip().casefold()[:200]
    show = params.get("show", ["all"])[0]
    items = list(STATE.comments.values())
    if kind != "all":
        items = [item for item in items if item.kind == kind]
    if show == "active":
        items = [item for item in items if STATE.statuses.get(item.id) not in BLOCK_REPEAT]
    if show == "deleted":
        items = [item for item in items if STATE.statuses.get(item.id) in BLOCK_REPEAT]
    if q:
        items = [c for c in items if q in (c.text + " " + c.url + " " + c.date).casefold()]
    return items


class Handler(BaseHTTPRequestHandler):
    server_version = "VKCM/0.4.0"

    def log_message(self, format, *args):
        # Tokens and comment text are deliberately never logged.
        pass

    def _valid_host(self):
        return self.headers.get("Host", "") in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")

    def _send(self, code, body, mimetype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", mimetype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload):
        return self._send(200, payload)

    def _bad(self, message, code=400):
        return self._send(code, {"error": message})

    def _html_home(self):
        warning = ""
        return (HTML.replace("__CSRF_PLACEHOLDER__", STATE.csrf)
                    .replace("__LOCAL_PORT_WARNING__", warning))

    def do_GET(self):
        if not self._valid_host():
            return self._bad("Invalid host", 403)
        p = urlsplit(self.path)
        if p.path == "/oauth/callback":
            url = "http://" + self.headers.get("Host", "") + self.path
            try:
                info = complete_oauth(url)
                with STATE.lock:
                    STATE.oauth_message = "VK ID подтверждён. " + (
                        "Аккаунт определён по users.get. Права методов комментариев пока не проверены." if info["legacy_api"] else
                        "Старый VK API может быть недоступен; браузерный режим требует отдельного подтверждения."
                    )
            except OAuthError as exc:
                with STATE.lock:
                    STATE.oauth_message = str(exc)
            except Exception:
                with STATE.lock:
                    STATE.oauth_message = "Проверка VK ID завершилась ошибкой."
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if p.path == "/api/oauth-state":
            with STATE.lock:
                return self._json({
                    "configured": bool(STATE.oauth_attempt),
                    "vkid_user": STATE.vkid,
                    "legacy_api": bool(STATE.user and STATE.token),
                    "message": STATE.oauth_message,
                })
        if p.path == "/":
            return self._send(200, self._html_home(), "text/html; charset=utf-8")
        if p.path == "/api/comments":
            params = parse_qs(p.query)
            with STATE.lock:
                records = _filter_comments(params)
                try:
                    page = max(1, min(100000, int(params.get("page", ["1"])[0])))
                except ValueError:
                    page = 1
                start = (page - 1) * 50
                return self._json({"total": len(records), "page": page, "items": [dict(c.to_dict(), status=STATE.statuses.get(c.id, "archived")) for c in records[start:start + 50]], "counts": {
                    "wall": sum(c.kind == "wall" for c in STATE.comments.values()),
                    "photo": sum(c.kind == "photo" for c in STATE.comments.values()),
                    "video": sum(c.kind == "video" for c in STATE.comments.values()),
                }, "archive": STATE.import_meta, "token_set": bool(STATE.token),
                    "auth_user": STATE.user, "unavailable_kinds": sorted(STATE.api_unavailable), "checks": {c.id: STATE.checks.get(c.id, {}) for c in records[start:start + 50]}})
        if p.path == "/api/report":
            with STATE.lock:
                uid = STATE.user["id"] if STATE.user else None
                return self._json({"account_id": uid, "saved": STATE.journal.summary(uid) if uid else {}})
        if p.path == "/api/job":
            with STATE.lock:
                return self._json(dict(STATE.job))
        if p.path == "/api/export":
            with STATE.lock:
                records = _filter_comments(parse_qs(p.query))
                csv_file = io.StringIO()
                writer = csv.writer(csv_file)
                writer.writerow(["type", "url", "date", "text_preview", "status", "source"])
                for c in records:
                    def safe(v):
                        v = str(v)
                        return "'" + v if v.startswith(("=", "+", "-", "@")) else v
                    writer.writerow([safe(c.kind), safe(c.url), safe(c.date), safe(c.text), STATE.statuses.get(c.id, "archived"), safe(c.source)])
            data = b"\xef\xbb\xbf" + csv_file.getvalue().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="vk-comments.csv"')
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return self.wfile.write(data)
        return self._bad("Not found", 404)

    def _body(self, max_size):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Invalid length")
        if not 0 < length <= max_size:
            raise ValueError("Invalid upload size")
        return self.rfile.read(length)

    def do_POST(self):
        if not self._valid_host() or self.headers.get("X-VKCM-CSRF", "") != STATE.csrf:
            return self._bad("Request refused", 403)
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"):
            return self._bad("Invalid origin", 403)
        p = urlsplit(self.path).path
        try:
            if p == "/api/import":
                blob = self._body(MAX_ZIP_BYTES)
                records, meta = parse_zip(blob)
                with STATE.lock:
                    if STATE.job["running"] or STATE.active_single:
                        return self._bad("Finish current operation before importing", 409)
                    STATE.comments = {c.id: c for c in records}
                    STATE.statuses = STATE.journal.statuses(STATE.user["id"], STATE.comments) if STATE.user else {}
                    STATE.checks = {}
                    STATE.import_meta = meta
                return self._json(meta)
            data = json.loads(self._body(20_000))
            if p == "/api/find-archives":
                # Explicit action by the local user: inspect ZIP directory entries
                # only. Do not send file contents, tokens or browser cookies.
                found = find_archives()
                return self._json({"archives": found, "folder": "Downloads"})
            if p == "/api/import-downloaded":
                name = data.get("name")
                with STATE.lock:
                    if STATE.job["running"] or STATE.active_single:
                        return self._bad("Finish current operation before importing", 409)
                records, meta = read_selected_archive(name)
                with STATE.lock:
                    if STATE.job["running"] or STATE.active_single:
                        return self._bad("Finish current operation before importing", 409)
                    STATE.comments = {c.id: c for c in records}
                    STATE.statuses = STATE.journal.statuses(STATE.user["id"], STATE.comments) if STATE.user else {}
                    STATE.checks = {}
                    STATE.import_meta = meta
                return self._json(meta)
            if p == "/api/add-links":
                new_comments, summary = parse_manual_links(data.get("links"))
                with STATE.lock:
                    if STATE.job["running"] or STATE.active_single:
                        return self._bad("Finish current operation before adding links", 409)
                    previously = len(STATE.comments)
                    for c in new_comments:
                        # Import is idempotent and never overwrites a better archive description.
                        STATE.comments.setdefault(c.id, c)
                    STATE.import_meta["found"] = len(STATE.comments)
                    if STATE.user:
                        STATE.statuses.update(STATE.journal.statuses(
                            STATE.user["id"], {c.id: c for c in new_comments}
                        ))
                    summary["added"] = len(STATE.comments) - previously
                    summary["total"] = len(STATE.comments)
                return self._json(summary)
            if p == "/api/oauth/start":
                with STATE.lock:
                    if STATE.job["running"] or STATE.active_single:
                        return self._bad("Cannot change account during VK operation", 409)
                attempt = begin_oauth(data.get("app_id"), data.get("redirect_uri"))
                with STATE.lock:
                    STATE.oauth_attempt = attempt
                    STATE.vkid = None
                    STATE.vkid_token = None
                    STATE.oauth_message = ""
                    STATE.api_unavailable.clear()
                    STATE.token = ""
                    STATE.user = None
                    STATE.statuses = {}
                    STATE.checks = {}
                return self._json({
                    "url": attempt.url,
                    "note": "Open this URL in Safari. Enter your password only on the official VK ID site. If your approved Redirect URL is not localhost, paste the complete redirect URL to finish.",
                })
            if p == "/api/oauth/finish":
                try:
                    result = complete_oauth(data.get("redirect_url"))
                except OAuthError as exc:
                    return self._bad(str(exc), 422)
                return self._json(result)
            if p == "/api/token":
                token = str(data.get("token", "")).strip()
                if len(token) > 4096 or (token and any(s.isspace() for s in token)):
                    raise ValueError("Invalid token format")
                with STATE.lock:
                    if STATE.job["running"] or STATE.active_single:
                        return self._bad("Cannot change account during an operation", 409)
                # A token must be tested on a read-only method before it can be used.
                # Failed reauthentication clears any previously connected account.
                with STATE.lock:
                    STATE.token = ""
                    STATE.user = None
                    STATE.oauth_attempt = None
                    STATE.vkid = None
                    STATE.vkid_token = None
                    STATE.oauth_message = ""
                    STATE.api_unavailable.clear()
                    STATE.statuses = {}
                    STATE.checks = {}
                if not token:
                    return self._json({"token_set": False, "user": None})
                try:
                    user = authorized_user(token)
                except VkApiError as exc:
                    return self._bad(str(exc), 422)
                with STATE.lock:
                    STATE.token = token
                    STATE.user = user
                    STATE.statuses = STATE.journal.statuses(user["id"], STATE.comments)
                return self._json({"token_set": True, "user": user, "note": "Account verified via users.get; destructive VK methods may still be restricted"})
            if p == "/api/preflight":
                ids = data.get("ids", [])
                if (not isinstance(ids, list) or not 1 <= len(ids) <= 10
                        or any(not isinstance(i, str) for i in ids) or len(ids) != len(set(ids))):
                    raise ValueError("Select 1–10 different comment IDs")
                with STATE.lock:
                    if STATE.job["running"]:
                        return self._bad("Batch deletion in progress", 409)
                    token, user = STATE.token, STATE.user
                    selected = [STATE.comments.get(i) for i in ids]
                if not token or not user:
                    return self._bad("Connect and verify a VK user token first", 403)
                if any(c is None for c in selected):
                    return self._bad("Comment not found in archive", 404)
                results = {}
                for comment in selected:
                    with STATE.lock:
                        unavailable = comment.kind in STATE.api_unavailable
                    if unavailable:
                        check = {
                            "status": "unsupported",
                            "reason": "VK API 1051: этот тип токена не имеет доступа к проверке комментариев. Откройте комментарий на сайте VK.",
                            "from_id": None,
                        }
                    else:
                        try:
                            check = verify_comment(token, comment, user["id"]).to_dict()
                        except VkApiError as exc:
                            if str(exc.code) == "1051":
                                with STATE.lock:
                                    STATE.api_unavailable.add(comment.kind)
                                check = {
                                    "status": "unsupported",
                                    "reason": "VK API 1051: метод недоступен для текущего типа токена. Повторная авторизация с тем же приложением не даст нужных прав.",
                                    "from_id": None,
                                }
                            else:
                                check = {"status": "error", "reason": str(exc), "from_id": None}
                        except Exception:
                            check = {"status": "error", "reason": "VK connection failed", "from_id": None}
                    results[comment.id] = check
                    with STATE.lock:
                        if STATE.token == token and STATE.user == user:
                            STATE.checks[comment.id] = check
                return self._json({"results": results})
            if p == "/api/manual-mark":
                ident = data.get("id")
                if not isinstance(ident, str):
                    raise ValueError("Неверный ID комментария")
                if data.get("confirmation") != "Я УДАЛИЛ В VK":
                    return self._bad("Подтвердите, что вы самостоятельно удалили комментарий на сайте VK", 400)
                with STATE.lock:
                    if STATE.job["running"] or STATE.active_single:
                        return self._bad("Дождитесь завершения предыдущей операции", 409)
                    comment = STATE.comments.get(ident)
                    user = STATE.user
                    if comment is None:
                        return self._bad("Комментарий не найден", 404)
                    if user is None:
                        return self._bad("Сначала войдите в VK ID", 403)
                    if STATE.statuses.get(ident) in BLOCK_REPEAT:
                        return self._bad("У комментария уже имеется невыясненный или подтверждённый API-ответ", 409)
                    if STATE.statuses.get(ident) == "user_marked_deleted":
                        return self._bad("Комментарий уже отмечен пользователем как удалённый", 409)
                    # Only the user's own report is saved, not an assertion
                    # that VK API or our program deleted the remote comment.
                    STATE.journal.record(user["id"], ident, "manual", "user_marked_deleted")
                    STATE.statuses[ident] = "user_marked_deleted"
                return self._json({"ok": True, "status": "user_marked_deleted",
                                   "note": "Отмечено по вашему подтверждению, удаление VK API не проверено."})
            if p == "/api/cancel":
                with STATE.lock:
                    if not STATE.job["running"]:
                        return self._json({"requested": False, "note": "No active batch"})
                    STATE.stop_requested.set()
                    STATE.job["message"] = "Stop requested; finishing current request"
                return self._json({"requested": True})
            if p == "/api/action":
                ident = str(data.get("id", ""))
                operation = str(data.get("operation", ""))
                message = str(data.get("message", ""))
                if len(message) > 10000:
                    raise ValueError("Text too long")
                with STATE.lock:
                    if STATE.job["running"] or STATE.active_single:
                        return self._bad("Another operation is running", 409)
                    comment = STATE.comments.get(ident)
                    token, user = STATE.token, STATE.user
                    if STATE.statuses.get(ident) in BLOCK_REPEAT:
                        return self._bad("This comment has an unresolved or accepted previous operation; review history", 409)
                if not comment:
                    return self._bad("Comment not found", 404)
                with STATE.lock:
                    if comment.kind in STATE.api_unavailable:
                        return self._bad("VK API 1051: этот тип токена не разрешает проверку и удаление комментариев этой категории. Откройте запись в VK.", 409)
                if not token or not user:
                    return self._bad("Connect and verify a VK user token first")
                if operation not in ("delete", "edit"):
                    return self._bad("Unsupported operation")
                if data.get("confirmed") is not True:
                    return self._bad("Explicit confirmation required")
                with STATE.lock:
                    if STATE.job["running"] or STATE.active_single:
                        return self._bad("Another operation is running", 409)
                    STATE.active_single = True
                try:
                    try:
                        check = verify_comment(token, comment, user["id"])
                    except VkApiError as exc:
                        if str(exc.code) == "1051":
                            with STATE.lock:
                                STATE.api_unavailable.add(comment.kind)
                                STATE.checks[ident] = {
                                    "status": "unsupported",
                                    "reason": "VK API 1051: метод недоступен для текущего типа токена",
                                    "from_id": None,
                                }
                        raise
                    with STATE.lock:
                        STATE.checks[ident] = check.to_dict()
                    if check.status != "verified":
                        return self._bad("Deletion/edit blocked: " + check.reason, 409)
                    STATE.journal.record(user["id"], ident, operation, "pending")
                    with STATE.lock:
                        STATE.statuses[ident] = "pending"
                    try:
                        action(token, comment, operation, message)
                    except VkApiError as exc:
                        if str(exc.code) == "1051":
                            with STATE.lock:
                                STATE.api_unavailable.add(comment.kind)
                        STATE.journal.record(user["id"], ident, operation, "api_error")
                        with STATE.lock:
                            STATE.statuses[ident] = "api_error"
                        raise
                    except Exception:
                        STATE.journal.record(user["id"], ident, operation, "outcome_unknown")
                        with STATE.lock:
                            STATE.statuses[ident] = "outcome_unknown"
                        raise
                    outcome = "api_accepted" if operation == "delete" else "edited_accepted"
                    STATE.journal.record(user["id"], ident, operation, outcome)
                    with STATE.lock:
                        STATE.statuses[ident] = outcome
                        if operation == "edit":
                            STATE.comments[ident] = replace(comment, text=message)
                    return self._json({"ok": True, "status": outcome, "note": "VK API accepted the request; remote disappearance not yet verified"})
                except VkApiError as exc:
                    return self._bad(str(exc), 422)
                finally:
                    with STATE.lock:
                        STATE.active_single = False
            if p == "/api/bulk":
                ids = data.get("ids", [])
                if not isinstance(ids, list) or not (1 <= len(ids) <= 50) or len(ids) != len(set(ids)):
                    raise ValueError("Select 1–50 different comments")
                if data.get("confirmation") != "УДАЛИТЬ":
                    raise ValueError("Confirmation does not match")
                with STATE.lock:
                    if STATE.job["running"] or STATE.active_single:
                        return self._bad("Another operation is running", 409)
                    if not STATE.token or not STATE.user:
                        return self._bad("Connect and verify a VK user token first")
                    selected = [STATE.comments[c] for c in ids if c in STATE.comments and STATE.statuses.get(c) not in BLOCK_REPEAT]
                    if len(selected) != len(ids):
                        raise ValueError("Some selected comments are missing or deleted")
                    if any(c.kind in STATE.api_unavailable for c in selected):
                        return self._bad("VK API 1051: для выбранной категории доступ запрещён; массовое удаление через этот токен не запустится.", 409)
                    token = STATE.token
                    user_id = STATE.user["id"]
                    STATE.stop_requested.clear()
                    STATE.job = {"running": True, "done": 0, "total": len(selected), "failed": 0, "message": "Running"}
                threading.Thread(target=run_bulk, args=(selected, token, user_id), daemon=True).start()
                return self._json({"started": True, "total": len(selected)})
        except (ValueError, json.JSONDecodeError) as exc:
            return self._bad(str(exc))
        except Exception as exc:
            # A generic error avoids leaking sensitive paths or credentials.
            return self._bad("Operation failed: " + type(exc).__name__, 500)
        return self._bad("Not found", 404)


def complete_oauth(redirect_url):
    """Consume a PKCE attempt exactly once and try optional legacy API access.

    A valid VK ID login is deliberately distinct from permissions to delete
    legacy wall/photo/video comments. No assumed permissions or guessed scopes.
    """
    with STATE.lock:
        if STATE.job["running"] or STATE.active_single:
            raise OAuthError("Завершите текущую операцию перед входом")
        attempt = STATE.oauth_attempt
        if attempt is None:
            raise OAuthError("Сначала начните вход через VK ID")
        code, device = parse_oauth_return(redirect_url, attempt)
        # Disallow replay even if a later network call fails.
        STATE.oauth_attempt = None
    try:
        granted = exchange_oauth(attempt, code, device)
    except OAuthError:
        raise
    except Exception as exc:
        raise OAuthError("Не удалось обменять код VK ID на токен") from exc
    public = {"id": granted["user_id"], "scope": granted.get("scope", "")}
    legacy = None
    try:
        candidate = authorized_user(granted["access_token"])
        if candidate["id"] == granted["user_id"]:
            legacy = candidate
    except (VkApiError, ValueError, OSError):
        pass
    with STATE.lock:
        STATE.vkid = public
        STATE.api_unavailable.clear()
        STATE.vkid_token = granted["access_token"]
        STATE.token = granted["access_token"] if legacy else ""
        STATE.user = legacy
        STATE.statuses = STATE.journal.statuses(legacy["id"], STATE.comments) if legacy else {}
        STATE.checks = {}
        STATE.oauth_message = (
            "Вход выполнен, users.get доступен. Доступность методов комментариев не подтверждена."
            if legacy else
            "VK ID авторизован, но старые методы VK API недоступны или не проверены."
        )
    return {"vkid_user": public, "legacy_api": bool(legacy), "message": STATE.oauth_message}


def run_bulk(selected, token, user_id):
    """Safe sequential job. Never reissues writes after ambiguous connection failure."""
    for c in selected:
        if STATE.stop_requested.is_set():
            break
        try:
            with STATE.lock:
                if c.kind in STATE.api_unavailable:
                    STATE.job["failed"] += 1
                    STATE.job["message"] = "Stopped: VK API 1051 blocks this comment category"
                    break
            check = verify_comment(token, c, user_id)
            with STATE.lock:
                STATE.checks[c.id] = check.to_dict()
            if check.status != "verified":
                with STATE.lock:
                    STATE.job["failed"] += 1
                    STATE.statuses[c.id] = "skipped_unverified"
                continue
            if STATE.stop_requested.is_set():
                break
            # Save intent BEFORE the first network write. If power is lost or
            # the response is lost, it is unsafe to repeat that comment blindly.
            STATE.journal.record(user_id, c.id, "delete", "pending")
            with STATE.lock:
                STATE.statuses[c.id] = "pending"
            try:
                action(token, c, "delete")
            except VkApiError:
                STATE.journal.record(user_id, c.id, "delete", "api_error")
                with STATE.lock:
                    STATE.statuses[c.id] = "api_error"
                raise
            except Exception:
                STATE.journal.record(user_id, c.id, "delete", "outcome_unknown")
                with STATE.lock:
                    STATE.statuses[c.id] = "outcome_unknown"
                raise
            STATE.journal.record(user_id, c.id, "delete", "api_accepted")
            with STATE.lock:
                STATE.statuses[c.id] = "api_accepted"
        except VkApiError as exc:
            with STATE.lock:
                if str(exc.code) == "1051":
                    STATE.api_unavailable.add(c.kind)
                    STATE.checks[c.id] = {
                        "status": "unsupported",
                        "reason": "VK API 1051: метод недоступен для этого типа токена",
                        "from_id": None,
                    }
                STATE.job["failed"] += 1
                # Authentication, flood control, permission denial, or captcha.
                if str(exc.code) in ("5", "6", "7", "9", "14", "17", "20", "28", "1051"):
                    STATE.job["message"] = f"Stopped on VK API error {exc.code}"
                    break
        except Exception:
            with STATE.lock:
                STATE.job["failed"] += 1
                STATE.job["message"] = "Stopped: unknown outcome or journal/connection error"
            break
        finally:
            with STATE.lock:
                STATE.job["done"] += 1
        # Event.wait permits an immediate stop without spinning or aggressive requests.
        if STATE.stop_requested.wait(0.8):
            break
    with STATE.lock:
        STATE.job["running"] = False
        if STATE.stop_requested.is_set():
            STATE.job["message"] = "Stopped by user; remaining comments were not attempted"
        elif STATE.job["message"] == "Running":
            STATE.job["message"] = "Finished; API accepted is not independently verified deleted"


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("VK Comment Manager 0.4.0 — " + BASE)
    print("Local only. Ctrl+C to stop. Tokens are cleared on exit; local SQLite history is retained.")
    webbrowser.open(BASE)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
