"""No-write, no-remote-request tests for localhost conflict diagnostics."""
import io
import json
import unittest
import urllib.error
import urllib.request
from unittest.mock import MagicMock, Mock, patch

from vkmgr import port_diagnostics as diag


class PortDiagnosticsTests(unittest.TestCase):
    def _response(self, payload, server="VKCM/0.3.10"):
        resp = MagicMock()
        resp.__enter__.return_value = resp
        resp.headers = {"Server": server}
        resp.read.return_value = json.dumps(payload).encode("utf-8")
        return resp

    def test_valid_local_vkcm_response(self):
        res = self._response({"items": [], "auth_user": None, "total": 0})
        success, msg = diag.check_http(opener=lambda req, timeout: res)
        self.assertTrue(success)
        self.assertIn("VKCM_LOCAL_OK", msg)

    def test_other_service_200_malformed_json_fails_closed(self):
        res = self._response({"hello": "world"}, server="Other/1")
        success, msg = diag.check_http(opener=lambda req, timeout: res)
        self.assertFalse(success)
        self.assertIn("does not match VKCM", msg)

    def test_html_on_200_is_not_treated_as_vkcm(self):
        res = self._response({}, server="Other/1")
        res.read.return_value = b"<html>foreign server</html>"
        ok, msg = diag.check_http(opener=lambda req, timeout: res)
        self.assertFalse(ok)
        self.assertIn("not VKCM JSON", msg)

    def test_403_captures_http_status_not_foreign_html(self):
        err = urllib.error.HTTPError(
            diag.BASE + "/api/comments?page=1", 403, "Forbidden",
            {"Server": "Foreign/2"}, io.BytesIO(b"<html>private body</html>"),
        )
        ok, msg = diag.check_http(opener=lambda req, timeout: (_ for _ in ()).throw(err))
        self.assertFalse(ok)
        self.assertIn("403", msg)
        self.assertNotIn("private body", msg)

    def test_refused_port_is_reported_safely(self):
        err = urllib.error.URLError(ConnectionRefusedError(61, "refused"))
        ok, msg = diag.check_http(opener=lambda req, timeout: (_ for _ in ()).throw(err))
        self.assertFalse(ok)
        self.assertIn("No connection", msg)

    def test_lsof_displays_only_pid_and_process_name(self):
        completed = Mock(returncode=0, stdout=(
            "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"
            "Python 1234 localuser 4u IPv4 0t0 TCP 127.0.0.1:8765 (LISTEN)\n"
        ))
        with patch.object(diag.shutil, "which", return_value="/usr/sbin/lsof"):
            result = diag.port_owners(runner=lambda *args, **kwargs: completed)
        self.assertEqual(result, ["PID 1234, process Python"])
        self.assertNotIn("localuser", result[0])

    def test_main_never_kills_or_edits_anything(self):
        with patch.object(diag, "check_http",
                          return_value=(False, "HTTP 403, не VKCM")), \
             patch.object(diag, "port_owners",
                          return_value=["PID 1234, process Python"]), \
             patch.object(diag.subprocess, "Popen") as popen, \
             patch.object(diag.subprocess, "run") as run:
            self.assertEqual(diag.main([]), 2)
        popen.assert_not_called()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
