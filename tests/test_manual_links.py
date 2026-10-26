"""Offline manual link import tests; never calls the VK network."""
import http.client
import json
import tempfile
import threading
from pathlib import Path
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer

from vkmgr.manual_links import parse_manual_links
from vkmgr.journal import Journal
import vkmgr.server as server


class LinkParser(unittest.TestCase):
    def test_all_three_categories(self):
        records, counts = parse_manual_links(
            "https://vk.com/wall-7_11?reply=41\n"
            "https://vk.com/photo13_25?comment=64\n"
            "https://vk.ru/video7_21?comment_id=102")
        self.assertEqual(counts['recognized'], 3)
        self.assertEqual([r.kind for r in records], ['wall', 'photo', 'video'])
        self.assertEqual(records[0].id, 'wall:-7:11:41')

    def test_dedupe_and_sources(self):
        records, counts = parse_manual_links(
            "https://vk.com/wall-7_11?reply=41\n"
            "https://vk.com/wall-7_11?reply=41")
        self.assertEqual(len(records), 1)
        self.assertEqual(counts['duplicates'], 1)
        self.assertEqual(records[0].source, 'manual')

    def test_rejects_unsafe_or_ambiguous(self):
        for raw in (
            'https://vk.com.evil.test/wall-7_11?reply=41',
            'https://vk.com/wall-7_11',
            'javascript:alert(1)',
            'https://other.example.com/wall-7_11?reply=41',
            '\n',
            'https://vk.com/wall-7_11?reply=41\ninvalid',
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    parse_manual_links(raw)

    def test_50_link_limit(self):
        text = '\n'.join(f'https://vk.com/wall-7_11?reply={i+1}' for i in range(50))
        records, _ = parse_manual_links(text)
        self.assertEqual(len(records), 50)
        with self.assertRaises(ValueError):
            parse_manual_links(text + '\nhttps://vk.com/wall-7_11?reply=99')


class ManualLinksHTTP(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous = server.STATE
        server.STATE = server.AppState(Journal(Path(self.temp.name) / 'progress.sqlite3'))
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)
        server.STATE = self.previous
        self.temp.cleanup()

    def call(self, uri, obj):
        conn = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=2)
        conn.request('POST', uri, json.dumps(obj), {
            'Host': f'127.0.0.1:{self.http.server_port}',
            'Content-Type': 'application/json',
            'X-VKCM-CSRF': server.STATE.csrf,
        })
        resp = conn.getresponse()
        out = resp.status, json.loads(resp.read())
        conn.close()
        return out

    def test_added_without_network_and_idempotent(self):
        link = 'https://vk.com/wall-7_11?reply=41'
        with patch('vkmgr.server.action') as write, patch('vkmgr.server.verify_comment') as verify:
            code, item = self.call('/api/add-links', {'links': link})
            self.assertEqual(code, 200)
            self.assertEqual(item['added'], 1)
            self.assertEqual(server.STATE.import_meta['found'], 1)
            self.assertEqual(len(server.STATE.comments), 1)
            code, item = self.call('/api/add-links', {'links': link})
            self.assertEqual(item['added'], 0)
            write.assert_not_called()
            verify.assert_not_called()

    def test_import_failure_does_not_replace_existing_comments(self):
        self.call('/api/add-links', {'links': 'https://vk.com/wall-7_11?reply=41'})
        code, _ = self.call('/api/add-links', {'links': 'https://vk.com/wall-7_11'})
        self.assertEqual(code, 400)
        self.assertEqual(len(server.STATE.comments), 1)

    def test_refuses_changes_while_batch_running(self):
        server.STATE.job['running'] = True
        code, result = self.call('/api/add-links', {'links': 'https://vk.com/wall-7_11?reply=41'})
        self.assertEqual(code, 409)
        self.assertIn('Finish current operation', result['error'])


if __name__ == '__main__':
    unittest.main()
