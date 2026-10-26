"""Fail-closed, read-only VK comment ownership checks.

Nothing here deletes or edits data. A comment is actionable only when VK
returns an exact matching comment ID with the currently authenticated user ID.
"""
from __future__ import annotations

from dataclasses import dataclass
from .vk_api import call_api, VkApiError

# Browsing 10 * 100 comments is a conservative per-media limit, not a promise
# to find any deeply nested or exceptionally old comment.
PAGE_SIZE = 100
MAX_PAGES = 10


@dataclass(frozen=True)
class CheckResult:
    status: str
    reason: str
    from_id: int | None = None

    def to_dict(self):
        return {"status": self.status, "reason": self.reason, "from_id": self.from_id}


def authorized_user(token):
    """Get the user ID from VK, never from untrusted archive metadata."""
    people = call_api(token, "users.get", {})
    if not isinstance(people, list) or len(people) != 1:
        raise VkApiError("identity", "VK did not return one authenticated user")
    identity = people[0]
    if not isinstance(identity, dict) or not isinstance(identity.get("id"), int) or identity["id"] <= 0:
        raise VkApiError("identity", "VK identity response is invalid")
    return {"id": identity["id"], "name": " ".join(str(identity.get(k, "")) for k in ("first_name", "last_name")).strip()}


def _match(item, target_id, me):
    if not isinstance(item, dict):
        return None
    if item.get("id") != target_id:
        return None
    # A missing or malformed author is NEVER treated as ours.
    author = item.get("from_id")
    if not isinstance(author, int) or author == 0:
        return CheckResult("unverified", "VK did not return a reliable author")
    if author != me:
        return CheckResult("not_mine", "Comment belongs to another account", author)
    return CheckResult("verified", "Comment author matches your VK account", author)


def verify_comment(token, comment, user_id):
    """Conservatively verify a specific comment against live VK data.

    For photos/videos, checks the first MAX_PAGES pages of top-level comments.
    Unmatched nested replies or inaccessible material always remain unverified.
    """
    if not isinstance(user_id, int) or user_id <= 0:
        return CheckResult("unverified", "Authenticated account ID is missing")
    if comment.kind == "wall":
        result = call_api(token, "wall.getComment", {
            "owner_id": comment.owner_id, "comment_id": comment.comment_id,
        })
        items = result.get("items") if isinstance(result, dict) else None
        if not isinstance(items, list):
            return CheckResult("unverified", "Unexpected wall.getComment response")
        for item in items:
            match = _match(item, comment.comment_id, user_id)
            if match:
                # When VK supplies a parent ID, ensure it matches the archive link.
                if (match.status == "verified" and isinstance(item.get("post_id"), int)
                        and item["post_id"] != comment.item_id):
                    return CheckResult("unverified", "Parent post ID differs from archive")
                return match
        return CheckResult("unverified", "Comment not returned by VK")

    if comment.kind not in ("photo", "video"):
        return CheckResult("unverified", "Unsupported comment category")
    method = {"photo": "photos.getComments", "video": "video.getComments"}[comment.kind]
    item_param = "photo_id" if comment.kind == "photo" else "video_id"
    for page in range(MAX_PAGES):
        result = call_api(token, method, {
            "owner_id": comment.owner_id, item_param: comment.item_id,
            "offset": page * PAGE_SIZE, "count": PAGE_SIZE, "sort": "desc",
        })
        items = result.get("items") if isinstance(result, dict) else None
        if not isinstance(items, list):
            return CheckResult("unverified", "Unexpected media comments response")
        for item in items:
            match = _match(item, comment.comment_id, user_id)
            if match:
                return match
        if len(items) < PAGE_SIZE:
            break
    return CheckResult("unverified", "Not found in accessible top-level comments; may be a nested reply")
