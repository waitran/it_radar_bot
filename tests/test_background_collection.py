"""B02: exercise the real scheduler loop during blocked source I/O."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from app.config import Config, TelegramChannelSource
from app.main import Bot, handle_update
from app.notify import DeliveryResult
from tests.test_delivery import ROOT, item


class BackgroundCollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = Config.load(str(ROOT/'config.yaml'))
        cfg.owner_id = 0
        cfg.db_path = str(Path(self.tmp.name)/'state.db')
        cfg.channels = [TelegramChannelSource('good','good'),TelegramChannelSource('bad','bad')]
        cfg.websites = []
        self.bot = Bot(cfg)
        self.addCleanup(self.bot.db.close)

    def test_real_loop_serves_commands_and_healthy_events_during_blocked_scan(self):
        bot = self.bot
        bot.db.user_control(101,active=True)
        bot.db._conn.execute('UPDATE users SET subscribed_at=subscribed_at-60')
        bot.db._conn.commit()
        bot.db.save_telegram_cursor('tg_good',1)
        entry = item('2','tg_good')
        entry.published_at = datetime.now(timezone.utc)
        entered,release,event_sent,reply_sent = [threading.Event() for _ in range(4)]
        def fetch(username,**kwargs):
            if username == 'good': return [entry]
            entered.set()
            release.wait(15)
            return []
        def send(text,**kwargs):
            (event_sent if 'Новая возможность' in text else reply_sent).set()
            return DeliveryResult('sent',message_id=42)
        bot.notifier.send = Mock(side_effect=send)
        with patch('app.main.telegram_channel.fetch_channel',side_effect=fetch):
            runner = threading.Thread(target=bot.loop,args=(bot.stop_event,))
            runner.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertTrue(event_sent.wait(5),'healthy event waited for the blocked source')
                self.assertFalse(release.is_set())
                with bot._lock:
                    bot.db.set_flag('send_after','0')
                handle_update(bot,{'update_id':1,'message':{'from':{'id':101},'chat':{'id':101,'type':'private'},'text':'/status'}})
                self.assertTrue(reply_sent.wait(5),'command answer waited for the blocked source')
                self.assertTrue(bot.db.user(101)['is_active'])
                import json
                from app.healthcheck import health_path,evaluate
                state=json.loads(health_path(bot.cfg.db_path).read_text())
                self.assertTrue(evaluate(state)[0])
            finally:
                bot.stop_event.set();release.set();runner.join(5)
            self.assertFalse(runner.is_alive())
            self.assertFalse(bot._background_collection)
        self.assertEqual(bot.db.user_delivery(101,entry.item_key)['status'],'sent')

    def test_shutdown_waits_for_scanner_before_returning(self):
        entered = threading.Event()
        def fetch(*args,**kwargs):
            entered.set()
            self.bot.stop_event.wait(5)
            return []
        with patch('app.main.telegram_channel.fetch_channel',side_effect=fetch):
            runner = threading.Thread(target=self.bot.loop,args=(self.bot.stop_event,))
            runner.start()
            try:
                self.assertTrue(entered.wait(3))
            finally:
                self.bot.stop_event.set();runner.join(5)
            self.assertFalse(runner.is_alive())
            self.assertFalse(self.bot._scan_running)

    def test_bad_first_source_does_not_hide_previous_healthy_snapshot(self):
        bot=self.bot
        bot.cfg.channels.reverse()
        bot.db.user_control(101,active=True)
        bot.db._conn.execute('UPDATE users SET subscribed_at=subscribed_at-60');bot.db._conn.commit()
        entry=item('2','tg_good');entry.published_at=datetime.now(timezone.utc)
        bot.db.add_item(entry,category='internships')
        bot._fresh_items[entry.item_key]=entry
        bot._available_sources={'tg_good'};bot._last_scan=entry.published_at.timestamp()
        entered,release,sent=[threading.Event() for _ in range(3)]
        def fetch(username,**kwargs):
            if username=='bad':entered.set();release.wait(15)
            return []
        bot.notifier.send=Mock(side_effect=lambda *args,**kwargs:(sent.set() or DeliveryResult('sent',message_id=42)))
        with patch('app.main.telegram_channel.fetch_channel',side_effect=fetch):
            runner=threading.Thread(target=bot.loop,args=(bot.stop_event,));runner.start()
            try:
                self.assertTrue(entered.wait(3))
                self.assertTrue(sent.wait(5),'a previous healthy snapshot was hidden by a different source')
                self.assertFalse(release.is_set())
            finally:bot.stop_event.set();release.set();runner.join(5)
            self.assertFalse(runner.is_alive())
        self.assertEqual(bot.db.user_delivery(101,entry.item_key)['status'],'sent')
