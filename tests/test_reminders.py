from datetime import datetime, timezone
from unittest.mock import patch
from app.db import Database
from app.notify import DeliveryResult
from app.reminders import due_at
from tests.personal_support import PersonalCase

class ReminderTests(PersonalCase):
    def prepare(self):
        item,ref=self.event();self.db.save_item(101,ref)
        self.assertTrue(self.db.enable_reminder(101,ref,1))
        self.db._conn.execute('UPDATE reminder_jobs SET due_at=0');self.db._conn.commit()
        return item,ref

    def test_date_only_9am_calendar_days_dst(self):
        m={'deadline_at':1,'deadline_day':'2027-03-29','deadline_precision':'date'}
        due=due_at(m,1,'Europe/Berlin')
        self.assertEqual(datetime.fromtimestamp(due,timezone.utc),datetime(2027,3,28,7,tzinfo=timezone.utc))
        m['deadline_precision']='time';m['deadline_at']=1000000
        self.assertEqual(due_at(m,3,'Asia/Tokyo'),1000000-3*86400)

    def test_restart_uncertain_and_success_no_repeat(self):
        self.prepare();self.db.close();self.bot.db=Database(self.cfg.db_path);self.db=self.bot.db
        self.bot.flush_reminder();self.assertEqual(self.bot.notifier.send.call_count,1)
        self.db.set_flag('send_after','0');self.bot.flush_reminder();self.assertEqual(self.bot.notifier.send.call_count,1)

    def test_uncertain_not_automatically_retried(self):
        _,ref=self.prepare();self.bot.notifier.send.return_value=DeliveryResult('uncertain')
        self.bot.flush_reminder();self.db.set_flag('send_after','0');self.bot.flush_reminder()
        self.assertEqual(self.bot.notifier.send.call_count,1)
        self.assertFalse(self.db._conn.execute("SELECT 1 FROM reminder_jobs WHERE status='pending'").fetchone())
        self.db.remove_saved(101,ref);self.db.save_item(101,ref);self.db.enable_reminder(101,ref,1)
        self.assertEqual(self.db._conn.execute('SELECT status FROM reminder_jobs').fetchone()[0],'uncertain')

    def test_cancel_unsave_stop_and_foreign_cancel(self):
        _,ref=self.prepare();job=self.db.reminders_page(101)[0]
        self.db.cancel_reminder(202,job['id']);self.assertEqual(len(self.db.reminders_page(101)),1)
        self.db.remove_saved(101,ref);self.bot.flush_reminder();self.bot.notifier.send.assert_not_called()
        self.db.save_item(101,ref);self.db.enable_reminder(101,ref,1);self.db.user_control(101,active=False)
        self.bot.flush_reminder();self.bot.notifier.send.assert_not_called()

    def test_429_honors_global_delay(self):
        self.prepare();self.bot.notifier.send.return_value=DeliveryResult('retry',429,120)
        before=datetime.now(timezone.utc).timestamp();self.bot.flush_reminder()
        self.assertGreaterEqual(float(self.db.get_flag('send_after')),before+120)
        self.assertEqual(self.db.reminders_page(101)[0]['status'],'pending')

    def test_deadline_revision_reschedules_and_closed_cancels(self):
        item,ref=self.prepare()
        item.raw_text='Митап 2027-06-10.\nРегистрация до 2027-06-08.'
        self.db.revise_item(item,item.content_hash,state='pending')
        self.assertGreater(self.db.reminders_page(101)[0]['due_at'],0)
        item.explicitly_closed=True;self.db.revise_item(item,item.content_hash,state='rejected')
        self.assertEqual(self.db.reminders_page(101),[])

    def test_interrupted_claim_becomes_uncertain(self):
        self.prepare();self.db.claim_reminder(datetime.now(timezone.utc).timestamp())
        self.db.close();self.bot.db=Database(self.cfg.db_path);self.db=self.bot.db
        self.assertEqual(self.db._conn.execute('SELECT status FROM reminder_jobs').fetchone()[0],'uncertain')

    def test_three_queues_share_slots(self):
        item,ref=self.prepare()
        for i in range(1,7):
            other,r=self.event(str(i+1));self.db.save_item(101,r);self.db.enable_reminder(101,r,1)
        self.db._conn.execute('UPDATE reminder_jobs SET due_at=0');self.db._conn.commit()
        kinds=[]
        for i in range(6):
            self.command('/help',uid=1000+i)
            self.db.set_flag('send_after','0');self.bot.dispatch_pending();kinds.append(self.db.get_flag('last_send_kind'))
        self.assertEqual(kinds,['command','event','reminder']*2)

    def test_no_deadline_and_expired_never_sent(self):
        item,ref=self.event('no-deadline',text='Митап скоро. Приходите!');self.db.save_item(101,ref)
        self.assertFalse(self.db.enable_reminder(101,ref,1))
        item,ref=self.prepare()
        self.db._conn.execute('UPDATE event_metadata SET deadline_at=0 WHERE item_id=?',(ref,));self.db._conn.commit()
        self.bot.flush_reminder();self.bot.notifier.send.assert_not_called()

    def test_403_deactivates_and_cancels_other_jobs(self):
        item,ref=self.prepare();self.db.enable_reminder(101,ref,3)
        self.bot.notifier.send.return_value=DeliveryResult('failed',403)
        self.bot.flush_reminder()
        self.assertFalse(self.db.user(101)['is_active']);self.assertEqual(self.db.reminders_page(101),[])

    def test_stop_during_retry_does_not_requeue(self):
        self.prepare()
        def send(*args,**kwargs):
            self.db.user_control(101,active=False)
            return DeliveryResult('retry',429,120)
        self.bot.notifier.send.side_effect=send;self.bot.flush_reminder()
        self.assertEqual(self.db.reminders_page(101),[])

    def test_timezone_edit_reschedules_date_only_job(self):
        item,ref=self.event();self.db.save_item(101,ref);self.db.enable_reminder(101,ref,1)
        old=self.db.reminders_page(101)[0]['due_at']
        prefs=self.db.preferences(101);prefs['timezone']='Asia/Tokyo';self.db.set_preferences(101,prefs)
        self.assertEqual(self.db.reminders_page(101)[0]['due_at'],old-6*3600)

    def test_cancellation_during_retry_is_respected(self):
        self.prepare();job=self.db.reminders_page(101)[0]
        def send(*args,**kwargs):
            self.db.cancel_reminder(101,job['id'])
            return DeliveryResult('retry',429,120)
        self.bot.notifier.send.side_effect=send;self.bot.flush_reminder()
        self.assertEqual(self.db.reminders_page(101),[])

    def test_restore_snapshot_quarantines_pending_reminders(self):
        from app.backup import create_backup,restore_backup
        self.prepare()
        snapshot=create_backup(self.cfg.db_path,self.tmp.name+'/backups')
        restored=self.tmp.name+'/restored.db'
        restore_backup(snapshot,restored)
        restored_db=Database(restored)
        try:self.assertEqual(restored_db._conn.execute('SELECT status FROM reminder_jobs').fetchone()[0],'uncertain')
        finally:restored_db.close()
