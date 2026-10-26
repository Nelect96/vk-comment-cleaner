"""Optional, user-authorized VK API calls. Network write operations are never automatic."""
from __future__ import annotations
import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_VERSION = "5.199"
METHODS = {
    "wall": ("wall.deleteComment", "wall.editComment"),
    "photo": ("photos.deleteComment", "photos.editComment"),
    "video": ("video.deleteComment", "video.editComment"),
}


class VkApiError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"VK API {code}: {message}")


def call_api(token, method, params):
    """Call an authorized VK API method, returning its *response* only.

    Bearer tokens are never embedded in URLs or printed in logs.
    """
    values = {"v": API_VERSION, **params}
    request = Request(
        "https://api.vk.com/method/" + method,
        data=urlencode(values).encode("utf-8"),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(request, timeout=20) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise VkApiError("?", "Invalid VK API response")
    if "error" in payload:
        err = payload["error"]
        raise VkApiError(err.get("error_code", "?"), err.get("error_msg", "Unknown error"))
    if "response" not in payload:
        raise VkApiError("?", "Missing API response")
    return payload["response"]


def action(token, comment, operation, message=""):
    if operation not in ("delete", "edit"):
        raise ValueError("Unsupported operation")
    if comment.kind not in METHODS:
        raise ValueError("Unsupported comment type")
    if operation == "edit" and not message.strip():
        raise ValueError("Comment text must not be empty")
    method = METHODS[comment.kind][0 if operation == "delete" else 1]
    params = {"owner_id": str(comment.owner_id), "comment_id": str(comment.comment_id)}
    if comment.kind == "wall":
        params["post_id"] = str(comment.item_id)
    if operation == "edit":
        params["message"] = message
    if call_api(token, method, params) != 1:
        raise VkApiError("?", "VK did not confirm the action")
    return True
