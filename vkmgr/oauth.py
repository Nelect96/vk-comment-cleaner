"""VK ID OAuth 2.1 Authorization Code + PKCE, without browser credential access.

Requires the owner's registered VK ID APP_ID and registered redirect URL.
No passwords or borrowed client IDs. Tokens only live in process memory.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

AUTH_ENDPOINT = "https://id.vk.ru/authorize"
TOKEN_ENDPOINT = "https://id.vk.ru/oauth2/auth"
MAX_AGE = 600


class OAuthError(ValueError):
    pass


@dataclass(frozen=True)
class AuthAttempt:
    app_id: int
    redirect_uri: str
    state: str
    verifier: str
    created_at: float
    url: str


def validate_app_id(app_id):
    if isinstance(app_id, bool) or not re.fullmatch(r"[1-9][0-9]{0,14}", str(app_id)):
        raise OAuthError("Введите числовой App ID своего приложения VK ID")
    return int(app_id)


def validate_redirect(uri):
    if not isinstance(uri, str) or not uri or len(uri) > 1000:
        raise OAuthError("Укажите зарегистрированный Redirect URL")
    parts = urlsplit(uri)
    if parts.fragment or parts.username or parts.password or parts.query:
        raise OAuthError("Redirect URL не должен содержать параметры, фрагмент или пароль")
    try:
        port = parts.port
    except ValueError as exc:
        raise OAuthError("Неверный порт Redirect URL") from exc
    if parts.scheme == "http":
        if parts.hostname not in ("127.0.0.1", "localhost") or port is None:
            raise OAuthError("HTTP возможен только для зарегистрированного localhost")
    elif parts.scheme != "https" or not parts.hostname:
        raise OAuthError("Нужен HTTPS Redirect URL либо локальный адрес разработки")
    return uri


def begin(app_id, redirect_uri, now=None):
    app_id = validate_app_id(app_id)
    redirect_uri = validate_redirect(redirect_uri)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    args = {
        "client_id": app_id,
        "app_id": app_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "s256",
        "prompt": "",
        "state": state,
    }
    return AuthAttempt(app_id, redirect_uri, state, verifier,
                       time.time() if now is None else now,
                       AUTH_ENDPOINT + "?" + urlencode(args))


def parse_return(value, attempt, now=None):
    """Validate redirect before code exchange. Never log raw callback URLs."""
    if not isinstance(value, str) or len(value) > 8000:
        raise OAuthError("Неверный ответ VK ID")
    got = urlsplit(value)
    expected = urlsplit(attempt.redirect_uri)
    if (got.scheme, got.netloc, got.path) != (expected.scheme, expected.netloc, expected.path):
        raise OAuthError("Адрес ответа VK ID не совпадает с зарегистрированным")
    if (time.time() if now is None else now) - attempt.created_at > MAX_AGE:
        raise OAuthError("Время ожидания входа истекло; начните заново")
    params = parse_qs(got.query, keep_blank_values=True)
    if any(len(v) != 1 for v in params.values()):
        raise OAuthError("Повторяющиеся параметры авторизации")
    # VK ID callback implementations can provide flat query arguments or a
    # serialized payload. Never merge conflicting forms or bypass state checks.
    if "payload" in params:
        if any(field in params for field in ("code", "state", "device_id")):
            raise OAuthError("Неоднозначный ответ VK ID")
        raw = params["payload"][0]
        if len(raw) > 4096:
            raise OAuthError("Слишком большой ответ VK ID")
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise OAuthError("Некорректный payload VK ID") from exc
        if not isinstance(decoded, dict):
            raise OAuthError("Некорректный payload VK ID")
        params = {key: [value] for key, value in decoded.items()
                  if key in ("state", "code", "device_id", "error")
                  and isinstance(value, str)}
    if params.get("state", [None])[0] != attempt.state:
        raise OAuthError("Несовпадение state: ответ VK ID не принят")
    if "error" in params:
        raise OAuthError("VK ID отказал в авторизации")
    code = params.get("code", [""])[0]
    device = params.get("device_id", [""])[0]
    if not 1 <= len(code) <= 4096 or not 1 <= len(device) <= 4096:
        raise OAuthError("VK ID не передал code или device_id")
    return code, device


def _post(query, body):
    request = Request(TOKEN_ENDPOINT + "?" + urlencode(query),
                      data=urlencode(body).encode("utf-8"),
                      headers={"Content-Type": "application/x-www-form-urlencoded"},
                      method="POST")
    with urlopen(request, timeout=20) as response:
        if response.status != 200:
            raise OAuthError("VK ID временно не отвечает")
        payload = response.read(65537)
    if len(payload) > 65536:
        raise OAuthError("Ответ VK ID слишком большой")
    try:
        result = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as exc:
        raise OAuthError("Некорректный ответ VK ID") from exc
    if not isinstance(result, dict) or "error" in result:
        raise OAuthError("VK ID не разрешил обмен кода")
    return result


def exchange(attempt, code, device):
    response = _post({
        "grant_type": "authorization_code",
        "redirect_uri": attempt.redirect_uri,
        "client_id": attempt.app_id,
        "code_verifier": attempt.verifier,
        "state": attempt.state,
        "device_id": device,
    }, {"code": code})
    if response.get("state") != attempt.state:
        raise OAuthError("VK ID вернул неверный state")
    token = response.get("access_token")
    uid = response.get("user_id")
    if not isinstance(token, str) or not 1 <= len(token) <= 4096:
        raise OAuthError("VK ID не вернул access_token")
    if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
        raise OAuthError("VK ID не вернул корректный ID пользователя")
    return {
        "access_token": token,
        "refresh_token": response.get("refresh_token"),
        "user_id": uid,
        "device_id": device,
        "scope": response.get("scope", ""),
        "expires_in": response.get("expires_in"),
    }
