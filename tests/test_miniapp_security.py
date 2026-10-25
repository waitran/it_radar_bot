"""Security and user-storage maintenance with disposable databases only."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
import os
from unittest.mock import patch
from fastapi.testclient import TestClient
from miniapp.backend.config import Settings
from miniapp.backend.main import create_app
from miniapp.backend.security import RateLimiter
from miniapp.backend.db import public_url
from miniapp.backend.maintenance import backup, restore, verify
from miniapp.backend.storage_init import prepare
from tests import test_miniapp as support


class MiniAppSecurityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = support.MiniAppTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client = self.fixture.client
        self.path = self.client.app.state.favorites.path

    def test_verified_user_and_aggregate_limits_cannot_be_spoofed_by_forwarded_headers(self):
        app = create_app(Settings(self.fixture.path, support.TOKEN, user_rate_limit=2, global_rate_limit=20))
        with TestClient(app) as client:
            for _ in range(2):
                self.assertEqual(client.get('/api/me', headers=self.fixture.headers).status_code, 200)
            response = client.get('/api/me', headers={**self.fixture.headers, 'X-Forwarded-For': '8.8.8.8'})
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.headers['retry-after'], '60')
            self.assertEqual(client.get('/api/health').status_code, 200)
        limiter = RateLimiter(1, capacity=2)
        self.assertTrue(limiter.allow('a', now=0)); self.assertFalse(limiter.allow('a', now=1))
        self.assertTrue(limiter.allow('a', now=61))
        limiter.allow('b'); limiter.allow('c')
        self.assertEqual(len(limiter._buckets), 2)

    def test_size_limits_and_no_cors_permission(self):
        for response in [self.client.put('/api/favorites/test:0', headers=self.fixture.headers, content=b'x'*16385),
                         self.client.get('/api/events?q='+'x'*2050, headers=self.fixture.headers)]:
            self.assertEqual(response.status_code, 413)
        response = self.client.options('/api/favorites', headers={'Origin':'https://evil.example','Access-Control-Request-Method':'PUT'})
        self.assertNotIn('access-control-allow-origin', response.headers)

    def test_safe_public_urls(self):
        for value in ['https://user:secret@example.org/', 'https://example.org:99999/',
                      'https://example.org/\nsecret', 'https://example.org\\secret', 'file:///data/bot.db']:
            self.assertIsNone(public_url(value))
        self.assertEqual(public_url('https://example.org/event'), 'https://example.org/event')

    def test_online_backup_offline_restore_and_running_api_guard(self):
        store = self.client.app.state.favorites
        store.save(42, 'test:0')
        directory = Path(self.fixture.tmp.name)/'backups'
        snapshot = backup(self.path, directory, catalog=self.fixture.path)
        self.assertEqual(verify(snapshot)['favorites'], 1)
        store.save(42, 'test:1')
        with TestClient(self.client.app):
            with self.assertRaises(sqlite3.OperationalError):
                restore(snapshot, self.path, catalog=self.fixture.path, replace=True, safety_dir=directory)
        restore(snapshot, self.path, catalog=self.fixture.path, replace=True, safety_dir=directory)
        self.assertEqual(store.page(42, 1, 20)[1], 1)
        self.assertEqual(len(list(directory.glob('*.sqlite3'))), 2)
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)

    def test_backup_and_restore_never_touch_bot_schema_or_allow_wrong_snapshots(self):
        before='\n'.join(self.fixture.writer._conn.iterdump())
        with self.assertRaises(ValueError):
            restore(self.fixture.path, self.path, catalog=self.fixture.path)
        with self.assertRaises(ValueError):
            restore(self.path, self.fixture.path, catalog=self.fixture.path)
        self.assertEqual(before,'\n'.join(self.fixture.writer._conn.iterdump()))

    def test_storage_init_refuses_links_and_root_uid(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'root';root.mkdir()
            target=Path(directory)/'outside';target.write_text('unchanged')
            (root/'link').symlink_to(target)
            with self.assertRaises(OSError):prepare([root],10001,10001)
            self.assertEqual(target.read_text(),'unchanged')
            with self.assertRaises(ValueError):prepare([root],0,10001)

    def test_production_configuration_fails_closed_and_redacts_invalid_values(self):
        for values in [{}, {'TELEGRAM_BOT_TOKEN':'private-invalid'},
                       {'TELEGRAM_BOT_TOKEN':'999999:'+('x'*32),'MINIAPP_AUTH_TTL':'private-invalid'}]:
            with patch.dict(os.environ, values, clear=True):
                with self.assertRaises(ValueError) as caught:Settings.from_env()
                self.assertNotIn('private-invalid',str(caught.exception))
