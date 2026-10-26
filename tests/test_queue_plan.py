"""Queue planner v0.3.9: offline and network-free regression checks."""
import csv
import io
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from vkmgr import queue_plan as qp
from vkmgr.journal import Journal


WALL_A = "https://vk.com/wall-111_222?reply=333"
WALL_A_ALIAS = "https://vk.ru/wall-111_222?reply=333"
WALL_B = "https://vk.com/wall-111_222?reply=334"
PHOTO = "https://vk.com/photo12_24?comment=36"
VIDEO = "https://vk.com/video12_25?comment=37"


class ParsePlanTests(unittest.TestCase):
    def test_empty_source_returns_empty_summary(self):
        plan = qp.build_plan([])
        self.assertEqual(plan.links, [])
        self.assertEqual(plan.imported, 0)

    def test_validates_exact_vk_comments_and_filters_duplicates(self):
        plan = qp.build_plan([WALL_A, WALL_A_ALIAS, WALL_B, WALL_A])
        self.assertEqual(plan.imported, 4)
        self.assertEqual(plan.duplicates, 2)
        self.assertEqual(plan.links, [WALL_A, WALL_B])

    def test_wall_only_is_default_but_counts_media(self):
        plan = qp.build_plan([WALL_A, PHOTO, VIDEO, WALL_B])
        self.assertEqual(plan.links, [WALL_A, WALL_B])
        self.assertEqual(plan.unsupported, 2)
        self.assertEqual(plan.by_type,
                         {"wall": 2, "photo": 1, "video": 1})
        all_plan = qp.build_plan([WALL_A, PHOTO, VIDEO], wall_only=False)
        self.assertEqual(len(all_plan.links), 3)

    def test_rejects_bad_urls_unverified_identity_or_unscoped_journal(self):
        with self.assertRaises(ValueError):
            qp.build_plan([WALL_A, "https://elsewhere.invalid/wall-1_2?reply=3"])
        for uid in (0, -1, True, "112233445"):
            with self.subTest(uid=uid), self.assertRaises(ValueError):
                qp.build_plan([WALL_A], user_id=uid)
        with self.assertRaises(ValueError):
            qp.build_plan([WALL_A], journal=object())

    def test_reads_private_journal_and_keeps_uncertain_results_out_of_queue(self):
        with tempfile.TemporaryDirectory() as d:
            journal = Journal(Path(d) / "test.sqlite3")
            journal.record(112233445, "wall:-111:222:333", "browser",
                           "browser_unknown")
            plan = qp.build_plan([WALL_A, WALL_B], user_id=112233445,
                                 journal=journal)
            self.assertEqual(plan.links, [WALL_B])
            self.assertEqual(plan.skipped_status, 1)
            # Explicit account scoping applies even during offline planning.
            other = qp.build_plan([WALL_A, WALL_B], user_id=99999,
                                  journal=journal)
            self.assertEqual(other.links, [WALL_A, WALL_B])

    def test_large_synthetic_set_deduplicates_without_network(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("No network")), \
             patch("webbrowser.open", side_effect=AssertionError("No browser")):
            plan = qp.synthetic_demo(1000)
        self.assertEqual(len(plan.links), 1000)
        self.assertEqual(plan.duplicates, 15)
        self.assertEqual(plan.source, "DEMO_ONLY")

    def test_demo_bounds(self):
        for n in (0, 5001, -1, True, "10"):
            with self.subTest(n=n), self.assertRaises(ValueError):
                qp.synthetic_demo(n)


class LocalFileTests(unittest.TestCase):
    def test_txt_and_csv_import_same_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "urls.txt"
            path.write_text("# vk comments\n" + WALL_A + "\n" + WALL_B,
                            encoding="utf-8")
            self.assertEqual(qp.load_source(path), [WALL_A, WALL_B])
            csv_path = Path(tmp) / "export.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as io_out:
                writer = csv.DictWriter(io_out, fieldnames=("url", "status"))
                writer.writeheader()
                writer.writerow({"url": WALL_A, "status": "new"})
            self.assertEqual(qp.load_source(csv_path), [WALL_A])

    def test_rejects_unknown_extension_and_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.md"
            path.write_text(WALL_A)
            with self.assertRaisesRegex(ValueError, "VK ZIP, CSV, or TXT"):
                qp.load_source(path)
            target = Path(tmp) / "urls.txt"
            target.write_text(WALL_A)
            alias = Path(tmp) / "alias.txt"
            alias.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "symlink"):
                qp.load_source(alias)

    def test_zipped_vk_comments_direct_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "from_vkontakte.zip"
            with zipfile.ZipFile(archive, "w") as zip_out:
                zip_out.writestr("comments/comments.html",
                    '<html><body>'
                    f'<div class="comment"><a href="{WALL_A}">Comment A</a></div>'
                    f'<div class="comment"><a href="{WALL_B}">Comment B</a></div>'
                    '</body></html>')
                zip_out.writestr("messages/private.html",
                                 "Do not read unrelated content")
            plan = qp.build_plan(qp.load_source(archive))
            self.assertEqual(plan.links, [WALL_A, WALL_B])

    def test_zip_without_comments_is_not_misreported_as_empty_user_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "not-comments.zip"
            with zipfile.ZipFile(archive, "w") as zip_out:
                zip_out.writestr("index.html", "<html>No comments export</html>")
            with self.assertRaisesRegex(ValueError, "Comments"):
                qp.load_source(archive)

    def test_private_snapshot_can_be_reloaded_by_existing_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "queue.txt"
            self.assertEqual(qp.write_snapshot(path, [WALL_A, WALL_B]), path)
            self.assertEqual(path.read_text().splitlines(), [WALL_A, WALL_B])
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            from vkmgr.playwright_batch import read_url_file
            self.assertEqual(read_url_file(str(path)), [WALL_A, WALL_B])

    def test_snapshot_never_overwrites_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "queue.txt"
            path.write_text("SENSITIVE ORIGINAL DATA", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                qp.write_snapshot(path, [WALL_A])
            self.assertEqual(path.read_text(), "SENSITIVE ORIGINAL DATA")

    def test_snapshot_does_not_overwrite_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "private.txt"
            target.write_text("ORIGINAL", encoding="utf-8")
            alias = Path(tmp) / "queue.txt"
            alias.symlink_to(target)
            with self.assertRaises(FileExistsError):
                qp.write_snapshot(alias, [WALL_A])
            self.assertEqual(target.read_text(), "ORIGINAL")

    def test_empty_snapshot_prohibited(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "No links"):
                qp.write_snapshot(Path(tmp) / "empty.txt", [])


class CLITests(unittest.TestCase):
    def test_demo_never_saves_or_logs_fake_urls(self):
        output = io.StringIO()
        with patch("builtins.print", lambda *a, **kw: output.write(
                " ".join(map(str, a)) + "\n")), \
             patch.object(qp, "write_snapshot") as save, \
             patch.object(qp, "load_source") as load:
            self.assertEqual(qp.main(["--demo", "500"]), 0)
        self.assertIn("DEMO_OK", output.getvalue())
        self.assertNotIn("https://vk.com/", output.getvalue())
        save.assert_not_called()
        load.assert_not_called()

    def test_demo_cannot_accidentally_save_fake_live_queue(self):
        with patch.object(qp, "write_snapshot") as save:
            self.assertEqual(qp.main(
                ["--demo", "500", "--save", "/tmp/do-not-write.txt"]), 2)
        save.assert_not_called()

    def test_import_save_cli_without_accessing_vk_or_running_deletes(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "real-links.txt"
            dst = Path(tmp) / "private-staging.txt"
            src.write_text(WALL_A + "\n" + WALL_B + "\n", encoding="utf-8")
            with patch("urllib.request.urlopen", side_effect=AssertionError("No VK API")), \
                 patch("webbrowser.open", side_effect=AssertionError("No Chromium")), \
                 patch("vkmgr.playwright_batch.run_batch",
                       side_effect=AssertionError("No deletion")):
                self.assertEqual(qp.main([
                    "--source", str(src), "--save", str(dst),
                ]), 0)
            self.assertEqual(dst.read_text().splitlines(), [WALL_A, WALL_B])


if __name__ == "__main__":
    unittest.main()
