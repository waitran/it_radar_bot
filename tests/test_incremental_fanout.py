"""A07: bounded, durable audience pagination independent of completed history size."""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from app.db import Database
from tests.test_delivery import item


class IncrementalFanoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=str(Path(self.tmp.name)/'state.db')
        self.db=Database(self.path);self.addCleanup(lambda:self.db.close())

    def users(self, identifiers):
        self.db._conn.executemany('INSERT INTO users(telegram_id,username,is_active,created_at,updated_at,subscribed_at) VALUES(?,NULL,1,0,0,1)',((uid,) for uid in identifiers))
        self.db._conn.commit()

    def add(self,key='1',source='s'):
        entry=item(key,source);entry.published_at=datetime.now(timezone.utc)
        self.db.add_item(entry,category='internships')
        return entry

    def progress(self, entry):
        row=self.db._conn.execute('SELECT * FROM delivery_fanout WHERE item_key=?',(entry.item_key,)).fetchone()
        return dict(row) if row else None

    def complete_ready(self):
        for row in self.db.user_pending(datetime.now(timezone.utc),limit=10000):
            self.db.finish_user_delivery(row['delivery_id'],'sent','test',message_id=42)

    def test_capacity_mid_page_does_not_skip_recipients_and_progress_survives_restart(self):
        self.users(range(1,7));entry=self.add()
        stats=self.db.fill_user_queue(capacity=2,batch=500)
        self.assertEqual((stats['created'],stats['users_examined']),(2,2))
        self.assertEqual(self.progress(entry)['cursor_id'],2)
        self.db.fill_user_queue(capacity=2,batch=500)
        self.assertEqual(self.progress(entry)['cursor_id'],2)
        self.complete_ready();self.db.close();self.db=Database(self.path)
        self.assertEqual(self.progress(entry)['cursor_id'],2)
        self.db.fill_user_queue(capacity=2,batch=500)
        self.assertEqual(self.progress(entry)['cursor_id'],4)
        self.complete_ready();self.db.fill_user_queue(capacity=2,batch=500)
        self.assertIsNone(self.progress(entry))
        self.complete_ready()
        stats=self.db.fill_user_queue()
        self.assertEqual(stats['users_examined'],0)
        self.assertEqual(self.db.multi_delivery_counts(),{'sent':6})
        self.db.close();self.db=Database(self.path)
        self.assertEqual(self.db.fill_user_queue()['users_examined'],0)
        self.assertEqual(self.db.multi_delivery_counts(),{'sent':6})

    def test_inactive_and_ineligible_audiences_still_have_bounded_work(self):
        self.users(range(1,101));entry=self.add()
        self.db._conn.execute('UPDATE users SET is_active=0');self.db._conn.commit()
        for expected in (13,26):
            stats=self.db.fill_user_queue(batch=13)
            self.assertEqual(stats['users_examined'],13)
            self.assertEqual(stats['created'],0)
            self.assertEqual(self.progress(entry)['cursor_id'],expected)
        self.db.close();self.db=Database(self.path)
        self.db.fill_user_queue(batch=13)
        self.assertEqual(self.progress(entry)['cursor_id'],39)
        for _ in range(10):self.db.fill_user_queue(batch=13)
        self.assertIsNone(self.progress(entry))
        self.assertEqual(self.db.fill_user_queue()['users_examined'],0)

    def test_late_signup_and_stop_resume_do_not_join_old_unmaterialized_audience(self):
        self.users((10,30,50));entry=self.add()
        self.db.fill_user_queue(batch=1)
        self.assertEqual(self.progress(entry)['cursor_id'],10)
        for uid in (5,20,99):self.db.user_control(uid,active=True)
        self.db.user_control(30,active=False);self.db.user_control(30,active=True)
        for _ in range(8):self.db.fill_user_queue(batch=1)
        self.assertIsNone(self.progress(entry))
        ids=[row[0] for row in self.db._conn.execute('SELECT telegram_id FROM user_deliveries ORDER BY telegram_id')]
        self.assertEqual(ids,[10,50])

    def test_existing_terminal_records_are_never_replayed_and_completed_work_is_retired(self):
        self.users(range(1,5));entry=self.add()
        for uid,state in enumerate(('sent','uncertain','failed','skipped'),1):
            self.db._conn.execute("INSERT INTO user_deliveries(telegram_id,item_key,status,attempts,telegram_message_id) VALUES(?,?,?,3,77)",(uid,entry.item_key,state))
        self.db._conn.commit()
        stats=self.db.fill_user_queue(batch=2)
        self.assertEqual((stats['users_examined'],stats['created']),(2,0))
        self.db.fill_user_queue(batch=2)
        self.assertIsNone(self.progress(entry))
        self.assertEqual(self.db.fill_user_queue()['users_examined'],0)
        self.assertEqual(self.db.multi_delivery_counts(),{'sent':1,'uncertain':1,'failed':1,'skipped':1})
        self.assertEqual({row[0] for row in self.db._conn.execute('SELECT attempts FROM user_deliveries')},{3})

    def test_failure_rolls_back_insertions_and_cursor_together(self):
        self.users((1,2,3));entry=self.add()
        self.db._conn.executescript("""CREATE TRIGGER injected_failure BEFORE INSERT ON user_deliveries
            WHEN NEW.telegram_id=2 BEGIN SELECT RAISE(ABORT,'injected failure'); END;""")
        with self.assertRaises(sqlite3.IntegrityError):self.db.fill_user_queue()
        self.assertEqual(self.progress(entry)['cursor_id'],0)
        self.assertEqual(self.db.multi_delivery_counts(),{})
        self.db._conn.executescript('DROP TRIGGER injected_failure;')
        self.db.fill_user_queue()
        self.assertIsNone(self.progress(entry))
        self.assertEqual(self.db.multi_delivery_counts(),{'pending':3})

    def test_ready_source_and_detail_recovery_preserve_cursor_and_history(self):
        self.users((1,2,3));entry=self.add()
        self.db.fill_user_queue(batch=1)
        self.db.fill_user_queue(available_sources=set())
        self.assertEqual(self.progress(entry)['cursor_id'],1)
        self.assertEqual(self.db.multi_delivery_counts(),{'suspended':1})
        self.db.fill_user_queue(available_sources={'s'},blocked_items={entry.item_key})
        self.assertEqual(self.progress(entry)['cursor_id'],1)
        self.db.close();self.db=Database(self.path)
        self.db.fill_user_queue(available_sources={'s'},blocked_items={entry.item_key})
        self.assertEqual(self.progress(entry)['cursor_id'],1)
        for _ in range(3):self.db.fill_user_queue(available_sources={'s'})
        self.assertEqual(self.db.multi_delivery_counts(),{'pending':3})
        self.assertIsNone(self.progress(entry))

    def test_reopening_resets_only_that_events_work_and_keeps_sent_history(self):
        self.users((1,2,3,4));entry=self.add()
        self.db.fill_user_queue(capacity=2,batch=2)
        row=self.db.user_delivery(1,entry.item_key)
        self.db.finish_user_delivery(row['id'],'sent','test',message_id=77)
        closed=item('1','s');closed.raw_text='Приём заявок закрыт'
        self.db.revise_item(closed,closed.content_hash,state='rejected')
        self.assertIsNone(self.progress(entry))
        self.db.revise_item(entry,entry.content_hash,state='pending',activate_at=time.time())
        self.assertEqual(self.progress(entry)['cursor_id'],0)
        for _ in range(10):
            self.db.fill_user_queue(capacity=2,batch=2)
            self.complete_ready()
        self.assertEqual(self.db.multi_delivery_counts(),{'sent':4})
        self.assertEqual(self.db.user_delivery(1,entry.item_key)['telegram_message_id'],77)
        self.assertEqual(self.db.user_delivery(1,entry.item_key)['attempts'],0)
        self.assertIsNone(self.progress(entry))

    def test_ordinary_content_edit_does_not_restart_completed_fanout(self):
        self.users((1,2));entry=self.add()
        self.db.fill_user_queue();self.complete_ready()
        entry.title+=' — уточнение'
        self.db.revise_item(entry,entry.content_hash,state='pending')
        self.assertIsNone(self.progress(entry))
        self.assertEqual(self.db.fill_user_queue()['users_examined'],0)
        self.assertEqual(self.db.multi_delivery_counts(),{'sent':2})

    def test_suspended_recovery_is_bounded_and_remembers_progress_after_restart(self):
        self.users(range(1,13));entry=self.add()
        self.db.fill_user_queue()
        self.db.fill_user_queue(available_sources=set())
        stats=self.db.fill_user_queue(available_sources={'s'},batch=3)
        self.assertEqual((stats['suspended_examined'],stats['resumed']),(3,3))
        cursor=self.db.get_flag('resume_cursor')
        self.db.close();self.db=Database(self.path)
        self.assertEqual(self.db.get_flag('resume_cursor'),cursor)
        stats=self.db.fill_user_queue(available_sources={'s'},batch=3)
        self.assertEqual((stats['suspended_examined'],stats['resumed']),(3,3))
        self.assertEqual(self.db.multi_delivery_counts(),{'pending':6,'suspended':6})

    def test_small_recovery_budget_does_not_starve_fresh_fanout(self):
        self.users((1,))
        for n in range(20):self.add(str(n),'dead')
        old_good=self.add('old','good');self.db.fill_user_queue()
        self.db.fill_user_queue(available_sources=set())
        fresh=self.add('fresh','good')
        for _ in range(2):
            stats=self.db.fill_user_queue(available_sources={'good'},batch=1)
            self.assertLessEqual(stats['users_examined']+stats['suspended_examined'],1)
        self.assertIsNotNone(self.db.user_delivery(1,fresh.item_key))

    def test_v5_migration_is_safe_and_fresh_work_precedes_one_time_reconciliation(self):
        self.users((1,2,3));old=self.add('old')
        self.db.fill_user_queue();self.complete_ready()
        self.db.set_flag('offset','17')
        self.db._conn.executescript('DROP TABLE delivery_fanout; PRAGMA user_version=5;')
        self.db.close();self.db=Database(self.path)
        self.assertTrue(Path(self.db.backup_path).is_file())
        backup=sqlite3.connect(self.db.backup_path)
        self.assertEqual(backup.execute('PRAGMA user_version').fetchone()[0],5)
        self.assertIsNone(backup.execute("SELECT name FROM sqlite_master WHERE name='delivery_fanout'").fetchone())
        backup.close()
        self.assertEqual(self.progress(old)['priority'],1)
        fresh=self.add('fresh')
        self.db.fill_user_queue(batch=1)
        self.assertIsNotNone(self.db.user_delivery(1,fresh.item_key))
        self.assertEqual(self.progress(old)['cursor_id'],0)
        self.db.close();self.db=Database(self.path)
        self.assertIsNone(self.db.backup_path)
        self.assertEqual(self.progress(fresh)['cursor_id'],1)
        for _ in range(10):self.db.fill_user_queue(batch=2)
        self.assertEqual(self.db.multi_delivery_counts(),{'sent':3,'pending':3})
        self.assertIsNone(self.progress(old));self.assertIsNone(self.progress(fresh))
        self.assertEqual(self.db.get_flag('offset'),'17')

    def test_empty_audience_jobs_and_user_scans_are_both_bounded(self):
        for n in range(30):self.add(str(n))
        first=self.db.fill_user_queue(batch=7)
        self.assertEqual(first['users_examined'],0)
        self.assertEqual(first['jobs_examined'],7)
        self.assertEqual(self.db._conn.execute('SELECT COUNT(*) FROM delivery_fanout').fetchone()[0],23)

    def test_registry_queries_use_seek_indexes_without_sorting(self):
        self.users(range(1,20));self.add()
        queries=[];self.db._conn.set_trace_callback(queries.append)
        self.db.fill_user_queue(batch=3)
        self.db._conn.set_trace_callback(None)
        user_query=next(sql for sql in queries if sql.lstrip().startswith('SELECT telegram_id,'))
        plan=' '.join(row[3] for row in self.db._conn.execute('EXPLAIN QUERY PLAN '+user_query))
        self.assertIn('PRIMARY KEY',plan);self.assertNotIn('TEMP B-TREE',plan)
        work_query=next(sql for sql in queries if sql.lstrip().startswith('SELECT f.*,s.delivery_state'))
        plan=' '.join(row[3] for row in self.db._conn.execute('EXPLAIN QUERY PLAN '+work_query))
        self.assertIn('idx_fanout_source',plan);self.assertNotIn('TEMP B-TREE',plan)

    def test_completed_history_does_not_add_pair_scans_to_idle_fill(self):
        self.users(range(1,101))
        self.db._conn.executemany("INSERT INTO seen_items(item_key,source_key,external_id,first_seen_at,delivery_state,eligible_at) VALUES(?,'s',?,'2026-10-24','pending',100)",((str(n),str(n)) for n in range(500)))
        self.db._conn.execute("INSERT INTO user_deliveries(telegram_id,item_key,status) SELECT telegram_id,item_key,'sent' FROM users CROSS JOIN seen_items")
        self.db._conn.commit()
        self.db.fill_user_queue(available_sources={'s'})
        steps=[0]
        def count():steps[0]+=100;return 0
        self.db._conn.set_progress_handler(count,100)
        try:
            for _ in range(3):
                stats=self.db.fill_user_queue(available_sources={'s'})
                self.assertEqual(stats['users_examined'],0);self.assertEqual(stats['jobs_examined'],0)
        finally:self.db._conn.set_progress_handler(None,0)
        self.assertLess(steps[0],5000)  # previous cross-product query needs far more than this
        self.assertEqual(self.db.multi_delivery_counts(),{'sent':50000})


if __name__=='__main__':unittest.main()

