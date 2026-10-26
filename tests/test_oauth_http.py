"""Local HTTP VK ID login tests with mocked VK responses.

No credentials, no real OAuth network traffic and no VK deletes.
"""
import http.client
import json
import tempfile
import threading
from pathlib import Path
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from vkmgr.journal import Journal
from vkmgr.oauth import OAuthError
import vkmgr.server as server


class OAuthHTTP(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = server.STATE
        server.STATE = server.AppState(Journal(Path(self.tmp.name) / "journal.sqlite3"))
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=3)
        server.STATE = self.original
        self.tmp.cleanup()

    def post(self, route, obj):
        conn = http.client.HTTPConnection("127.0.0.1", self.http.server_port, timeout=3)
        conn.request("POST", route, body=json.dumps(obj), headers={
            "Host": f"127.0.0.1:{self.http.server_port}",
            "Content-Type": "application/json",
            "X-VKCM-CSRF": server.STATE.csrf,
        })
        res = conn.getresponse()
        status, body = res.status, json.loads(res.read().decode("utf-8"))
        conn.close()
        return status, body

    def start(self):
        redirect = f"http://127.0.0.1:{self.http.server_port}/oauth/callback"
        status, data = self.post("/api/oauth/start", {"app_id": "123", "redirect_uri": redirect})
        self.assertEqual(status, 200)
        self.assertTrue(data["url"].startswith("https://id.vk.ru/authorize?"))
        return redirect

    def test_refuses_invalid_application(self):
        status, _ = self.post("/api/oauth/start", {
            "app_id": "external-app", "redirect_uri": "http://evil.example/cb",
        })
        self.assertEqual(status, 400)
        self.assertIsNone(server.STATE.oauth_attempt)

    def test_successful_vkid_login_does_not_imply_legacy_api(self):
        redirect = self.start()
        attempt = server.STATE.oauth_attempt
        callback = redirect + "?code=one&device_id=dev&state=" + attempt.state
        response = {"user_id": 81, "access_token": "token-secret", "scope": "email"}
        with patch("vkmgr.server.exchange_oauth", return_value=response) as exchange, \
             patch("vkmgr.server.authorized_user", side_effect=server.VkApiError(1051, "permission")):
            status, data = self.post("/api/oauth/finish", {"redirect_url": callback})
        self.assertEqual(status, 200)
        self.assertFalse(data["legacy_api"])
        self.assertEqual(data["vkid_user"]["id"], 81)
        self.assertIsNone(server.STATE.user)
        self.assertEqual(server.STATE.token, "")
        self.assertIsNone(server.STATE.oauth_attempt)
        exchange.assert_called_once()
        status, _ = self.post("/api/oauth/finish", {"redirect_url": callback})
        self.assertEqual(status, 422)

    def test_identity_mismatch_never_enables_legacy_access(self):
        redirect = self.start()
        attempt = server.STATE.oauth_attempt
        callback = redirect + "?code=one&device_id=dev&state=" + attempt.state
        with patch("vkmgr.server.exchange_oauth", return_value={
            "user_id": 81, "access_token": "token", "scope": "",
        }), patch("vkmgr.server.authorized_user", return_value={"id": 82, "name": "Different"}):
            status, data = self.post("/api/oauth/finish", {"redirect_url": callback})
        self.assertEqual(status, 200)
        self.assertFalse(data["legacy_api"])
        self.assertFalse(server.STATE.token)

    def test_valid_legacy_identity_allows_read_access_but_not_deletion(self):
        redirect = self.start()
        attempt = server.STATE.oauth_attempt
        callback = redirect + "?code=one&device_id=dev&state=" + attempt.state
        with patch("vkmgr.server.exchange_oauth", return_value={
            "user_id": 81, "access_token": "token", "scope": "",
        }), patch("vkmgr.server.authorized_user", return_value={"id": 81, "name": "Test"}), \
             patch("vkmgr.server.action") as write:
            status, data = self.post("/api/oauth/finish", {"redirect_url": callback})
            self.assertEqual(status, 200)
            self.assertTrue(data["legacy_api"])
            self.assertEqual(server.STATE.user["id"], 81)
            write.assert_not_called()

    def test_external_https_redirect_must_not_be_relayed_by_local_callback(self):
        redirect = "https://example.com/vkcm-auth-response"
        status, _ = self.post("/api/oauth/start", {
            "app_id": "123456", "redirect_uri": redirect,
        })
        self.assertEqual(status, 200)
        attempt = server.STATE.oauth_attempt
        uri = "/oauth/callback?code=one-time-code&device_id=device&state=" + attempt.state
        with patch("vkmgr.server.exchange_oauth") as exchange:
            conn = http.client.HTTPConnection("127.0.0.1", self.http.server_port, timeout=3)
            conn.request("GET", uri, headers={
                "Host": f"127.0.0.1:{self.http.server_port}"
            })
            res = conn.getresponse()
            self.assertEqual(res.status, 303)
            self.assertEqual(res.getheader("Location"), "/")
            res.read()
            conn.close()
            exchange.assert_not_called()
        self.assertIsNotNone(server.STATE.oauth_attempt)

    def test_local_callback_does_not_impersonate_external_https_redirect(self):
        redirect = "https://example.com/vkcm-auth-response"
        self.post("/api/oauth/start", {"app_id": "123456",
                                        "redirect_uri": redirect})
        with patch("vkmgr.server.exchange_oauth") as exchange:
            conn = http.client.HTTPConnection("127.0.0.1", self.http.server_port, timeout=3)
            conn.request("GET", "/oauth/callback?code=try&device_id=dev&state=WRONG", headers={
                "Host": f"127.0.0.1:{self.http.server_port}"
            })
            res = conn.getresponse()
            self.assertEqual(res.status, 303)
            res.read()
            conn.close()
            exchange.assert_not_called()
        self.assertIsNotNone(server.STATE.oauth_attempt)
        self.assertIn("Адрес ответа", server.STATE.oauth_message)

    def test_external_https_return_is_valid_for_manual_finish(self):
        redirect = "https://example.com/vkcm-auth-response"
        self.post("/api/oauth/start", {"app_id": "123456",
                                        "redirect_uri": redirect})
        state = server.STATE.oauth_attempt.state
        with patch("vkmgr.server.exchange_oauth", return_value={
            "user_id": 81, "access_token": "secret-token", "scope": ""
        }), patch("vkmgr.server.authorized_user",
                 side_effect=server.VkApiError(1051, "legacy denied")):
            status, response = self.post("/api/oauth/finish", {
                "redirect_url": redirect + "?code=once&device_id=dev&state=" + state
            })
        self.assertEqual(status, 200)
        self.assertFalse(response["legacy_api"])

    def test_state_mismatch_does_not_consume_attempt(self):
        redirect = self.start()
        status, _ = self.post("/api/oauth/finish", {
            "redirect_url": redirect + "?code=one&device_id=dev&state=WRONG",
        })
        self.assertEqual(status, 422)
        self.assertIsNotNone(server.STATE.oauth_attempt)


if __name__ == "__main__":
    unittest.main()
