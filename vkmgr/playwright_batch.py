"""Controlled browser deletion queue for the authenticated user's OWN wall comments.

The legacy 1..5 interactive pilot is preserved. In explicitly authorized
--auto mode, up to 1000 archive links are processed in one Chromium session
without per-comment prompts. Each target still requires exact DOM and author
proof before one irreversible click.

Ambiguous post-click results are journaled as browser_unknown and are NEVER
clicked again. Automatic mode moves on to the next eligible comment after
that checkpoint; errors before an authorized click still stop the batch.
No DOM observation is misrepresented as VK-server verification.
"""
from __future__ import annotations

import argparse
import csv
from contextlib import nullcontext
from pathlib import Path
import re
import sys
import time
import webbrowser
from urllib.parse import parse_qs, unquote, urlsplit

from .browser_assistant import BASE, BLOCKED_STATUSES, _get, _start_server, get_queue
from .auto_lock import automatic_process_lock
from .journal import Journal
from .local_config import PORT, DEFAULT_PORT
from .parser import parse_comment_link
from .playwright_delete import (
    ExactTarget, ExactCommentNotFound, allowed_destination, author_proof, comment_selectors, direct_delete_button,
    locate_exact_comment, local_browser_profile, open_target_with_login_retry,
    safe_page_path,
)

MAX_BATCH = 5
MAX_AUTO_BATCH = 1000
# Deliberate time between automatic actions to avoid hammering VK.
AUTO_COOLDOWN_SECONDS = 3
MAX_FILE_BYTES = 2_000_000
MAX_FILE_LINKS = 5000


def read_url_file(path):
    """Read explicit plaintext VK comment permalinks (one URL per line)."""
    file = Path(path).expanduser()
    if not file.is_file() or file.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("Input file is missing or exceeds 2 MB")
    urls = []
    with file.open(encoding="utf-8-sig", newline="") as stream:
        if file.suffix.lower() == ".csv":
            reader = csv.DictReader(stream)
            if not reader.fieldnames or "url" not in reader.fieldnames:
                raise ValueError("CSV must have a url column (VKCM export)")
            candidates = (row.get("url", "") for row in reader)
        else:
            candidates = stream
        for lineno, raw in enumerate(candidates, 1):
            line = str(raw or "").strip()
            if not line or line.startswith("#"):
                continue
            if len(line) > 2048 or parse_comment_link(line) is None:
                raise ValueError("Invalid VK comment URL at row " + str(lineno))
            urls.append(line)
            if len(urls) > MAX_FILE_LINKS:
                raise ValueError("Input file may contain no more than 5,000 links")
    if not urls:
        raise ValueError("No comment URLs found in the file")
    return urls


def _conversation_thread_id(target):
    """Use VK's explicit thread hint, not DOM guesses, to group archive links."""
    raw = parse_qs(urlsplit(target.url).query).get("thread", [""])[0]
    if re.fullmatch(r"[1-9][0-9]{0,14}", raw) and int(raw) != target.comment_id:
        return int(raw)
    return target.comment_id


def order_conversations(targets):
    """Post -> thread, replies before the top-level author comment.

    The source ZIP is an inventory of *authored* comment IDs, not an inventory
    of every visible message. Never add IDs from the page or delete a whole
    discussion. Keep related authored replies together where possible, and
    schedule a root comment AFTER its archived descendants so its deletion
    cannot collapse the reply UI before we reach them.
    """
    posts = {}
    for target in targets:
        post = (target.owner_id, target.item_id)
        threads = posts.setdefault(post, {})
        root_id = _conversation_thread_id(target)
        threads.setdefault(root_id, []).append(target)
    ordered = []
    for threads in posts.values():
        for root_id, replies in threads.items():
            ordered.extend(t for t in replies if t.comment_id != root_id)
            ordered.extend(t for t in replies if t.comment_id == root_id)
    return ordered


def create_batch(urls, user_id, limit, journal, echo=print, automatic=False,
                 review_not_found=False):
    """Select account-owned archive IDs; group threads only in automatic mode."""
    allowed_limit = MAX_AUTO_BATCH if automatic else MAX_BATCH
    if type(limit) is not int or not 1 <= limit <= allowed_limit:
        raise ValueError("Batch limit must be 1.." + str(allowed_limit) + " comments")
    if not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("Verified numeric VK user ID required")
    if review_not_found and not automatic:
        raise ValueError("Review of missing comments is allowed only with --auto")
    result = []
    seen = set()
    ignored = {"previous": 0, "duplicate": 0, "unsupported": 0}
    for url in urls:
        parsed = parse_comment_link(str(url))
        if parsed is None:
            raise ValueError("Batch contains an invalid VK comment link")
        kind, owner, post, comment, cleaned = parsed
        key = f"{kind}:{owner}:{post}:{comment}"
        if key in seen:
            ignored["duplicate"] += 1
            continue
        seen.add(key)
        if kind != "wall":
            ignored["unsupported"] += 1
            continue
        # No-click misses are the ONLY journal entries eligible for a separate
        # explicit recovery pass. Never retry a pending, clicked or unknown ID.
        status = journal.statuses(user_id, [key]).get(key)
        if review_not_found:
            if status != "browser_not_found":
                continue
        elif status in BLOCKED_STATUSES:
            ignored["previous"] += 1
            continue
        if not allowed_destination(cleaned):
            raise ValueError("Unsafe VK destination or URL scheme")
        result.append(ExactTarget(kind, owner, post, comment, key, cleaned, user_id))
        if not automatic and len(result) >= limit:
            break
    if automatic:
        # Inspect the whole offline archive before choosing the first 1000:
        # siblings can be far apart in chronological export order.
        result = order_conversations(result)
        if result:
            posts = len({(t.owner_id, t.item_id) for t in result[:limit]})
            echo("Grouped by discussions: " + str(posts)
                 + " posts in upcoming batch; own nested replies before "
                   "their roots, when included in the archive.")
        result = result[:limit]
    if review_not_found:
        echo("MISSING-COMMENT REVIEW: only comments not found "
             "BEFORE a click. Previously handled/uncertain attempts are excluded.")
    if ignored["previous"]:
        echo("Previously handled or uncertain: "
             + str(ignored["previous"]) + " (no retries)")
    if ignored["duplicate"]:
        echo("Duplicate links: " + str(ignored["duplicate"]) + " (skipped)")
    if ignored["unsupported"]:
        echo("Photo/video deletion unsupported by browser batch: "
             + str(ignored["unsupported"]) + " (not touched)")
    return result


def same_parent_visible(page, target):
    """Avoid treating a redirect to another page as proof of deletion."""
    if not allowed_destination(page.url):
        return False
    p = urlsplit(page.url)
    wanted = f"wall{target.owner_id}_{target.item_id}"
    # VK canonicalizes to vk.ru/wall-123_456 and may remove query params.
    return bool(re.search(r"(?<![0-9])" + re.escape(wanted) + r"(?![0-9])",
                          unquote(p.path)))


def explicit_deleted_marker(page, target):
    """Recognize a tombstone only WITHIN the exact comment row.

    VK may keep a placeholder and "Восстановить" link after Delete.
    Require both an exact deletion label and a restore ACTION in the same
    unique row. Quoted words in a surviving user's comment alone do not count.
    Never inspect a neighboring comment or page-wide text.
    """
    label = re.compile(r"^Комментарий\s+удал[её]н(?:\.|\s*)(?:\s*Восстановить)?$", re.I)
    restore = re.compile(r"^Восстановить$", re.I)
    if not same_parent_visible(page, target):
        return False
    for css in comment_selectors(target):
        row = page.locator(css)
        if row.count() != 1 or not row.is_visible():
            continue
        # Only direct row-scoped action elements, NEVER plain comment text.
        links = row.get_by_role("link", name=restore)
        buttons = row.get_by_role("button", name=restore)
        if not any(item.count() == 1 and item.is_visible()
                   for item in (links, buttons)):
            continue
        # Exact localized deletion line inside the same row, not whole page.
        lines = [x.strip() for x in row.inner_text(timeout=1200).splitlines()]
        if any(label.fullmatch(line) for line in lines):
            return True
    return False


def observed_ui_result(page, target):
    """Positive UI indication only, not a verified VK backend deletion."""
    if not same_parent_visible(page, target):
        return "unknown"
    for css in comment_selectors(target):
        loc = page.locator(css)
        if loc.count() > 1:
            return "unknown"
        if loc.count() == 1 and loc.is_visible():
            return "unknown"
    # The exact previously-visible comment is absent from the same parent.
    return "browser_ui_removed"



class TargetNotFound(ValueError):
    """Exact comment not rendered; no write attempted and safe to skip."""


class PostClickUnverified(ValueError):
    """VK action was clicked, result not proven; NEVER retry this target."""


_EXPAND_LABEL = re.compile(
    r"^(?:показать|посмотреть|загрузить|открыть|все|ещё|еще)"
    r".{0,65}(?:комментари|комментариев|ответ|ответов)",
    re.IGNORECASE,
)


def _target_in_dom(page, target):
    """Existence proof only. The normal locator still verifies ownership."""
    for selector in comment_selectors(target):
        loc = page.locator(selector)
        if loc.count() > 1:
            raise ValueError("Ambiguous comment ID in VK page DOM")
        if loc.count() == 1 and loc.is_visible():
            return True
    return False


def _expand_visible_replies(page, echo=print, max_clicks=4):
    """Only click visible, clearly labelled SHOW-comments/replies controls.

    No Delete/Like/Send/Reply controls are eligible. At most four read-more
    operations per pass; every actual write still requires owner proof.
    """
    count = 0
    candidates = page.locator("button, a, [role='button']")
    for index in range(min(candidates.count(), 350)):
        if count >= max_clicks:
            break
        button = candidates.nth(index)
        try:
            if not button.is_visible():
                continue
            label = (button.get_attribute("aria-label")
                     or button.inner_text(timeout=400) or "").strip()
            label = " ".join(label.split())[:110]
            if not _EXPAND_LABEL.match(label):
                continue
            button.click(timeout=1500)
            count += 1
            page.wait_for_timeout(550)
        except Exception:
            # A stale SPA button is not evidence of anything.
            continue
    if count:
        echo("Expanded comment/reply sections: " + str(count))
    return count


def _scroll_comments(page):
    """Scroll bounded, visible scroll containers in an SPA, not indefinitely."""
    return page.evaluate("""() => {
      const elements = [document.scrollingElement,
          ...document.querySelectorAll("main, [role=dialog], section, div")];
      let moved = 0;
      const seen = new Set();
      for (const el of elements) {
        if (!el || seen.has(el) || moved >= 3) continue;
        seen.add(el);
        if (el.clientHeight < 220 || el.scrollHeight - el.clientHeight < 120) continue;
        const before = el.scrollTop;
        el.scrollTop = Math.min(el.scrollTop + 700, el.scrollHeight);
        if (el.scrollTop !== before) moved++;
      }
      return moved;
    }""")


def _focus_thread_root(page, target):
    """Scroll to the exact thread hint if VK actually rendered a unique node.

    Read-only navigation aid. It is NEVER used to select a delete control.
    """
    root_id = _conversation_thread_id(target)
    if root_id == target.comment_id:
        return False
    locator = page.locator(f'[id$="_{root_id}"]')
    if locator.count() != 1 or not locator.is_visible():
        return False
    try:
        locator.scroll_into_view_if_needed(timeout=2000)
        return True
    except Exception:
        return False


def reveal_target_comment(page, target, echo=print):
    """Bounded read-only search; focus thread hint and expand nested replies."""
    if not same_parent_visible(page, target):
        raise ValueError("Browser is showing a different VK post")
    echo("Searching for comment " + str(target.comment_id) +
         ": expanding replies and waiting for content (read only)...")
    for step in range(9):
        if _target_in_dom(page, target):
            return True
        if step in (0, 3, 6):
            _focus_thread_root(page, target)
        if step in (0, 2, 3, 5, 6, 8):
            _expand_visible_replies(page, echo=echo, max_clicks=4)
        else:
            _scroll_comments(page)
        page.wait_for_timeout(1000)
    if _target_in_dom(page, target):
        return True
    # Diagnostics deliberately capture only numerical DOM IDs, never
    # comment texts, auth state, or cookies.
    visible_ids = page.evaluate("""() => Array.from(document.querySelectorAll("[id]"))
      .map(el => el.id)
      .filter(id => /^[0-9]+_[0-9]+$/.test(id))
      .slice(0, 12)""")
    echo("Comment " + str(target.comment_id) +
         " was not loaded into the DOM of " + safe_page_path(page.url) +
         ". Other visible comment IDs: " + str(visible_ids) +
         ". No Delete control was clicked.")
    return False



def delete_one_in_open_page(page, target, journal, echo=print, automatic=False,
                            review_not_found=False):
    """One irreversible click; never retry after journal pending is committed."""
    if automatic:
        # No interactive per-target login, even if VK redirects to feed.
        # The built-in STOP instruction prevents a repeated write.
        open_target_with_login_retry(page, target, ask=lambda _: "STOP", echo=echo)
    else:
        open_target_with_login_retry(page, target, echo=echo)
    if not same_parent_visible(page, target):
        raise ValueError("Navigation opened the wrong VK post")
    try:
        css, row = locate_exact_comment(page, target)
    except ExactCommentNotFound:
        if not reveal_target_comment(page, target, echo=echo):
            raise TargetNotFound("Exact comment not found after expanding replies")
        try:
            css, row = locate_exact_comment(page, target)
        except ExactCommentNotFound as exc:
            raise TargetNotFound("Exact comment is still missing after expanding replies") from exc
    mine, evidence = author_proof(row, target.user_id)
    if not mine:
        raise ValueError("Comment author is not verified: " + str(evidence))
    row.hover()
    candidate = direct_delete_button(row)
    if candidate is None:
        raise ValueError("Direct Delete control not found inside the exact comment")
    previous = journal.statuses(target.user_id, [target.key]).get(target.key)
    if previous in BLOCKED_STATUSES and not (
        review_not_found and previous == "browser_not_found"
    ):
        raise ValueError("Already attempted. A second click is prohibited")
    echo("Verified " + target.key + " with author " + str(target.user_id)
         + " (" + css + "). Sending one click...")
    journal.record(target.user_id, target.key, "browser", "browser_pending")
    try:
        # A browser JS confirmation, if any, belongs to the exact click here.
        page.once("dialog", lambda d: d.accept())
        candidate.click(timeout=6000)
        # VK React/SPA may retain a deleted-comment placeholder while
        # asynchronous UI updates finish. Check a few times but never click
        # Delete twice.
        outcome = "unknown"
        clear_checks = 0
        for delay in (900, 1300, 1800, 2400):
            page.wait_for_timeout(delay)
            # A unique same-comment deleted marker + actionable
            # "Восстановить" is stronger than transient SPA disappearance.
            if automatic and explicit_deleted_marker(page, target):
                outcome = "browser_ui_deleted_marker"
                break
            state = observed_ui_result(page, target)
            if state == "browser_ui_removed":
                clear_checks += 1
                if clear_checks >= 2 and not automatic:
                    outcome = "browser_ui_removed"
                    break
            else:
                # A transient VK SPA redraw or virtual scroll is not proof
                # of deletion. Require two consecutive absent checks on
                # the SAME parent post, never a single disappearance.
                clear_checks = 0
    except Exception:
        journal.record(target.user_id, target.key, "browser", "browser_unknown")
        raise
    # Every uncertain outcome must use the BLOCK_REPEAT value browser_unknown.
    # Never persist generic "unknown" which would allow accidental replay.
    journal.record(target.user_id, target.key, "browser",
                   outcome if outcome in ("browser_ui_removed", "browser_ui_deleted_marker")
                   else "browser_unknown")
    if outcome not in ("browser_ui_removed", "browser_ui_deleted_marker"):
        raise PostClickUnverified(
            "Delete clicked, but VK's DOM cannot confirm "
            "removal. Clicking again is prohibited."
        )
    echo("VK rendered a deletion marker inside the exact comment DOM. "
         "This is UI evidence, NOT independent VK server confirmation.")
    return outcome


def confirmation_matches(answer, comment_id):
    """Require ASCII CONFIRM DELETED plus the exact numeric ID."""
    normalized = " ".join(str(answer).strip().upper().split())
    return normalized == "CONFIRM DELETED " + str(comment_id)


def reconcile_user_reported_deletion(url, user_id, journal, ask=None, echo=print):
    """Record eyewitness confirmation for a PREVIOUS uncertain browser click.

    Important: no Playwright browser, no VK API and NO DELETE CLICK. This is
    only allowed for the authenticated account's existing browser_unknown
    journal entry. It never silently converts an unknown to a verified remote
    action, and never operates on a guessed URL or different account.
    """
    if ask is None:
        ask = input
    parsed = parse_comment_link(str(url or ""))
    if parsed is None:
        raise ValueError("Exact URL of a previously attempted comment required")
    kind, owner, item, comment_id, _ = parsed
    if kind != "wall":
        raise ValueError("Previous deletion confirmation supports wall comments only")
    key = f"{kind}:{owner}:{item}:{comment_id}"
    previous = journal.statuses(user_id, [key]).get(key)
    if previous == "browser_user_confirmed":
        echo("Comment " + str(comment_id) + " already has user confirmation. No repeat click.")
        return False
    if previous != "browser_unknown":
        raise ValueError(
            "Cannot change journal: comment " + str(comment_id)
            + " has no previous uncertain browser action. "
              "No deletions were started."
        )
    echo("Journal indicates a previous Delete click for " + key + ".")
    echo("No repeat click. This is your observation, not server verification.")
    answer = ask("If you personally verified that this comment is deleted, type "
                 "CONFIRM DELETED " + str(comment_id) + ". Otherwise press Enter to cancel: ")
    if not confirmation_matches(answer, comment_id):
        raise ValueError("User confirmation cancelled. Journal unchanged.")
    journal.record(user_id, key, "browser", "browser_user_confirmed")
    echo("Comment " + str(comment_id) + " marked as user-confirmed, "
         "and will not be deleted again.")
    return True


def run_batch(targets, journal, ask=input, echo=print, driver=None,
              pause_between=False, automatic=False, cooldown=None,
              review_not_found=False):
    """Single upfront approval, one browser login, sequential fail-closed work."""
    if not targets or len(targets) > (MAX_AUTO_BATCH if automatic else MAX_BATCH):
        raise ValueError("Invalid batch size")
    if automatic and pause_between:
        raise ValueError("Automatic batch cannot pause between comments")
    if review_not_found and not automatic:
        raise ValueError("Missing-comment review requires automatic mode")
    if cooldown is None:
        cooldown = AUTO_COOLDOWN_SECONDS if automatic else 0
    if automatic and (type(cooldown) not in (int, float) or cooldown < 1):
        raise ValueError("Automatic mode requires at least a 1-second delay")
    owner = targets[0].user_id
    if any(t.user_id != owner or t.kind != "wall" for t in targets):
        raise ValueError("Batch includes a different account or unsupported comment type")
    echo("AUTOMATIC BATCH: only the listed comments may be deleted:"
         if automatic else "INTERACTIVE BATCH: only the listed comments may be deleted:")
    for i, t in enumerate(targets, 1):
        echo(f"  {i}. {t.key}   {t.url}")
    phrase = "DELETE BATCH " + str(len(targets))
    if ask("Type exactly " + phrase + " to proceed: ").strip() != phrase:
        echo("Cancelled. No deletions were started.")
        return {"ui_removed": 0, "attempted": 0, "cancelled": True}
    if driver is None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ValueError("Playwright is not installed in this Python environment") from exc
        driver = sync_playwright
    verified = 0
    user_confirmed = 0
    attempted = 0
    unknown = 0
    with driver() as pw:
        context = pw.chromium.launch_persistent_context(
            str(local_browser_profile()), headless=False,
            accept_downloads=False, viewport={"width": 1250, "height": 900}
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            # Launch the actual first comment instead of leaving about:blank
            # while the user signs in.
            echo("Opening the first comment in Chromium...")
            page.goto(targets[0].url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(900)
            echo("Opened page: " + safe_page_path(page.url))
            echo("Chromium uses an isolated VK session; if login "
                 "expires, automatic mode stops without further clicks.")
            ready = ("" if automatic else ask(
                "After signing in, press Enter to continue, "
                "STOP to cancel: ").strip())
            if ready:
                echo("STOPPED before the first deletion: continuation "
                     "was not confirmed. No clicks were performed.")
                return {"ui_removed": 0, "user_confirmed": 0,
                        "attempted": 0, "skipped": 0, "cancelled": True}
            skipped = 0
            for i, target in enumerate(targets, 1):
                # Pause BEFORE opening any later target, including when a
                # preceding target was skipped without clicking Delete.
                if i > 1 and pause_between:
                    command = ask("Pause: Enter = next comment, "
                                  "STOP = finish without further clicks: ").strip()
                    if command:
                        echo("PAUSE_OK: stopped before comment " +
                             str(target.comment_id) + ". Journal saved.")
                        return {"ui_removed": verified,
                                "user_confirmed": user_confirmed,
                                "attempted": attempted, "skipped": skipped,
                                "cancelled": True}
                echo(f"\n[{i}/{len(targets)}] {target.url}")
                previous = journal.statuses(owner, [target.key]).get(target.key)
                if previous in BLOCKED_STATUSES and not (
                    review_not_found and previous == "browser_not_found"
                ):
                    echo("Already handled or uncertain; skipping.")
                    continue
                # Before a click, unexpected owner/DOM/auth errors stop safely.
                # After a click, an ambiguous outcome MUST be journaled and
                # cannot be retried, but automatic mode can move to a NEW ID.
                try:
                    if automatic:
                        # No interactive per-comment prompt. A redirect to login
                        # must stop rather than request or bypass authentication.
                        if review_not_found:
                            delete_one_in_open_page(
                                page, target, journal, echo=echo, automatic=True,
                                review_not_found=True)
                        else:
                            delete_one_in_open_page(
                                page, target, journal, echo=echo, automatic=True)
                    else:
                        delete_one_in_open_page(page, target, journal, echo=echo)
                except TargetNotFound:
                    # No delete button was clicked. Save the pre-click miss
                    # separately so a 4691-item queue can make progress after
                    # restart without repeatedly selecting this same URL.
                    journal.record(owner, target.key, "browser", "browser_not_found")
                    skipped += 1
                    echo("NOT FOUND (before click): VK did not render this comment. "
                         "Marked for separate review; no deletion was performed.")
                    if automatic and i < len(targets):
                        time.sleep(cooldown)
                    continue
                except PostClickUnverified as exc:
                    if automatic:
                        # Only continue after verifying the no-repeat checkpoint.
                        # A missing journal entry here would risk another click
                        # on the same comment during resume.
                        current = journal.statuses(owner, [target.key]).get(target.key)
                        if current != "browser_unknown":
                            raise RuntimeError(
                                "Missing safety checkpoint browser_unknown. "
                                "Batch stopped before processing another comment."
                            ) from exc
                        attempted += 1
                        unknown += 1
                        echo("UNCERTAIN OUTCOME: " + target.key + ". " + str(exc))
                        echo("Saved browser_unknown. No repeat click. "
                             "Continuing to the next eligible comment; "
                             "this one requires separate review.")
                        if i < len(targets):
                            time.sleep(cooldown)
                        continue
                    # The journal has ALREADY recorded browser_unknown; do
                    # NOT click again. If the person actually saw their comment
                    # disappear, allow continuation based only on their
                    # ID-specific, explicit observation.
                    attempted += 1
                    echo(str(exc))
                    echo("In Chromium, check whether your exact comment disappeared: "
                         + str(target.comment_id) + ".")
                    phrase = "CONFIRM DELETED " + str(target.comment_id)
                    answer = ask("If you personally verified deletion, type "
                                 + phrase + "; otherwise press Enter to stop: ")
                    if not confirmation_matches(answer, target.comment_id):
                        raise PostClickUnverified(
                            "Deletion was not user-confirmed. Batch stopped, "
                            "and repeated deletion remains blocked."
                        ) from exc
                    # This is user testimony, not proof from the VK backend.
                    journal.record(owner, target.key, "browser", "browser_user_confirmed")
                    user_confirmed += 1
                    echo("User-confirmed deletion recorded. "
                         "Continuing without another click.")
                else:
                    attempted += 1
                    verified += 1
                if i < len(targets):
                    if automatic:
                        # Controlled pacing; no API request and no repeated
                        # Delete for the same ID even after a failed click.
                        time.sleep(cooldown)
                    else:
                        page.wait_for_timeout(1000)
            echo("Batch finished. UI deletion markers: " + str(verified)
                 + ", user-confirmed: " + str(user_confirmed)
                 + ", uncertain after click: " + str(unknown)
                 + ", not found before click: " + str(skipped)
                 + ". No independent VK server verification.")
            if automatic and unknown:
                echo("WARNING: browser_unknown does NOT mean deleted. "
                     "These items cannot be retried and require review.")
            return {"ui_removed": verified, "user_confirmed": user_confirmed,
                    "attempted": attempted, "unknown": unknown,
                    "skipped": skipped, "cancelled": False}
        finally:
            context.close()


def authenticated_browser_identity(legacy_user, read=_get):
    """Get account identity from verified VK ID, not legacy users.get alone.

    /api/oauth-state reports vkid_user only AFTER completing a valid PKCE
    OAuth callback. A new-style VK ID token may be denied by legacy users.get
    even though authentication has succeeded. Never mix mismatched accounts.
    """
    oauth = read("/api/oauth-state")
    vkid = oauth.get("vkid_user") if isinstance(oauth, dict) else None
    legacy_id = legacy_user.get("id") if isinstance(legacy_user, dict) else None
    vkid_id = vkid.get("id") if isinstance(vkid, dict) else None
    if legacy_id is not None and (type(legacy_id) is not int or legacy_id <= 0):
        raise ValueError("Invalid VK API user ID")
    if vkid_id is not None and (type(vkid_id) is not int or vkid_id <= 0):
        raise ValueError("Invalid VK ID user ID")
    if legacy_id is not None and vkid_id is not None and legacy_id != vkid_id:
        raise ValueError("VK ID and VK API identify different accounts. Deletion blocked.")
    uid = vkid_id if vkid_id is not None else legacy_id
    if uid is None:
        return None, oauth.get("message", "") if isinstance(oauth, dict) else ""
    return {"id": uid, "source": "VK ID" if vkid_id is not None else "VK API"}, ""


def require_authenticated_browser_identity(limit, urls_supplied, ask=input,
                                          open_ui=webbrowser.open, echo=print,
                                          retrieve=get_queue,
                                          identity=authenticated_browser_identity,
                                          automatic=False, poll_interval=2,
                                          max_checks=120):
    """Ask until a real authenticated identity is verified; don't equate Enter to login."""
    rows, legacy = retrieve(limit)
    user, message = identity(legacy)
    if user is not None and (urls_supplied or rows):
        echo("Account verified: " + user["source"] + ", ID " + str(user["id"]))
        return rows, user
    echo("Opening local application: " + BASE)
    open_ui(BASE)
    echo("In the local app, click the VK ID sign-in button and finish logging in.")
    if PORT != DEFAULT_PORT:
        echo("If using an HTTPS redirect, paste the complete callback URL in the local finish-login field. Do not share it.")
    if urls_supplied:
        echo("Links already loaded from the file. No need to reimport.")
    else:
        echo("Add comment links or import a ZIP archive in the local app.")
    if automatic:
        echo("Checking VK ID authentication on the local server. "
             "No Enter needed. Press Ctrl+C to stop.")
    for attempt in range(1, (max_checks if automatic else 6) + 1):
        if automatic:
            time.sleep(poll_interval)
        else:
            response = ask("After VK ID sign-in, press Enter to CHECK "
                           " (STOP = cancel): ").strip().upper()
            if response == "STOP":
                raise ValueError("Cancelled by user. No deletions.")
        rows, legacy = retrieve(limit)
        user, message = identity(legacy)
        if user is not None and (urls_supplied or rows):
            echo("Account verified: " + user["source"] + ", ID " + str(user["id"]))
            return rows, user
        if user is None:
            if not automatic or attempt % 10 == 0:
                echo("Sign-in not yet verified. " + (
                    "Application message: " + str(message)[:180]
                    if message else "Complete VK ID authentication in your browser."
                ))
        else:
            echo("Account identified, but no comments have been imported. "
                 "Add links in the local app.")
    raise ValueError("After " + str(max_checks if automatic else 6)
                     + " VK ID checks without verified login. "
                     "No deletions were started.")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="VKCM: verified browser deletion of your own comments; --auto up to 1000"
    )
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--preview", action="store_true", help="Preview only, by default")
    choice.add_argument("--execute", action="store_true", help="Trial batch requiring one confirmation")
    parser.add_argument("--limit", type=int, default=3, help="Comments per run, 1-5")
    parser.add_argument("--pause-between", action="store_true",
                        help="Allow Enter or STOP before processing the next comment")
    parser.add_argument("--auto", action="store_true",
                        help="Automatic batch up to 1000, no per-comment prompts; journal unknown outcomes")
    parser.add_argument("--review-not-found", action="store_true",
                        help="Separate automatic review ONLY for browser_not_found (pre-click)")
    parser.add_argument("--source", default="",
                        help="Official VK ZIP/TXT/CSV source; only with --auto")
    parser.add_argument("--file", default="", help="TXT with one URL per line or VKCM CSV")
    parser.add_argument(
        "--confirm-deleted", default="",
        help="Record personal confirmation of a browser_unknown deletion, without clicking again",
    )
    args = parser.parse_args(argv)
    server = None
    execution_started = False
    try:
        max_size = MAX_AUTO_BATCH if args.auto else MAX_BATCH
        if not 1 <= args.limit <= max_size:
            raise ValueError("For this mode, --limit must be 1.." + str(max_size))
        if args.auto and not args.execute:
            raise ValueError("--auto requires --execute; preview never deletes")
        if args.auto and args.pause_between:
            raise ValueError("--auto is incompatible with --pause-between")
        if args.review_not_found and not args.auto:
            raise ValueError("--review-not-found requires --auto --execute and --source/--file")
        if args.source and (args.file or not args.auto):
            raise ValueError("--source requires --auto and cannot be combined with --file")
        if args.confirm_deleted and args.auto:
            raise ValueError("--confirm-deleted is not available in automatic mode")
        if args.auto and not (args.source or args.file):
            raise ValueError("--auto requires --source ZIP/TXT/CSV or --file: "
                             "the local list of comments can be incomplete")
        links = []
        if args.source:
            from .queue_plan import load_source
            links = load_source(args.source)
        elif args.file:
            links = read_url_file(args.file)
        server = _start_server()
        rows, user = require_authenticated_browser_identity(
            min(args.limit, MAX_BATCH), urls_supplied=bool(links),
            **({"automatic": True} if args.auto else {})
        )
        if not links:
            # Feed links come from the local VKCM archive (already filtered for journal).
            links = [row["url"] for row in rows]
        journal = Journal()
        if args.confirm_deleted:
            if not args.execute:
                raise ValueError("--confirm-deleted requires --execute; preview changes nothing")
            reconcile_user_reported_deletion(
                args.confirm_deleted, user["id"], journal
            )
        queue = create_batch(links, user["id"], args.limit, journal,
                             automatic=args.auto,
                             review_not_found=args.review_not_found)
        if not queue:
            print("No eligible comments for this batch. "
                  "Add links or select another source file.")
            return 0
        print("Selected " + str(len(queue)) + " comments:")
        for target in queue:
            print("  " + target.url)
        if not args.execute:
            print("PREVIEW_OK: no deletions performed. "
                  "Run again with --execute to start.")
            return 0
        execution_started = True
        # Keep a single irreversible worker even if two terminal windows
        # launch the same archive at once. The journal also blocks repeats.
        with automatic_process_lock() if args.auto else nullcontext():
            result = run_batch(
                queue, journal,
                **({"automatic": True, "review_not_found": True}
                   if args.review_not_found else
                   {"automatic": True} if args.auto else
                   {"pause_between": True} if args.pause_between else {})
            )
        # A finished queue may contain pre-click misses or uncertain
        # post-click outcomes. These are explicitly reported, never counted
        # as confirmed deletions. Exit 0 means the batch *finished*, not that
        # VK independently verified every deletion.
        if result["cancelled"]:
            return 0
        return 0 if result["attempted"] + result["skipped"] >= len(queue) else 2
    except KeyboardInterrupt:
        print("\nStopped by user. Existing attempts will not be retried automatically.")
        return 130
    except Exception as exc:
        print("BATCH STOPPED: " + str(exc), file=sys.stderr)
        if not execution_started:
            print("Deletion not started: verify VK ID "
                  "and select a batch.", file=sys.stderr)
        else:
            print("If Delete was clicked, the outcome may be "
                  "unknown. Do not click the comment again; "
                  "check the journal.", file=sys.stderr)
        return 1
    finally:
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except Exception:
                server.kill()
                server.wait(timeout=3)


if __name__ == "__main__":
    raise SystemExit(main())
