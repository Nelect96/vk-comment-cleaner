"""VK API 1051 must disable prohibited methods, never generate a write.

Manual confirmation is a user-authored local journal fact, NOT a VK deletion.
"""
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
import http.client
import json
import tempfile
import threading
import unittest

from vkmgr.journal import Journal
from vkmgr.parser import Comment
import vkmgr.server as app


class RestrictedTokenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = app.STATE
        self.c = Comment("wall:-7:11:41", "wall", -7, 11, 41,
                         "https://vk.com/wall-7_11?reply=41",
                         "", "", "manual")
        self.other = Comment("photo:7:12:42", "photo", 7, 12, 42,
                             "https://vk.com/photo7_12?comment=42",
                             "", "", "manual")
        app.STATE = app.AppState(Journal(Path(self.tmp.name) / "local.sqlite3"))
        app.STATE.comments = {self.c.id: self.c, self.other.id: self.other}
        app.STATE.token = "vk2-example"
        app.STATE.user = {"id": 123, "name": "Test"}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=3)
        app.STATE = self.original
        self.tmp.cleanup()

    def post(self, route, value):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        conn.request("POST", route, body=json.dumps(value), headers={
            "Host": "127.0.0.1:" + str(self.server.server_port),
            "X-VKCM-CSRF": app.STATE.csrf,
            "Content-Type": "application/json",
        })
        response = conn.getresponse()
        result = response.status, json.loads(response.read().decode("utf-8"))
        conn.close()
        return result

    def get_comments(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        conn.request("GET", "/api/comments", headers={
            "Host": "127.0.0.1:" + str(self.server.server_port),
        })
        res = conn.getresponse()
        data = json.loads(res.read().decode("utf-8"))
        conn.close()
        return data

    def test_preflight_1051_is_recorded_and_not_retried(self):
        with patch("vkmgr.server.verify_comment",
                   side_effect=app.VkApiError(1051, "profile type")) as check, \
             patch("vkmgr.server.action") as write:
            status, data = self.post("/api/preflight", {"ids": [self.c.id]})
            self.assertEqual(status, 200)
            self.assertEqual(data["results"][self.c.id]["status"], "unsupported")
            self.assertEqual(self.get_comments()["unavailable_kinds"], ["wall"])
            status, data = self.post("/api/preflight", {"ids": [self.c.id]})
            self.assertEqual(status, 200)
            self.assertEqual(data["results"][self.c.id]["status"], "unsupported")
            self.assertEqual(check.call_count, 1)
            write.assert_not_called()

    def test_direct_delete_preflight_failure_never_writes(self):
        with patch("vkmgr.server.verify_comment",
                   side_effect=app.VkApiError(1051, "method profile type")) as verify, \
             patch("vkmgr.server.action") as write:
            status, reply = self.post("/api/action", {
                "id": self.c.id, "operation": "delete", "confirmed": True,
            })
            self.assertEqual(status, 422)
            self.assertIn("1051", reply["error"])
            self.assertEqual(verify.call_count, 1)
            write.assert_not_called()
        self.assertIn("wall", app.STATE.api_unavailable)
        self.assertNotIn(self.c.id, app.STATE.statuses)
        self.assertEqual(app.STATE.checks[self.c.id]["status"], "unsupported")

    def test_denied_delete_is_blocked_without_more_calls(self):
        app.STATE.api_unavailable.add("wall")
        with patch("vkmgr.server.verify_comment") as verify, \
             patch("vkmgr.server.action") as write:
            status, data = self.post("/api/action", {
                "id": self.c.id, "operation": "delete", "confirmed": True
            })
            self.assertEqual(status, 409)
            self.assertIn("1051", data["error"])
            verify.assert_not_called()
            write.assert_not_called()

    def test_denied_batch_is_blocked_before_background_worker(self):
        app.STATE.api_unavailable.add("wall")
        with patch("vkmgr.server.verify_comment") as verify, \
             patch("vkmgr.server.action") as write:
            status, data = self.post("/api/bulk", {
                "ids": [self.c.id], "confirmation": "УДАЛИТЬ",
            })
            self.assertEqual(status, 409)
            self.assertIn("1051", data["error"])
            self.assertFalse(app.STATE.job["running"])
            verify.assert_not_called()
            write.assert_not_called()

    def test_wall_denial_does_not_claim_photo_methods_are_denied(self):
        app.STATE.api_unavailable.add("wall")
        self.assertEqual(self.get_comments()["unavailable_kinds"], ["wall"])
        self.assertNotIn("photo", app.STATE.api_unavailable)

    def test_manual_progress_requires_user_explicit_statement_no_vk_write(self):
        with patch("vkmgr.server.action") as write, \
             patch("vkmgr.server.verify_comment") as verify:
            status, _ = self.post("/api/manual-mark", {"id": self.c.id})
            self.assertEqual(status, 400)
            status, response = self.post("/api/manual-mark", {
                "id": self.c.id, "confirmation": "Я УДАЛИЛ В VK",
            })
            self.assertEqual(status, 200)
            self.assertEqual(response["status"], "user_marked_deleted")
            self.assertIn("не проверено", response["note"])
            status, _ = self.post("/api/manual-mark", {
                "id": self.c.id, "confirmation": "Я УДАЛИЛ В VK",
            })
            self.assertEqual(status, 409)
            write.assert_not_called()
            verify.assert_not_called()
            self.assertEqual(
                app.STATE.journal.statuses(123, [self.c.id])[self.c.id],
                "user_marked_deleted",
            )
            self.assertEqual(
                self.get_comments()["items"][0]["status"],
                "user_marked_deleted",
            )

    def test_manual_progress_fails_when_not_authenticated(self):
        app.STATE.user = None
        status, _ = self.post("/api/manual-mark", {
            "id": self.c.id, "confirmation": "Я УДАЛИЛ В VK",
        })
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
