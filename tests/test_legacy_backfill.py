"""One-time batched legacy alias migration, including restart and rollback."""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from app.db import Database, SCHEMA
from app.models import Item


class LegacyBackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=str(Path(self.tmp.name)/'state.db')
        conn=sqlite3.connect(self.path);conn.executescript(SCHEMA)
        conn.executemany('INSERT INTO seen_items(item_key,source_key,external_id,url,category,first_seen_at,notified) VALUES(?,?,?,?,?,?,1)',
            ((f'old:{n}','old',str(n),f'https://example.test/jobs/{n}','internships',datetime.now(timezone.utc).isoformat()) for n in range(1101)))
        conn.commit();conn.close()

    def test_batched_backfill_is_not_repeated_after_restart_and_keeps_dedup(self):
        db=Database(self.path)
        self.assertEqual(db._conn.execute('SELECT COUNT(*) FROM item_aliases').fetchone()[0],1101)
        self.assertEqual(db.get_flag('legacy_aliases_backfilled'),'1');db.close()
        original=sqlite3.connect;statements=[]
        def connect(*args,**kwargs):
            conn=original(*args,**kwargs);conn.set_trace_callback(statements.append);return conn
        with patch('app.db.sqlite3.connect',side_effect=connect):db=Database(self.path)
        try:
            self.assertFalse(any('payload IS NULL AND category IS NOT NULL' in sql for sql in statements))
            self.assertFalse(any('INSERT OR IGNORE INTO item_aliases' in sql for sql in statements))
            other=Item('new','New','1','https://example.test/jobs/1','Python','','',None)
            self.assertTrue(db.is_duplicate(other))
        finally:db.close()

    def test_failure_rolls_back_aliases_and_completion_marker(self):
        conn=sqlite3.connect(self.path)
        conn.executescript("""CREATE TABLE item_aliases(alias TEXT NOT NULL,item_key TEXT NOT NULL,PRIMARY KEY(alias,item_key));
            CREATE TRIGGER injected BEFORE INSERT ON item_aliases WHEN NEW.item_key='old:500'
            BEGIN SELECT RAISE(ABORT,'injected'); END;""")
        conn.close()
        with self.assertRaises(sqlite3.IntegrityError):Database(self.path)
        conn=sqlite3.connect(self.path)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM item_aliases').fetchone()[0],0)
        self.assertIsNone(conn.execute("SELECT value FROM flags WHERE name='legacy_aliases_backfilled'").fetchone())
        conn.execute('DROP TRIGGER injected');conn.commit();conn.close()
        db=Database(self.path)
        try:self.assertEqual(db.get_flag('legacy_aliases_backfilled'),'1')
        finally:db.close()
