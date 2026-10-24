"""B03: age-bound shared verification without blocking the scheduler."""
import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from app.config import Config, TelegramChannelSource, HttpSource
from app.main import Bot, handle_update
from app.notify import DeliveryResult
from app.collectors.website_http import WebsiteSnapshot, WebsiteSourceError
from tests.test_delivery import ROOT, item


class VerificationFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        cfg=Config.load(str(ROOT/'config.yaml'));cfg.owner_id=0
        cfg.db_path=str(Path(self.tmp.name)/'state.db')
        cfg.channels=[TelegramChannelSource('configured','example')];cfg.websites=[]
        self.bot=Bot(cfg);self.addCleanup(self.bot.db.close)
        self.now=datetime.now(timezone.utc)
        for uid in (101,202):self.bot.db.user_control(uid,active=True)
        self.bot.db._conn.execute('UPDATE users SET subscribed_at=subscribed_at-60');self.bot.db._conn.commit()
        self.bot.notifier.send=Mock(return_value=DeliveryResult('sent',message_id=42))
        self.bot._last_scan=self.now.timestamp()-1800
        self.bot._available_sources={'tg_example'}
        self.entry=item('1');self.entry.published_at=self.now-timedelta(seconds=10)
        self.bot.db.add_item(self.entry,category='internships')
        self.bot._fresh_items={self.entry.item_key:self.entry}

    def test_stale_cached_closed_post_is_rechecked_and_expires_all_unsent(self):
        closed=copy.deepcopy(self.entry);closed.raw_text='Приём заявок закрыт'
        with patch('app.main.telegram_channel.fetch_post',return_value=closed) as fetch:
            self.bot.drain_pending(now=self.now)
        fetch.assert_called_once_with('example',1)
        self.bot.notifier.send.assert_not_called()
        self.assertEqual(self.bot.db.multi_delivery_counts(),{'expired':2})

    def test_one_refresh_is_shared_by_two_subscribers_and_recent_cache_is_reused(self):
        with patch('app.main.telegram_channel.fetch_post',return_value=copy.deepcopy(self.entry)) as fetch:
            self.bot.drain_pending(now=self.now)
            self.bot.drain_pending(now=self.now+timedelta(seconds=2))
        fetch.assert_called_once()
        self.assertEqual(self.bot.notifier.send.call_count,2)
        self.assertEqual(self.bot.db.multi_delivery_counts(),{'sent':2})

    def test_failed_check_never_sends_stale_data_and_has_retry_delay(self):
        with patch('app.main.telegram_channel.fetch_post',side_effect=WebsiteSourceError('unavailable')) as fetch:
            self.bot.drain_pending(now=self.now)
            self.bot.drain_pending(now=self.now+timedelta(seconds=2))
            self.assertEqual(fetch.call_count,1)
            self.bot.drain_pending(now=self.now+timedelta(seconds=61))
            self.assertEqual(fetch.call_count,2)
        self.bot.notifier.send.assert_not_called()
        self.assertEqual(self.bot.db.multi_delivery_counts(),{'pending':2})

    def test_website_catalogue_is_rechecked_when_cache_is_stale(self):
        cfg=self.bot.cfg
        cfg.channels=[];cfg.websites=[HttpSource('web','Web','https://careers.croc.ru/vacancies/','croc_vacancies')]
        self.bot._available_sources={'web'}
        entry=item('2','web');entry.published_at=self.entry.published_at
        self.bot.db.add_item(entry,category='internships');self.bot._fresh_items={entry.item_key:entry}
        closed=copy.deepcopy(entry);closed.raw_text='Приём заявок закрыт'
        snapshot=WebsiteSnapshot([closed],catalog={closed.item_key:('2',closed.url)},failed_details={})
        collector=Mock();collector.fetch.return_value=snapshot
        with patch.dict('app.main.WEBSITE_COLLECTORS',{'croc_vacancies':Mock(return_value=collector)}):
            self.bot.drain_pending(now=self.now)
        collector.fetch.assert_called_once()
        self.bot.notifier.send.assert_not_called()
        self.assertEqual(self.bot.db.user_delivery(101,entry.item_key)['status'],'expired')

    def test_queue_deduplicates_checks_for_same_event(self):
        self.bot._background_collection=True
        for _ in range(10):
            self.bot._current_for_delivery(self.entry.item_key,self.bot.cfg.channels[0],self.now)
        self.assertEqual(self.bot._verification_queue.qsize(),1)
        self.assertEqual(len(self.bot._verification_jobs),1)
        self.bot._background_collection=False

    def test_command_reply_is_sent_while_background_verification_waits_on_network(self):
        entered,release,replied=[threading.Event() for _ in range(3)]
        def fetch(*args,**kwargs):
            entered.set();release.wait(15);return self.entry
        self.bot.notifier.send.side_effect=lambda *args,**kwargs:(replied.set() or DeliveryResult('sent',message_id=42))
        with patch.object(self.bot,'run_cycle',return_value=None),patch('app.main.telegram_channel.fetch_post',side_effect=fetch):
            runner=threading.Thread(target=self.bot.loop,args=(self.bot.stop_event,));runner.start()
            try:
                self.assertTrue(entered.wait(5))
                handle_update(self.bot,{'update_id':1,'message':{'from':{'id':101},'chat':{'id':101,'type':'private'},'text':'/help'}})
                self.assertTrue(replied.wait(5))
                self.assertFalse(release.is_set())
            finally:
                self.bot.stop_event.set();release.set();runner.join(5)
            self.assertFalse(runner.is_alive())
        self.assertTrue(all('Новая возможность' not in call.args[0] for call in self.bot.notifier.send.call_args_list))

    def test_ttl_is_loaded_and_validated(self):
        import os
        with patch.dict(os.environ,{'ITEM_VERIFICATION_TTL_SECONDS':'7'}):
            self.assertEqual(Config.load(str(ROOT/'config.yaml')).item_verification_ttl_seconds,7)
        for value in ('0','-1','invalid'):
            with patch.dict(os.environ,{'ITEM_VERIFICATION_TTL_SECONDS':value}),self.assertRaises(ValueError):
                Config.load(str(ROOT/'config.yaml'))
