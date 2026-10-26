"""Durability and fail-closed queue regression tests (no live VK requests)."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from vkmgr.journal import Journal, BLOCK_REPEAT
from vkmgr.parser import Comment
from vkmgr.verification import CheckResult
import vkmgr.server as server


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'private' / 'progress.sqlite3'
        self.journal = Journal(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_reloads_pending_and_accepted_after_restart(self):
        self.journal.record(10, 'wall:-1:22:55', 'delete', 'pending')
        self.assertEqual(Journal(self.path).statuses(10, ['wall:-1:22:55']),
                         {'wall:-1:22:55': 'pending'})
        self.journal.record(10, 'wall:-1:22:55', 'delete', 'api_accepted')
        self.assertEqual(Journal(self.path).summary(10), {'api_accepted': 1})
        self.assertIn('api_accepted', BLOCK_REPEAT)
        self.assertIn('pending', BLOCK_REPEAT)

    def test_account_scoping_prevents_cross_account_leaks(self):
        self.journal.record(11, 'photo:4:5:6', 'delete', 'api_accepted')
        self.assertEqual(self.journal.statuses(12, ['photo:4:5:6']), {})
        self.assertEqual(self.journal.summary(12), {})

    def test_secrets_and_texts_not_in_schema_or_data(self):
        self.journal.record(11, 'video:-1:99:42', 'edit', 'edited_accepted')
        data = self.path.read_bytes()
        self.assertNotIn(b'access_token', data)
        self.assertNotIn(b'comment_text', data)
        self.assertNotIn(b'password', data)

    def test_rejects_invalid_account(self):
        with self.assertRaises(ValueError):
            self.journal.record(0, 'wall:1:2:3', 'delete', 'pending')


class BulkSafety(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous_state = server.STATE
        server.STATE = server.AppState(Journal(Path(self.temp.name) / 'progress.sqlite3'))
        server.STATE.token = 'token-from-test'
        server.STATE.user = {'id': 81, 'name': 'Test'}
        self.c1 = Comment('wall:10:20:30', 'wall', 10, 20, 30, '', '', '', '')
        self.c2 = Comment('wall:10:20:31', 'wall', 10, 20, 31, '', '', '', '')
        server.STATE.comments = {c.id: c for c in (self.c1, self.c2)}
        server.STATE.job = {'running': True, 'done': 0, 'total': 2, 'failed': 0, 'message': 'Running'}

    def tearDown(self):
        server.STATE = self.previous_state
        self.temp.cleanup()

    def test_stop_after_first_inflight_request(self):
        def accepted(*args, **kwargs):
            server.STATE.stop_requested.set()
            return True
        with patch('vkmgr.server.verify_comment', return_value=CheckResult('verified', 'mine', 81)), \
             patch('vkmgr.server.action', side_effect=accepted) as writes:
            server.run_bulk([self.c1, self.c2], 'token-from-test', 81)
        self.assertEqual(writes.call_count, 1)
        self.assertEqual(server.STATE.journal.statuses(81, [self.c1.id]).get(self.c1.id), 'api_accepted')
        self.assertNotIn(self.c2.id, server.STATE.statuses)
        self.assertFalse(server.STATE.job['running'])

    def test_does_not_write_other_author_comment(self):
        with patch('vkmgr.server.verify_comment', return_value=CheckResult('not_mine', 'other', 82)), \
             patch('vkmgr.server.action') as writes, \
             patch('vkmgr.server.time.sleep'):
            server.run_bulk([self.c1], 'token-from-test', 81)
        writes.assert_not_called()
        self.assertEqual(server.STATE.job['failed'], 1)
        self.assertEqual(server.STATE.statuses[self.c1.id], 'skipped_unverified')

    def test_unknown_network_outcome_is_stored_and_job_stops(self):
        def crash(*args, **kwargs):
            raise TimeoutError('response lost')
        with patch('vkmgr.server.verify_comment', return_value=CheckResult('verified', 'mine', 81)), \
             patch('vkmgr.server.action', side_effect=crash) as writes:
            server.run_bulk([self.c1, self.c2], 'token-from-test', 81)
        self.assertEqual(writes.call_count, 1)
        self.assertEqual(server.STATE.statuses[self.c1.id], 'outcome_unknown')
        self.assertEqual(Journal(server.STATE.journal.path).statuses(81, [self.c1.id])[self.c1.id], 'outcome_unknown')
        self.assertEqual(server.STATE.job['done'], 1)


if __name__ == '__main__':
    unittest.main()
