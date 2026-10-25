import sqlite3
from app.db import Database
from tests.personal_support import PersonalCase

class MigrationV7Tests(PersonalCase):
    def test_v6_backup_preserves_history_cursors_offset_and_subscription(self):
        item,ref=self.event();self.db.set_flag('offset','123');self.db.save_telegram_cursor('tg_example',77)
        self.bot.fill_delivery_queue(__import__('datetime').datetime.now().astimezone())
        before=[tuple(r) for r in self.db._conn.execute('SELECT * FROM user_deliveries')]
        self.db.close()
        with sqlite3.connect(self.cfg.db_path) as conn:
            conn.execute('PRAGMA user_version=6')
            for name in ['reminder_jobs','saved_items','event_metadata','item_refs','user_preferences']:conn.execute('DROP TABLE '+name)
            conn.execute("DELETE FROM flags WHERE name='event_index_backfilled'")
        self.bot.db=Database(self.cfg.db_path);self.db=self.bot.db
        self.assertTrue(self.db.backup_path)
        self.assertEqual(self.db._conn.execute('PRAGMA user_version').fetchone()[0],7)
        self.assertEqual(self.db.get_flag('offset'),'123');self.assertEqual(self.db.telegram_cursor('tg_example'),77)
        self.assertEqual(before,[tuple(r) for r in self.db._conn.execute('SELECT * FROM user_deliveries')])
        self.assertTrue(self.db.user(101)['is_active']);self.assertEqual(self.db.preferences(101)['topics'],[])
        self.assertIsNotNone(self.db.item_ref(item.item_key))
        self.db.close();self.bot.db=Database(self.cfg.db_path);self.db=self.bot.db
        self.assertIsNone(self.db.backup_path)

    def test_failed_index_migration_rolls_back_and_can_resume(self):
        from unittest.mock import patch
        self.event();self.db.close()
        with sqlite3.connect(self.cfg.db_path) as conn:
            conn.execute('PRAGMA user_version=6')
            for name in ['reminder_jobs','saved_items','event_metadata','item_refs','user_preferences']:conn.execute('DROP TABLE '+name)
            conn.execute("DELETE FROM flags WHERE name='event_index_backfilled'")
        with patch('app.personal_db.PersonalDatabase._index_item',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):Database(self.cfg.db_path)
        with sqlite3.connect(self.cfg.db_path) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],6)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM seen_items').fetchone()[0],1)
            self.assertIsNone(conn.execute("SELECT value FROM flags WHERE name='event_index_backfilled'").fetchone())
        self.bot.db=Database(self.cfg.db_path);self.db=self.bot.db
        self.assertEqual(self.db._conn.execute('PRAGMA user_version').fetchone()[0],7)
