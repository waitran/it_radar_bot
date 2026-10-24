"""A06: commands remain responsive while one serialized sendMessage is in flight."""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from app.config import Config, TelegramChannelSource
from app.main import Bot, handle_update
from app.notify import DeliveryResult
from tests.test_delivery import ROOT, item


class SendConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.cfg=Config.load(str(ROOT/'config.yaml'))
        self.cfg.db_path=str(Path(self.tmp.name)/'state.db');self.cfg.owner_id=0
        self.cfg.channels=[TelegramChannelSource('example','example')];self.cfg.websites=[]
        self.bot=Bot(self.cfg);self.addCleanup(lambda:self.bot.db.close())
        self.bot.notifier.send=Mock(return_value=DeliveryResult('sent',message_id=42))
        self.bot.db.user_control(101,active=True)
        self.bot.db._conn.execute('UPDATE users SET subscribed_at=?',(time.time()-100,));self.bot.db._conn.commit()
        self.bot._available_sources={'tg_example'};self.bot._last_scan=time.time()
        self.offset=0

    def update(self,uid,text):
        self.offset+=1
        return {'update_id':self.offset,'message':{'chat':{'id':uid,'type':'private'},'from':{'id':uid},'text':text}}

    def command(self,uid,text):handle_update(self.bot,self.update(uid,text))

    def queue(self,key='1'):
        entry=item(key);entry.published_at=datetime.now(timezone.utc)
        self.bot.db.add_item(entry,category='internships');self.bot._fresh_items[entry.item_key]=entry
        self.bot.fill_delivery_queue(datetime.now(timezone.utc))
        return entry

    def launch(self,call):
        finished=threading.Event();errors=[];results=[]
        def work():
            try:results.append(call())
            except Exception as exc:errors.append(exc)
            finally:finished.set()
        thread=threading.Thread(target=work);thread.start()
        self.addCleanup(lambda:(thread.join(5)))
        return thread,finished,errors,results

    def block_send(self,call,result=None):
        entered,released=threading.Event(),threading.Event()
        result=result or DeliveryResult('sent',message_id=42)
        def send(*args,**kwargs):
            entered.set()
            if not released.wait(5):raise RuntimeError('test send not released')
            if isinstance(result,Exception):raise result
            return result
        self.bot.notifier.send.side_effect=send
        worker=self.launch(call)
        self.addCleanup(released.set)  # unblock before joining/closing the database
        self.assertTrue(entered.wait(2))
        return released,worker

    def responsive_command(self,uid,text):
        update=self.update(uid,text)
        thread,done,errors,_=self.launch(lambda:handle_update(self.bot,update))
        self.assertTrue(done.wait(1),'command waited for another user HTTP request')
        thread.join(1);self.assertEqual(errors,[])

    def finish(self,released,worker):
        released.set();thread,done,errors,_=worker
        self.assertTrue(done.wait(2));thread.join(1)
        return errors

    def test_other_stop_and_status_commit_while_event_http_is_blocked(self):
        self.bot.db.user_control(202,active=True)
        entry=self.queue()
        released,worker=self.block_send(lambda:self.bot.deliver(entry,telegram_id=101))
        self.responsive_command(202,'/stop');self.responsive_command(202,'/status')
        self.assertFalse(self.bot.db.user(202)['is_active'])
        self.assertEqual(self.bot.db.get_flag('offset'),'3')
        pending=self.bot.db._conn.execute('SELECT text FROM command_replies WHERE telegram_id=202').fetchone()[0]
        self.assertIn('выключены',pending)
        self.assertEqual(self.bot.db.user_delivery(101,entry.item_key)['status'],'sending')
        self.assertEqual(self.finish(released,worker),[])

    def test_own_stop_does_not_retract_claim_but_prevents_next_send(self):
        first,second=self.queue('1'),self.queue('2')
        released,worker=self.block_send(lambda:self.bot.deliver(first,telegram_id=101))
        self.responsive_command(101,'/stop')
        self.assertEqual(self.bot.db.user_delivery(101,first.item_key)['status'],'sending')
        self.assertEqual(self.bot.db.user_delivery(101,second.item_key)['status'],'skipped')
        self.finish(released,worker)
        self.assertEqual(self.bot.db.user_delivery(101,first.item_key)['status'],'sent')
        self.assertFalse(self.bot.deliver(second,telegram_id=101))
        self.assertEqual(self.bot.notifier.send.call_count,1)

    def test_safe_retry_after_stop_resume_is_skipped_even_if_timestamp_repeats(self):
        entry=self.queue();before=dict(self.bot.db.user(101))
        released,worker=self.block_send(lambda:self.bot.deliver(entry,telegram_id=101),DeliveryResult('retry',retry_after=10))
        with patch('app.db.time.time',return_value=before['subscribed_at']):
            self.responsive_command(101,'/stop');self.responsive_command(101,'/start')
        self.assertEqual(self.bot.db.user(101)['subscribed_at'],before['subscribed_at'])
        self.assertGreater(self.bot.db.user(101)['subscription_epoch'],before['subscription_epoch'])
        self.finish(released,worker)
        self.assertTrue(self.bot.db.user(101)['is_active'])
        self.assertEqual(self.bot.db.user_delivery(101,entry.item_key)['status'],'skipped')

    def test_repeat_active_start_does_not_discard_safe_retry(self):
        entry=self.queue();before=dict(self.bot.db.user(101))
        released,worker=self.block_send(lambda:self.bot.deliver(entry,telegram_id=101),DeliveryResult('retry',retry_after=10))
        self.responsive_command(101,'/start');self.finish(released,worker)
        self.assertEqual(self.bot.db.user(101)['subscription_epoch'],before['subscription_epoch'])
        row=self.bot.db.user_delivery(101,entry.item_key)
        self.assertEqual((row['status'],row['attempts']),('pending',1))
        self.assertGreater(row['available_at'],time.time())

    def test_late_event_403_does_not_disable_newer_start(self):
        entry=self.queue()
        released,worker=self.block_send(lambda:self.bot.deliver(entry,telegram_id=101),DeliveryResult('failed',code=403))
        self.responsive_command(101,'/start');self.finish(released,worker)
        self.assertTrue(self.bot.db.user(101)['is_active'])
        self.assertEqual(self.bot.db.user_delivery(101,entry.item_key)['status'],'failed')

    def test_old_reply_completion_never_erases_or_changes_new_generation(self):
        outcomes=[DeliveryResult('sent',message_id=42),DeliveryResult('retry',retry_after=10),
                  DeliveryResult('retry',code=429,retry_after=30),DeliveryResult('uncertain'),
                  DeliveryResult('failed',code=403),RuntimeError('unknown result')]
        for uid,outcome in enumerate(outcomes,202):
            with self.subTest(outcome=outcome):
                self.bot.db.set_flag('send_after','0')
                self.command(uid,'/help')
                old=self.bot.db.pending_reply(time.time())
                released,worker=self.block_send(self.bot.flush_command_reply,outcome)
                self.responsive_command(uid,'/start')
                self.finish(released,worker)
                row=self.bot.db._conn.execute('SELECT * FROM command_replies WHERE telegram_id=?',(uid,)).fetchone()
                self.assertEqual(row['status'],'pending')
                self.assertGreater(row['generation'],old['generation'])
                self.assertIn('Уведомления включены',row['text'])
                self.assertTrue(self.bot.db.user(uid)['is_active'])
                if getattr(outcome,'code',0)==429:
                    self.assertGreater(float(self.bot.db.get_flag('send_after')),time.time()+29)
                self.bot.db.set_reply_state(uid,'sent')

    def test_evicted_reply_cannot_reuse_an_inflight_generation(self):
        self.command(101,'/help')
        old=self.bot.db.pending_reply(time.time())
        released,worker=self.block_send(self.bot.flush_command_reply)
        self.responsive_command(101,'/status')
        # The bounded outbox can evict a pending replacement under a flood.
        self.bot.db._conn.execute("DELETE FROM command_replies WHERE telegram_id=101 AND status='pending'")
        self.bot.db._conn.commit()
        self.responsive_command(101,'/start')
        fresh=self.bot.db._conn.execute('SELECT generation FROM command_replies WHERE telegram_id=101').fetchone()[0]
        self.assertGreater(fresh,old['generation'])
        self.finish(released,worker)
        row=self.bot.db._conn.execute('SELECT * FROM command_replies WHERE telegram_id=101').fetchone()
        self.assertEqual((row['generation'],row['status']),(fresh,'pending'))

    def test_late_reply_403_does_not_disable_existing_newer_subscription(self):
        self.command(101,'/help')
        released,worker=self.block_send(self.bot.flush_command_reply,DeliveryResult('failed',code=403))
        self.responsive_command(101,'/start');self.finish(released,worker)
        self.assertTrue(self.bot.db.user(101)['is_active'])

    def test_global_http_serialization_does_not_take_the_command_lock(self):
        self.bot.db.user_control(202,active=True)
        self.bot.db._conn.execute('UPDATE users SET subscribed_at=?',(time.time()-100,));self.bot.db._conn.commit()
        first,second=self.queue('1'),self.queue('2')
        released,worker=self.block_send(lambda:self.bot.deliver(first,telegram_id=101))
        second_worker=self.launch(lambda:self.bot.deliver(second,telegram_id=202))
        self.responsive_command(303,'/status')
        self.assertEqual(self.bot.notifier.send.call_count,1)
        self.finish(released,worker)
        self.assertTrue(second_worker[1].wait(2));self.assertEqual(second_worker[2],[])
        self.assertEqual(self.bot.notifier.send.call_count,1)

    def test_concurrent_claim_of_same_delivery_sends_once(self):
        entry=self.queue()
        released,worker=self.block_send(lambda:self.bot.deliver(entry,telegram_id=101))
        second_worker=self.launch(lambda:self.bot.deliver(entry,telegram_id=101))
        self.responsive_command(202,'/help');self.finish(released,worker)
        self.assertTrue(second_worker[1].wait(2));self.assertEqual(second_worker[2],[])
        self.assertEqual(self.bot.notifier.send.call_count,1)
        self.assertEqual(self.bot.db.user_delivery(101,entry.item_key)['attempts'],1)

    def test_check_summary_transport_also_allows_commands(self):
        self.cfg.owner_id=101
        self.bot.db.set_flag('check_summary','Проверка завершена')
        self.bot.db.set_flag('check_summary_pending','1')
        released,worker=self.block_send(self.bot.flush_check_summary)
        self.responsive_command(202,'/stop');self.finish(released,worker)
        self.assertEqual(self.bot.db.get_flag('check_summary_pending'),'0')
        self.assertFalse(self.bot.db.user(202)['is_active'])

    def test_personal_status_reports_inflight_without_exposing_other_users(self):
        entry=self.queue()
        released,worker=self.block_send(lambda:self.bot.deliver(entry,telegram_id=101))
        self.responsive_command(101,'/status')
        text=self.bot.db._conn.execute('SELECT text FROM command_replies WHERE telegram_id=101').fetchone()[0]
        self.assertIn('Отправляются: 1',text);self.assertNotIn('101',text)
        self.finish(released,worker)

    def test_uncertain_result_after_stop_is_quarantined(self):
        entry=self.queue()
        released,worker=self.block_send(lambda:self.bot.deliver(entry,telegram_id=101),DeliveryResult('uncertain'))
        self.responsive_command(101,'/stop');self.finish(released,worker)
        self.assertEqual(self.bot.db.user_delivery(101,entry.item_key)['status'],'uncertain')
        self.bot.db.close();self.bot=Bot(self.cfg)
        self.assertEqual(self.bot.db.user_delivery(101,entry.item_key)['status'],'uncertain')

    def test_current_403_still_disables_only_its_subscriber(self):
        self.bot.db.user_control(202,active=True)
        entry=self.queue()
        self.bot.notifier.send.return_value=DeliveryResult('failed',code=403)
        self.bot.deliver(entry,telegram_id=101)
        self.assertFalse(self.bot.db.user(101)['is_active'])
        self.assertTrue(self.bot.db.user(202)['is_active'])

    def test_stale_reply_claim_and_result_are_conditional(self):
        self.command(101,'/help');old=self.bot.db.pending_reply(time.time())
        self.command(101,'/status');new=self.bot.db.pending_reply(time.time())
        self.assertFalse(self.bot.db.claim_command_reply(101,time.time(),generation=old['generation']))
        self.assertTrue(self.bot.db.claim_command_reply(101,time.time(),generation=new['generation']))
        self.bot.db.set_reply_state(101,'sent',generation=old['generation'])
        row=self.bot.db._conn.execute('SELECT * FROM command_replies WHERE telegram_id=101').fetchone()
        self.assertEqual((row['generation'],row['status']),(new['generation'],'sending'))

    def test_v4_migration_backs_up_and_preserves_history_and_reply_limits(self):
        entry=self.queue();row=self.bot.db.user_delivery(101,entry.item_key)
        self.bot.db.finish_user_delivery(row['id'],'sent','test',message_id=77)
        self.command(101,'/help');self.bot.db.claim_command_reply(101,time.time())
        self.bot.db.set_reply_state(101,'pending',time.time()+20)
        self.bot.db._conn.executescript('ALTER TABLE users DROP COLUMN subscription_version; ALTER TABLE users DROP COLUMN subscription_epoch; ALTER TABLE command_replies DROP COLUMN generation; PRAGMA user_version=4;')
        self.bot.db.close();self.bot=Bot(self.cfg)
        self.assertTrue(Path(self.bot.db.backup_path).is_file())
        backup=sqlite3.connect(self.bot.db.backup_path)
        self.assertEqual(backup.execute('PRAGMA user_version').fetchone()[0],4);backup.close()
        user=self.bot.db.user(101)
        self.assertEqual((user['subscription_version'],user['subscription_epoch']),(1,1))
        self.assertEqual(self.bot.db.user_delivery(101,entry.item_key)['telegram_message_id'],77)
        reply=self.bot.db._conn.execute('SELECT * FROM command_replies WHERE telegram_id=101').fetchone()
        self.assertEqual((reply['generation'],reply['status']),(1,'pending'))
        self.assertIsNotNone(self.bot.db._conn.execute('SELECT * FROM command_reply_limits WHERE telegram_id=101').fetchone())
        self.assertEqual(self.bot.db.get_flag('offset'),'2')
        self.bot.db.close();self.bot=Bot(self.cfg)
        self.assertIsNone(self.bot.db.backup_path)


if __name__=='__main__':unittest.main()
