"""Offline ZIP discovery/import regression tests; no VK network calls."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import http.client
import threading
from http.server import ThreadingHTTPServer

from vkmgr.archive_discovery import find_archives, read_selected_archive
from vkmgr.journal import Journal
import vkmgr.server as server


COMMENT_HTML = '<html><a href="https://vk.com/wall-10_20?reply=30">my comment</a></html>'


def make_zip(path, members):
    with zipfile.ZipFile(path, 'w') as archive:
        for name, body in members.items():
            archive.writestr(name, body)


class ArchiveDiscovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        make_zip(self.folder / 'vk-data.zip', {
            'archive/comments/comments0.html': COMMENT_HTML,
            'archive/profile.html': 'ignored',
        })
        make_zip(self.folder / 'other.zip', {
            'private_notes.txt': 'not a VK archive',
        })
        (self.folder / 'broken.zip').write_bytes(b'not a ZIP')

    def tearDown(self):
        self.tmp.cleanup()

    def test_manifest_only_returns_comment_archives(self):
        found = find_archives(self.folder)
        self.assertEqual([x['name'] for x in found], ['vk-data.zip'])
        self.assertEqual(found[0]['pages'], 1)
        self.assertGreater(found[0]['size'], 0)

    def test_explicit_import_reads_and_parses_selected_archive(self):
        comments, summary = read_selected_archive('vk-data.zip', self.folder)
        self.assertEqual(summary['files'], 1)
        self.assertEqual(summary['found'], 1)
        self.assertEqual(comments[0].id, 'wall:-10:20:30')

    def test_rejects_traversal_and_unrelated_files(self):
        for name in ('../vk-data.zip', '/tmp/vk-data.zip', '..', 'other.zip',
                     'broken.zip', 'missing.zip', 'private_notes.txt', ''):
            with self.subTest(name=name), self.assertRaises(ValueError):
                read_selected_archive(name, self.folder)

    def test_symlink_does_not_get_selected_or_imported(self):
        (self.folder / 'linked.zip').symlink_to(self.folder / 'vk-data.zip')
        self.assertNotIn('linked.zip', [x['name'] for x in find_archives(self.folder)])
        with self.assertRaises(ValueError):
            read_selected_archive('linked.zip', self.folder)

    def test_never_extracts_archive_to_disk(self):
        make_zip(self.folder / 'archive-two.zip', {
            '../../should-not-exist.html': '<html></html>',
            'comments1.html': COMMENT_HTML,
        })
        comments, _ = read_selected_archive('archive-two.zip', self.folder)
        self.assertEqual(len(comments), 1)
        self.assertFalse((self.folder / 'should-not-exist.html').exists())

    def test_realistic_large_zip_is_discovered_without_scanning_media_comments(self):
        large = {
            "comments/comments.html":
                '<div class="item"><a href="https://vk.ru/wall-10_20?reply=30">'
                'Authored</a></div>',
            "comments/comments2.html":
                '<div class="item"><a href="https://vk.ru/wall-10_20?reply=31">'
                'Authored</a></div>',
            "photos/photo-comments/999/comments0.html":
                '<a href="https://vk.ru/wall-20_30?reply=40">Media context</a>',
            "wall/888/comments0.html":
                '<a href="https://vk.ru/wall-20_30?reply=41">Other people</a>',
        }
        for n in range(2100):
            large["messages/msg{}.html".format(n)] = "PRIVATE PLACEHOLDER"
        make_zip(self.folder / "large-export.zip", large)
        matches = find_archives(self.folder)
        large_match = next(x for x in matches if x["name"] == "large-export.zip")
        self.assertEqual(large_match["pages"], 2)
        records, summary = read_selected_archive("large-export.zip", self.folder)
        self.assertEqual(summary["files"], 2)
        self.assertEqual(summary["found"], 2)
        self.assertEqual({x.comment_id for x in records}, {30, 31})

    def test_reads_no_comment_contents_during_discovery(self):
        with patch('vkmgr.archive_discovery.zipfile.ZipFile.read', side_effect=AssertionError('read called')):
            matches = find_archives(self.folder)
        self.assertEqual([x['name'] for x in matches], ['vk-data.zip'])


class ArchiveDiscoveryHTTP(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.downloads = Path(self.temp.name) / 'Downloads'
        self.downloads.mkdir()
        make_zip(self.downloads / 'vk-archive.zip', {'comments0.html': COMMENT_HTML})
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

    def post(self, path, obj):
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=3)
        connection.request('POST', path, body=json.dumps(obj), headers={
            'Host': f'127.0.0.1:{self.http.server_port}',
            'Content-Type': 'application/json',
            'X-VKCM-CSRF': server.STATE.csrf,
        })
        response = connection.getresponse()
        result = response.status, json.loads(response.read().decode('utf-8'))
        connection.close()
        return result

    def test_scan_then_import_without_network_or_deletion(self):
        with patch('vkmgr.archive_discovery.default_downloads', return_value=self.downloads), \
             patch('vkmgr.server.action') as action, \
             patch('vkmgr.server.verify_comment') as verify:
            status, response = self.post('/api/find-archives', {})
            self.assertEqual(status, 200)
            self.assertEqual(len(response['archives']), 1)
            self.assertEqual(response['archives'][0]['name'], 'vk-archive.zip')
            status, response = self.post('/api/import-downloaded', {'name': 'vk-archive.zip'})
            self.assertEqual(status, 200)
            self.assertEqual(response['found'], 1)
            self.assertEqual(len(server.STATE.comments), 1)
            action.assert_not_called()
            verify.assert_not_called()

    def test_scan_requires_csrf(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=3)
        conn.request('POST', '/api/find-archives', body='{}', headers={
            'Content-Type': 'application/json',
            'Host': f'127.0.0.1:{self.http.server_port}',
        })
        response = conn.getresponse()
        self.assertEqual(response.status, 403)
        response.read()
        conn.close()


if __name__ == '__main__':
    unittest.main()
