"""VK ID OAuth tests. Never contact VK and never delete comments."""
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit, quote

from vkmgr.oauth import OAuthError, begin, exchange, parse_return


class PKCE(unittest.TestCase):
    def test_authorize_has_challenge_but_no_verifier_or_password(self):
        attempt = begin(123456, "http://127.0.0.1:8765/oauth/callback", now=100)
        url = urlsplit(attempt.url)
        params = parse_qs(url.query)
        self.assertEqual(url.hostname, "id.vk.ru")
        self.assertEqual(params["client_id"], ["123456"])
        self.assertEqual(params["response_type"], ["code"])
        self.assertEqual(params["code_challenge_method"], ["s256"])
        self.assertEqual(params["redirect_uri"], [attempt.redirect_uri])
        self.assertNotIn("code_verifier", params)
        self.assertNotIn("password", params)
        self.assertGreater(len(attempt.verifier), 40)

    def test_rejects_invalid_app_id_and_redirect(self):
        for app_id in (0, "abc", -1, True):
            with self.subTest(app_id=app_id), self.assertRaises(OAuthError):
                begin(app_id, "http://127.0.0.1:8765/oauth/callback")
        for address in (
            "http://attacker.example/cb",
            "file:///tmp/cb",
            "https://user:pass@example.com/cb",
            "https://example.com/cb#fragment",
            "https://example.com/cb?x=y",
        ):
            with self.subTest(address=address), self.assertRaises(OAuthError):
                begin(123, address)

    def test_redirect_state_device_and_expiration(self):
        attempt = begin(123, "http://127.0.0.1:8765/oauth/callback", now=100)
        response = attempt.redirect_uri + "?code=one-time-code&device_id=dev&state=" + attempt.state
        self.assertEqual(parse_return(response, attempt, now=101), ("one-time-code", "dev"))
        for bad in (
            response.replace(attempt.state, "bad"),
            response.replace("code=one-time-code", "code="),
            response.replace("device_id=dev", "device_id="),
            response.replace("127.0.0.1:8765", "evil.example"),
            response + "&code=duplicate",
        ):
            with self.subTest(bad=bad), self.assertRaises(OAuthError):
                parse_return(bad, attempt, now=101)
        with self.assertRaises(OAuthError):
            parse_return(response, attempt, now=1000)

    def test_payload_style_callback_is_accepted_only_with_valid_state(self):
        attempt = begin(123, "https://example.com/callback", now=100)
        import json
        payload = json.dumps({"code": "single-use", "device_id": "device",
                              "state": attempt.state}, separators=(",", ":"))
        returned = attempt.redirect_uri + "?payload=" + quote(payload)
        self.assertEqual(parse_return(returned, attempt, now=101), ("single-use", "device"))
        with self.assertRaises(OAuthError):
            parse_return(returned + "&state=" + attempt.state, attempt, now=101)
        altered = returned.replace(attempt.state, "incorrect")
        with self.assertRaises(OAuthError):
            parse_return(altered, attempt, now=101)

    def test_code_exchange_sends_pkce_and_device_id(self):
        attempt = begin(123, "https://example.com/callback")
        returned = {
            "state": attempt.state, "user_id": 81,
            "access_token": "secret", "refresh_token": "refresh",
            "expires_in": 3600, "scope": "email",
        }
        with patch("vkmgr.oauth._post", return_value=returned) as post:
            result = exchange(attempt, "code", "device")
        self.assertEqual(result["user_id"], 81)
        self.assertEqual(result["access_token"], "secret")
        self.assertEqual(post.call_args.args[0]["code_verifier"], attempt.verifier)
        self.assertEqual(post.call_args.args[0]["device_id"], "device")
        self.assertEqual(post.call_args.args[1], {"code": "code"})

    def test_rejects_bad_token_response(self):
        attempt = begin(123, "https://example.com/callback")
        for response in (
            {}, {"state": "different", "user_id": 81, "access_token": "token"},
            {"state": attempt.state, "user_id": 81},
            {"state": attempt.state, "user_id": "81", "access_token": "token"},
        ):
            with self.subTest(response=response), patch("vkmgr.oauth._post", return_value=response):
                with self.assertRaises(OAuthError):
                    exchange(attempt, "code", "device")


if __name__ == "__main__":
    unittest.main()
