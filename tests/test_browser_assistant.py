"""Browser-assisted navigation tests: no VK API writes, no automatic clicks."""
import unittest
from unittest.mock import patch

from vkmgr import browser_assistant as browser


class BrowserQueueTests(unittest.TestCase):
    def test_filters_completed_entries_and_untrusted_urls(self):
        def fake_get(path):
            self.assertEqual(path, "/api/comments?page=1")
            return {
                "total": 4, "auth_user": {"id": 123, "name": "Me"},
                "items": [
                    {"id": "wall:-7:11:41", "url": "https://vk.com/wall-7_11?reply=41",
                     "kind": "wall", "status": "archived"},
                    {"id": "wall:-7:11:42", "url": "https://vk.com/wall-7_11?reply=42",
                     "kind": "wall", "status": "user_marked_deleted"},
                    {"id": "wall:-7:11:43", "url": "https://evil.example/wall-7_11?reply=43",
                     "kind": "wall", "status": "archived"},
                    {"id": "wall:-7:11:44", "url": "https://vk.com/wall-7_11?reply=45",
                     "kind": "wall", "status": "archived"},
                ],
            }
        with patch.object(browser, "_get", side_effect=fake_get):
            queue, owner = browser.get_queue(20)
        self.assertEqual([item["id"] for item in queue], ["wall:-7:11:41"])
        self.assertEqual(owner["id"], 123)

    def test_limit_is_bounded(self):
        for limit in (0, 51, -1, None):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                browser.get_queue(limit)

    def test_explicit_manual_confirmation_records_without_remote_write(self):
        opened, recorded, printed = [], [], []
        prompts = iter(["", "DELETED", "YES"])
        queue = [{"id": "wall:-7:11:41", "url": "https://vk.com/wall-7_11?reply=41",
                  "preview": "Из архива"}]
        result = browser.guide(
            queue, {"id": 123},
            opener=lambda url: opened.append(url) or True,
            ask=lambda _prompt: next(prompts),
            echo=printed.append,
            recorder=lambda key, csrf: recorded.append((key, csrf)),
            csrf_factory=lambda: "local-csrf",
        )
        self.assertEqual(result, {"visited": 1, "marked": 1})
        self.assertEqual(opened, ["https://vk.com/wall-7_11?reply=41"])
        self.assertEqual(recorded, [("wall:-7:11:41", "local-csrf")])
        self.assertTrue(any("NOT verified by VK API" in x for x in printed))

    def test_empty_enter_skips_journal(self):
        opened, recorded = [], []
        result = browser.guide(
            [{"id": "wall:-7:11:41", "url": "https://vk.com/wall-7_11?reply=41"}],
            {"id": 123},
            opener=lambda url: opened.append(url) or True,
            ask=lambda _prompt: "",
            echo=lambda _msg: None,
            recorder=lambda *args: recorded.append(args),
            csrf_factory=lambda: "csrf",
        )
        self.assertEqual(result, {"visited": 1, "marked": 0})
        self.assertEqual(len(opened), 1)
        self.assertFalse(recorded)

    def test_cannot_mark_without_local_account(self):
        opened = []
        result = browser.guide(
            [{"id": "wall:-7:11:41", "url": "https://vk.com/wall-7_11?reply=41"}],
            None, opener=lambda u: opened.append(u), echo=lambda _m: None,
        )
        self.assertEqual(result, {"visited": 0, "marked": 0})
        self.assertEqual(opened, [])

    def test_untrusted_url_is_not_opened(self):
        result = browser.guide(
            [{"id": "x", "url": "javascript:alert(1)"}],
            {"id": 123}, opener=lambda url: self.fail("Should not open"),
            ask=lambda _: self.fail("Should not prompt"),
            echo=lambda _msg: None,
        )
        self.assertEqual(result, {"visited": 0, "marked": 0})


class LocalPortProbeTests(unittest.TestCase):
    def test_loopback_open_explicitly_disables_system_and_vpn_proxies(self):
        import urllib.request
        request = urllib.request.Request(browser.BASE + "/api/comments?page=1")
        with patch.object(urllib.request, "ProxyHandler",
                          wraps=urllib.request.ProxyHandler) as proxy, \
             patch.object(urllib.request, "build_opener") as build:
            build.return_value.open.return_value = object()
            browser._open_local(request, timeout=3)
        proxy.assert_called_once_with({})
        build.assert_called_once()
        build.return_value.open.assert_called_once_with(request, timeout=3)

    def test_http_403_reports_real_status_and_never_starts_foreign_app(self):
        import io
        from urllib.error import HTTPError
        response = HTTPError(
            browser.BASE + "/api/comments?page=1", 403,
            "Forbidden", {}, io.BytesIO(b"forbidden"),
        )
        with patch.object(browser, "_get", side_effect=response), \
             self.assertRaisesRegex(ValueError, "HTTP 403"):
            browser._local_server_running()

    def test_http_404_reports_real_status(self):
        import io
        from urllib.error import HTTPError
        response = HTTPError(
            browser.BASE + "/api/comments?page=1", 404,
            "Not Found", {}, io.BytesIO(b"not found"),
        )
        with patch.object(browser, "_get", side_effect=response), \
             self.assertRaisesRegex(ValueError, "HTTP 404"):
            browser._local_server_running()

    def test_expected_local_json_is_accepted(self):
        with patch.object(browser, "_get", return_value={
            "items": [], "auth_user": None, "total": 0,
        }):
            self.assertTrue(browser._local_server_running())

    def test_non_json_port_reply_is_rejected_without_attempt_to_login(self):
        import io
        from unittest.mock import Mock
        content = Mock()
        content.__enter__ = Mock(return_value=content)
        content.__exit__ = Mock(return_value=False)
        content.status = 200
        content.read.return_value = b"<html>not our app</html>"
        with patch.object(browser, "_open_local", return_value=content):
            with self.assertRaisesRegex(ValueError, "did not return expected VKCM JSON"):
                browser._get("/api/comments?page=1")


class AutoStartTests(unittest.TestCase):
    def test_probe_rejects_wrong_local_application(self):
        with patch.object(browser, "_get", return_value={"hello": "other"}):
            with self.assertRaises(ValueError):
                browser._local_server_running()

    def test_probe_connection_refused_means_server_stopped(self):
        from urllib.error import URLError
        with patch.object(browser, "_get",
                          side_effect=URLError(ConnectionRefusedError(61, "refused"))):
            self.assertFalse(browser._local_server_running())

    def test_autostart_waits_until_our_server_is_ready(self):
        from unittest.mock import Mock
        process = Mock()
        process.poll.return_value = None
        with patch.object(browser, "_local_server_running",
                          side_effect=[False, False, True]), \
             patch.object(browser.subprocess, "Popen", return_value=process) as popen, \
             patch.object(browser.time, "sleep"):
            self.assertIs(browser._start_server(), process)
        self.assertEqual(popen.call_count, 1)
        self.assertIn("-m", popen.call_args.args[0])
        self.assertIn("vkmgr", popen.call_args.args[0])
        self.assertEqual(popen.call_args.kwargs["stdin"], browser.subprocess.DEVNULL)

    def test_autostart_not_necessary_when_server_running(self):
        with patch.object(browser, "_local_server_running", return_value=True), \
             patch.object(browser.subprocess, "Popen") as popen:
            self.assertIsNone(browser._start_server())
            popen.assert_not_called()

    def test_main_guides_login_when_no_comments_or_user(self):
        item = {"id": "wall:-7:11:41", "url": "https://vk.com/wall-7_11?reply=41"}
        with patch.object(browser, "_start_server", return_value=None), \
             patch.object(browser, "get_queue",
                          side_effect=[([], None), ([item], {"id": 123})]) as queue, \
             patch.object(browser.webbrowser, "open") as browser_open, \
             patch("builtins.input", return_value=""), \
             patch.object(browser, "guide", return_value={"visited": 1, "marked": 0}) as guide:
            self.assertEqual(browser.main(["--limit", "1"]), 0)
            self.assertEqual(queue.call_count, 2)
            browser_open.assert_called_once_with(browser.BASE)
            guide.assert_called_once()

    def test_main_stops_server_started_just_for_this_session(self):
        from unittest.mock import Mock
        process = Mock()
        process.poll.return_value = None
        with patch.object(browser, "_start_server", return_value=process), \
             patch.object(browser, "get_queue", return_value=(
                 [{"id": "wall:-7:11:41", "url": "https://vk.com/wall-7_11?reply=41"}],
                 {"id": 123})), \
             patch.object(browser, "guide"):
            self.assertEqual(browser.main(["--limit", "1"]), 0)
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=5)

    def test_dry_run_never_launches_browser_or_server(self):
        with patch.object(browser, "_start_server") as start, \
             patch.object(browser, "get_queue",
                          return_value=([{"url": "https://vk.com/wall-7_11?reply=41"}], None)), \
             patch.object(browser.webbrowser, "open") as open_page:
            self.assertEqual(browser.main(["--dry-run"]), 0)
            start.assert_not_called()
            open_page.assert_not_called()


if __name__ == "__main__":
    unittest.main()
