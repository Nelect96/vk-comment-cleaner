"""Autonomous archive batches: no per-item prompts, no blind retries.

All 1000-comment stress tests are synthetic and never contact VK.
"""
import unittest
from pathlib import Path
from contextlib import nullcontext
from unittest.mock import MagicMock, Mock, patch

from vkmgr import playwright_batch as batch
from vkmgr.journal import Journal, BLOCK_REPEAT
import tempfile


USER = 112233445
LINK = "https://vk.com/wall-111_222?reply=333"
KEY = "wall:-111:222:333"


class OfflineJournal:
    def __init__(self):
        self.latest = {}
        self.calls = []

    def statuses(self, user_id, keys):
        return {key: self.latest[(user_id, key)]
                for key in keys if (user_id, key) in self.latest}

    def record(self, user_id, key, operation, status):
        self.latest[user_id, key] = status
        self.calls.append((user_id, key, operation, status))


def fake_browser(url):
    cm = MagicMock()
    page = Mock()
    page.url = url
    cm.__enter__.return_value.chromium.launch_persistent_context.return_value.pages = [page]
    return cm, page


class TombstoneEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.target = batch.create_batch(
            [LINK], USER, 1, OfflineJournal(), echo=lambda _: None)[0]

    def row(self, text, restore_link=True, restore_button=False):
        row = Mock()
        row.count.return_value = 1
        row.is_visible.return_value = True
        row.inner_text.return_value = text
        link = Mock()
        link.count.return_value = int(restore_link)
        link.is_visible.return_value = restore_link
        button = Mock()
        button.count.return_value = int(restore_button)
        button.is_visible.return_value = restore_button
        row.get_by_role.side_effect = lambda role, name: (
            link if role == "link" else button
        )
        page = Mock()
        page.url = LINK
        page.locator.return_value = row
        return page

    def test_explicit_deleted_text_plus_restore_control(self):
        page = self.row("Комментарий удалён.\nВосстановить")
        self.assertTrue(batch.explicit_deleted_marker(page, self.target))

    def test_yo_and_e_variants(self):
        for text in ("Комментарий удален.\nВосстановить",
                     "Комментарий удалён\nВосстановить",
                     "Комментарий удален. Восстановить"):
            with self.subTest(text=text):
                self.assertTrue(batch.explicit_deleted_marker(
                    self.row(text, restore_button=True, restore_link=False),
                    self.target
                ))

    def test_no_restore_action_never_confirms_deletion(self):
        page = self.row("Комментарий удалён.", restore_link=False)
        self.assertFalse(batch.explicit_deleted_marker(page, self.target))

    def test_not_deleted_text_cannot_be_faked_by_restore_action(self):
        page = self.row("Оригинальный комментарий не удалён.\nВосстановить")
        self.assertFalse(batch.explicit_deleted_marker(page, self.target))

    def test_sibling_restore_outside_target_row_is_ignored(self):
        page = self.row("Оригинальный комментарий", restore_link=False)
        self.assertFalse(batch.explicit_deleted_marker(page, self.target))

    def test_redirected_to_different_post_never_confirms(self):
        page = self.row("Комментарий удалён.\nВосстановить")
        page.url = "https://vk.ru/wall-999_888?reply=333"
        self.assertFalse(batch.explicit_deleted_marker(page, self.target))
        page.locator.assert_not_called()

    def test_duplicate_target_row_never_confirms(self):
        page = self.row("Комментарий удален.\nВосстановить")
        page.locator.return_value.count.return_value = 2
        self.assertFalse(batch.explicit_deleted_marker(page, self.target))

    def test_invisible_target_row_never_confirms(self):
        page = self.row("Комментарий удален.\nВосстановить")
        page.locator.return_value.is_visible.return_value = False
        self.assertFalse(batch.explicit_deleted_marker(page, self.target))

    def test_auto_click_writes_explicit_ui_marker_only(self):
        page = Mock()
        page.url = LINK
        row = Mock()
        button = Mock()
        journal = OfflineJournal()
        with patch.object(batch, "open_target_with_login_retry") as open_target, \
             patch.object(batch, "locate_exact_comment",
                          return_value=('id="112233445_333"', row)), \
             patch.object(batch, "author_proof", return_value=(True, [USER])), \
             patch.object(batch, "direct_delete_button", return_value=button), \
             patch.object(batch, "explicit_deleted_marker", return_value=True), \
             patch.object(batch, "observed_ui_result",
                          side_effect=AssertionError("no fallback necessary")):
            result = batch.delete_one_in_open_page(
                page, self.target, journal, automatic=True,
                echo=lambda _: None,
            )
        self.assertEqual(result, "browser_ui_deleted_marker")
        self.assertEqual(button.click.call_count, 1)
        self.assertEqual(open_target.call_args.kwargs["ask"]("some prompt"), "STOP")
        self.assertEqual([x[3] for x in journal.calls],
                         ["browser_pending", "browser_ui_deleted_marker"])
        self.assertIn("browser_ui_deleted_marker", BLOCK_REPEAT)

    def test_auto_without_tombstone_records_unknown_even_if_row_disappeared(self):
        page = Mock()
        page.url = LINK
        row = Mock()
        button = Mock()
        journal = OfflineJournal()
        with patch.object(batch, "open_target_with_login_retry"), \
             patch.object(batch, "locate_exact_comment",
                          return_value=('id="112233445_333"', row)), \
             patch.object(batch, "author_proof", return_value=(True, [USER])), \
             patch.object(batch, "direct_delete_button", return_value=button), \
             patch.object(batch, "explicit_deleted_marker", return_value=False), \
             patch.object(batch, "observed_ui_result",
                          return_value="browser_ui_removed"):
            with self.assertRaises(batch.PostClickUnverified):
                batch.delete_one_in_open_page(
                    page, self.target, journal, automatic=True, echo=lambda _: None
                )
        self.assertEqual(button.click.call_count, 1)
        self.assertEqual([x[3] for x in journal.calls],
                         ["browser_pending", "browser_unknown"])


class OneThousandOfflineTests(unittest.TestCase):
    def links(self, count=1000):
        return ["https://vk.com/wall-111_222?reply=" + str(100000 + i)
                for i in range(count)]

    def test_create_1000_links_only_with_explicit_automatic_mode(self):
        j = OfflineJournal()
        with self.assertRaises(ValueError):
            batch.create_batch(self.links(), USER, 1000, j, echo=lambda _: None)
        chosen = batch.create_batch(
            self.links(), USER, 1000, j, echo=lambda _: None, automatic=True)
        self.assertEqual(len(chosen), 1000)
        self.assertEqual(chosen[0].comment_id, 100000)
        self.assertEqual(chosen[-1].comment_id, 100999)

    def test_refuses_more_than_1000_and_invalid_zero(self):
        for limit in (0, 1001, -1, True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                batch.create_batch(
                    self.links(1001), USER, limit, OfflineJournal(),
                    automatic=True, echo=lambda _: None
                )

    def test_auto_successful_1000_needs_only_one_approval_and_zero_enter(self):
        journal = OfflineJournal()
        links = self.links()
        targets = batch.create_batch(
            links, USER, 1000, journal, automatic=True, echo=lambda _: None)
        cm, page = fake_browser(links[0])
        prompts = []
        processed = []

        def fake_delete(_page, target, store, echo=print, automatic=False):
            self.assertTrue(automatic)
            processed.append(target.comment_id)
            store.record(target.user_id, target.key, "browser",
                         "browser_ui_deleted_marker")
            return "browser_ui_deleted_marker"

        with patch.object(batch, "local_browser_profile",
                          return_value=Path("/tmp/fake-chromium")), \
             patch.object(batch, "delete_one_in_open_page", side_effect=fake_delete), \
             patch.object(batch.time, "sleep") as sleep:
            result = batch.run_batch(
                targets, journal,
                ask=lambda p: prompts.append(p) or "DELETE BATCH 1000",
                echo=lambda _: None, driver=lambda: cm,
                automatic=True
            )
        self.assertEqual(len(processed), 1000)
        self.assertEqual(len(set(processed)), 1000)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(result["ui_removed"], 1000)
        self.assertFalse(result["cancelled"])
        self.assertEqual(sleep.call_count, 999)
        self.assertEqual(len(journal.calls), 1000)

        again = batch.create_batch(links, USER, 1000, journal,
                                   echo=lambda _: None, automatic=True)
        self.assertEqual(again, [])

    def test_auto_unknown_continues_all_targets_without_manual_confirmation(self):
        journal = OfflineJournal()
        links = self.links(3)
        targets = batch.create_batch(
            links, USER, 3, journal, automatic=True, echo=lambda _: None)
        cm, page = fake_browser(links[0])
        clicked = []
        prompts = []

        def fake_delete(_page, target, store, echo=print, automatic=False):
            self.assertTrue(automatic)
            clicked.append(target.comment_id)
            if target.comment_id in (100000, 100002):
                store.record(target.user_id, target.key, "browser",
                             "browser_unknown")
                raise batch.PostClickUnverified("not confirmed")
            store.record(target.user_id, target.key, "browser",
                         "browser_ui_deleted_marker")
            return "browser_ui_deleted_marker"

        with patch.object(batch, "local_browser_profile",
                          return_value=Path("/tmp/fake-chromium")), \
             patch.object(batch, "delete_one_in_open_page",
                          side_effect=fake_delete), \
             patch.object(batch.time, "sleep") as sleep:
            result = batch.run_batch(
                targets, journal,
                ask=lambda p: prompts.append(p) or "DELETE BATCH 3",
                echo=lambda _: None, driver=lambda: cm, automatic=True,
            )
        self.assertEqual(clicked, [100000, 100001, 100002])
        self.assertEqual(len(prompts), 1)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(result["unknown"], 2)
        self.assertEqual(result["ui_removed"], 1)
        self.assertEqual(result["attempted"], 3)
        self.assertEqual(journal.statuses(USER, [targets[0].key]).get(targets[0].key),
                         "browser_unknown")
        self.assertEqual(journal.statuses(USER, [targets[2].key]).get(targets[2].key),
                         "browser_unknown")
        self.assertEqual(
            batch.create_batch(links, USER, 3, journal,
                               automatic=True, echo=lambda _: None), [])

    def test_auto_unknown_requires_durable_journal_checkpoint_to_continue(self):
        journal = OfflineJournal()
        links = self.links(2)
        targets = batch.create_batch(
            links, USER, 2, journal, automatic=True, echo=lambda _: None)
        cm, page = fake_browser(links[0])
        clicked = []

        def broken_delete(_page, target, store, echo=print, automatic=False):
            clicked.append(target.comment_id)
            # Deliberately simulate a bug that loses the no-repeat checkpoint.
            raise batch.PostClickUnverified("missing journal")

        with patch.object(batch, "local_browser_profile",
                          return_value=Path("/tmp/fake-chromium")), \
             patch.object(batch, "delete_one_in_open_page",
                          side_effect=broken_delete):
            with self.assertRaisesRegex(RuntimeError, "browser_unknown"):
                batch.run_batch(
                    targets, journal, automatic=True,
                    ask=lambda _: "DELETE BATCH 2",
                    echo=lambda _: None, driver=lambda: cm,
                )
        self.assertEqual(clicked, [100000])

    def test_auto_wrong_phrase_never_launches_browser(self):
        targets = batch.create_batch(
            self.links(4), USER, 4, OfflineJournal(),
            automatic=True, echo=lambda _: None)
        with patch.object(batch, "local_browser_profile") as profile:
            result = batch.run_batch(
                targets, OfflineJournal(), automatic=True,
                ask=lambda _: "DELETE BATCH 3", echo=lambda _: None,
                driver=lambda: self.fail("No browser")
            )
        self.assertTrue(result["cancelled"])
        profile.assert_not_called()

    def test_auto_cannot_use_pause_between_and_no_zero_delay(self):
        targets = batch.create_batch(
            self.links(1), USER, 1, OfflineJournal(),
            automatic=True, echo=lambda _: None)
        with self.assertRaisesRegex(ValueError, "cannot pause"):
            batch.run_batch(targets, OfflineJournal(),
                            automatic=True, pause_between=True)
        with self.assertRaisesRegex(ValueError, "at least a 1-second delay"):
            batch.run_batch(targets, OfflineJournal(),
                            automatic=True, cooldown=0)

    def test_not_found_marks_for_review_no_delete_and_automatically_moves_on(self):
        journal = OfflineJournal()
        links = self.links(2)
        targets = batch.create_batch(
            links, USER, 2, journal, automatic=True, echo=lambda _: None)
        cm, page = fake_browser(links[0])
        deleted = []
        def fake_delete(_page, target, store, echo=print, automatic=False):
            if target.comment_id == 100000:
                raise batch.TargetNotFound("Missing before clicking")
            deleted.append(target.comment_id)
            store.record(target.user_id, target.key, "browser",
                         "browser_ui_deleted_marker")
            return "browser_ui_deleted_marker"
        with patch.object(batch, "local_browser_profile",
                          return_value=Path("/tmp/fake-chromium")), \
             patch.object(batch, "delete_one_in_open_page",
                          side_effect=fake_delete), \
             patch.object(batch.time, "sleep"):
            result = batch.run_batch(
                targets, journal, automatic=True,
                ask=lambda _: "DELETE BATCH 2",
                echo=lambda _: None, driver=lambda: cm,
            )
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["ui_removed"], 1)
        self.assertEqual(deleted, [100001])
        self.assertEqual(journal.statuses(USER, [targets[0].key]),
                         {targets[0].key: "browser_not_found"})

    def test_real_sqlite_does_not_repeat_success_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Journal(Path(tmp) / "progress.sqlite3")
            journal.record(USER, KEY, "browser", "browser_ui_deleted_marker")
            remaining = batch.create_batch(
                [LINK], USER, 1, journal, echo=lambda _: None, automatic=True,
            )
            self.assertEqual(remaining, [])


class AutoLoginTests(unittest.TestCase):
    def test_auto_login_polls_without_keyboard_after_user_authenticated(self):
        id_results = iter([
            (None, "not yet"), (None, "not yet"),
            ({"id": USER, "source": "VK ID"}, ""),
        ])
        ask = Mock(side_effect=AssertionError("No Enter prompts"))
        with patch.object(batch.time, "sleep") as sleep:
            result, user = batch.require_authenticated_browser_identity(
                5, True, ask=ask, open_ui=lambda _: None,
                echo=lambda _: None, retrieve=lambda _: ([], None),
                identity=lambda _: next(id_results),
                automatic=True, max_checks=3,
            )
        self.assertEqual(user["id"], USER)
        self.assertEqual(sleep.call_count, 2)
        ask.assert_not_called()

    def test_auto_login_timeout_no_vk_delete(self):
        with patch.object(batch.time, "sleep"):
            with self.assertRaisesRegex(ValueError, "3 VK ID checks"):
                batch.require_authenticated_browser_identity(
                    5, True, ask=lambda _: self.fail("Must not prompt"),
                    open_ui=lambda _: None, echo=lambda _: None,
                    retrieve=lambda _: ([], None),
                    identity=lambda _: (None, "not yet"),
                    automatic=True, max_checks=3,
                )


class AutoCLITests(unittest.TestCase):
    def test_requires_opt_in_execute_before_any_server_or_browser(self):
        with patch.object(batch, "_start_server") as start, \
             patch.object(batch, "require_authenticated_browser_identity") as auth:
            self.assertEqual(batch.main([
                "--auto", "--limit", "1000",
                "--source", "/never-read-archive.zip",
            ]), 1)
        start.assert_not_called()
        auth.assert_not_called()

    def test_cannot_combine_manual_pause_with_auto(self):
        with patch.object(batch, "_start_server") as start:
            self.assertEqual(batch.main([
                "--auto", "--execute", "--limit", "1000",
                "--source", "/never-read-archive.zip", "--pause-between"
            ]), 1)
        start.assert_not_called()

    def test_auto_requires_full_explicit_file_before_server_starts(self):
        with patch.object(batch, "_start_server") as start:
            self.assertEqual(batch.main([
                "--auto", "--execute", "--limit", "1000",
            ]), 1)
        start.assert_not_called()

    def test_review_not_found_requires_auto_and_source_before_starting(self):
        with patch.object(batch, "_start_server") as start:
            self.assertEqual(batch.main([
                "--review-not-found", "--execute", "--limit", "5",
                "--file", "/no-such-file.txt",
            ]), 1)
            self.assertEqual(batch.main([
                "--review-not-found", "--auto", "--execute",
                "--limit", "1000",
            ]), 1)
        start.assert_not_called()

    def test_review_not_found_only_passes_unclicked_misses_to_worker(self):
        links = [
            "https://vk.com/wall-111_222?reply=333",
            "https://vk.com/wall-111_222?reply=334",
            "https://vk.com/wall-111_222?reply=335",
        ]
        j = OfflineJournal()
        j.record(USER, "wall:-111:222:333", "browser", "browser_not_found")
        j.record(USER, "wall:-111:222:334", "browser", "browser_unknown")
        with patch("vkmgr.queue_plan.load_source", return_value=links), \
             patch.object(batch, "_start_server", return_value=None), \
             patch.object(batch, "require_authenticated_browser_identity",
                          return_value=([], {"id": USER, "source": "VK ID"})), \
             patch.object(batch, "Journal", return_value=j), \
             patch.object(batch, "automatic_process_lock",
                          side_effect=lambda: nullcontext()), \
             patch.object(batch, "run_batch", return_value={
                 "ui_removed": 1, "user_confirmed": 0,
                 "skipped": 0, "attempted": 1, "cancelled": False,
             }) as runner:
            code = batch.main([
                "--auto", "--execute", "--review-not-found",
                "--limit", "1000", "--source", "Archive.zip",
            ])
        self.assertEqual(code, 0)
        self.assertEqual([t.comment_id for t in runner.call_args.args[0]], [333])
        self.assertEqual(runner.call_args.kwargs, {
            "automatic": True, "review_not_found": True
        })

    def test_auto_archive_uses_full_source_and_skips_existing_journal(self):
        urls = [
            "https://vk.com/wall-111_222?reply=333",
            "https://vk.com/wall-111_222?reply=334",
        ]
        j = OfflineJournal()
        j.record(USER, KEY, "browser", "browser_user_confirmed")
        with patch("vkmgr.queue_plan.load_source", return_value=urls), \
             patch.object(batch, "_start_server", return_value=None), \
             patch.object(batch, "require_authenticated_browser_identity",
                          return_value=([], {"id": USER, "source": "VK ID"})) as login, \
             patch.object(batch, "Journal", return_value=j), \
             patch.object(batch, "automatic_process_lock",
                          side_effect=lambda: nullcontext()), \
             patch.object(batch, "run_batch", return_value={
                 "ui_removed": 1, "user_confirmed": 0,
                 "skipped": 0, "attempted": 1, "cancelled": False,
             }) as runner:
            result = batch.main([
                "--auto", "--execute", "--limit", "1000",
                "--source", "Archive.zip",
            ])
        self.assertEqual(result, 0)
        self.assertEqual(login.call_args.kwargs, {
            "urls_supplied": True, "automatic": True
        })
        chosen = runner.call_args.args[0]
        self.assertEqual([x.comment_id for x in chosen], [334])
        self.assertEqual(runner.call_args.kwargs, {"automatic": True})

    def test_limits_auto_1000_before_attempts(self):
        with patch.object(batch, "_start_server") as start:
            self.assertEqual(batch.main([
                "--auto", "--execute", "--limit", "1001",
                "--source", "/never-read.zip",
            ]), 1)
        start.assert_not_called()

    def test_existing_five_only_mode_is_unchanged(self):
        with patch.object(batch, "_start_server") as start:
            self.assertEqual(batch.main([
                "--execute", "--limit", "1000",
                "--file", "old-batch.txt",
            ]), 1)
        start.assert_not_called()



class ConcurrentWorkerLockTests(unittest.TestCase):
    def test_refuse_parallel_automatic_worker_and_release_on_exit(self):
        from vkmgr.auto_lock import automatic_process_lock
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "batch.lock"
            with automatic_process_lock(path):
                with self.assertRaisesRegex(ValueError, "Another automatic VKCM batch"):
                    with automatic_process_lock(path):
                        self.fail("Concurrent work must never start")
            with automatic_process_lock(path):
                self.assertTrue(path.exists())

    def test_refuse_symlinked_lock_file(self):
        from vkmgr.auto_lock import automatic_process_lock
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real.lock"
            path.write_text("")
            alias = Path(tmp) / "batch.lock"
            alias.symlink_to(path)
            with self.assertRaisesRegex(ValueError, "symlink"):
                with automatic_process_lock(alias):
                    self.fail("Must not acquire symlink")

    def test_private_lock_permissions(self):
        from vkmgr.auto_lock import automatic_process_lock
        import stat
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "private.lock"
            with automatic_process_lock(path):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)



if __name__ == "__main__":
    unittest.main()
