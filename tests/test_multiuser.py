"""Public subscriptions and durable per-recipient delivery; no external requests."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from app.config import Config, TelegramChannelSource
from app.db import Database, SCHEMA
from app.main import Bot, handle_update
from app.notify import DeliveryResult
from tests.test_delivery import item, ROOT


class MultiuserTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cfg = Config.load(str(ROOT/'config.yaml'))
        self.cfg.db_path = str(Path(self.directory.name)/'bot.db')
        self.cfg.owner_id = 0
        self.cfg.channels = [TelegramChannelSource('example', 'example')]
        self.cfg.websites = []
        self.bot = Bot(self.cfg)
        self.addCleanup(lambda: self.bot.db.close())
        self.bot.service_message = Mock(return_value=DeliveryResult('sent',message_id=1))
        self.bot.notifier.send = Mock(return_value=DeliveryResult('sent', message_id=42))
        self.offset = 0

    def command(self, uid, command, **sender):
        self.offset += 1
        handle_update(self.bot, {'update_id':self.offset, 'message':{
            'chat':{'type':'private','id':uid}, 'from':{'id':uid, **sender}, 'text':command}})

    def subscribe(self):
        self.command(101, '/start', username='first')
        self.command(202, '/start')

    def queue(self, identifier='1'):
        it = item(identifier)
        it.published_at = datetime.now(timezone.utc)
        self.bot.process_item(it, baseline=False)
        self.bot.db.fill_user_queue()
        self.bot._available_sources = {'tg_example'}
        self.bot._last_scan = time.time()
        self.bot._fresh_items[it.item_key] = it
        return it

    def later(self):
        return datetime.fromtimestamp(max(time.time(), float(self.bot.db.get_flag('send_after','0')))+2, timezone.utc)

    def test_registration_repeat_start_and_personal_stop_resume(self):
        self.subscribe()
        original = dict(self.bot.db.user(101))
        self.command(101, '/start', username='renamed')
        current = dict(self.bot.db.user(101))
        self.assertEqual(current['created_at'], original['created_at'])
        self.assertEqual(current['subscribed_at'], original['subscribed_at'])
        self.assertEqual(current['username'], 'renamed')
        self.command(101, '/stop')
        self.assertEqual(self.bot.db.user(101)['is_active'], 0)
        self.assertEqual(self.bot.db.user(202)['is_active'], 1)
        self.command(101, '/start')
        self.assertGreater(self.bot.db.user(101)['subscribed_at'], original['subscribed_at'])
        self.assertEqual(self.bot.db._conn.execute('SELECT COUNT(*) FROM users').fetchone()[0],2)

    def test_same_item_delivered_to_both_exactly_once(self):
        self.subscribe()
        it = self.queue()
        self.bot.drain_pending(now=self.later())
        self.bot.drain_pending(now=self.later())
        self.bot.drain_pending(now=self.later())
        self.assertEqual(self.bot.notifier.send.call_count,2)
        self.assertEqual({call.kwargs['chat_id'] for call in self.bot.notifier.send.call_args_list},{101,202})
        for uid in (101,202):
            row = self.bot.db.user_delivery(uid,it.item_key)
            self.assertEqual(row['status'],'sent')
            self.assertEqual(row['attempts'],1)
            self.assertEqual(row['telegram_message_id'],42)
            self.assertIsNotNone(row['sent_at'])

    def test_rate_limited_next_recipient_is_not_skipped_as_queue_grows(self):
        self.subscribe();first=self.queue('1')
        self.bot.drain_pending(now=self.later())
        for key in ('2','3','4'):
            self.queue(key)
            self.bot.drain_pending(now=self.later())
        self.assertEqual(self.bot.db.user_delivery(202,first.item_key)['status'],'sent')
        self.assertEqual(self.bot.notifier.send.call_args_list[1].kwargs['chat_id'],202)

    def test_new_subscriber_and_resume_do_not_receive_archive(self):
        self.command(101,'/start')
        old = self.queue()
        self.command(202,'/start')
        self.bot.db.fill_user_queue()
        self.assertIsNone(self.bot.db.user_delivery(202,old.item_key))
        self.command(101,'/stop')
        paused = self.queue('2')
        self.command(101,'/start')
        self.bot.db.fill_user_queue()
        self.assertEqual(self.bot.db.user_delivery(101,old.item_key)['status'],'skipped')
        self.assertIsNone(self.bot.db.user_delivery(101,paused.item_key))
        fresh = self.queue('3')
        self.assertEqual(self.bot.db.user_delivery(101,fresh.item_key)['status'],'pending')

    def test_subscription_during_collection_does_not_join_current_batch(self):
        self.command(101,'/start')
        self.bot.db.save_telegram_cursor('tg_example',1)
        def collect(*args,**kwargs):
            self.command(202,'/start')
            it = item('2'); it.published_at=datetime.now(timezone.utc)
            return [it]
        with patch('app.main.telegram_channel.fetch_channel',side_effect=collect):
            self.bot.run_cycle()
        self.bot.db.fill_user_queue()
        self.assertIsNone(self.bot.db.user_delivery(202,'tg_example:2'))
        self.assertIsNotNone(self.bot.db.user_delivery(101,'tg_example:2'))
        fresh=item('3');fresh.published_at=datetime.now(timezone.utc)
        with patch('app.main.telegram_channel.fetch_channel',return_value=[fresh]):
            self.bot.run_cycle()
        self.assertIsNotNone(self.bot.db.user_delivery(202,'tg_example:3'))

    def test_backfilled_dated_archive_is_not_new_for_subscriber(self):
        self.subscribe()
        old=item('1');old.published_at=datetime.now(timezone.utc)-timedelta(days=1)
        self.bot.process_item(old,baseline=False)
        self.bot.db.fill_user_queue()
        self.assertEqual(self.bot.db.user_pending(self.later()),[])

    def test_undated_source_first_personal_snapshot_is_baseline(self):
        self.subscribe()
        self.bot._cycle_started=time.time()
        self.bot._undated_cutoff=0
        old=item('1');old.published_at=None
        self.bot.process_item(old,baseline=False)
        self.bot.db.fill_user_queue()
        self.assertIsNone(self.bot.db.user_delivery(101,old.item_key))
        self.bot._undated_cutoff=time.time()
        fresh=item('2');fresh.published_at=None
        self.bot.process_item(fresh,baseline=False)
        self.bot.db.fill_user_queue()
        self.assertIsNotNone(self.bot.db.user_delivery(101,fresh.item_key))

    def test_blocking_only_disables_recipient(self):
        self.subscribe();it=self.queue()
        self.bot.notifier.send.side_effect=[DeliveryResult('failed',code=403),DeliveryResult('sent',message_id=9)]
        self.bot.drain_pending(now=self.later())
        self.bot.drain_pending(now=self.later())
        self.assertFalse(self.bot.db.user(101)['is_active'])
        self.assertTrue(self.bot.db.user(202)['is_active'])
        self.assertEqual(self.bot.db.user_delivery(202,it.item_key)['status'],'sent')

    def test_retry_and_uncertain_do_not_block_other_recipient(self):
        for result in (DeliveryResult('retry',retry_after=30),DeliveryResult('uncertain'),DeliveryResult('failed',code=400)):
            with self.subTest(result=result):
                self.subscribe();it=self.queue(str(time.time_ns()))
                self.bot.notifier.send.reset_mock()
                self.bot.notifier.send.side_effect=[result,DeliveryResult('sent',message_id=9)]
                self.bot.drain_pending(now=self.later())
                self.bot.drain_pending(now=self.later())
                self.assertEqual(self.bot.db.user_delivery(202,it.item_key)['status'],'sent')
                self.assertEqual(self.bot.notifier.send.call_count,2)
                # Stop clears a safely retryable task before the next subtest.
                self.command(101,'/stop')

    def test_429_respects_global_retry_after(self):
        self.subscribe();it=self.queue()
        self.bot.notifier.send.return_value=DeliveryResult('retry',code=429,retry_after=45)
        self.bot.drain_pending(now=self.later())
        until=float(self.bot.db.get_flag('send_after'))
        self.bot.drain_pending(now=datetime.fromtimestamp(until-1,timezone.utc))
        self.assertEqual(self.bot.notifier.send.call_count,1)
        self.assertEqual(self.bot.db.user_delivery(101,it.item_key)['available_at'],until)

    def test_restart_preserves_users_sent_and_quarantines_inflight(self):
        self.subscribe();it=self.queue()
        self.bot.deliver(it,telegram_id=101,now=self.later())
        row=self.bot.db.user_delivery(202,it.item_key)
        self.assertTrue(self.bot.db.claim_user_delivery(row['id'],self.later()))
        self.bot.db.close();self.bot=Bot(self.cfg)
        self.assertTrue(self.bot.db.user(101)['is_active'])
        self.assertEqual(self.bot.db.user_delivery(101,it.item_key)['status'],'sent')
        self.assertEqual(self.bot.db.user_delivery(202,it.item_key)['status'],'uncertain')
        self.bot.db.fill_user_queue()
        self.assertEqual(self.bot.db.user_pending(self.later()),[])

    def test_queue_bound_and_lazy_fanout_survive_restart(self):
        self.subscribe();it=self.queue()
        # Isolate capacity test from the fixture's initial fill.
        self.bot.db._conn.execute('DELETE FROM user_deliveries');self.bot.db._conn.commit()
        self.bot.db.fill_user_queue(capacity=1,batch=1)
        rows=self.bot.db.user_pending(self.later())
        self.assertEqual(len(rows),1)
        self.bot.db.fill_user_queue(capacity=1,batch=1)
        self.assertEqual(len(self.bot.db.user_pending(self.later())),1)
        self.bot.db.finish_user_delivery(rows[0]['delivery_id'],'sent','test')
        self.bot.db.close();self.bot=Bot(self.cfg)
        self.bot.db.fill_user_queue(capacity=1,batch=1)
        rows=self.bot.db.user_pending(self.later())
        self.assertEqual(len(rows),1)
        self.assertNotEqual(rows[0]['telegram_id'],101)

    def test_help_status_unknown_and_spoofed_text(self):
        self.command(101,'/help')
        self.assertIsNone(self.bot.db.user(101))
        self.command(101,'/start 202')
        self.assertIsNone(self.bot.db.user(202))
        self.command(101,'/status')
        reply=self.bot.db.pending_reply(time.time())['text']
        self.assertNotIn('101',reply)
        self.assertNotIn('202',reply)
        self.command(101,'/unknown')
        reply=self.bot.db.pending_reply(time.time())
        self.assertIn('Неизвестная команда',reply['text'])
        self.assertEqual(reply['telegram_id'],101)
        self.bot.service_message.assert_not_called()

    def test_offset_and_subscription_commit_before_failed_ack(self):
        self.bot.service_message.side_effect=RuntimeError('response lost')
        self.command(101,'/start')
        with self.assertRaises(RuntimeError):self.bot.flush_command_reply()
        self.assertTrue(self.bot.db.user(101)['is_active'])
        self.assertEqual(self.bot.db.get_flag('offset'),'2')

    def test_welcome_waits_for_429_pause_and_survives_restart(self):
        self.bot.db.set_flag('send_after',str(time.time()+45))
        self.command(101,'/start')
        self.bot.service_message.assert_not_called()
        self.assertTrue(self.bot.db.user(101)['is_active'])
        self.bot.db.close();self.bot=Bot(self.cfg)
        self.bot.notifier.send=Mock(return_value=DeliveryResult('sent',message_id=42))
        self.bot.db.set_flag('send_after','0')
        self.bot.flush_command_reply()
        self.assertEqual(self.bot.notifier.send.call_args.kwargs['chat_id'],101)
        self.assertIn('Привет',self.bot.notifier.send.call_args.args[0])
        self.assertIsNone(self.bot.db.pending_reply(time.time()))

    def test_interrupted_command_reply_is_not_replayed(self):
        self.bot.db.user_control(101,active=True,reply='Привет')
        self.bot.db.set_reply_state(101,'sending')
        self.bot.db.close();self.bot=Bot(self.cfg)
        self.bot.notifier.send=Mock()
        self.bot.flush_command_reply()
        self.bot.notifier.send.assert_not_called()
        self.assertTrue(self.bot.db.user(101)['is_active'])

    def test_safe_reply_retry_is_retained(self):
        self.bot.service_message.return_value=DeliveryResult('retry',code=429,retry_after=30)
        self.command(101,'/start')
        self.bot.flush_command_reply()
        self.assertIsNone(self.bot.db.pending_reply(time.time()+29))
        self.assertEqual(self.bot.db.pending_reply(time.time()+31)['telegram_id'],101)

    def test_operator_resolution_is_per_recipient_and_explicit(self):
        self.subscribe();it=self.queue()
        row=self.bot.db.user_delivery(101,it.item_key)
        self.bot.db.finish_user_delivery(row['id'],'uncertain','timeout')
        with self.assertRaises(ValueError):
            self.bot.db.resolve_user_delivery(101,it.item_key,'retry')
        self.bot.db.resolve_user_delivery(101,it.item_key,'retry',True)
        self.assertEqual(self.bot.db.user_delivery(101,it.item_key)['status'],'pending')
        self.assertEqual(self.bot.db.user_delivery(202,it.item_key)['attempts'],0)


class OwnerMigrationTests(unittest.TestCase):
    def test_existing_owner_history_backup_and_repeatable_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            path=str(Path(directory)/'old.db')
            db=Database(path)
            db.bind_identity('owner_id',123)
            db.apply_control(enabled=True,offset=17)
            for key,state in [('1','sent'),('2','pending'),('3','uncertain'),('4','sending')]:
                it=item(key);db.add_item(it,category='internships')
                if state=='sending':db.claim_delivery(it.item_key,datetime.now(timezone.utc))
                else:db.set_delivery(it.item_key,state,'fixture',message_id=77 if state=='sent' else None)
            # Simulate an existing v1 database, without the new tables/column.
            db._conn.executescript('DROP TABLE user_deliveries; DROP TABLE users; PRAGMA user_version=1;')
            db.close()
            db=Database(path)
            backup=db.backup_path
            self.assertTrue(Path(backup).is_file())
            old=sqlite3.connect(backup)
            self.assertEqual(old.execute('SELECT COUNT(*) FROM seen_items').fetchone()[0],4)
            self.assertEqual(old.execute('PRAGMA user_version').fetchone()[0],1)
            old.close()
            db.migrate_owner(999)
            self.assertTrue(db.user(123)['is_active'])
            self.assertIsNone(db.user(999))
            self.assertEqual(db.user_delivery(123,'tg_example:1')['telegram_message_id'],77)
            self.assertEqual(db.user_delivery(123,'tg_example:2')['status'],'pending')
            self.assertEqual(db.user_delivery(123,'tg_example:4')['status'],'uncertain')
            db.migrate_owner(999);db.close()
            db=Database(path)
            self.assertIsNone(db.backup_path)
            db.migrate_owner(999)
            self.assertEqual(db.multi_delivery_counts(),{'pending':1,'sent':1,'uncertain':2})
            self.assertEqual(db.get_flag('offset'),'17')
            db.close()

    def test_unknown_legacy_owner_requires_original_id_before_marking_migrated(self):
        db=Database(':memory:')
        try:
            db.add_item(item(),baseline=True,category='internships')
            with self.assertRaisesRegex(RuntimeError,'previous OWNER_TELEGRAM_ID'):
                db.migrate_owner(0)
            self.assertIsNone(db.get_flag('multi_owner_migrated'))
            db.migrate_owner(123)
            self.assertIsNotNone(db.user(123))
        finally:
            db.close()

    def test_pre_delivery_schema_migrates_paused_owner_without_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            path=str(Path(directory)/'old.db')
            old=sqlite3.connect(path);old.executescript(SCHEMA)
            old.execute("INSERT INTO flags VALUES('enabled','0')")
            old.execute("INSERT INTO seen_items(item_key,source_key,external_id,first_seen_at) VALUES('old','x','1',?)",(datetime.now(timezone.utc).isoformat(),))
            old.commit();old.close()
            db=Database(path);db.migrate_owner(123)
            self.assertFalse(db.user(123)['is_active'])
            self.assertEqual(db.user_delivery(123,'old')['status'],'legacy_review')
            db.user_control(123,active=True);db.fill_user_queue()
            self.assertEqual(db.user_pending(datetime.now(timezone.utc)),[])
            db.close()


if __name__=='__main__':unittest.main()
