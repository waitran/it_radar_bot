"""Regular online snapshots, retention, cancellation and offline recovery."""
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from app.backup import create_backup, restore_backup, verify_backup, BackupCancelled
from app.db import Database


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);self.path=root/'data'/'state.db';self.copies=root/'backups'
        self.db=Database(str(self.path));self.addCleanup(self.db.close)
        self.db.user_control(101,active=True);self.db.set_flag('offset','77')

    def test_live_snapshot_restores_users_flags_and_schema(self):
        from app.models import Item
        from datetime import datetime,timezone
        entry=Item('source','Source','1','https://example.test/jobs/1','Python','','',None)
        self.db.add_item(entry,category='internships');self.db.fill_user_queue()
        row=self.db.user_delivery(101,entry.item_key)
        self.db.claim_user_delivery(row['id'],datetime.now(timezone.utc))
        self.db.finish_user_delivery(row['id'],'sent','test',message_id=42)
        queued=Item('source','Source','2','https://example.test/jobs/2','Python 2','','',None)
        self.db.add_item(queued,category='internships');self.db.fill_user_queue()
        snapshot=create_backup(self.path,self.copies)
        self.assertEqual(verify_backup(snapshot)['users'],1)
        target=self.path.parent/'restored.db'
        restore_backup(snapshot,target)
        restored=Database(str(target))
        try:
            self.assertTrue(restored.user(101)['is_active'])
            self.assertEqual(restored.get_flag('offset'),'77')
            delivery=restored.user_delivery(101,entry.item_key)
            self.assertEqual((delivery['status'],delivery['attempts'],delivery['telegram_message_id']),('sent',1,42))
            self.assertEqual(restored.user_delivery(101,queued.item_key)['status'],'legacy_review')
            archived=Item('source','Source','3','https://example.test/jobs/3','Python 3','','',datetime.fromtimestamp(1,timezone.utc))
            restored.add_item(archived,category='internships');restored.fill_user_queue()
            self.assertIsNone(restored.user_delivery(101,archived.item_key))
        finally:restored.close()
        self.assertEqual(snapshot.stat().st_mode & 0o777,0o600)

    def test_retention_keeps_completed_copies_and_leaves_unknown_files_alone(self):
        self.copies.mkdir();other=self.copies/'manual.sqlite3';other.write_text('untouched')
        for _ in range(4):create_backup(self.path,self.copies,keep=2)
        self.assertEqual(len(list(self.copies.glob('it-radar-*.sqlite3'))),2)
        self.assertEqual(other.read_text(),'untouched')
        self.assertEqual(list(self.copies.glob('*.part')),[])

    def test_cancelled_copy_preserves_previous_snapshot_and_cleans_partial(self):
        first=create_backup(self.path,self.copies)
        stop=threading.Event();stop.set()
        with self.assertRaises(BackupCancelled):create_backup(self.path,self.copies,stop=stop)
        self.assertTrue(first.exists());self.assertEqual(verify_backup(first)['users'],1)
        self.assertEqual(list(self.copies.glob('*.part')),[])

    def test_restore_cannot_replace_live_database(self):
        snapshot=create_backup(self.path,self.copies)
        with self.assertRaises(RuntimeError):restore_backup(snapshot,self.path,replace=True,safety_dir=self.copies)
        self.assertEqual(self.db.get_flag('offset'),'77')

    def test_replacement_requires_explicit_opt_in_and_preserves_prior_state(self):
        snapshot=create_backup(self.path,self.copies)
        self.db.set_flag('offset','88');self.db.close()
        with self.assertRaises(ValueError):restore_backup(snapshot,self.path)
        restore_backup(snapshot,self.path,replace=True,safety_dir=self.copies)
        self.db=Database(str(self.path));self.addCleanup(self.db.close)
        self.assertEqual(self.db.get_flag('offset'),'77')
        safety=list(self.copies.glob('it-radar-before-restore-*.sqlite3'))
        self.assertEqual(len(safety),1)
        conn=sqlite3.connect(safety[0])
        try:self.assertEqual(conn.execute("SELECT value FROM flags WHERE name='offset'").fetchone()[0],'88')
        finally:conn.close()

    def test_invalid_snapshot_cannot_touch_target(self):
        invalid=self.copies/'invalid.sqlite3';self.copies.mkdir();invalid.write_bytes(b'not sqlite')
        with self.assertRaises(sqlite3.DatabaseError):restore_backup(invalid,self.path,replace=True,safety_dir=self.copies)
        self.assertEqual(self.db.get_flag('offset'),'77')

    def test_committed_wal_data_is_included_without_stopping_source(self):
        self.db._conn.execute('PRAGMA journal_mode=WAL')
        self.db.set_flag('offset','99')
        snapshot=create_backup(self.path,self.copies)
        conn=sqlite3.connect(snapshot)
        try:self.assertEqual(conn.execute("SELECT value FROM flags WHERE name='offset'").fetchone()[0],'99')
        finally:conn.close()

    def test_periodic_worker_records_success_and_retries_failures_without_touching_db(self):
        from app.config import Config
        from app.main import Bot
        from unittest.mock import patch
        root=Path(__file__).resolve().parents[1]
        self.db.close()
        cfg=Config.load(str(root/'config.yaml'));cfg.db_path=str(self.path);cfg.owner_id=0
        cfg.backup_dir=str(self.copies)
        bot=Bot(cfg);self.addCleanup(bot.db.close)
        stop=threading.Event()
        def copy_and_stop(*args,**kwargs):
            result=create_backup(*args,**kwargs);stop.set();return result
        with patch('app.main.create_backup',side_effect=copy_and_stop):bot._backup_loop(stop)
        self.assertIsNotNone(bot.db.get_flag('last_backup_at'))
        self.assertIsNone(bot.backup_error)
        bot.db.set_flag('last_backup_at','0');stop.clear()
        def fail(*args,**kwargs):stop.set();raise PermissionError('private path')
        with patch('app.main.create_backup',side_effect=fail):bot._backup_loop(stop)
        self.assertEqual(bot.backup_error,'PermissionError')
        self.assertEqual(bot.db.get_flag('last_backup_at'),'0')
        self.assertEqual(bot.db.get_flag('offset'),'77')
