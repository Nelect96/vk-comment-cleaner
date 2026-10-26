"""Offline regression tests for thread-aware archive queues and no-click recovery.

Never visits VK and never makes an actual browser deletion.
"""
import unittest
from unittest.mock import Mock, patch

from vkmgr import playwright_batch as batch
from vkmgr.journal import BLOCK_REPEAT

USER = 112233445
POST = "https://vk.com/wall-111_222"


class JournalStub:
    def __init__(self, states=None):
        self.states = dict(states or {})
        self.events = []

    def statuses(self, uid, keys):
        if uid != USER:
            return {}
        return {key: self.states[key] for key in keys if key in self.states}

    def record(self, uid, key, operation, status):
        assert uid == USER
        self.states[key] = status
        self.events.append((key, status))


class ThreadAwarePlanTests(unittest.TestCase):
    def test_scattered_child_answers_get_grouped_before_root(self):
        # Real archive order may interleave posts and mix main and nested replies.
        urls = [
            POST + "?reply=1562",                         # root written by user
            "https://vk.com/wall-999_123?reply=777",    # another post
            POST + "?reply=1565&thread=1562",             # later answer by user
            POST + "?reply=1568&thread=1562",
            POST + "?reply=2000",                         # unrelated top-level
        ]
        selected = batch.create_batch(
            urls, USER, 5, JournalStub(), automatic=True, echo=lambda _: None
        )
        self.assertEqual([t.comment_id for t in selected],
                         [1565, 1568, 1562, 2000, 777])
        self.assertEqual(len({t.key for t in selected}), 5)

    def test_grouping_never_invents_comment_ids(self):
        urls = [POST + "?reply=1565&thread=1562",
                POST + "?reply=1568&thread=1562"]
        selected = batch.create_batch(
            urls, USER, 1000, JournalStub(),
            automatic=True, echo=lambda _: None
        )
        self.assertEqual([t.comment_id for t in selected], [1565, 1568])
        self.assertNotIn(1562, [t.comment_id for t in selected])

    def test_cap_1000_remains_enforced_after_full_archive_grouping(self):
        urls = ["https://vk.com/wall-111_222?reply=" + str(i)
                for i in range(100001, 101202)]
        actual = batch.create_batch(
            urls, USER, 1000, JournalStub(),
            automatic=True, echo=lambda _: None
        )
        self.assertEqual(len(actual), 1000)
        self.assertEqual(len({t.key for t in actual}), 1000)

    def test_review_selects_only_not_found_before_click(self):
        urls = [POST + "?reply=" + str(cid)
                for cid in (101, 102, 103, 104)]
        states = {
            "wall:-111:222:101": "browser_not_found",
            "wall:-111:222:102": "browser_unknown",
            "wall:-111:222:103": "browser_ui_deleted_marker",
        }
        j = JournalStub(states)
        normal = batch.create_batch(
            urls, USER, 1000, j, automatic=True, echo=lambda _: None)
        self.assertEqual([t.comment_id for t in normal], [104])
        retry = batch.create_batch(
            urls, USER, 1000, j, automatic=True,
            review_not_found=True, echo=lambda _: None)
        self.assertEqual([t.comment_id for t in retry], [101])
        self.assertIn("browser_unknown", BLOCK_REPEAT)

    def test_review_rejected_in_interactive_mode(self):
        with self.assertRaisesRegex(ValueError, "--auto"):
            batch.create_batch(
                [POST + "?reply=101"], USER, 1, JournalStub(),
                review_not_found=True, echo=lambda _: None)


class RecoveryClickTests(unittest.TestCase):
    def setUp(self):
        self.url = POST + "?reply=1565&thread=1562"
        self.target = batch.create_batch(
            [self.url], USER, 1, JournalStub(),
            automatic=True, echo=lambda _: None)[0]
        self.page = Mock()
        self.page.url = self.url
        self.journal = JournalStub({self.target.key: "browser_not_found"})

    def _try_click(self, allow):
        row, button = Mock(), Mock()
        with patch.object(batch, "open_target_with_login_retry"), \
             patch.object(batch, "locate_exact_comment",
                          return_value=('[id="112233445_1565"]', row)), \
             patch.object(batch, "author_proof", return_value=(True, [USER])), \
             patch.object(batch, "direct_delete_button", return_value=button), \
             patch.object(batch, "explicit_deleted_marker", return_value=True):
            if not allow:
                with self.assertRaisesRegex(ValueError, "second click"):
                    batch.delete_one_in_open_page(
                        self.page, self.target, self.journal,
                        automatic=True, echo=lambda _: None)
            else:
                outcome = batch.delete_one_in_open_page(
                    self.page, self.target, self.journal,
                    automatic=True, review_not_found=True,
                    echo=lambda _: None)
                self.assertEqual(outcome, "browser_ui_deleted_marker")
        return button

    def test_normal_run_never_retries_previously_not_found(self):
        button = self._try_click(allow=False)
        button.click.assert_not_called()
        self.assertEqual(self.journal.states[self.target.key], "browser_not_found")

    def test_explicit_no_click_recovery_requires_author_and_once_only(self):
        button = self._try_click(allow=True)
        button.click.assert_called_once()
        self.assertEqual(self.journal.events, [
            (self.target.key, "browser_pending"),
            (self.target.key, "browser_ui_deleted_marker"),
        ])
        another = self._try_click(allow=False)
        another.click.assert_not_called()

    def test_recovery_never_deletes_without_proven_same_author(self):
        row, button = Mock(), Mock()
        with patch.object(batch, "open_target_with_login_retry"), \
             patch.object(batch, "locate_exact_comment",
                          return_value=('[id="999_1565"]', row)), \
             patch.object(batch, "author_proof", return_value=(False, [999])), \
             patch.object(batch, "direct_delete_button", return_value=button):
            with self.assertRaisesRegex(ValueError, "Comment author"):
                batch.delete_one_in_open_page(
                    self.page, self.target, self.journal, automatic=True,
                    review_not_found=True, echo=lambda _: None)
        button.click.assert_not_called()
        self.assertEqual(self.journal.states[self.target.key], "browser_not_found")
        self.assertEqual(self.journal.events, [])

    def test_never_revisit_uncertain_clicked_action(self):
        self.journal.states[self.target.key] = "browser_unknown"
        with self.assertRaisesRegex(ValueError, "second click"):
            self._try_click(allow=True)
        self.assertEqual(self.journal.states[self.target.key], "browser_unknown")


if __name__ == "__main__":
    unittest.main()
