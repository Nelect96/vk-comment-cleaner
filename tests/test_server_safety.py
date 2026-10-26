"""No VK write operation may run without fresh ownership verification."""
import http.client
import json
import threading
import tempfile
from pathlib import Path
from vkmgr.journal import Journal
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from vkmgr.parser import Comment
from vkmgr.verification import CheckResult
import vkmgr.server as web


class HandlerSafety(unittest.TestCase):
    def setUp(self):
        self.original = web.STATE
        self.temp = tempfile.TemporaryDirectory()
        web.STATE = web.AppState(journal=Journal(Path(self.temp.name) / "history.sqlite3"))
        c = Comment('wall:-123:40:50', 'wall', -123, 40, 50,
                    'https://vk.com/wall-123_40?reply=50', '', '', 'comments0.html')
        web.STATE.comments[c.id] = c
        web.STATE.token = 'secret'
        web.STATE.user = {'id': 481, 'name': 'Me'}
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.comment_id = c.id

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        web.STATE = self.original
        self.temp.cleanup()

    def post(self, path, data):
        client = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
        client.request('POST', path, body=json.dumps(data), headers={
            'Host': '127.0.0.1:' + str(self.server.server_port),
            'Content-Type': 'application/json',
            'X-VKCM-CSRF': web.STATE.csrf,
        })
        response = client.getresponse()
        status, content = response.status, json.loads(response.read().decode())
        client.close()
        return status, content

    def test_rejects_other_person_before_any_write(self):
        with patch('vkmgr.server.verify_comment', return_value=CheckResult('not_mine', 'Different author', 555)), \
             patch('vkmgr.server.action') as write:
            status, result = self.post('/api/action', {
                'id': self.comment_id, 'operation': 'delete', 'confirmed': True,
            })
            self.assertEqual(status, 409)
            self.assertIn('blocked', result['error'])
            write.assert_not_called()

    def test_rejects_unverified_before_any_edit(self):
        with patch('vkmgr.server.verify_comment', return_value=CheckResult('unverified', 'Not found')), \
             patch('vkmgr.server.action') as write:
            status, result = self.post('/api/action', {
                'id': self.comment_id, 'operation': 'edit', 'message': 'hello', 'confirmed': True,
            })
            self.assertEqual(status, 409)
            write.assert_not_called()

    def test_fresh_verification_allows_explicit_single_delete(self):
        with patch('vkmgr.server.verify_comment', return_value=CheckResult('verified', 'Mine', 481)), \
             patch('vkmgr.server.action') as write:
            status, result = self.post('/api/action', {
                'id': self.comment_id, 'operation': 'delete', 'confirmed': True,
            })
            self.assertEqual(status, 200)
            write.assert_called_once()
            self.assertEqual(result['status'], 'api_accepted')

    def test_duplicate_single_write_is_blocked_after_api_ack(self):
        with patch('vkmgr.server.verify_comment', return_value=CheckResult('verified', 'Mine', 481)), \
             patch('vkmgr.server.action') as write:
            request = {'id': self.comment_id, 'operation': 'delete', 'confirmed': True}
            first, first_body = self.post('/api/action', request)
            second, second_body = self.post('/api/action', request)
        self.assertEqual(first, 200)
        self.assertEqual(first_body['status'], 'api_accepted')
        self.assertEqual(second, 409)
        self.assertEqual(write.call_count, 1)
        self.assertEqual(web.STATE.journal.statuses(481, [self.comment_id])[self.comment_id], 'api_accepted')

    def test_stop_endpoint_requests_cancel_without_deleting(self):
        with patch('vkmgr.server.action') as write:
            web.STATE.job['running'] = True
            status, reply = self.post('/api/cancel', {})
        self.assertEqual(status, 200)
        self.assertTrue(reply['requested'])
        self.assertTrue(web.STATE.stop_requested.is_set())
        write.assert_not_called()

    def test_restore_progress_on_login(self):
        web.STATE.journal.record(481, self.comment_id, 'delete', 'api_accepted')
        web.STATE.statuses.clear()
        with patch('vkmgr.server.authorized_user', return_value={'id': 481, 'name': 'Me'}):
            status, body = self.post('/api/token', {'token': 'valid-example'})
        self.assertEqual(status, 200)
        self.assertTrue(body['token_set'])
        self.assertEqual(web.STATE.statuses[self.comment_id], 'api_accepted')

    def test_token_auth_readonly_and_revoked_on_bad_identity(self):
        with patch('vkmgr.server.authorized_user', side_effect=web.VkApiError(5, 'not authorized')):
            status, result = self.post('/api/token', {'token': 'bad'})
            self.assertEqual(status, 422)
            self.assertFalse(web.STATE.token)
            self.assertIsNone(web.STATE.user)

    def test_preflight_is_read_only(self):
        with patch('vkmgr.server.verify_comment', return_value=CheckResult('verified', 'Mine', 481)), \
             patch('vkmgr.server.action') as write:
            status, result = self.post('/api/preflight', {'ids': [self.comment_id]})
            self.assertEqual(status, 200)
            self.assertEqual(result['results'][self.comment_id]['status'], 'verified')
            write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
