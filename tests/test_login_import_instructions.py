"""Verify that login instructions follow source availability, not port number."""
import unittest
from unittest.mock import patch

from vkmgr import playwright_batch as batch


class LoginSourceGuidanceTests(unittest.TestCase):
    def test_already_supplied_archive_never_requires_reimport(self):
        for port in (batch.DEFAULT_PORT, 18765):
            with self.subTest(port=port), patch.object(batch, "PORT", port):
                responses = iter([
                    (None, "pending"),
                    ({"id": 1234567, "source": "VK ID"}, ""),
                ])
                messages = []
                rows, user = batch.require_authenticated_browser_identity(
                    1, True, ask=lambda _: "",
                    echo=messages.append,
                    open_ui=lambda _: None,
                    retrieve=lambda _: ([], None),
                    identity=lambda _: next(responses),
                )
                self.assertEqual(rows, [])
                self.assertEqual(user["id"], 1234567)
                self.assertTrue(any("No need to reimport" in m for m in messages))
                self.assertFalse(any("import a ZIP archive" in m for m in messages))

    def test_missing_source_shows_import_help(self):
        messages = []
        with patch.object(batch, "PORT", batch.DEFAULT_PORT):
            with self.assertRaisesRegex(ValueError, "Cancelled"):
                batch.require_authenticated_browser_identity(
                    1, False, ask=lambda _: "STOP",
                    echo=messages.append,
                    open_ui=lambda _: None,
                    retrieve=lambda _: ([], None),
                    identity=lambda _: (None, "pending"),
                )
        self.assertTrue(any("import a ZIP archive" in m for m in messages))


if __name__ == "__main__":
    unittest.main()
