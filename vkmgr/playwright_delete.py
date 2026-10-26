"""Experimental Playwright pilot for deleting ONE owned VK comment via visible UI.

This is NOT VK API access and never bypasses API 1051. It uses a separate,
locally persisted Chromium browser profile. By design this pilot:
* requires an exact comment permalink and a matching authenticated VK ID;
* fails closed unless a unique comment DOM node + author ID + menu are proven;
* requires explicit typed consent for the single target;
* never accesses cookies, localStorage, password fields or private VK APIs;
* never reports remote deletion as verified merely because a click succeeded.

DOM selectors below are provisional: VK changes its page layout. First run
--inspect on one disposable comment you own; --execute-one is opt-in.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import sys
import time
from urllib.parse import quote, urlsplit, urlunsplit

from .parser import parse_comment_link
from .browser_assistant import _start_server, get_queue, _get, BLOCKED_STATUSES, BASE
from .journal import Journal

# Using playwright==1.59.0 supports macOS Python 3.9. Latest requires >=3.10.
# Imported inside main so the project's offline unit tests need no playwright.

@dataclass(frozen=True)
class ExactTarget:
    kind: str
    owner_id: int
    item_id: int
    comment_id: int
    key: str
    url: str
    user_id: int


def from_record(record, user_id):
    if not isinstance(record, dict) or not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("Verified VK ID account and comment record required")
    parsed = parse_comment_link(str(record.get("url") or ""))
    if parsed is None:
        raise ValueError("Exact VK comment URL not recognized")
    kind, owner, item, cid, url = parsed
    key = f"{kind}:{owner}:{item}:{cid}"
    if key != record.get("id"):
        raise ValueError("Comment ID does not match its URL")
    return ExactTarget(kind, owner, item, cid, key, url, user_id)


def allowed_destination(url):
    """Never navigate browser automation to an arbitrary destination."""
    p = urlsplit(url)
    return p.scheme == "https" and (p.hostname or "").lower() in {
        "vk.com", "www.vk.com", "m.vk.com", "vk.ru", "www.vk.ru", "m.vk.ru"
    } and p.username is None and p.password is None


def comment_selectors(target):
    """Exact IDs only, never loose .comment, text contains(), or substring search."""
    cid, owner = target.comment_id, target.owner_id
    return [
        # Observed in the user's real VK DOM: id="112233445_1564".
        # Account ID here is a search hint, NOT independent proof of authorship.
        f'[id="{target.user_id}_{cid}"]',
        f'[data-comment-id="{cid}"]',
        f'[data-reply-id="{cid}"]',
        f'[data-testid="comment-{cid}"]',
        f'[id="reply{cid}"]',
        f'[id="reply_{cid}"]',
        f'[id="comment{cid}"]',
        f'[id="comment_{cid}"]',
        f'[id="wall_reply{owner}_{cid}"]',
        f'[id="wall_reply-{abs(owner)}_{cid}"]' if owner < 0 else
            f'[id="wall_reply{owner}_{cid}"]',
        f'[id="post_reply{owner}_{cid}"]',
        f'[id="reply{owner}_{cid}"]',
        f'[data-id="{cid}"][class*="reply"]',
    ]


def numeric_profile_id(href):
    """Only an explicit numeric VK profile link counts as author evidence."""
    if not isinstance(href, str):
        return None
    p = urlsplit(href)
    if p.scheme not in ("", "https") or (p.hostname or "").lower() not in {
        "", "vk.com", "www.vk.com", "vk.ru", "www.vk.ru",
    }:
        return None
    match = re.fullmatch(r"/id([1-9][0-9]{0,14})/?", p.path)
    return int(match.group(1)) if match else None


def author_proof(row, user_id):
    """Read only author-marked nodes, excluding mentions in comment body."""
    evidence = []
    for name in ("data-from-id", "data-author-id"):
        value = row.get_attribute(name)
        if value and value.lstrip("-").isdigit():
            evidence.append(int(value))
    try:
        for node in row.locator('[data-from-id],[data-author-id]').all()[:20]:
            for name in ("data-from-id", "data-author-id"):
                value = node.get_attribute(name)
                if value and value.lstrip("-").isdigit():
                    evidence.append(int(value))
        for link in row.locator(
            'a.reply_author[href],a.reply__author[href],'
            'a.comment_author[href],a[class*="author"][href],'
            'a[class*="Author"][href],'
            '[data-testid="comment-author"] a[href]'
        ).all()[:20]:
            parsed = numeric_profile_id(link.get_attribute("href"))
            if parsed is not None:
                evidence.append(parsed)
    except Exception:
        return False, []
    # Live VK (Oct 2026) renders an exact reply container id as
    # `author_id_comment_id`, e.g. 112233445_1564. This by itself is NOT
    # enough to prove ownership: additionally require the first two links in
    # that exact container to be profile anchors of the same author. Those
    # correspond to avatar + author name in the observed VK DOM. This
    # excludes arbitrary profile mentions later in the comment body.
    row_id = row.get_attribute("id") or ""
    observed = re.fullmatch(r"([1-9][0-9]{0,14})_([1-9][0-9]{0,14})", row_id)
    if observed:
        row_author = int(observed.group(1))
        evidence.append(row_author)
        try:
            leading_links = row.locator("a[href]").all()[:2]
            profiles = [numeric_profile_id(a.get_attribute("href"))
                        for a in leading_links]
        except Exception:
            return False, sorted(set(evidence))
        if len(profiles) != 2 or profiles != [row_author, row_author]:
            return False, sorted(set(evidence))
    unique = set(evidence)
    return unique == {user_id}, sorted(unique)


class ExactCommentNotFound(ValueError):
    """Exact target comment is absent from the current VK DOM, before any click."""


def locate_exact_comment(page, target):
    """Fail closed if different exact selectors point to different DOM nodes."""
    findings = []
    for css in comment_selectors(target):
        locator = page.locator(css)
        number = locator.count()
        if number > 1:
            raise ValueError(f"Ambiguous comment ID: {css}, matches {number}")
        if number == 1 and locator.is_visible():
            findings.append((css, locator))
    if not findings:
        raise ExactCommentNotFound(
            "Could not find the exact comment DOM element. VK may not have expanded replies. "
            "Scroll to your comment and try again."
        )
    # The first unique candidate is a DOM node representing this target ID.
    # Other selectors may point to children of that same comment; in that case
    # use the first candidate only, never select a generic neighboring reply.
    css, locator = findings[0]
    return css, locator


def direct_delete_button(row):
    """VK's observed direct delete control, scoped to the verified reply.

    Never search for this test ID in the full document. A duplicate or
    invisible control refuses deletion instead of clicking a neighbor.
    """
    loc = row.locator('button[data-testid="comment_action_delete"]')
    if loc.count() > 1:
        raise ValueError("Multiple Delete controls found inside one comment. Stopping.")
    if loc.count() == 1 and loc.is_visible():
        return loc
    return None


def menu_buttons(row):
    """Read-only collection of exact, comment-scoped menu candidates."""
    queries = [
        'button[aria-label="Ещё"]',
        'button[aria-label="Еще"]',
        'button[aria-label="Действия"]',
        'button[title="Ещё"]',
        'button[title="Еще"]',
        'button[title="Действия"]',
        'button[data-testid="comment-more"]',
        'button[data-testid="comment-actions"]',
        '[role="button"][aria-label="Ещё"]',
        '[role="button"][aria-label="Действия"]',
    ]
    out = []
    for query in queries:
        loc = row.locator(query)
        if loc.count() == 1 and loc.is_visible():
            out.append((query, loc))
    # Multiple matching aliases for the same button are okay only after
    # comparing element identities; no blind fallback to a page-wide button.
    return out


def delete_options_in_open_menu(page, trigger):
    """Require that the Delete control belongs to the menu just opened.

    Only an aria-controls-linked popup or a unique visible role=menu is
    acceptable. A global text match is unsafe and intentionally forbidden.
    """
    controlled_id = trigger.get_attribute("aria-controls")
    if controlled_id:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", controlled_id):
            raise ValueError("Unsafe menu ID")
        root = page.locator(f'[id="{controlled_id}"]')
        if root.count() != 1 or not root.is_visible():
            raise ValueError("Action menu did not open")
    else:
        menus = page.get_by_role("menu")
        visible = [menus.nth(i) for i in range(min(menus.count(), 10))
                   if menus.nth(i).is_visible()]
        if len(visible) != 1:
            raise ValueError("Could not associate Delete menu with the exact selected comment")
        root = visible[0]
    return unique_visible([
        root.get_by_role("menuitem", name=re.compile(r"^Удалить(?: комментарий)?$", re.I)),
        root.get_by_role("button", name=re.compile(r"^Удалить(?: комментарий)?$", re.I)),
    ])


def dom_clues(page, comment_id):
    """Print nonsecret DOM attributes only to help tune selectors for VK UI."""
    return page.evaluate("""(cid) => {
      const re = new RegExp("(^|[^0-9])" + cid + "([^0-9]|$)");
      const found = [];
      for (const el of document.querySelectorAll("[id], [data-comment-id], [data-reply-id], [data-testid]")) {
        const attrs = {};
        for (const key of ["id", "data-comment-id", "data-reply-id", "data-testid"]) {
          const value = el.getAttribute(key);
          if (value && value.length <= 120 && re.test(value)) attrs[key] = value;
        }
        if (Object.keys(attrs).length && found.length < 16) {
          found.push({tag:el.tagName.toLowerCase(),attrs});
        }
      }
      return found;
    }""", str(comment_id))


def unique_visible(locators):
    seen = []
    for loc in locators:
        for i in range(min(loc.count(), 10)):
            item = loc.nth(i)
            if item.is_visible() and not any(
                    item.evaluate("(n, other) => n === other", other.element_handle())
                    for other in seen):
                seen.append(item)
    return seen


def sanitized_row_clues(row):
    """Only CSS class, role, href pathname and button metadata. No user text."""
    return row.evaluate("""node => {
      const links = Array.from(node.querySelectorAll("a[href]")).slice(0, 16).map(a => {
        let path = "";
        try { path = new URL(a.href).pathname.slice(0, 120); } catch(e) {}
        return {className:String(a.className||"").slice(0, 100),path};
      });
      const buttons = Array.from(node.querySelectorAll("button, [role=button]")).slice(0, 16)
        .map(b => ({
          tag:b.tagName.toLowerCase(),
          className:String(b.className||"").slice(0,100),
          role:(b.getAttribute("role")||"").slice(0,40),
          ariaLabel:(b.getAttribute("aria-label")||"").slice(0,100),
          title:(b.getAttribute("title")||"").slice(0,100),
          testId:(b.getAttribute("data-testid")||"").slice(0,100),
        }));
      return {tag:node.tagName.toLowerCase(), links, buttons};
    }""")


def local_browser_profile():
    root = Path.home() / "Library" / "Application Support" / "vk-comment-manager" / "chromium-vkcm"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink():
        raise ValueError("Symlinked browser profile is not allowed")
    root.chmod(0o700)
    return root


def find_imported_by_url(url):
    """Retrieve exact previously imported comment even if not on the first page.

    Searching by the *parent* object keeps result pagination bounded without
    requiring new server endpoints or modifying the SQLite store.
    """
    parsed = parse_comment_link(str(url or ""))
    if parsed is None:
        raise ValueError("Paste an exact VK comment URL containing reply= or comment=.")
    kind, owner, item_id, comment_id, _ = parsed
    key = f"{kind}:{owner}:{item_id}:{comment_id}"
    parent = f"{kind}{owner}_{item_id}"
    for page in range(1, 21):
        data = _get("/api/comments?q=" + quote(parent, safe="") + "&page=" + str(page))
        rows = data.get("items", [])
        for row in rows:
            if row.get("id") == key:
                if row.get("status") in BLOCKED_STATUSES:
                    raise ValueError("This comment was previously handled or has an uncertain outcome. Choose another test comment.")
                return row
        if page * 50 >= data.get("total", 0):
            break
    raise ValueError("This link was not found in the VKCM list. Add the link in the local interface, then try again.")


def target_from_pasted_url(url, user_id):
    """Direct selection does not need archive import; DOM must still prove ownership."""
    parsed = parse_comment_link(str(url or ""))
    if parsed is None:
        raise ValueError("An exact VK comment link is required, e.g. wall...?...reply=123")
    kind, owner, post, cid, cleaned = parsed
    key = f"{kind}:{owner}:{post}:{cid}"
    if Journal().statuses(user_id, [key]).get(key) in BLOCKED_STATUSES:
        raise ValueError("This comment was already handled or has an uncertain outcome. Select another.")
    return ExactTarget(kind, owner, post, cid, key, cleaned, user_id)


def choose_comment(account, ask=input, echo=print):
    """Always require an explicit choice; never silently pick queue[0]."""
    if not isinstance(account, dict) or not isinstance(account.get("id"), int):
        raise ValueError("Sign into VK ID in VK Comment Manager first")
    available, _ = get_queue(20)
    if available:
        echo("Available unhandled comments (first 20):")
        for i, record in enumerate(available, 1):
            echo(f"  {i}. {record['url']}")
    echo("Select an exact comment. --inspect never deletes anything.")
    answer = ask("Paste a comment URL or select its number (STOP to cancel): ").strip()
    if answer.upper() == "STOP" or not answer:
        raise ValueError("No comment selected. No deletion started.")
    if answer.isdigit():
        index = int(answer)
        if index < 1 or index > len(available):
            raise ValueError("The selected number is not in the displayed list")
        record = available[index - 1]
    else:
        # A link copied from vk.ru may not be imported into the local table.
        # An explicit user selection plus independent live DOM author check is
        # safer than forcing a second manual copy into the localhost UI.
        return target_from_pasted_url(answer, account["id"])
    return from_record(record, account["id"])


def is_target_page(actual_url, target):
    """Accept vk.com/vk.ru canonical redirects only when target key survives."""
    if not allowed_destination(actual_url):
        return False
    parsed = parse_comment_link(actual_url)
    return bool(parsed and f"{parsed[0]}:{parsed[1]}:{parsed[2]}:{parsed[3]}" == target.key)


def exact_dom_visible(page, target):
    """Allow VK's feed-address SPA modal only if the exact comment is visible."""
    for css in comment_selectors(target):
        locator = page.locator(css)
        if locator.count() == 1 and locator.is_visible():
            return True
    return False


def url_from_continue_prompt(answer, target):
    """Never silently consume a permalink as if it were an Enter press."""
    answer = str(answer).strip()
    if not answer:
        return
    if answer.upper() == "STOP":
        raise ValueError("Stopped by user")
    parsed = parse_comment_link(answer)
    if parsed is None:
        raise ValueError("At this step, press Enter after VK login. Choose another comment before starting.")
    if f"{parsed[0]}:{parsed[1]}:{parsed[2]}:{parsed[3]}" != target.key:
        raise ValueError("URL points to a different comment. Select the target at the beginning to avoid deleting the wrong one.")


def safe_page_path(url):
    """Never log OAuth parameters or session tokens."""
    u = urlsplit(url)
    return (u.hostname or "(no URL)") + (u.path or "/")


def alternate_official_link(url):
    """Preserve exact comment path/query when trying VK's other official domain."""
    p = urlsplit(url)
    if not allowed_destination(url):
        raise ValueError("Unsafe VK URL")
    mapping = {"vk.com": "vk.ru", "www.vk.com": "www.vk.ru",
               "m.vk.com": "m.vk.ru", "vk.ru": "vk.com",
               "www.vk.ru": "www.vk.com", "m.vk.ru": "m.vk.com"}
    host = mapping.get((p.hostname or "").lower(), p.hostname)
    return urlunsplit(("https", host, p.path, p.query, p.fragment))


def open_target_with_login_retry(page, target, ask=input, echo=print):
    """Open a selected comment first. Retry after a human VK login if redirected."""
    for attempt in range(2):
        link = target.url if attempt == 0 else alternate_official_link(target.url)
        echo("Opening selected comment: " + link)
        page.goto(link, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1700)
        current = safe_page_path(page.url)
        echo("Chromium page: " + current)
        if is_target_page(page.url, target) or exact_dom_visible(page, target):
            return
        if attempt == 0:
            echo("VK redirected from the comment permalink to " + current + ".")
            echo("Chromium may require VK sign-in or the post may be restricted.")
            echo("After sign-in, the same comment will be retried via the other official VK domain.")
            answer = ask("If needed, sign in on the official VK site in Chromium. Then press Enter to retry the comment URL: ")
            url_from_continue_prompt(answer, target)
            continue
        raise ValueError(
            "VK redirected away from the target comment a second time to " + current
            + ". Both official domains were checked. Verify your access and browser login; no deletion was performed."
        )


def run_browser(target, execute=False, ask=input, echo=print):
    if not allowed_destination(target.url):
        raise ValueError("Unsafe comment URL")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ValueError("Install the app dependencies first: bash scripts/install.sh") from exc

    echo("Playwright uses its own local Chromium profile.")
    echo("Sign into vk.com in the opened Chromium window if needed. Safari cookies are not used.")
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(local_browser_profile()), headless=False,
            accept_downloads=False, viewport={"width": 1250, "height": 900},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            open_target_with_login_retry(page, target, ask=ask, echo=echo)
            answer = ask(
                "Confirm Chromium is signed into your VK account (ID " + str(target.user_id)
                + ") and the target comment is visible. Expand hidden replies, then press Enter: "
            )
            url_from_continue_prompt(answer, target)
            if not is_target_page(page.url, target) and not exact_dom_visible(page, target):
                raise ValueError("VK navigated away from the target post to " + safe_page_path(page.url)
                                 + ". No deletion was performed.")
            try:
                css, row = locate_exact_comment(page, target)
            except ValueError as exc:
                echo("DOM identifiers for diagnostics (no comment texts or tokens):")
                echo(str(dom_clues(page, target.comment_id)))
                raise
            echo("Exact comment ID matched using: " + css)
            is_mine, ids = author_proof(row, target.user_id)
            echo("Author IDs found in DOM: " + (str(ids) if ids else "not verified"))
            if not is_mine:
                echo("Sanitized author/action DOM clues (CSS and paths only):")
                echo(str(sanitized_row_clues(row)))
                raise ValueError("Could not reliably confirm comment author. No deletion was performed.")
            echo("Author matches your VK ID " + str(target.user_id))
            row.hover()
            direct = direct_delete_button(row)
            buttons = menu_buttons(row) if direct is None else []
            if direct is not None:
                echo("Direct Delete control found in exact comment: comment_action_delete")
            else:
                echo("Available action-menu buttons in comment: " +
                     (", ".join(q for q, _ in buttons) if buttons else "NONE FOUND"))
            if not execute:
                if direct is None and not buttons:
                    echo("INSPECTION_PARTIAL: exact comment and author found, but no recognized Delete control.")
                    echo(str(sanitized_row_clues(row)))
                else:
                    echo("INSPECTION_OK: exact comment, author and possible action found; no deletion performed.")
                echo("Next step: --execute-one with exact comment-ID confirmation.")
                return 0
            if direct is not None:
                candidate = direct
            else:
                if not buttons:
                    raise ValueError("No unambiguous action control found in target comment. Nothing deleted.")
                unique = unique_visible([b for _, b in buttons])
                if len(unique) != 1:
                    raise ValueError("Multiple action controls found; deletion stopped.")
                unique[0].click(timeout=5000)
                candidates = delete_options_in_open_menu(page, unique[0])
                if len(candidates) != 1:
                    raise ValueError("Could not find an unambiguous Delete entry for this comment.")
                candidate = candidates[0]
            message = (f"About to delete ONE comment {target.key} "
                       f"for account {target.user_id}. Type DELETE {target.comment_id}: ")
            if ask(message).strip() != "DELETE " + str(target.comment_id):
                echo("Operation cancelled.")
                return 0
            # Before the possible irreversible click, record the attempt in the
            # account-specific journal. Never retry blindly if outcome unknown.
            journal = Journal()
            if journal.statuses(target.user_id, [target.key]).get(target.key) in {
                "pending", "outcome_unknown", "api_accepted", "user_marked_deleted",
                "browser_pending", "browser_unknown",
            }:
                raise ValueError("Journal contains a previous attempt with an uncertain outcome. Repeating deletion is prohibited.")
            journal.record(target.user_id, target.key, "browser", "browser_pending")
            context.once("dialog", lambda dialog: dialog.accept())
            candidate.click(timeout=5000)
            # Never claim verified removal on the basis of a click. VK may
            # show a further custom confirmation or reject it.
            journal.record(target.user_id, target.key, "browser", "browser_unknown")
            echo("Delete was clicked in Chromium; the VK server outcome is NOT verified.")
            echo("Check whether your comment disappeared in the open VK page.")
            ask("Press Enter after checking to close the controlled browser: ")
            return 0
        finally:
            context.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="VKCM: inspect or delete one own comment with Playwright, without VK API"
    )
    flags = parser.add_mutually_exclusive_group()
    flags.add_argument("--inspect", action="store_true", help="Read-only DOM inspection (default)")
    flags.add_argument("--execute-one", action="store_true", help="Allow one deletion after exact author/ID verification")
    parser.add_argument("--url", default="", help="Exact VK comment URL; import is optional")
    args = parser.parse_args(argv)
    server = None
    try:
        server = _start_server()
        queue, user = get_queue(1)
        prepared_url = args.url.strip()
        if not user:
            import webbrowser
            webbrowser.open(BASE)
            print("Please sign in with VK ID in the local VK Comment Manager app. Import is optional.")
            typed = input("After signing in, press Enter (or paste your own comment URL): ").strip()
            if typed:
                if not parse_comment_link(typed):
                    raise ValueError("Unrecognized VK comment URL")
                if prepared_url and parse_comment_link(typed)[:4] != parse_comment_link(prepared_url)[:4]:
                    raise ValueError("Entered URL identifies a different comment. Stopping.")
                prepared_url = typed
            _, user = get_queue(1)
        if not user or not isinstance(user.get("id"), int):
            raise ValueError("A verified VK ID sign-in is required in the local app")
        if prepared_url:
            target = target_from_pasted_url(prepared_url, user["id"])
        else:
            target = choose_comment(user)
        print("Selected comment: " + target.url)
        return run_browser(target, execute=args.execute_one)
    except KeyboardInterrupt:
        print("\\nInterrupted.")
        return 130
    except Exception as exc:
        # No tokens/cookies are included in this error report.
        print("STOPPED: " + str(exc), file=sys.stderr)
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
