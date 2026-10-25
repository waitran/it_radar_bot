"""API integration uses disposable databases; Telegram is never contacted."""
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from urllib.parse import urlencode
from fastapi.testclient import TestClient
from app.db import Database
from app.models import Item
from datetime import datetime, timezone
from miniapp.backend.config import Settings
from miniapp.backend.main import create_app

TOKEN = 'synthetic-test-key'


def signed(**overrides):
    data = {'auth_date':str(int(time.time())), 'user':json.dumps({'id':42,'first_name':'Test'})}
    data.update(overrides)
    secret = hmac.digest(b'WebAppData', TOKEN.encode(), 'sha256')
    data['hash'] = hmac.new(secret, '\n'.join(f'{k}={v}' for k,v in sorted(data.items())).encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


class MiniAppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name)/'bot.db')
        self.writer = Database(self.path)
        self.addCleanup(self.writer.close)
        self.writer._conn.execute('PRAGMA journal_mode=WAL')
        for i, title in enumerate(['Python meetup', 'Хакатон SQL', 'No date']):
            item = Item('test','Example',str(i),'https://example.org',title,'Description',None,
                        datetime(2026,10,20+i,tzinfo=timezone.utc) if i<2 else None)
            item.categories = ['events','contests'] if i==1 else ['events']
            self.writer.add_item(item, category='events')
        self.client = TestClient(create_app(Settings(self.path,TOKEN)))
        self.addCleanup(self.client.close)
        self.headers = {'Authorization':'tma '+signed()}

    def get(self,path,**kwargs):
        return self.client.get(path,headers=self.headers,**kwargs)

    def test_auth_valid_and_forged(self):
        self.assertEqual(self.get('/api/me').json()['id'],42)
        self.assertEqual(self.client.get('/api/me',headers={'Authorization':'tma '+signed().replace('Test','Evil')}).status_code,401)

    def test_missing_expired_future_malformed_and_duplicate(self):
        for endpoint in ['/api/me','/api/events','/api/events/test:0']:
            self.assertEqual(self.client.get(endpoint).status_code,401)
        for value in [signed(auth_date='1'),signed(auth_date=str(int(time.time())+3600)),
                      signed(user='{}'), signed()+'&auth_date=1', '%xx']:
            self.assertEqual(self.client.get('/api/me',headers={'Authorization':'tma '+value}).status_code,401)

    def test_signature_field_is_included_in_hmac(self):
        self.assertEqual(self.client.get('/api/me',headers={'Authorization':'tma '+signed(signature='telegram-signature')}).status_code,200)

    def test_catalog_sort_search_category_pagination(self):
        data = self.get('/api/events?page_size=1').json()
        self.assertEqual(data['total'],3)
        self.assertEqual(data['items'][0]['title'],'Хакатон SQL')
        self.assertEqual(self.get('/api/events?page_size=1&page=2').json()['items'][0]['title'],'Python meetup')
        self.assertEqual(self.get('/api/events?q=хАКАтон&category=contests').json()['total'],1)
        self.assertEqual(self.get('/api/events?q=description').json()['total'],3)
        self.assertEqual(self.get('/api/events?q=%25').json()['total'],0)
        self.assertEqual(self.get('/api/events?q=%27%20OR%201=1--').json()['total'],0)
        self.assertEqual(self.get('/api/events?sort=asc').json()['items'][0]['title'],'Python meetup')
        self.assertEqual(self.get('/api/events?page=999').json()['items'],[])

    def test_detail_dates_and_private_fields(self):
        data = self.get('/api/events/test:2').json()
        for name in ['published_at','event_at','deadline_at']:
            self.assertIsNone(data[name])
        self.assertNotIn('payload',data)
        self.assertNotIn('delivery_state',data)
        self.assertEqual(self.get('/api/events/absent').status_code,404)

    def test_active_filter(self):
        self.assertEqual(self.get('/api/events?active_only=true').json()['total'],0)
        with self.writer._conn:
            self.writer._conn.execute("UPDATE event_metadata SET event_at=?,registration_status='unknown' WHERE item_id=(SELECT id FROM item_refs WHERE item_key='test:0')",(time.time()+3600,))
        self.assertEqual(self.get('/api/events?active_only=true').json()['total'],1)
        with self.writer._conn:
            self.writer._conn.execute("UPDATE event_metadata SET deadline_at=?",(time.time()-1,))
        self.assertEqual(self.get('/api/events?active_only=true').json()['total'],0)

    def test_validation(self):
        for query in ['page=0','page_size=51','category=unknown','sort=invalid','q='+'a'*201]:
            self.assertEqual(self.get('/api/events?'+query).status_code,422)

    def test_read_with_writer_and_no_mutations(self):
        before = '\n'.join(self.writer._conn.iterdump())
        self.writer._conn.execute('BEGIN IMMEDIATE')
        try:
            self.writer._conn.execute("UPDATE seen_items SET title='uncommitted' WHERE item_key='test:0'")
            self.assertEqual(self.get('/api/events').status_code,200)
            self.assertEqual(self.get('/api/events/test:0').json()['title'],'Python meetup')
        finally:
            self.writer._conn.rollback()
        self.assertEqual(before,'\n'.join(self.writer._conn.iterdump()))
        with self.client.app.state.catalog.connect() as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute('DELETE FROM seen_items')
        # Existing exclusive bot lock is still held, yet API can read.
        with self.assertRaises(RuntimeError):
            Database(self.path)

    def test_missing_db_and_sanitized_errors(self):
        missing = str(Path(self.tmp.name)/'missing.db')
        with TestClient(create_app(Settings(missing,TOKEN))) as client:
            response = client.get('/api/health')
            self.assertEqual(response.status_code,503)
            self.assertNotIn(self.tmp.name,response.text)
        self.assertFalse(Path(missing).exists())

    def test_health(self):
        self.assertEqual(self.client.get('/api/health').json(),{'status':'ok'})

    def test_legacy_schema_without_event_index(self):
        with self.writer._conn:
            self.writer._conn.execute('DROP TABLE event_metadata')
        data = self.get('/api/events').json()
        self.assertEqual(data['total'],3)
        self.assertIsNone(data['items'][0]['event_at'])
        self.assertEqual(self.get('/api/events?active_only=true').json()['total'],0)

    def test_closed_future_event_and_response_limits(self):
        with self.writer._conn:
            self.writer._conn.execute("UPDATE event_metadata SET event_at=?,registration_status='closed'",(time.time()+3600,))
            self.writer._conn.execute("UPDATE seen_items SET summary=?,url='/private/file' WHERE item_key='test:0'",('a'*20000,))
        self.assertEqual(self.get('/api/events?active_only=true').json()['total'],0)
        data = self.get('/api/events/test:0').json()
        self.assertEqual(len(data['summary']),8000)
        self.assertIsNone(data['url'])
        self.assertEqual(self.client.post('/api/events',headers=self.headers).status_code,405)

    def test_configuration_has_no_auth_bypass(self):
        with self.assertRaises(ValueError):
            Settings(self.path,'')
        with self.assertRaises(ValueError):
            Settings(self.path,TOKEN,0)
