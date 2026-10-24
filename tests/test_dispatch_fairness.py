"""A02: public commands and events share HTTP slots without starving each other."""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from app.config import Config, TelegramChannelSource
from app.db import Database, COMMAND_REPLY_INTERVAL
from app.main import Bot, handle_update
from app.notify import DeliveryResult
from tests.test_delivery import ROOT, item


class DispatchFairnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.clock = time.time()
        self.clock_patch = patch('app.main.time.time', side_effect=lambda:self.clock)
        self.clock_patch.start(); self.addCleanup(self.clock_patch.stop)
        self.cfg = Config.load(str(ROOT/'config.yaml'))
        self.cfg.db_path = str(Path(self.tmp.name)/'state.db'); self.cfg.owner_id = 0
        self.cfg.channels = [TelegramChannelSource('example','example')]; self.cfg.websites = []
        self.bot = Bot(self.cfg); self.addCleanup(lambda:self.bot.db.close())
        self.bot.notifier.send = Mock(return_value=DeliveryResult('sent',message_id=42))
        self.bot.db.user_control(101,active=True)
        self.bot.db._conn.execute('UPDATE users SET subscribed_at=?',(self.clock-100,)); self.bot.db._conn.commit()
        self.bot._available_sources = {'tg_example'}; self.bot._last_scan = self.clock
        self.offset = 0

    def command(self, uid=202, text='/help'):
        self.offset += 1
        handle_update(self.bot,{'update_id':self.offset,'message':{
            'chat':{'id':uid,'type':'private'},'from':{'id':uid},'text':text}})

    def queue(self, count=10):
        for key in range(count):
            publication = item(str(key)); publication.published_at = datetime.fromtimestamp(self.clock-10,timezone.utc)
            self.bot.db.add_item(publication,category='internships')
            self.bot._fresh_items[publication.item_key] = publication
        self.bot.fill_delivery_queue(datetime.fromtimestamp(self.clock,timezone.utc))

    def dispatch(self):
        self.bot.dispatch_pending(now=datetime.fromtimestamp(self.clock,timezone.utc))
        self.clock += 2

    def recipients(self):
        return [call.kwargs['chat_id'] for call in self.bot.notifier.send.call_args_list]

    def test_commands_only_persist_subscription_offset_and_reply(self):
        self.command(text='/start')
        self.assertTrue(self.bot.db.user(202)['is_active'])
        self.assertEqual(self.bot.db.get_flag('offset'),'2')
        self.bot.notifier.send.assert_not_called()
        self.assertEqual(self.bot.db.pending_reply(self.clock)['telegram_id'],202)

    def test_continuous_different_users_alternate_with_events(self):
        self.queue()
        for uid in range(202,210):
            self.command(uid)
            self.dispatch()
        recipients = self.recipients()
        self.assertEqual(len(recipients),8)
        self.assertEqual([uid == 101 for uid in recipients],[False,True]*4)
        self.assertEqual(self.bot.db.user_delivery_counts(101)['sent'],4)

    def test_single_user_spam_is_coalesced_and_rate_limited(self):
        self.queue()
        for _ in range(10):
            self.command()
            self.dispatch()
        recipients = self.recipients()
        self.assertGreater(recipients.count(101),0)
        self.assertGreater(recipients.count(202),0)
        self.assertLessEqual(recipients.count(202),4)
        self.assertEqual(len(recipients),10)

    def test_only_events_can_use_every_free_slot(self):
        self.queue()
        for _ in range(4): self.dispatch()
        self.assertEqual(self.recipients(),[101]*4)

    def test_only_commands_can_use_every_free_slot(self):
        for uid in range(202,206):
            self.command(uid)
            self.dispatch()
        self.assertEqual(self.recipients(),list(range(202,206)))

    def test_unavailable_source_does_not_hold_the_command_turn(self):
        self.queue()
        self.bot._available_sources.clear()
        self.bot.db.set_flag('last_send_kind','command')
        self.command(); self.dispatch()
        self.assertEqual(self.recipients(),[202])
        self.assertEqual(self.bot.db.user_delivery_counts(101),{'suspended':10})

    def test_stop_changes_state_immediately_even_during_reply_cooldown(self):
        self.command(text='/start'); self.dispatch()
        self.command(text='/stop')
        self.assertFalse(self.bot.db.user(202)['is_active'])
        self.assertEqual(self.bot.db.get_flag('offset'),'3')
        self.bot.flush_command_reply()
        self.assertEqual(self.recipients(),[202])
        self.clock += COMMAND_REPLY_INTERVAL
        self.bot.flush_command_reply()
        self.assertIn('отключены',self.bot.notifier.send.call_args.args[0])

    def test_reply_cooldown_survives_restart_and_does_not_block_another_user(self):
        self.command(); self.dispatch()
        self.command()
        self.bot.db.close(); self.bot = Bot(self.cfg)
        self.bot.notifier.send = Mock(return_value=DeliveryResult('sent',message_id=43))
        self.assertIsNone(self.bot.db.pending_reply(self.clock))
        self.command(303); self.bot.flush_command_reply()
        self.assertEqual(self.recipients(),[303])
        self.clock += COMMAND_REPLY_INTERVAL
        self.bot.flush_command_reply()
        self.assertEqual(self.recipients(),[303,202])

    def test_fair_turn_survives_restart(self):
        self.queue(); self.command(); self.dispatch()
        self.assertEqual(self.bot.db.get_flag('last_send_kind'),'command')
        fresh = self.bot._fresh_items
        self.bot.db.close(); self.bot = Bot(self.cfg)
        self.bot._fresh_items = fresh; self.bot._available_sources = {'tg_example'}; self.bot._last_scan = self.clock
        self.bot.notifier.send = Mock(return_value=DeliveryResult('sent',message_id=43))
        self.command(303); self.dispatch()
        self.assertEqual(self.recipients(),[101])

    def test_uncertain_event_consumes_a_turn_without_replay(self):
        self.queue(); self.bot.db.set_flag('last_send_kind','command')
        self.command()
        self.bot.notifier.send.side_effect = [DeliveryResult('uncertain'),DeliveryResult('sent',message_id=9)]
        self.dispatch(); self.dispatch()
        self.assertEqual(self.recipients(),[101,202])
        self.assertEqual(self.bot.db.user_delivery(101,'tg_example:0')['status'],'uncertain')

    def test_global_429_stops_both_queues_until_retry_after(self):
        self.queue(); self.command()
        self.bot.notifier.send.return_value = DeliveryResult('retry',code=429,retry_after=30)
        self.bot.dispatch_pending(now=datetime.fromtimestamp(self.clock,timezone.utc))
        self.clock += 29; self.bot.dispatch_pending(now=datetime.fromtimestamp(self.clock,timezone.utc))
        self.assertEqual(self.bot.notifier.send.call_count,1)
        self.clock += 2
        self.bot.notifier.send.return_value = DeliveryResult('sent',message_id=42)
        self.bot.dispatch_pending(now=datetime.fromtimestamp(self.clock,timezone.utc))
        self.assertEqual(self.recipients(),[202,101])

    def test_claim_is_atomic_and_old_throttle_entries_are_pruned(self):
        self.command()
        self.assertTrue(self.bot.db.claim_command_reply(202,self.clock))
        self.assertFalse(self.bot.db.claim_command_reply(202,self.clock))
        self.bot.db.set_reply_state(202,'sent')
        self.command()
        self.assertFalse(self.bot.db.claim_command_reply(202,self.clock+1))
        self.clock += COMMAND_REPLY_INTERVAL
        self.command(303)
        self.assertTrue(self.bot.db.claim_command_reply(303,self.clock))
        rows = list(self.bot.db._conn.execute('SELECT telegram_id FROM command_reply_limits'))
        self.assertEqual([row[0] for row in rows],[303])

    def test_v2_upgrade_backs_up_preserves_pending_replies_and_subscriptions(self):
        self.command(text='/start')
        self.queue(1)
        self.bot.db._conn.executescript('DROP TABLE command_reply_limits; PRAGMA user_version=2;')
        self.bot.db.close(); self.bot = Bot(self.cfg)
        self.assertTrue(Path(self.bot.db.backup_path).is_file())
        backup = sqlite3.connect(self.bot.db.backup_path)
        self.assertEqual(backup.execute('PRAGMA user_version').fetchone()[0],2)
        self.assertIsNone(backup.execute("SELECT name FROM sqlite_master WHERE name='command_reply_limits'").fetchone())
        backup.close()
        self.assertTrue(self.bot.db.user(202)['is_active'])
        self.assertEqual(self.bot.db.pending_reply(self.clock)['telegram_id'],202)
        self.assertEqual(self.bot.db.user_delivery(101,'tg_example:0')['status'],'pending')
        self.assertEqual(self.bot.db.get_flag('offset'),'2')


if __name__ == '__main__': unittest.main()
