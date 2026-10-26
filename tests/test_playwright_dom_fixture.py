"""Optional offline Chromium DOM integration tests for VKCM's Playwright pilot.

Only runs when Playwright AND its Chromium are installed. Uses local HTML
fixtures, never opens VK or causes a remote deletion.
"""
import importlib.util
import unittest

from vkmgr import playwright_delete as pilot


@unittest.skipUnless(importlib.util.find_spec("playwright") is not None,
                     "Playwright optional dependency is not installed")
class PlaywrightDOMFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch(headless=True)
        except Exception as exc:
            cls.pw.stop()
            raise unittest.SkipTest("Local Chromium browser not yet installed: " + str(exc))
        cls.page = cls.browser.new_page()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page.set_content("""
        <!doctype html><html lang="ru"><body>
        <div data-comment-id="777" data-from-id="9876">
          <a class="reply_author" href="/id9876">Other author</a>
          <button aria-label="Ещё" aria-controls="menu777"
                  onclick="document.querySelector('#menu777').hidden=false">...</button>
        </div>
        <div id="menu777" role="menu" hidden><button role="menuitem">Удалить</button></div>
        <div id="112233445_1564">
          <a class="meXqZJ vkitInternalRichAvatar" href="/id112233445">Avatar</a>
          <a class="vkit-b0dQw7 vkuiTypography" href="/id112233445">Observed author</a>
          <a href="/club111222333">Post community</a>
          <button data-testid="comment_action_delete"
                  onclick="this.closest('div[id]').setAttribute('data-test-deleted', 'yes')">X</button>
        </div>
        <div id="menu1564" role="menu" hidden><button role="menuitem">Удалить</button></div>
        <div data-comment-id="43002" data-from-id="112233445">
          <a class="reply_author" href="/id112233445">Owner</a>
          <button aria-label="Ещё" aria-controls="menu43002"
                  onclick="document.querySelector('#menu43002').hidden=false">...</button>
        </div>
        <div id="menu43002" role="menu" hidden><button role="menuitem">Удалить</button></div>
        </body></html>
        """)

    def test_exact_target_wins_over_other_comment(self):
        record = {"id": "wall:-600000003:43001:43002",
                  "url": "https://vk.com/wall-600000003_43001?reply=43002"}
        target = pilot.from_record(record, 112233445)
        selector, row = pilot.locate_exact_comment(self.page, target)
        self.assertEqual(selector, '[data-comment-id="43002"]')
        is_author, ids = pilot.author_proof(row, target.user_id)
        self.assertTrue(is_author)
        self.assertEqual(ids, [112233445])
        menu = pilot.menu_buttons(row)
        self.assertGreater(len(menu), 0)
        buttons = pilot.unique_visible([b for _, b in menu])
        self.assertEqual(len(buttons), 1)
        buttons[0].click()
        allowed = pilot.delete_options_in_open_menu(self.page, buttons[0])
        self.assertEqual(len(allowed), 1)
        self.assertFalse(self.page.locator("#menu777").is_visible())
        self.assertTrue(self.page.locator("#menu43002").is_visible())

    def test_real_observed_author_comment_dom_id_is_recognized(self):
        # VK returned id="112233445_1564" for reply=1564.
        record = {
            "id": "wall:-111222333:1556:1564",
            "url": "https://vk.com/wall-111222333_1556?reply=1564&thread=1562",
        }
        target = pilot.from_record(record, 112233445)
        css, row = pilot.locate_exact_comment(self.page, target)
        self.assertEqual(css, '[id="112233445_1564"]')
        matched, ids = pilot.author_proof(row, 112233445)
        self.assertTrue(matched)
        self.assertEqual(ids, [112233445])

    def test_observed_vk_direct_delete_button_scoped_to_verified_reply(self):
        record = {
            "id": "wall:-111222333:1556:1564",
            "url": "https://vk.com/wall-111222333_1556?reply=1564&thread=1562",
        }
        target = pilot.from_record(record, 112233445)
        _, row = pilot.locate_exact_comment(self.page, target)
        owner, ids = pilot.author_proof(row, target.user_id)
        self.assertTrue(owner)
        self.assertEqual(ids, [112233445])
        direct = pilot.direct_delete_button(row)
        self.assertIsNotNone(direct)
        self.assertEqual(direct.get_attribute("data-testid"), "comment_action_delete")
        self.assertIsNone(self.page.locator('[data-comment-id="777"]').get_attribute("data-test-deleted"))
        # Fixture-only click; this local markup has no VK network calls.
        direct.click()
        self.assertEqual(row.get_attribute("data-test-deleted"), "yes")
        self.assertIsNone(self.page.locator('[data-comment-id="777"]').get_attribute("data-test-deleted"))

    def test_unrelated_mention_cannot_prove_author(self):
        self.page.set_content("""<div id="9876_1564">
          <a href="/id9876">Avatar</a>
          <a href="/id9876">Author</a>
          <a href="/id112233445">Mention of account to fool scraper</a>
          <button data-testid="comment_action_delete">X</button>
          </div>""")
        row = self.page.locator('[id="9876_1564"]')
        matched, ids = pilot.author_proof(row, 112233445)
        self.assertFalse(matched)
        self.assertEqual(ids, [9876])

    def test_first_two_author_links_must_match_root_identity(self):
        self.page.set_content("""<div id="112233445_1564">
          <a href="/id112233445">Avatar</a>
          <a href="/id9876">Spoofed author</a>
          <button data-testid="comment_action_delete">X</button>
          </div>""")
        row = self.page.locator('[id="112233445_1564"]')
        matched, ids = pilot.author_proof(row, 112233445)
        self.assertFalse(matched)
        self.assertEqual(ids, [112233445])

    def test_wrong_author_never_verifies(self):
        record = {"id": "wall:-600000003:43001:777",
                  "url": "https://vk.com/wall-600000003_43001?reply=777"}
        target = pilot.from_record(record, 112233445)
        _, row = pilot.locate_exact_comment(self.page, target)
        is_author, ids = pilot.author_proof(row, target.user_id)
        self.assertFalse(is_author)
        self.assertEqual(ids, [9876])

    def test_unknown_id_does_not_pick_neighbor(self):
        record = {"id": "wall:-600000003:43001:8000",
                  "url": "https://vk.com/wall-600000003_43001?reply=8000"}
        target = pilot.from_record(record, 112233445)
        with self.assertRaises(ValueError):
            pilot.locate_exact_comment(self.page, target)


if __name__ == "__main__":
    unittest.main()
