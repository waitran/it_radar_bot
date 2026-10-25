"""Favorites use disposable private storage and the real authenticated HTTP API."""
import json
import os
from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor
import unittest
from fastapi.testclient import TestClient
from miniapp.backend.config import Settings
from miniapp.backend.main import create_app
from miniapp.backend.favorites import Favorites
from tests import test_miniapp as support


class FavoriteTests(unittest.TestCase):
    def setUp(self):
        self.fixture = support.MiniAppTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client = self.fixture.client
        self.storage = self.client.app.state.favorites

    def request(self, method, path='/api/favorites', uid=42, **kwargs):
        headers={'Authorization':'tma '+support.signed(user=json.dumps({'id':uid,'first_name':'Test'}))}
        return self.client.request(method,path,headers=headers,**kwargs)

    def test_two_users_are_isolated_and_client_uid_is_never_trusted(self):
        self.assertEqual(self.request('PUT','/api/favorites/test:0').status_code,200)
        self.assertEqual(self.request('GET',uid=99).json()['total'],0)
        self.request('DELETE','/api/favorites/test:0?telegram_id=42',uid=99)
        self.assertEqual(self.request('GET').json()['total'],1)
        self.request('PUT','/api/favorites/test:0?telegram_id=42',uid=99,json={'telegram_id':42})
        self.request('DELETE','/api/favorites/test:0',uid=99)
        self.assertEqual(self.request('GET').json()['total'],1)
        self.assertEqual(self.request('GET',uid=99).json()['total'],0)

    def test_duplicate_save_keeps_original_timestamp_and_delete_is_idempotent(self):
        for _ in range(2):
            self.assertTrue(self.request('PUT','/api/favorites/test:0').json()['is_favorite'])
        original=self.request('GET').json()['items'][0]['created_at']
        self.request('PUT','/api/favorites/test:0')
        data=self.request('GET').json()
        self.assertEqual(data['total'],1)
        self.assertEqual(data['items'][0]['created_at'],original)
        for _ in range(2):
            self.assertEqual(self.request('DELETE','/api/favorites/test:0').json(),{'id':'test:0','is_favorite':False})
        self.assertEqual(self.request('GET').json()['total'],0)

    def test_restart_preserves_records(self):
        self.request('PUT','/api/favorites/test:0')
        with TestClient(create_app(Settings(self.fixture.path,support.TOKEN,user_db_path=str(self.storage.path)))) as restarted:
            data=restarted.get('/api/favorites',headers=self.fixture.headers).json()
            self.assertEqual(data['total'],1)
            self.assertEqual(data['items'][0]['event']['id'],'test:0')

    def test_missing_event_is_a_removable_tombstone(self):
        self.assertEqual(self.request('PUT','/api/favorites/missing').status_code,404)
        self.request('PUT','/api/favorites/test:0')
        with self.fixture.writer._conn:
            self.fixture.writer._conn.execute("UPDATE seen_items SET category=NULL WHERE item_key='test:0'")
        data=self.request('GET').json()
        self.assertEqual(data['total'],1)
        self.assertIsNone(data['items'][0]['event'])
        self.assertEqual(self.request('PUT','/api/favorites/test:0').status_code,404)
        self.assertEqual(self.request('DELETE','/api/favorites/test:0').status_code,200)

    def test_every_favorites_endpoint_requires_valid_auth(self):
        for method,path in [('GET','/api/favorites'),('PUT','/api/favorites/test:0'),('DELETE','/api/favorites/test:0')]:
            for headers in [{},{'Authorization':'tma '+support.signed(auth_date='1')},{'Authorization':'tma forged'}]:
                self.assertEqual(self.client.request(method,path,headers=headers).status_code,401)
        self.assertEqual(self.request('GET').json()['total'],0)

    def test_catalog_marks_favorite_per_verified_user(self):
        self.request('PUT','/api/favorites/test:0')
        self.assertTrue(self.request('GET','/api/events/test:0').json()['is_favorite'])
        self.assertFalse(self.request('GET','/api/events/test:0',uid=99).json()['is_favorite'])
        rows=self.request('GET','/api/events').json()['items']
        self.assertEqual({item['id'] for item in rows if item['is_favorite']},{'test:0'})

    def test_pagination_and_validation(self):
        for i in range(3):self.request('PUT',f'/api/favorites/test:{i}')
        first=self.request('GET','/api/favorites?page_size=1').json()
        second=self.request('GET','/api/favorites?page_size=1&page=2').json()
        self.assertEqual(first['total'],3)
        self.assertNotEqual(first['items'][0]['item_key'],second['items'][0]['item_key'])
        for query in ['page=0','page_size=51','page=10001']:
            self.assertEqual(self.request('GET','/api/favorites?'+query).status_code,422)
        self.assertEqual(self.request('PUT','/api/favorites/'+'x'*513).status_code,422)

    def test_separate_schema_and_catalog_unchanged_including_queues(self):
        before='\n'.join(self.fixture.writer._conn.iterdump())
        self.request('PUT','/api/favorites/test:0');self.request('GET');self.request('DELETE','/api/favorites/test:0')
        self.assertEqual(before,'\n'.join(self.fixture.writer._conn.iterdump()))
        with self.storage.connect() as conn:
            self.assertEqual([r[1] for r in conn.execute('PRAGMA table_info(favorites)')],['telegram_id','item_key','created_at'])
            self.assertEqual(conn.execute('PRAGMA foreign_key_list(favorites)').fetchall(),[])
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],1)
        self.assertEqual(os.stat(self.storage.path).st_mode & 0o777,0o600)

    def test_catalog_aliases_are_rejected_before_any_write(self):
        before='\n'.join(self.fixture.writer._conn.iterdump())
        aliases=[Path(self.fixture.path), Path(str(self.fixture.path)+'-wal')]
        link=Path(self.fixture.tmp.name)/'alias.db';link.symlink_to(self.fixture.path);aliases.append(link)
        hard=Path(self.fixture.tmp.name)/'hard.db';os.link(self.fixture.path,hard);aliases.append(hard)
        for path in aliases:
            with self.assertRaises(ValueError):Favorites(path,self.fixture.path)
        self.assertEqual(before,'\n'.join(self.fixture.writer._conn.iterdump()))

    def test_concurrent_initialization_and_duplicate_writes(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda _: Favorites(self.storage.path,self.fixture.path).save(42,'test:0'),range(12)))
        self.assertEqual(self.request('GET').json()['total'],1)

    def test_newer_user_schema_is_not_downgraded(self):
        with self.storage.connect() as conn, conn:
            conn.execute('PRAGMA user_version=2')
        with self.assertRaises(ValueError):Favorites(self.storage.path,self.fixture.path)
        with self.storage.connect() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],2)

    def test_unrelated_database_is_rejected_without_changing_its_journal(self):
        other=Path(self.fixture.tmp.name)/'unrelated.db'
        with sqlite3.connect(other) as conn:
            conn.execute('CREATE TABLE user_deliveries(id INTEGER)')
        with self.assertRaises(ValueError):Favorites(other,self.fixture.path)
        with sqlite3.connect(other) as conn:
            self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0],'delete')
            self.assertEqual([r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")],['user_deliveries'])
