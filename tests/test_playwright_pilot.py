"""Offline safety tests for the experimental one-comment Playwright pilot.

Tests deliberately do not perform live VK API calls, open a browser, or delete
any comment. The first end-to-end dry inspection requires the user's Mac.
"""
import unittest
from unittest.mock import Mock, patch
from vkmgr import playwright_delete as pilot


class ExactCommentTests(unittest.TestCase):
    def setUp(self):
        self.record = {
            "id": "wall:-600000003:43001:43002",
            "url": "https://vk.com/wall-600000003_43001?reply=43002",
            "kind": "wall",
            "status": "archived",
        }

    def test_exact_target(self):
        target = pilot.from_record(self.record, 112233445)
        self.assertEqual(target.user_id, 112233445)
        self.assertEqual(target.comment_id, 43002)
        self.assertEqual(target.owner_id, -600000003)
        self.assertEqual(target.key, self.record["id"])

    def test_mismatched_link_fails_closed(self):
        bad = dict(self.record, id="wall:-600000003:43001:7777")
        with self.assertRaises(ValueError):
            pilot.from_record(bad, 112233445)

    def test_no_account_id_fails_closed(self):
        for user in (-1, None, "112233445", 0):
            with self.subTest(user=user), self.assertRaises(ValueError):
                pilot.from_record(self.record, user)

    def test_unsafe_redirect_hosts(self):
        self.assertTrue(pilot.allowed_destination(self.record["url"]))
        for url in (
            "https://evil.example/wall-600000003_43001?reply=43002",
            "http://vk.com/wall-600000003_43001?reply=43002",
            "javascript:alert(1)",
            "https://vk.com.evil.example/",
        ):
            with self.subTest(url=url):
                self.assertFalse(pilot.allowed_destination(url))

    def test_selectors_are_exact_not_generic(self):
        t = pilot.from_record(self.record, 112233445)
        selectors = pilot.comment_selectors(t)
        self.assertIn('[data-comment-id="43002"]', selectors)
        self.assertIn('[id="112233445_43002"]', selectors)
        self.assertIn('[id="wall_reply-600000003_43002"]', selectors)
        self.assertTrue(all('43002' in selector for selector in selectors))
        self.assertTrue(all('[id*=' not in selector and
                            '[data-comment-id*=' not in selector and
                            '[data-reply-id*=' not in selector
                            for selector in selectors))

    def test_numeric_vk_author_id_is_strict(self):
        self.assertEqual(pilot.numeric_profile_id("https://vk.com/id112233445"), 112233445)
        self.assertEqual(pilot.numeric_profile_id("/id112233445"), 112233445)
        for value in ("https://vk.com/id1122334450", "https://vk.com/durov",
                      "https://evil.example/id112233445", "/id-5",
                      "/id112233445/reposts"):
            with self.subTest(value=value):
                self.assertNotEqual(pilot.numeric_profile_id(value), 112233445)


class ExplicitSelectionTests(unittest.TestCase):
    def setUp(self):
        self.url = "https://vk.com/wall-111222333_1556?reply=1564&thread=1562"
        self.row = {
            "id": "wall:-111222333:1556:1564",
            "url": self.url,
            "kind": "wall",
            "status": "archived",
        }

    def test_find_exact_url_with_thread_and_query_encoding(self):
        with patch.object(pilot, "_get", return_value={
            "items": [self.row],
            "total": 1,
        }) as api:
            found = pilot.find_imported_by_url(self.url)
            self.assertEqual(found["id"], self.row["id"])
            self.assertEqual(api.call_count, 1)
            self.assertIn("q=wall-111222333_1556", api.call_args.args[0])

    def test_lookup_never_uses_approximate_comment_id(self):
        with patch.object(pilot, "_get", return_value={
            "items": [dict(self.row, id="wall:-111222333:1556:1565")],
            "total": 1,
        }):
            with self.assertRaisesRegex(ValueError, "not found in the VKCM list"):
                pilot.find_imported_by_url(self.url)

    def test_manual_status_never_selected_for_browser_retry(self):
        with patch.object(pilot, "_get", return_value={
            "items": [dict(self.row, status="browser_unknown")], "total": 1,
        }):
            with self.assertRaisesRegex(ValueError, "another test comment"):
                pilot.find_imported_by_url(self.url)

    def test_user_must_explicitly_choose_number(self):
        available = [self.row, {
            "id": "wall:-111222333:1556:1565",
            "url": "https://vk.com/wall-111222333_1556?reply=1565",
        }]
        with patch.object(pilot, "get_queue", return_value=(available, {"id": 112233445})):
            selected = pilot.choose_comment(
                {"id": 112233445}, ask=lambda _: "2", echo=lambda _: None,
            )
        self.assertEqual(selected.comment_id, 1565)

    def test_user_can_paste_exact_url_without_import(self):
        fake_journal = Mock()
        fake_journal.statuses.return_value = {}
        with patch.object(pilot, "get_queue", return_value=([], {"id": 112233445})), \
             patch.object(pilot, "Journal", return_value=fake_journal):
            selected = pilot.choose_comment(
                {"id": 112233445}, ask=lambda _: self.url, echo=lambda _: None,
            )
        self.assertEqual(selected.comment_id, 1564)
        self.assertEqual(selected.user_id, 112233445)
        fake_journal.statuses.assert_called_once_with(112233445, [self.row["id"]])

    def test_empty_or_bad_choice_does_not_choose_first_implicitly(self):
        with patch.object(pilot, "get_queue", return_value=([self.row], {"id": 112233445})):
            for answer in ("", "STOP", "99"):
                with self.subTest(answer=answer), self.assertRaises(ValueError):
                    pilot.choose_comment(
                        {"id": 112233445}, ask=lambda _: answer, echo=lambda _: None,
                    )


class DirectNavigationTests(unittest.TestCase):
    def setUp(self):
        self.target = pilot.ExactTarget(
            "wall", -111222333, 1556, 1564,
            "wall:-111222333:1556:1564",
            "https://vk.com/wall-111222333_1556?reply=1564&thread=1562",
            112233445,
        )

    def test_first_navigation_opens_comment_not_feed(self):
        page = Mock()
        def navigate(url, **_kwargs):
            page.url = url
        page.goto.side_effect = navigate
        with patch.object(pilot, "exact_dom_visible", return_value=False), \
             patch.object(pilot, "url_from_continue_prompt") as check:
            pilot.open_target_with_login_retry(
                page, self.target, ask=lambda _: self.fail("No prompt needed"),
                echo=lambda _: None,
            )
            check.assert_not_called()
        page.goto.assert_called_once()
        self.assertEqual(page.goto.call_args.args[0], self.target.url)

    def test_redirect_to_feed_retries_after_login(self):
        page = Mock()
        pages = iter(["https://vk.ru/feed", self.target.url])
        page.goto.side_effect = lambda _url, **_kw: setattr(page, "url", next(pages))
        prompts = []
        with patch.object(pilot, "exact_dom_visible", return_value=False):
            pilot.open_target_with_login_retry(
                page, self.target,
                ask=lambda prompt: prompts.append(prompt) or "",
                echo=lambda _: None,
            )
        self.assertEqual(page.goto.call_count, 2)
        self.assertEqual([x.args[0] for x in page.goto.call_args_list],
                         [self.target.url, pilot.alternate_official_link(self.target.url)])
        self.assertEqual(len(prompts), 1)

    def test_redirect_to_feed_twice_stops_without_guessing(self):
        page = Mock()
        page.goto.side_effect = lambda _url, **_kw: setattr(page, "url", "https://vk.ru/feed")
        with patch.object(pilot, "exact_dom_visible", return_value=False):
            with self.assertRaisesRegex(ValueError, "a second time"):
                pilot.open_target_with_login_retry(
                    page, self.target, ask=lambda _: "", echo=lambda _: None,
                )
        self.assertEqual(page.goto.call_count, 2)

    def test_feed_spa_with_exact_comment_dom_is_accepted(self):
        page = Mock()
        page.goto.side_effect = lambda _url, **_kw: setattr(page, "url", "https://vk.ru/feed")
        with patch.object(pilot, "exact_dom_visible", return_value=True):
            pilot.open_target_with_login_retry(
                page, self.target,
                ask=lambda _: self.fail("SPA already shows exact comment"),
                echo=lambda _: None,
            )
        self.assertEqual(page.goto.call_count, 1)

    def test_pasting_link_at_enter_prompt_is_not_ignored(self):
        pilot.url_from_continue_prompt(self.target.url, self.target)
        pilot.url_from_continue_prompt("", self.target)
        different = self.target.url.replace("reply=1564", "reply=1565")
        with self.assertRaisesRegex(ValueError, "different comment"):
            pilot.url_from_continue_prompt(different, self.target)
        with self.assertRaisesRegex(ValueError, "press Enter"):
            pilot.url_from_continue_prompt("anything", self.target)

    def test_official_host_fallback_preserves_reply_and_thread(self):
        swapped = pilot.alternate_official_link(self.target.url)
        self.assertEqual(
            swapped,
            "https://vk.ru/wall-111222333_1556?reply=1564&thread=1562"
        )
        self.assertTrue(pilot.is_target_page(swapped, self.target))
        self.assertEqual(pilot.alternate_official_link(swapped), self.target.url)
        with self.assertRaises(ValueError):
            pilot.alternate_official_link("https://evil.example/path")

    def test_browser_location_logging_strips_sensitive_query(self):
        self.assertEqual(
            pilot.safe_page_path("https://id.vk.ru/authorize?code=SECRET&state=SECRET"),
            "id.vk.ru/authorize",
        )

    def test_direct_target_requires_same_vk_id_journal(self):
        fake = Mock()
        fake.statuses.return_value = {}
        with patch.object(pilot, "Journal", return_value=fake):
            chosen = pilot.target_from_pasted_url(self.target.url, self.target.user_id)
        self.assertEqual(chosen.key, self.target.key)
        fake.statuses.assert_called_once_with(self.target.user_id, [self.target.key])

    def test_journal_repeat_status_prohibits_retry(self):
        fake = Mock()
        fake.statuses.return_value = {self.target.key: "browser_unknown"}
        with patch.object(pilot, "Journal", return_value=fake):
            with self.assertRaisesRegex(ValueError, "Select another"):
                pilot.target_from_pasted_url(self.target.url, self.target.user_id)


class DirectCliTests(unittest.TestCase):
    def setUp(self):
        self.url = "https://vk.com/wall-111222333_1556?reply=1564&thread=1562"
        self.user = {"id": 112233445, "name": "Test"}
        self.target = pilot.ExactTarget(
            "wall", -111222333, 1556, 1564,
            "wall:-111222333:1556:1564", self.url, 112233445,
        )

    def test_url_option_goes_directly_to_browser_without_import(self):
        with patch.object(pilot, "_start_server", return_value=None), \
             patch.object(pilot, "get_queue", return_value=([], self.user)), \
             patch.object(pilot, "target_from_pasted_url", return_value=self.target) as picker, \
             patch.object(pilot, "run_browser", return_value=0) as run, \
             patch.object(pilot, "choose_comment") as interactive:
            self.assertEqual(pilot.main(["--inspect", "--url", self.url]), 0)
        picker.assert_called_once_with(self.url, 112233445)
        run.assert_called_once_with(self.target, execute=False)
        interactive.assert_not_called()

    def test_permalink_at_login_prompt_is_used_not_ignored(self):
        with patch.object(pilot, "_start_server", return_value=None), \
             patch.object(pilot, "get_queue",
                          side_effect=[([], None), ([], self.user)]), \
             patch.object(pilot, "target_from_pasted_url", return_value=self.target) as picker, \
             patch.object(pilot, "run_browser", return_value=0), \
             patch("builtins.input", return_value=self.url), \
             patch("webbrowser.open") as opened:
            self.assertEqual(pilot.main(["--inspect"]), 0)
        picker.assert_called_once_with(self.url, 112233445)
        opened.assert_called_once_with(pilot.BASE)

class AuthorEvidenceTests(unittest.TestCase):
    def test_missing_evidence_fails_closed(self):
        row = Mock()
        row.get_attribute.return_value = None
        row.locator.return_value.all.return_value = []
        matched, identifiers = pilot.author_proof(row, 112233445)
        self.assertFalse(matched)
        self.assertEqual(identifiers, [])

    def test_author_id_attribute_verifies(self):
        row = Mock()
        row.get_attribute.side_effect = lambda name: (
            "112233445" if name == "data-from-id" else None)
        row.locator.return_value.all.return_value = []
        matched, ids = pilot.author_proof(row, 112233445)
        self.assertTrue(matched)
        self.assertEqual(ids, [112233445])

    def test_conflicting_author_evidence_fails_closed(self):
        row = Mock()
        row.get_attribute.side_effect = lambda name: (
            "112233445" if name == "data-from-id" else
            "12345" if name == "data-author-id" else None)
        row.locator.return_value.all.return_value = []
        matched, ids = pilot.author_proof(row, 112233445)
        self.assertFalse(matched)
        self.assertEqual(ids, [12345, 112233445])


class MenuSafetyTests(unittest.TestCase):
    def test_no_popup_cannot_be_mistaken_for_delete_button(self):
        page = Mock()
        trigger = Mock()
        trigger.get_attribute.return_value = None
        page.get_by_role.return_value.count.return_value = 0
        with self.assertRaises(ValueError):
            pilot.delete_options_in_open_menu(page, trigger)

    def test_unexpected_menu_id_blocks(self):
        page = Mock()
        trigger = Mock()
        trigger.get_attribute.return_value = "unsafe\"id"
        with self.assertRaises(ValueError):
            pilot.delete_options_in_open_menu(page, trigger)
        page.locator.assert_not_called()

    def test_no_comment_dom_node_fails_closed(self):
        page = Mock()
        page.locator.return_value.count.return_value = 0
        t = pilot.from_record({
            "id": "photo:1:2:3",
            "url": "https://vk.com/photo1_2?comment=3",
        }, 7)
        with self.assertRaises(ValueError):
            pilot.locate_exact_comment(page, t)


if __name__ == "__main__":
    unittest.main()
