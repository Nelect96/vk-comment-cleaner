"""Validate selectable localhost port without contacting VK or deleting."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from vkmgr import local_config
from vkmgr import server


class ConfiguredPortTests(unittest.TestCase):
    def test_default_port_preserves_existing_setups(self):
        self.assertEqual(local_config.configured_port({}), 8765)
        self.assertEqual(local_config.configured_port({"VKCM_PORT": "18765"}), 18765)
        self.assertEqual(local_config.configured_port({"VKCM_PORT": "1024"}), 1024)
        self.assertEqual(local_config.configured_port({"VKCM_PORT": "65535"}), 65535)

    def test_invalid_port_values_fail_closed(self):
        for value in ("", "876", "0", "80", "65536", "655350",
                      "not-a-port", "18765; echo bad", " 18765",
                      "18765 ", "18765/tcp", "-8765"):
            with self.subTest(port=value), self.assertRaisesRegex(
                    ValueError, "VKCM_PORT"):
                local_config.configured_port({"VKCM_PORT": value})

    def test_invalid_non_string_env_value_fails_closed(self):
        for raw in (None, 18765, True):
            with self.subTest(port=raw), self.assertRaises(ValueError):
                local_config.configured_port({"VKCM_PORT": raw})

    def test_actual_new_process_uses_same_alternate_port_everywhere(self):
        """Protects from importing port 8765 into client despite a new server."""
        env = dict(os.environ, VKCM_PORT="18765")
        command = (
            "import json;"
            "from vkmgr import local_config,server,browser_assistant,port_diagnostics;"
            "print(json.dumps({'server':server.PORT,'client':browser_assistant.PORT,"
            "'doctor':port_diagnostics.PORT,'base':browser_assistant.BASE,"
            "'server_base':server.BASE,'doctor_base':port_diagnostics.BASE}))"
        )
        result = subprocess.run(
            [sys.executable, "-c", command],
            env=env, capture_output=True, text=True, timeout=10, check=True,
        )
        values = json.loads(result.stdout)
        self.assertEqual(values["server"], 18765)
        self.assertEqual(values["client"], 18765)
        self.assertEqual(values["doctor"], 18765)
        self.assertEqual(values["base"], "http://127.0.0.1:18765")
        self.assertEqual(values["server_base"], values["base"])
        self.assertEqual(values["doctor_base"], values["base"])

    def test_server_binding_uses_shared_port_not_hardcoded_8765(self):
        with patch.object(server, "ThreadingHTTPServer") as factory, \
             patch.object(server.webbrowser, "open") as opener:
            factory.return_value.serve_forever.side_effect = KeyboardInterrupt()
            server.main()
        factory.assert_called_once_with(("127.0.0.1", server.PORT),
                                        server.Handler)
        opener.assert_called_once_with(server.BASE)
        factory.return_value.server_close.assert_called_once_with()

    def test_alternate_port_ui_has_no_private_oauth_relay(self):
        with patch.object(server, "PORT", 18765):
            html = server.Handler._html_home(object())
        self.assertIn('id="localPortWarning"', html)
        self.assertNotIn("__LOCAL_PORT_WARNING__", html)
        self.assertNotIn("__CSRF_PLACEHOLDER__", html)
        self.assertIn('id="vkidAppId"', html)
        self.assertNotIn('data-private-relay', html)

    def test_default_port_ui_has_no_extra_warning(self):
        with patch.object(server, "PORT", local_config.DEFAULT_PORT):
            html = server.Handler._html_home(object())
        self.assertIn('id="vkidAppId"', html)
        self.assertNotIn('data-private-relay', html)
        self.assertNotIn("__LOCAL_PORT_WARNING__", html)

    def test_server_binds_only_loopback(self):
        self.assertTrue(local_config.BASE.startswith("http://127.0.0.1:"))
        self.assertNotIn("0.0.0.0", local_config.BASE)


if __name__ == "__main__":
    unittest.main()
