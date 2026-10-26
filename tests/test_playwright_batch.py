"""Offline tests for VKCM v0.3.8 guarded browser queue.

No real VK login, network request or destructive action is performed.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from vkmgr import playwright_batch as batch


class LocalJournal:
    """Lightweight fake with the same account-scoped semantics as SQLite Journal.

    Tests historically use .latest for the authenticated test user 112233445.
    A lookup under another user's ID must NEVER see those entries.
    """
    DEFAULT_USER_ID = 112233445

    def __init__(self, statuses=None):
        self.latest = dict(statuses or {})
        self.other_users = {}
        self.calls = []

    def statuses(self, user_id, keys):
        bucket = (self.latest if user_id == self.DEFAULT_USER_ID
                  else self.other_users.get(user_id, {}))
        return {k: bucket[k] for k in keys if k in bucket}

    def record(self, user_id, key, operation, status):
        self.calls.append((user_id, key, operation, status))
        bucket = (self.latest if user_id == self.DEFAULT_USER_ID
                  else self.other_users.setdefault(user_id, {}))
        bucket[key] = status


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.url1 = "https://vk.com/wall-111222333_1556?reply=1564&thread=1562"
        self.url2 = "https://vk.ru/wall-111222333_1556?reply=1565&thread=1562"
        self.url3 = "https://vk.com/wall-111222333_1556?reply=1566"
        self.user = 112233445

    def test_skips_previous_unknown_test_and_preserves_order(self):
        existing = LocalJournal({"wall:-111222333:1556:1564": "browser_unknown"})
        echoed = []
        actual = batch.create_batch(
            [self.url1, self.url2, self.url3], self.user, 3, existing, echoed.append,
        )
        self.assertEqual([x.comment_id for x in actual], [1565, 1566])
        self.assertEqual([x.user_id for x in actual], [self.user, self.user])
        self.assertTrue(any("no retries" in x for x in echoed))

    def test_filters_duplicates_and_photo_comments(self):
        journal = LocalJournal()
        rows = [self.url2, self.url2.replace("vk.ru", "vk.com"),
                "https://vk.com/photo112233445_22?comment=33", self.url3]
        actual = batch.create_batch(rows, self.user, 5, journal)
        self.assertEqual([x.comment_id for x in actual], [1565, 1566])

    def test_invalid_id_limit_and_link_fail_closed(self):
        for count in (0, 6, -1):
            with self.subTest(count=count), self.assertRaises(ValueError):
                batch.create_batch([self.url2], self.user, count, LocalJournal())
        for user in (None, 0, -1, "112233445"):
            with self.subTest(user=user), self.assertRaises(ValueError):
                batch.create_batch([self.url2], user, 1, LocalJournal())
        with self.assertRaises(ValueError):
            batch.create_batch(["https://evil.example/post?reply=1"],
                               self.user, 1, LocalJournal())

    def test_file_txt_and_vkmgr_csv(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / "urls.txt"
            file.write_text("# comments\n\n" + self.url2 + "\n", encoding="utf-8")
            self.assertEqual(batch.read_url_file(str(file)), [self.url2])
            csv = Path(temp) / "vk-comments.csv"
            csv.write_text("type,url,date,text_preview,status,source\n"
                           "wall,\"" + self.url2 + "\",,,,archive\n",
                           encoding="utf-8-sig")
            self.assertEqual(batch.read_url_file(str(csv)), [self.url2])

    def test_bad_csv_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / "test.csv"
            file.write_text("wrong,header\na,b\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "url column"):
                batch.read_url_file(str(file))

    def test_page_context_must_stay_on_same_post(self):
        page = Mock()
        page.url = "https://vk.ru/wall-111222333_1556"
        target = batch.create_batch([self.url2], self.user, 1, LocalJournal())[0]
        self.assertTrue(batch.same_parent_visible(page, target))
        page.url = "https://vk.ru/wall-111222333_15560"
        self.assertFalse(batch.same_parent_visible(page, target))
        page.url = "https://vk.ru/feed"
        self.assertFalse(batch.same_parent_visible(page, target))


class DeleteOneTests(unittest.TestCase):
    def setUp(self):
        self.target = batch.create_batch([
            "https://vk.com/wall-111222333_1556?reply=1565"
        ], 112233445, 1, LocalJournal())[0]

    def _verified(self, outcome="browser_ui_removed", click_error=None):
        page = Mock()
        page.url = self.target.url
        row = Mock()
        button = Mock()
        if click_error is not None:
            button.click.side_effect = click_error
        journal = LocalJournal()
        patches = [
            patch.object(batch, "open_target_with_login_retry"),
            patch.object(batch, "locate_exact_comment",
                         return_value=('[id="112233445_1565"]', row)),
            patch.object(batch, "author_proof", return_value=(True, [112233445])),
            patch.object(batch, "direct_delete_button", return_value=button),
            patch.object(batch, "observed_ui_result", return_value=outcome),
        ]
        return page, journal, button, patches

    def test_one_click_and_ui_only_journal(self):
        page, journal, button, patches = self._verified()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            result = batch.delete_one_in_open_page(page, self.target, journal,
                                                   echo=lambda x: None)
        self.assertEqual(result, "browser_ui_removed")
        self.assertEqual(button.click.call_count, 1)
        self.assertEqual([x[3] for x in journal.calls],
                         ["browser_pending", "browser_ui_removed"])

    def test_single_transient_disappearance_cannot_count_as_deleted(self):
        page, journal, button, patches = self._verified()
        observed = ["browser_ui_removed", "unknown", "unknown", "unknown"]
        with patches[0], patches[1], patches[2], patches[3], \
             patch.object(batch, "observed_ui_result", side_effect=observed) as ui:
            with self.assertRaises(batch.PostClickUnverified):
                batch.delete_one_in_open_page(
                    page, self.target, journal, echo=lambda _: None,
                )
        self.assertEqual(button.click.call_count, 1)
        self.assertEqual(ui.call_count, 4)
        self.assertEqual([c[3] for c in journal.calls],
                         ["browser_pending", "browser_unknown"])

    def test_two_consecutive_same_post_ui_absences_are_required(self):
        page, journal, button, patches = self._verified()
        with patches[0], patches[1], patches[2], patches[3], \
             patch.object(batch, "observed_ui_result",
                          side_effect=["unknown", "browser_ui_removed",
                                       "browser_ui_removed"]) as ui:
            outcome = batch.delete_one_in_open_page(
                page, self.target, journal, echo=lambda _: None,
            )
        self.assertEqual(outcome, "browser_ui_removed")
        self.assertEqual(ui.call_count, 3)
        self.assertEqual(button.click.call_count, 1)
        self.assertEqual([c[3] for c in journal.calls],
                         ["browser_pending", "browser_ui_removed"])

    def test_unknown_ui_stops_even_if_click_returned(self):
        page, journal, button, patches = self._verified(outcome="unknown")
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with self.assertRaisesRegex(batch.PostClickUnverified, "cannot confirm"):
                batch.delete_one_in_open_page(page, self.target, journal,
                                              echo=lambda x: None)
        self.assertEqual(button.click.call_count, 1)
        self.assertEqual([x[3] for x in journal.calls],
                         ["browser_pending", "browser_unknown"])

    def test_click_exception_keeps_unknown_and_never_retries(self):
        page, journal, button, patches = self._verified(
            click_error=RuntimeError("browser click timeout"))
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with self.assertRaises(RuntimeError):
                batch.delete_one_in_open_page(page, self.target, journal,
                                              echo=lambda x: None)
        self.assertEqual(button.click.call_count, 1)
        self.assertEqual([x[3] for x in journal.calls],
                         ["browser_pending", "browser_unknown"])

    def test_wrong_author_no_record_and_no_click(self):
        page, journal, button, patches = self._verified()
        with patches[0], patches[1], patch.object(
                batch, "author_proof", return_value=(False, [1234])), patches[3], patches[4]:
            with self.assertRaisesRegex(ValueError, "not verified"):
                batch.delete_one_in_open_page(page, self.target, journal,
                                              echo=lambda x: None)
        self.assertEqual(journal.calls, [])
        button.click.assert_not_called()

    def test_prior_unknown_blocks_click_even_when_dom_valid(self):
        page, journal, button, patches = self._verified()
        journal.latest[self.target.key] = "browser_unknown"
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with self.assertRaisesRegex(ValueError, "second click"):
                batch.delete_one_in_open_page(page, self.target, journal,
                                              echo=lambda x: None)
        button.click.assert_not_called()
        self.assertFalse(journal.calls)

    def test_dom_result_without_same_page_never_claims_removed(self):
        page = Mock()
        page.url = "https://vk.ru/feed"
        page.locator.return_value.count.return_value = 0
        self.assertEqual(batch.observed_ui_result(page, self.target), "unknown")
        page.url = "https://vk.ru/wall-111222333_1556"
        self.assertEqual(batch.observed_ui_result(page, self.target),
                         "browser_ui_removed")
        page.locator.return_value.count.return_value = 1
        page.locator.return_value.is_visible.return_value = True
        self.assertEqual(batch.observed_ui_result(page, self.target), "unknown")


class BrowserLoginTests(unittest.TestCase):
    def test_oauth_only_identity_when_legacy_api_is_restricted(self):
        user, message = batch.authenticated_browser_identity(
            None, read=lambda _: {
                "vkid_user": {"id": 112233445},
                "legacy_api": False,
                "message": "VK ID confirmed, legacy API unavailable",
            }
        )
        self.assertEqual(user, {"id": 112233445, "source": "VK ID"})
        self.assertEqual(message, "")

    def test_rejects_mismatched_legacy_and_vkid_identity(self):
        with self.assertRaisesRegex(ValueError, "different accounts"):
            batch.authenticated_browser_identity(
                {"id": 112233445},
                read=lambda _: {"vkid_user": {"id": 7654321}}
            )

    def test_falls_back_to_legacy_own_identity(self):
        user, _ = batch.authenticated_browser_identity(
            {"id": 112233445},
            read=lambda _: {"vkid_user": None, "message": "no VK ID"}
        )
        self.assertEqual(user, {"id": 112233445, "source": "VK API"})

    def test_bool_nonpositive_and_string_user_ids_rejected(self):
        for uid in (True, False, "112233445", -1, 0):
            with self.subTest(uid=uid), self.assertRaisesRegex(ValueError, "Invalid VK ID"):
                batch.authenticated_browser_identity(
                    None, read=lambda _: {"vkid_user": {"id": uid}}
                )

    def test_empty_enter_before_auth_does_not_start_browser(self):
        calls = []
        def retrieve(limit):
            return [], None
        def identity(_):
            calls.append("auth-checked")
            return None, "PKCE not finished"
        prompts = iter(["", "STOP"])
        opened = []
        messages = []
        with self.assertRaisesRegex(ValueError, "Cancelled"):
            batch.require_authenticated_browser_identity(
                2, True, ask=lambda _: next(prompts),
                open_ui=lambda url: opened.append(url), echo=messages.append,
                retrieve=retrieve, identity=identity,
            )
        self.assertEqual(opened, [batch.BASE])
        self.assertEqual(calls, ["auth-checked", "auth-checked"])
        self.assertTrue(any("not yet verified" in line for line in messages))

    def test_login_on_second_confirmation_succeeds_without_imported_links(self):
        identity_calls = iter([
            (None, "not yet"),
            (None, "not yet"),
            ({"id": 112233445, "source": "VK ID"}, ""),
        ])
        observed = []
        links = ["https://vk.com/wall-600000001_41001?reply=41002"]
        # Text-file mode has links already, local UI list can stay empty.
        rows, user = batch.require_authenticated_browser_identity(
            2, True, ask=lambda _: "", echo=observed.append,
            open_ui=lambda _: None, retrieve=lambda _: ([], None),
            identity=lambda _: next(identity_calls)
        )
        self.assertEqual(rows, [])
        self.assertEqual(user["id"], 112233445)
        self.assertTrue(any("no need" in msg.casefold() for msg in observed))
        self.assertTrue(any("Account verified" in msg for msg in observed))

    def test_all_six_failed_login_checks_abort_without_browser_actions(self):
        with self.assertRaisesRegex(ValueError, "6 VK ID checks"):
            batch.require_authenticated_browser_identity(
                2, True, ask=lambda _: "", open_ui=lambda _: None,
                echo=lambda _: None, retrieve=lambda _: ([], None),
                identity=lambda _: (None, "invalid callback")
            )

    def test_previously_authenticated_skips_browser_login(self):
        opened = []
        rows, user = batch.require_authenticated_browser_identity(
            2, True, open_ui=lambda url: opened.append(url),
            echo=lambda _: None, retrieve=lambda _: ([], {"id": 112233445}),
            identity=lambda _: ({"id": 112233445, "source": "VK ID"}, "")
        )
        self.assertEqual(rows, [])
        self.assertEqual(user["id"], 112233445)
        self.assertEqual(opened, [])


class AuthGateMainTests(unittest.TestCase):
    def test_no_browser_deletion_when_vk_id_is_not_confirmed(self):
        urls = ["https://vk.com/wall-600000001_41001?reply=41002"]
        with patch.object(batch, "_start_server", return_value=None), \
             patch.object(batch, "read_url_file", return_value=urls), \
             patch.object(batch, "require_authenticated_browser_identity",
                          side_effect=ValueError("VK ID not verified")), \
             patch.object(batch, "run_batch") as destructive:
            self.assertEqual(batch.main([
                "--execute", "--limit", "1", "--file", "urls.txt",
            ]), 1)
        destructive.assert_not_called()

    def test_confirmed_vk_id_without_legacy_user_can_process_file(self):
        urls = ["https://vk.com/wall-600000001_41001?reply=41002"]
        journal = LocalJournal()
        with patch.object(batch, "_start_server", return_value=None), \
             patch.object(batch, "read_url_file", return_value=urls), \
             patch.object(batch, "require_authenticated_browser_identity",
                          return_value=([], {"id": 112233445, "source": "VK ID"})), \
             patch.object(batch, "Journal", return_value=journal), \
             patch.object(batch, "run_batch", return_value={
                 "ui_removed": 1, "user_confirmed": 0,
                 "attempted": 1, "skipped": 0, "cancelled": False,
             }) as destructive:
            self.assertEqual(batch.main([
                "--execute", "--limit", "1", "--file", "urls.txt",
            ]), 0)
        self.assertEqual(destructive.call_count, 1)
        self.assertEqual(destructive.call_args.args[0][0].user_id, 112233445)


class ReconciliationCLITests(unittest.TestCase):
    def test_reconciliation_before_queue_selection_removes_already_clicked_item(self):
        links = [
            "https://vk.com/wall-600000001_41001?reply=41002",
            "https://vk.com/wall-600000002_42001?reply=42002",
        ]
        previous = LocalJournal({
            "wall:-600000001:41001:41002": "browser_unknown",
        })
        observed = []
        with patch.object(batch, "_start_server", return_value=None), \
             patch.object(batch, "read_url_file", return_value=links), \
             patch.object(batch, "require_authenticated_browser_identity",
                          return_value=([], {"id": 112233445, "source": "VK ID"})), \
             patch.object(batch, "Journal", return_value=previous), \
             patch("builtins.input", return_value="CONFIRM DELETED 41002"), \
             patch.object(batch, "run_batch", return_value={
                 "ui_removed": 1, "user_confirmed": 0,
                 "attempted": 1, "skipped": 0, "cancelled": False,
             }) as runner:
            result = batch.main([
                "--execute", "--limit", "2", "--file", "two-comments.txt",
                "--confirm-deleted", links[0],
            ])
            observed.extend(runner.call_args.args[0])
        self.assertEqual(result, 0)
        self.assertEqual([t.comment_id for t in observed], [42002])
        self.assertEqual(previous.latest["wall:-600000001:41001:41002"],
                         "browser_user_confirmed")

    def test_preview_cannot_mutate_journal_through_reconcile(self):
        links = ["https://vk.com/wall-600000001_41001?reply=41002"]
        previous = LocalJournal({
            "wall:-600000001:41001:41002": "browser_unknown",
        })
        with patch.object(batch, "_start_server", return_value=None), \
             patch.object(batch, "read_url_file", return_value=links), \
             patch.object(batch, "require_authenticated_browser_identity",
                          return_value=([], {"id": 112233445, "source": "VK ID"})), \
             patch.object(batch, "Journal", return_value=previous), \
             patch("builtins.input", side_effect=AssertionError("Must not prompt")):
            result = batch.main([
                "--preview", "--limit", "1", "--file", "two-comments.txt",
                "--confirm-deleted", links[0],
            ])
        self.assertEqual(result, 1)
        self.assertEqual(previous.latest["wall:-600000001:41001:41002"],
                         "browser_unknown")
        self.assertFalse(previous.calls)


class MainExitTests(unittest.TestCase):
    def test_cancelled_batch_returns_zero_without_missing_status(self):
        links = [
            "https://vk.com/wall-111222333_1556?reply=1565",
            "https://vk.com/wall-111222333_1556?reply=1566",
        ]
        with patch.object(batch, "_start_server", return_value=None), \
             patch.object(batch, "read_url_file", return_value=links), \
             patch.object(batch, "require_authenticated_browser_identity",
                          return_value=([], {"id": 112233445})), \
             patch.object(batch, "Journal", return_value=LocalJournal()), \
             patch.object(batch, "run_batch", return_value={
                 "ui_removed": 0, "attempted": 0, "cancelled": True}):
            self.assertEqual(batch.main([
                "--execute", "--limit", "2", "--file", "existing-urls.txt"
            ]), 0)


class RealJournalAccountIsolationTests(unittest.TestCase):
    def test_actual_sqlite_status_is_scoped_to_verified_user(self):
        from vkmgr.journal import Journal

        with tempfile.TemporaryDirectory() as tmp:
            journal = Journal(Path(tmp) / "actual-progress.sqlite3")
            key = "wall:-600000001:41001:41002"
            journal.record(112233445, key, "browser", "browser_unknown")
            self.assertEqual(journal.statuses(112233445, [key]),
                             {key: "browser_unknown"})
            self.assertEqual(journal.statuses(222222, [key]), {})

            with self.assertRaisesRegex(ValueError, "no previous"):
                batch.reconcile_user_reported_deletion(
                    "https://vk.com/wall-600000001_41001?reply=41002",
                    222222, journal, ask=lambda _: self.fail("Must reject wrong user"),
                    echo=lambda _: None,
                )

            self.assertEqual(journal.statuses(112233445, [key]),
                             {key: "browser_unknown"})
            self.assertEqual(journal.statuses(222222, [key]), {})


class ConfirmationRecoveryTests(unittest.TestCase):
    URL = "https://vk.com/wall-600000001_41001?reply=41002"
    KEY = "wall:-600000001:41001:41002"
    ID = 112233445

    def test_accepts_ascii_case_and_spaces_of_explicit_confirmation(self):
        for typed in ("CONFIRM DELETED 41002", "CONFIRM DELETED 41002",
                      "confirm deleted 41002", "confirm deleted 41002",
                      " CoNfIrM DeLeTeD   41002 "):
            with self.subTest(typed=typed):
                self.assertTrue(batch.confirmation_matches(typed, 41002))

    def test_rejects_missing_wrong_or_different_comment_id(self):
        for typed in ("", "Enter", "yes", "INVALID", "CONFIRM DELETED 42002",
                      "CONFIRM DELETED 410020", "CONFIRM DELETED 41002 plus another",
                      "INVALIDИЕ 41002"):
            with self.subTest(typed=typed):
                self.assertFalse(batch.confirmation_matches(typed, 41002))

    def test_previous_unknown_user_confirmed_without_browser_click(self):
        journal = LocalJournal({self.KEY: "browser_unknown"})
        prompts = []
        output = []
        done = batch.reconcile_user_reported_deletion(
            self.URL, self.ID, journal,
            ask=lambda p: prompts.append(p) or "CONFIRM DELETED 41002",
            echo=output.append
        )
        self.assertTrue(done)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(journal.latest[self.KEY], "browser_user_confirmed")
        self.assertEqual(journal.calls, [
            (self.ID, self.KEY, "browser", "browser_user_confirmed")
        ])
        self.assertTrue(any("will not be deleted again" in x for x in output))

    def test_after_recovery_batch_skips_first_and_selects_second(self):
        journal = LocalJournal({self.KEY: "browser_unknown"})
        batch.reconcile_user_reported_deletion(
            self.URL, self.ID, journal,
            ask=lambda _: "CONFIRM DELETED 41002", echo=lambda _: None
        )
        target = batch.create_batch([
            self.URL,
            "https://vk.com/wall-600000002_42001?reply=42002",
        ], self.ID, 2, journal, echo=lambda _: None)
        self.assertEqual([x.comment_id for x in target], [42002])

    def test_wrong_confirmation_preserves_unknown(self):
        journal = LocalJournal({self.KEY: "browser_unknown"})
        with self.assertRaisesRegex(ValueError, "cancelled"):
            batch.reconcile_user_reported_deletion(
                self.URL, self.ID, journal,
                ask=lambda _: "CONFIRM DELETED 42002", echo=lambda _: None
            )
        self.assertEqual(journal.latest[self.KEY], "browser_unknown")
        self.assertEqual(journal.calls, [])

    def test_will_not_claim_success_without_prior_unknown(self):
        for initial in (None, "browser_ui_removed", "pending"):
            with self.subTest(initial=initial):
                journal = LocalJournal(
                    {} if initial is None else {self.KEY: initial}
                )
                with self.assertRaisesRegex(ValueError, "no previous"):
                    batch.reconcile_user_reported_deletion(
                        self.URL, self.ID, journal,
                        ask=lambda _: self.fail("Must not prompt"),
                        echo=lambda _: None,
                    )
                self.assertFalse(journal.calls)

    def test_cannot_reconcile_other_account(self):
        journal = LocalJournal({self.KEY: "browser_unknown"})
        with self.assertRaisesRegex(ValueError, "no previous"):
            batch.reconcile_user_reported_deletion(
                self.URL, 222222, journal,
                ask=lambda _: self.fail("No record for other account"),
                echo=lambda _: None,
            )
        self.assertEqual(journal.latest[self.KEY], "browser_unknown")

    def test_repeat_confirmation_is_noop_not_second_journal_event(self):
        journal = LocalJournal({self.KEY: "browser_user_confirmed"})
        self.assertFalse(batch.reconcile_user_reported_deletion(
            self.URL, self.ID, journal,
            ask=lambda _: self.fail("No additional confirmation"),
            echo=lambda _: None,
        ))
        self.assertFalse(journal.calls)

    def test_invalid_url_does_not_change_status(self):
        journal = LocalJournal({self.KEY: "browser_unknown"})
        for url in ("https://evil.example/wall-600000001_41001?reply=41002",
                    "https://vk.com/wall-600000001_41001",
                    "https://vk.com/photo112233445_5?comment=6"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                batch.reconcile_user_reported_deletion(
                    url, self.ID, journal,
                    ask=lambda _: self.fail("Must fail before prompt"),
                    echo=lambda _: None,
                )
        self.assertFalse(journal.calls)


class BatchDriverTests(unittest.TestCase):
    def setUp(self):
        self.urls = [
            "https://vk.com/wall-111222333_1556?reply=1565",
            "https://vk.com/wall-111222333_1556?reply=1566",
        ]
        self.targets = batch.create_batch(self.urls, 112233445, 2, LocalJournal())

    def _fake_browser(self):
        cm = MagicMock()
        pw = cm.__enter__.return_value
        context = pw.chromium.launch_persistent_context.return_value
        page = Mock()
        # A real Playwright Page.url is always a string. Keep this mock
        # realistic now that the batch opens its first comment before login.
        page.url = self.targets[0].url
        context.pages = [page]
        return cm, page

    def test_batch_requires_explicit_one_time_approval(self):
        journal = LocalJournal()
        cm, page = self._fake_browser()
        with patch.object(batch, "delete_one_in_open_page") as action:
            result = batch.run_batch(
                self.targets, journal, ask=lambda _: "НЕ СОГЛАСЕН",
                echo=lambda _: None, driver=lambda: cm,
            )
        action.assert_not_called()
        self.assertEqual(result, {"ui_removed": 0, "attempted": 0,
                                  "cancelled": True})

    def test_batch_two_actions_one_confirmation(self):
        journal = LocalJournal()
        cm, page = self._fake_browser()
        prompts = iter(["DELETE BATCH 2", ""])
        with patch.object(batch, "local_browser_profile",
                          return_value=Path("/tmp/mock-batch-profile")), \
             patch.object(batch, "delete_one_in_open_page",
                          return_value="browser_ui_removed") as action:
            outcome = batch.run_batch(
                self.targets, journal, ask=lambda _: next(prompts),
                echo=lambda _: None, driver=lambda: cm,
            )
        self.assertEqual(outcome, {"ui_removed": 2, "user_confirmed": 0,
                                   "attempted": 2, "unknown": 0, "skipped": 0, "cancelled": False})
        self.assertEqual(action.call_count, 2)

    def test_confirmed_post_click_ambiguous_action_moves_to_second(self):
        journal = LocalJournal()
        cm, page = self._fake_browser()
        prompts = iter(["DELETE BATCH 2", "", "CONFIRM DELETED 1565"])
        done = []

        def action(_page, target, store, echo=print):
            done.append(target.comment_id)
            if target.comment_id == 1565:
                store.record(target.user_id, target.key, "browser", "browser_unknown")
                raise batch.PostClickUnverified("VK left a placeholder")
            return "browser_ui_removed"

        with patch.object(batch, "local_browser_profile",
                          return_value=Path("/tmp/mock-batch-profile")), \
             patch.object(batch, "delete_one_in_open_page", side_effect=action):
            result = batch.run_batch(self.targets, journal,
                                     ask=lambda _: next(prompts),
                                     echo=lambda _: None, driver=lambda: cm)
        self.assertEqual(done, [1565, 1566])
        self.assertEqual(result, {"ui_removed": 1, "user_confirmed": 1,
                                  "attempted": 2, "unknown": 0, "skipped": 0, "cancelled": False})
        self.assertEqual(journal.latest[self.targets[0].key], "browser_user_confirmed")
        self.assertIn("browser_user_confirmed", batch.BLOCKED_STATUSES)
        self.assertEqual([call[3] for call in journal.calls],
                         ["browser_unknown", "browser_user_confirmed"])

    def test_without_exact_post_click_confirmation_batch_stops(self):
        for response in ("", "yes", "CONFIRM DELETED 1566"):
            with self.subTest(response=response):
                journal = LocalJournal()
                cm, page = self._fake_browser()
                prompts = iter(["DELETE BATCH 2", "", response])
                processed = []

                def action(_page, target, store, echo=print):
                    processed.append(target.comment_id)
                    store.record(target.user_id, target.key, "browser",
                                 "browser_unknown")
                    raise batch.PostClickUnverified("ambiguous")

                with patch.object(batch, "local_browser_profile",
                                  return_value=Path("/tmp/mock-batch-profile")), \
                     patch.object(batch, "delete_one_in_open_page", side_effect=action):
                    with self.assertRaises(batch.PostClickUnverified):
                        batch.run_batch(
                            self.targets, journal, ask=lambda _: next(prompts),
                            echo=lambda _: None, driver=lambda: cm,
                        )
                self.assertEqual(processed, [1565])
                self.assertEqual(journal.latest[self.targets[0].key], "browser_unknown")
                self.assertNotIn(self.targets[1].key, journal.latest)

    def test_previous_user_confirmation_skipped_from_new_batch(self):
        old = LocalJournal({"wall:-111222333:1556:1565": "browser_user_confirmed"})
        links = ["https://vk.com/wall-111222333_1556?reply=1565",
                 "https://vk.com/wall-111222333_1556?reply=1566"]
        result = batch.create_batch(links, 112233445, 2, old, echo=lambda _: None)
        self.assertEqual([t.comment_id for t in result], [1566])

    def test_batch_stops_immediately_on_first_unknown(self):
        journal = LocalJournal()
        cm, page = self._fake_browser()
        prompts = iter(["DELETE BATCH 2", ""])
        with patch.object(batch, "local_browser_profile",
                          return_value=Path("/tmp/mock-batch-profile")), \
             patch.object(batch, "delete_one_in_open_page",
                          side_effect=ValueError("VK UI unknown")) as action:
            with self.assertRaisesRegex(ValueError, "VK UI unknown"):
                batch.run_batch(
                    self.targets, journal, ask=lambda _: next(prompts),
                    echo=lambda _: None, driver=lambda: cm,
                )
        self.assertEqual(action.call_count, 1)

    def test_browser_opens_first_comment_before_login_prompt(self):
        journal = LocalJournal()
        cm, page = self._fake_browser()
        prompts = []
        with patch.object(batch, "local_browser_profile",
                          return_value=Path("/tmp/mock-batch-profile")), \
             patch.object(batch, "delete_one_in_open_page",
                          return_value="browser_ui_removed"):
            batch.run_batch(
                self.targets, journal,
                ask=lambda text: prompts.append((text, page.goto.call_count))
                                 or ("DELETE BATCH 2" if len(prompts) == 1 else ""),
                echo=lambda _: None, driver=lambda: cm,
            )
        self.assertEqual(page.goto.call_args.args[0], self.targets[0].url)
        self.assertEqual(prompts[1][1], 1, "VK must open before the login Enter prompt")

    def test_not_found_before_click_skips_and_continues(self):
        journal = LocalJournal()
        cm, page = self._fake_browser()
        prompts = iter(["DELETE BATCH 2", ""])
        with patch.object(batch, "local_browser_profile",
                          return_value=Path("/tmp/mock-batch-profile")), \
             patch.object(batch, "delete_one_in_open_page",
                          side_effect=[batch.TargetNotFound("not rendered"),
                                       "browser_ui_removed"]) as action:
            outcome = batch.run_batch(
                self.targets, journal, ask=lambda _: next(prompts),
                echo=lambda _: None, driver=lambda: cm,
            )
        self.assertEqual(outcome, {"ui_removed": 1, "user_confirmed": 0,
                                   "attempted": 1, "unknown": 0, "skipped": 1, "cancelled": False})
        self.assertEqual(action.call_count, 2)
        self.assertEqual(journal.calls, [
            (112233445, self.targets[0].key, "browser", "browser_not_found")
        ])
        self.assertIn("browser_not_found", batch.BLOCKED_STATUSES)
        later = batch.create_batch(self.urls, 112233445, 2, journal,
                                   echo=lambda _: None)
        self.assertEqual([x.comment_id for x in later], [1566])

    def test_stop_at_login_gate_never_attempts_any_action(self):
        journal = LocalJournal()
        cm, page = self._fake_browser()
        prompts = iter(["DELETE BATCH 2", "STOP"])
        with patch.object(batch, "local_browser_profile",
                          return_value=Path("/tmp/mock-batch-profile")), \
             patch.object(batch, "delete_one_in_open_page") as action:
            outcome = batch.run_batch(
                self.targets, journal, ask=lambda _: next(prompts),
                echo=lambda _: None, driver=lambda: cm,
            )
        action.assert_not_called()
        self.assertFalse(journal.calls)
        self.assertEqual(outcome, {
            "ui_removed": 0, "user_confirmed": 0,
            "attempted": 0, "skipped": 0, "cancelled": True,
        })

    def test_pause_after_first_click_does_not_touch_second_comment(self):
        journal = LocalJournal()
        cm, page = self._fake_browser()
        prompts = iter(["DELETE BATCH 2", "", "STOP"])
        touched = []

        def action(_page, target, store, echo=print):
            touched.append(target.comment_id)
            store.record(target.user_id, target.key, "browser",
                         "browser_ui_removed")
            return "browser_ui_removed"

        with patch.object(batch, "local_browser_profile",
                          return_value=Path("/tmp/mock-batch-profile")), \
             patch.object(batch, "delete_one_in_open_page", side_effect=action):
            result = batch.run_batch(self.targets, journal,
                                     ask=lambda _: next(prompts),
                                     echo=lambda _: None, driver=lambda: cm,
                                     pause_between=True)
        self.assertEqual(touched, [1565])
        self.assertEqual(result, {
            "ui_removed": 1, "user_confirmed": 0, "attempted": 1,
            "skipped": 0, "cancelled": True,
        })
        available = batch.create_batch(self.urls, 112233445, 2, journal,
                                       echo=lambda _: None)
        self.assertEqual([x.comment_id for x in available], [1566])

    def test_pause_after_not_found_prevents_next_click(self):
        journal = LocalJournal()
        cm, page = self._fake_browser()
        prompts = iter(["DELETE BATCH 2", "", "STOP"])
        processed = []

        def action(_page, target, store, echo=print):
            processed.append(target.comment_id)
            raise batch.TargetNotFound("not shown before click")

        with patch.object(batch, "local_browser_profile",
                          return_value=Path("/tmp/mock-batch-profile")), \
             patch.object(batch, "delete_one_in_open_page", side_effect=action):
            result = batch.run_batch(
                self.targets, journal, ask=lambda _: next(prompts),
                echo=lambda _: None, driver=lambda: cm,
                pause_between=True
            )
        self.assertEqual(processed, [1565])
        self.assertEqual(journal.latest[self.targets[0].key],
                         "browser_not_found")
        self.assertEqual(result["skipped"], 1)
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["attempted"], 0)

    def test_pause_enter_allows_second_click(self):
        journal = LocalJournal()
        cm, page = self._fake_browser()
        prompts = iter(["DELETE BATCH 2", "", ""])
        processed = []
        def action(_page, target, store, echo=print):
            processed.append(target.comment_id)
            return "browser_ui_removed"
        with patch.object(batch, "local_browser_profile",
                          return_value=Path("/tmp/mock-batch-profile")), \
             patch.object(batch, "delete_one_in_open_page", side_effect=action):
            result = batch.run_batch(
                self.targets, journal, ask=lambda _: next(prompts),
                echo=lambda _: None, driver=lambda: cm,
                pause_between=True
            )
        self.assertEqual(processed, [1565, 1566])
        self.assertFalse(result["cancelled"])

    def test_reveal_first_checks_exact_dom_before_any_expand(self):
        target = self.targets[0]
        page = Mock()
        page.url = target.url
        with patch.object(batch, "_target_in_dom", return_value=True), \
             patch.object(batch, "_expand_visible_replies") as expand, \
             patch.object(batch, "_scroll_comments") as scroll:
            self.assertTrue(batch.reveal_target_comment(page, target,
                                                        echo=lambda _: None))
        expand.assert_not_called()
        scroll.assert_not_called()

    def test_reveal_reads_only_bounded_steps_then_skips(self):
        target = self.targets[0]
        page = Mock()
        page.url = target.url
        with patch.object(batch, "_target_in_dom", return_value=False), \
             patch.object(batch, "_expand_visible_replies", return_value=0) as expand, \
             patch.object(batch, "_scroll_comments", return_value=0) as scroll:
            self.assertFalse(batch.reveal_target_comment(page, target,
                                                         echo=lambda _: None))
        self.assertEqual(expand.call_count, 6)
        self.assertEqual(scroll.call_count, 3)

    def test_missing_comment_must_not_write_journal(self):
        target = self.targets[0]
        page = Mock()
        page.url = target.url
        journal = LocalJournal()
        with patch.object(batch, "open_target_with_login_retry"), \
             patch.object(batch, "locate_exact_comment",
                          side_effect=batch.ExactCommentNotFound("Exact comment not in DOM")), \
             patch.object(batch, "reveal_target_comment", return_value=False):
            with self.assertRaises(batch.TargetNotFound):
                batch.delete_one_in_open_page(page, target, journal)
        self.assertEqual(journal.calls, [])


    def test_ambiguous_comment_stops_before_journal_or_click(self):
        target = self.targets[0]
        page = Mock()
        page.url = target.url
        journal = LocalJournal()
        with patch.object(batch, "open_target_with_login_retry"), \
             patch.object(batch, "locate_exact_comment",
                          side_effect=ValueError("Ambiguous comment ID")), \
             patch.object(batch, "reveal_target_comment") as reveal:
            with self.assertRaisesRegex(ValueError, "Ambiguous"):
                batch.delete_one_in_open_page(page, target, journal)
        reveal.assert_not_called()
        self.assertEqual(journal.calls, [])

    def test_comment_missing_again_after_expansion_is_safe_no_click(self):
        target = self.targets[0]
        page = Mock()
        page.url = target.url
        journal = LocalJournal()
        with patch.object(batch, "open_target_with_login_retry"), \
             patch.object(batch, "locate_exact_comment",
                          side_effect=batch.ExactCommentNotFound("missing")), \
             patch.object(batch, "reveal_target_comment", return_value=True):
            with self.assertRaises(batch.TargetNotFound):
                batch.delete_one_in_open_page(page, target, journal)
        self.assertEqual(journal.calls, [])


    def test_wrong_account_in_batch_rejected_before_opening_browser(self):
        bad = list(self.targets)
        bad[1] = batch.ExactTarget(
            bad[1].kind, bad[1].owner_id, bad[1].item_id,
            bad[1].comment_id, bad[1].key, bad[1].url, 7777)
        with self.assertRaisesRegex(ValueError, "different account"):
            batch.run_batch(bad, LocalJournal())


if __name__ == "__main__":
    unittest.main()
