"""Allowlisted experiment media retain authentication and bundle containment."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from infofilm.auth import Auth
from infofilm.core import write
from infofilm.web import PUBLIC_ORIGIN, create_app


class ExperimentMediaTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.base = self.root / 'artifacts/experiments/cli-smoke'
        self.base.mkdir(parents=True)
        self.files = ['report.html', 'delivery/film.mp4', 'delivery/narration.wav', 'delivery/captions.vtt']
        for name in self.files:
            target = self.base / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'0123456789abcdef')
        self.publish(self.files)
        config = write(self.root / 'config.json', {'planner_url': 'http://planner/v1',
            'reviewer_url': 'http://reviewer/v1', 'tts_url': 'http://tts', 'image_url': 'http://images'})
        Auth(self.root / 'interface').set_password('test-user', 'test-only-password')
        root_patch = patch('infofilm.web.ROOT', self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        queue_patch = patch('infofilm.queue.ROOT', self.root)
        queue_patch.start()
        self.addCleanup(queue_patch.stop)
        self.client = TestClient(create_app(config, self.root / 'interface', start_worker=False))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def publish(self, files):
        write(self.base / 'public-report.json', {'schema_version': 'infofilm.public-experiment-report.v1', 'files': files})

    def login(self):
        response = self.client.post('/api/login', json={'username': 'test-user', 'password': 'test-only-password'})
        self.assertEqual(response.status_code, 200)

    def url(self, name):
        return '/experiments/cli-smoke/' + name

    def test_new_media_need_authentication_then_have_explicit_mime_and_private_headers(self):
        for name in self.files[1:]:
            self.assertEqual(self.client.get(self.url(name)).status_code, 401)
            self.assertEqual(self.client.head(self.url(name)).status_code, 401)
        self.login()
        for name, mime in [('delivery/film.mp4', 'video/mp4'), ('delivery/narration.wav', 'audio/wav'),
                           ('delivery/captions.vtt', 'text/vtt')]:
            with self.subTest(name=name):
                response = self.client.get(self.url(name))
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.headers['content-type'].startswith(mime))
                self.assertEqual(response.headers['cache-control'], 'private, no-store')
                self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
                head = self.client.head(self.url(name))
                self.assertEqual(head.status_code, 200)
                self.assertEqual(head.content, b'')

    def test_authenticated_mp4_range_requests_support_video_playback(self):
        self.login()
        response = self.client.get(self.url('delivery/film.mp4'), headers={'Range': 'bytes=2-5'})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b'2345')
        self.assertEqual(response.headers['content-range'], 'bytes 2-5/16')
        self.assertEqual(response.headers['content-type'], 'video/mp4')

    def test_unlisted_media_archives_and_marker_remain_private(self):
        self.login()
        for name in ['private.wav', 'requests/model.request.json', 'research-source-archive/raw.json']:
            target = self.base / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('private archive')
            self.assertEqual(self.client.get(self.url(name)).status_code, 404)
        self.assertEqual(self.client.get(self.url('public-report.json')).status_code, 404)
        self.assertEqual(self.client.get(self.url('delivery/missing.mp4')).status_code, 404)

    def test_even_allowlisted_traversal_symlinks_and_unsupported_extensions_are_denied(self):
        self.login()
        (self.base.parent / 'private.wav').write_bytes(b'outside bundle')
        outside = self.root / 'outside.mp4'
        outside.write_bytes(b'outside bundle')
        (self.base / 'linked.mp4').symlink_to(outside)
        (self.base / 'active.svg').write_text('<svg/>')
        self.publish(self.files + ['../private.wav', 'linked.mp4', 'active.svg'])
        for name in ['%2e%2e%2fprivate.wav', 'linked.mp4', 'active.svg']:
            with self.subTest(name=name):
                self.assertEqual(self.client.get(self.url(name)).status_code, 404)

    def test_publication_requires_exact_file_list_and_valid_marker(self):
        self.login()
        for value in [{'schema_version': 'infofilm.public-experiment-report.v1', 'files': 'delivery/film.mp4'},
                      {'schema_version': 'wrong', 'files': self.files},
                      ['delivery/film.mp4']]:
            write(self.base / 'public-report.json', value)
            self.assertEqual(self.client.get(self.url('delivery/film.mp4')).status_code, 404)
        (self.base / 'public-report.json').unlink()
        self.assertEqual(self.client.get(self.url('delivery/film.mp4')).status_code, 404)

    def test_media_keep_origin_access_control(self):
        self.login()
        self.assertEqual(self.client.get(self.url('delivery/film.mp4'),
            headers={'Origin': 'https://untrusted.example'}).status_code, 403)
        response = self.client.get(self.url('delivery/film.mp4'), headers={'Origin': PUBLIC_ORIGIN})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['access-control-allow-origin'], PUBLIC_ORIGIN)


if __name__ == '__main__':
    unittest.main()
