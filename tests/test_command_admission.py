"""Bounded admission, durable decisions and isolation using synthetic users only."""
from pathlib import Path
import tempfile
import time
import unittest

from app.db import Database


class CommandAdmissionTests(unittest.TestCase):
    def test_overflow_never_evicts_accepted_chat(self):
        db = Database(':memory:')
        self.addCleanup(db.close)
        for uid in range(1, 1102):
            db.user_control(uid, reply='synthetic', offset=uid)
        rows = list(db._conn.execute('SELECT telegram_id FROM command_replies'))
        self.assertEqual({r[0] for r in rows}, set(range(1, 1001)))
        self.assertEqual(db.get_flag('offset'), '1101')

    def test_rejection_coalescing_restart_and_capacity_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'synthetic.db')
            db = Database(path)
            try:
                for uid in range(1, 1001):
                    self.assertTrue(db.user_control(uid, reply='old'))
                first = dict(db.pending_reply(time.time()))
                self.assertFalse(db.user_control(1001, active=True, reply='rejected', offset=1100))
                self.assertTrue(db.user(1001)['is_active'])
                self.assertTrue(db.user_control(1, reply='latest'))
                newest = dict(db.pending_reply(time.time()))
                self.assertEqual(newest['telegram_id'], 1)
                self.assertEqual(newest['updated_at'], first['updated_at'])
                self.assertGreater(newest['generation'], first['generation'])
                self.assertEqual(newest['text'], 'latest')
                db.close()
                db = Database(path)
                self.assertEqual(db.get_flag('offset'), '1100')
                self.assertEqual(db.health_metrics(include_commands=True)['commands']['rejected'], 1)
                self.assertEqual(db.health_metrics(include_commands=True)['commands']['coalesced'], 1)
                self.assertTrue(db.user(1001)['is_active'])
                db.set_reply_state(1, 'sent', generation=first['generation'])
                self.assertEqual(db.health_metrics(include_commands=True)['commands']['rows'], 1000)
                db.set_reply_state(1, 'sent', generation=newest['generation'])
                self.assertTrue(db.user_control(1001, reply='retry'))
                self.assertEqual(db.health_metrics(include_commands=True)['commands']['rows'], 1000)
            finally:
                db.close()

    def test_quarantine_consumes_capacity_and_rejection_preserves_other_state(self):
        db = Database(':memory:')
        self.addCleanup(db.close)
        for uid in range(1, 1001):
            db.user_control(uid, reply='synthetic')
            db.set_reply_state(uid, 'uncertain')
        db.user_control(1001, active=True)
        preferences = db.preferences(1001)
        self.assertFalse(db.user_control(1001, active=False, reply='stop', offset=1200))
        self.assertEqual(db.preferences(1001), preferences)
        self.assertFalse(db.user(1001)['is_active'])
        self.assertEqual(db.get_flag('offset'), '1200')
        self.assertEqual(db.health_metrics(include_commands=True)['commands']['rows'], 1000)
        self.assertTrue(db.user_control(1, reply='explicit new action'))


class SaturatedSenderTests(unittest.TestCase):
    def test_saturation_keeps_event_and_reminder_turns(self):
        from tests.personal_support import PersonalCase
        from datetime import datetime, timezone
        case = PersonalCase()
        case.setUp()
        try:
            entry, ref = case.event()
            case.db.save_item(101, ref)
            self.assertTrue(case.db.enable_reminder(101, ref, 1))
            case.db._conn.execute('UPDATE reminder_jobs SET due_at=0')
            case.db._conn.commit()
            for uid in range(1, 1102):
                case.db.user_control(uid, reply='synthetic')
            turns = []
            for _ in range(3):
                case.db.set_flag('send_after', '0')
                case.bot.dispatch_pending(now=datetime.now(timezone.utc))
                turns.append(case.db.get_flag('last_send_kind'))
            self.assertEqual(turns, ['command', 'event', 'reminder'])
            self.assertEqual(case.db.health_metrics(include_commands=True)['commands']['rejected'], 101)
            self.assertEqual(case.db.user_delivery_counts(101)['sent'], 1)
        finally:
            case.doCleanups()

    def test_concurrent_admission_cannot_exceed_capacity(self):
        from concurrent.futures import ThreadPoolExecutor
        db = Database(':memory:')
        self.addCleanup(db.close)
        with ThreadPoolExecutor(max_workers=8) as pool:
            accepted = list(pool.map(lambda uid: db.user_control(uid, reply='synthetic'), range(1, 1102)))
        self.assertEqual(sum(accepted), 1000)
        metrics = db.health_metrics(include_commands=True)['commands']
        self.assertEqual((metrics['rows'], metrics['accepted'], metrics['rejected']), (1000, 1000, 101))
