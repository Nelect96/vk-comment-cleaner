"""Offline account-scoped progress and safe next-five plan tests."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from vkmgr import queue_progress as progress
from vkmgr.journal import Journal

A = "https://vk.com/wall-111_222?reply=333"
B = "https://vk.com/wall-111_222?reply=334"
C = "https://vk.com/wall-111_222?reply=335"
D = "https://vk.com/wall-111_222?reply=336"
E = "https://vk.com/wall-111_222?reply=337"
F = "https://vk.com/wall-111_222?reply=338"
PHOTO = "https://vk.com/photo111_222?comment=99"
USER = 112233445


class StatusReportTests(unittest.TestCase):
    def test_all_status_groups_are_distinct(self):
        links = [
            A, B, C, D, E, F, PHOTO,
            "https://vk.ru/wall-111_222?reply=333",
        ]
        states = {
            "wall:-111:222:333": "browser_ui_removed",
            "wall:-111:222:334": "browser_user_confirmed",
            "wall:-111:222:335": "browser_unknown",
            "wall:-111:222:336": "browser_not_found",
            "wall:-111:222:337": "api_accepted",
        }
        plan = progress.calculate_progress(links, states, "Archive.zip")
        self.assertEqual(plan.total, 7)
        self.assertEqual(plan.duplicates, 1)
        self.assertEqual(plan.ui_removed, 1)
        self.assertEqual(plan.user_confirmed, 1)
        self.assertEqual(plan.unknown, 2)
        self.assertEqual(plan.not_found, 1)
        self.assertEqual(plan.not_supported, 1)
        self.assertEqual(plan.eligible, 1)
        self.assertEqual(plan.next_urls, [F])

    def test_unknown_never_rerequested_or_counted_deleted(self):
        for status in progress.UNKNOWN_STATUSES:
            with self.subTest(status=status):
                p = progress.calculate_progress(
                    [A, B], {"wall:-111:222:333": status}, "archive"
                )
                self.assertEqual(p.next_urls, [B])
                self.assertEqual(p.unknown, 1)
                self.assertEqual(p.ui_removed, 0)
                self.assertEqual(p.user_confirmed, 0)

    def test_not_found_is_not_success_and_not_in_next_batch(self):
        plan = progress.calculate_progress(
            [A, B], {"wall:-111:222:333": "browser_not_found"}, "source",
        )
        self.assertEqual(plan.not_found, 1)
        self.assertEqual(plan.eligible, 1)
        self.assertEqual(plan.next_urls, [B])
        self.assertEqual(plan.ui_removed + plan.user_confirmed, 0)

    def test_manual_and_other_blocked(self):
        plan = progress.calculate_progress(
            [A, B, C], {
                "wall:-111:222:333": "user_marked_deleted",
                "wall:-111:222:334": "api_accepted",
            }, "csv")
        self.assertEqual(plan.manual_confirmed, 1)
        self.assertEqual(plan.unknown, 1)
        self.assertEqual(plan.next_urls, [C])

    def test_next_is_bounded_to_five(self):
        links = ["https://vk.com/wall-111_222?reply=" + str(i + 100)
                 for i in range(100)]
        p = progress.calculate_progress(links, {}, "some-source", limit=5)
        self.assertEqual(p.eligible, 100)
        self.assertEqual(p.next_urls, links[:5])
        with self.assertRaisesRegex(ValueError, "1..5"):
            progress.calculate_progress(links, {}, "source", limit=6)

    def test_dedupe_full_post_identity(self):
        links = [
            "https://vk.com/wall-111_222?reply=333",
            "https://vk.com/wall-111_223?reply=333",
        ]
        p = progress.calculate_progress(links, {}, "source")
        self.assertEqual(p.total, 2)
        self.assertEqual(p.duplicates, 0)

    def test_rejects_invalid_url_and_oversized_queue(self):
        with self.assertRaisesRegex(ValueError, "invalid VK comment URL"):
            progress.calculate_progress([A, "javascript:alert(1)"], {}, "bad")
        urls = ["https://vk.com/wall-111_222?reply=" + str(1000 + n)
                for n in range(5001)]
        with self.assertRaisesRegex(ValueError, "5000"):
            progress.calculate_progress(urls, {}, "bad")


class ReadOnlyJournalTests(unittest.TestCase):
    def test_missing_journal_does_not_create_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.sqlite3"
            statuses = progress.read_journal_statuses(USER, path)
            self.assertEqual(statuses, {})
            self.assertFalse(path.exists())

    def test_actual_sqlite_account_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "progress.sqlite3"
            journal = Journal(path)
            journal.record(USER, "wall:-111:222:333", "browser", "browser_unknown")
            original = path.read_bytes()
            found = progress.read_journal_statuses(USER, path)
            self.assertEqual(found["wall:-111:222:333"], "browser_unknown")
            self.assertEqual(progress.read_journal_statuses(999999, path), {})
            self.assertEqual(path.read_bytes(), original)

    def test_reject_wrong_vk_id_and_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "progress.sqlite3"
            Journal(path)
            alias = Path(tmp) / "alias.sqlite3"
            alias.symlink_to(path)
            with self.assertRaisesRegex(ValueError, "symlink"):
                progress.read_journal_statuses(USER, alias)
            for uid in (0, True, -2, "112233445"):
                with self.subTest(uid=uid), self.assertRaises(ValueError):
                    progress.read_journal_statuses(uid, path)


class CommandTests(unittest.TestCase):
    def test_cli_reads_existing_zip_or_txt_without_vkontakte(self):
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp) / "links.txt"
            links.write_text(A + "\n" + B + "\n")
            missing_db = Path(tmp) / "never-created.sqlite3"
            output = io.StringIO()
            with patch.object(progress, "journal_path", return_value=missing_db), \
                 patch("urllib.request.urlopen",
                       side_effect=AssertionError("No network")), \
                 patch("webbrowser.open",
                       side_effect=AssertionError("No Chromium")), \
                 contextlib.redirect_stdout(output):
                rc = progress.main(["--source", str(links),
                                    "--user-id", str(USER), "--next", "1"])
            self.assertEqual(rc, 0)
            self.assertIn("Eligible for inspection: 2", output.getvalue())
            self.assertIn(A, output.getvalue())
            self.assertNotIn(B, output.getvalue())
            self.assertFalse(missing_db.exists())

    def test_next_five_saved_private_without_network_or_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp) / "source.txt"
            links.write_text("\n".join([A, B, C, D, E, F]))
            packet = Path(tmp) / "packet.txt"
            with patch.object(progress, "journal_path",
                              return_value=Path(tmp) / "missing.sqlite3"), \
                 patch("vkmgr.playwright_batch.run_batch",
                       side_effect=AssertionError("Never delete")):
                self.assertEqual(progress.main([
                    "--source", str(links), "--user-id", str(USER),
                    "--save-next", str(packet),
                ]), 0)
            self.assertEqual(packet.read_text().splitlines(), [A, B, C, D, E])
            self.assertFalse(F in packet.read_text())
            # No overwrite on second run
            self.assertEqual(progress.main([
                "--source", str(links), "--user-id", str(USER),
                "--save-next", str(packet),
            ]), 2)

    def test_json_output_excludes_urls_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "links.txt"
            src.write_text(A)
            buf = io.StringIO()
            with patch.object(progress, "journal_path",
                              return_value=Path(tmp) / "notfound.db"), \
                 contextlib.redirect_stdout(buf):
                rc = progress.main([
                    "--source", str(src), "--user-id", str(USER), "--json",
                ])
            self.assertEqual(rc, 0)
            parsed = json.loads(buf.getvalue())
            self.assertEqual(parsed["eligible"], 1)
            self.assertNotIn("next_urls", parsed)
            self.assertNotIn(A, buf.getvalue())


if __name__ == "__main__":
    unittest.main()
