"""A03: cooperative cancellation, source deadlines and incomplete snapshot safety."""
import copy
from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from app.collectors.base import (CollectionCancelled, CollectionTimeout, collection_scope,
                                 check_collection, http_get, http_post_json)
from app.collectors.croc import CrocCollector
from app.collectors.k2 import K2Collector
from app.config import Config, TelegramChannelSource
from app.main import Bot
from tests.test_delivery import ROOT, item
from tests.test_telegram import response, page
from tests.test_websites import croc_source, k2_source, fixture, catalog


class CollectionControlTests(unittest.TestCase):
    def test_pre_cancelled_get_and_post_make_no_request(self):
        stop = threading.Event(); stop.set()
        with patch('app.collectors.base._request') as get, patch('app.collectors.base._request') as post:
            for request in (lambda:http_get('https://example.test'),lambda:http_post_json('https://example.test',{})):
                with self.assertRaises(CollectionCancelled), collection_scope(stop): request()
            get.assert_not_called(); post.assert_not_called()

    def test_pacing_wait_is_interruptible(self):
        stop = threading.Event()
        with patch('app.collectors.base._last_request_at',time.monotonic()), \
             patch.object(stop,'wait',side_effect=lambda seconds:stop.set()) as wait, \
             patch('app.collectors.base.time.sleep') as sleep, \
             patch('app.collectors.base._request') as get:
            with self.assertRaises(CollectionCancelled), collection_scope(stop):
                http_get('https://example.test')
            wait.assert_called_once(); sleep.assert_not_called(); get.assert_not_called()

    def test_deadline_is_shared_by_nested_scopes_and_prevents_next_request(self):
        clock = [1000.0]
        with patch('app.collectors.base.time.monotonic',side_effect=lambda:clock[0]), \
             patch('app.collectors.base._request') as get:
            with self.assertRaises(CollectionTimeout):
                with collection_scope(budget=5):
                    with collection_scope(budget=500):
                        clock[0] += 6
                        http_get('https://example.test')
            get.assert_not_called()
        # A cancelled/expired scope must not poison the next independent source.
        with collection_scope(budget=5): self.assertGreater(check_collection(),0)

    def test_inflight_request_timeout_uses_remaining_budget(self):
        with patch('app.collectors.base._last_request_at',0), \
             patch('app.collectors.base._request',return_value=Mock(status_code=200, headers={}, raw=Mock(stream=Mock(return_value=iter(()))))) as get:
            with collection_scope(budget=8): http_get('https://example.test')
            self.assertGreater(get.call_args.kwargs['timeout'],0)
            self.assertLessEqual(get.call_args.kwargs['timeout'],4)

    def test_cancelled_response_is_closed_before_parsing(self):
        stop = threading.Event(); reply = Mock()
        def send(*args,**kwargs): stop.set(); return reply
        with patch('app.collectors.base._last_request_at',0), \
             patch('app.collectors.base._request',side_effect=send):
            with self.assertRaises(CollectionCancelled), collection_scope(stop): http_get('https://example.test')
        reply.close.assert_called_once(); reply.raise_for_status.assert_not_called()

    def test_source_budget_is_validated_and_loaded(self):
        with patch.dict(os.environ,{'SOURCE_TIMEOUT_SECONDS':'7'}):
            self.assertEqual(Config.load(str(ROOT/'config.yaml')).source_timeout_seconds,7)
        for value in ('0','-1','invalid'):
            with patch.dict(os.environ,{'SOURCE_TIMEOUT_SECONDS':value}):
                with self.assertRaises(ValueError): Config.load(str(ROOT/'config.yaml'))


class BotCancellationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.cfg = Config.load(str(ROOT/'config.yaml'))
        self.cfg.db_path = str(Path(self.tmp.name)/'state.db'); self.cfg.owner_id = 0
        self.cfg.channels = []; self.cfg.websites = []
        self.bot = Bot(self.cfg); self.addCleanup(lambda:self.bot.db.close())
        self.bot.notifier.send = Mock()

    def test_croc_stops_after_first_detail_without_establishing_baseline(self):
        self.cfg.websites = [croc_source()]; calls = []
        def read(url):
            calls.append(url)
            if len(calls)==1:return fixture('croc_listing.html')
            if urlsplit(url).path=='/vacancies/':return fixture('croc_filtered.html')
            self.bot.stop_event.set()
            return fixture('croc_detail.html')
        with patch('app.collectors.croc.read_text',side_effect=read): self.bot.run_cycle()
        self.assertEqual(len(calls),3)  # discovery, listing, first detail; remaining 12 not requested
        self.assertFalse(self.bot.db.source_initialized('croc'))
        self.assertIsNone(self.bot.db.get_flag('source_snapshot:croc'))
        self.assertEqual(self.bot.db.count_items(),0)
        self.assertNotIn('croc',self.bot._available_sources)
        self.bot.notifier.send.assert_not_called()

    def test_k2_stops_before_second_detail(self):
        self.cfg.websites = [k2_source()]; data = catalog()
        second = copy.deepcopy(data['products'][0]); second['uid']='second'; second['url']='https://careers.cybersecurity.k2.tech/second'
        data['products'].append(second); data['total']=2
        calls = []
        def read(url):
            calls.append(url)
            if len(calls)==1:return fixture('k2_listing.html')
            self.bot.stop_event.set(); return fixture('k2_detail.html')
        with patch('app.collectors.k2.read_text',side_effect=read),patch('app.collectors.k2.read_json',return_value=data):
            self.bot.run_cycle()
        self.assertEqual(len(calls),2)
        self.assertFalse(self.bot.db.source_initialized('k2'))
        self.assertIsNone(self.bot.db.get_source_error('k2'))

    def test_telegram_cancelled_page_does_not_advance_cursor(self):
        self.cfg.channels = [TelegramChannelSource('example','example')]
        self.bot.db.save_telegram_cursor('tg_example',1)
        def read(*args,**kwargs): self.bot.stop_event.set(); return response(page([3,4],before=3))
        with patch('app.collectors.telegram_channel.http_get',side_effect=read) as get:
            self.bot.run_cycle()
        get.assert_called_once()
        self.assertEqual(self.bot.db.telegram_cursor('tg_example'),1)
        self.assertFalse(self.bot.db.source_initialized('example'))
        self.assertIsNone(self.bot.db.get_flag('source_snapshot:example'))

    def test_cancelled_ingestion_remains_baseline_after_restart(self):
        self.cfg.channels = [TelegramChannelSource('example','example')]
        original = self.bot.process_item
        def process(publication,**kwargs):
            result = original(publication,**kwargs)
            self.bot.stop_event.set()
            return result
        with patch('app.main.telegram_channel.fetch_channel',return_value=[item('1'),item('2')]), \
             patch.object(self.bot,'process_item',side_effect=process): self.bot.run_cycle()
        self.assertEqual(self.bot.db.count_items(),1)
        self.assertIsNone(self.bot.db.telegram_cursor('tg_example'))
        self.assertFalse(self.bot.db.source_initialized('example'))
        self.bot.db.close(); self.bot = Bot(self.cfg); self.bot.notifier.send = Mock()
        with patch('app.main.telegram_channel.fetch_channel',return_value=[item('1'),item('2')]): self.bot.run_cycle()
        self.assertEqual(self.bot.db.item_row('tg_example:2')['delivery_state'],'baseline')
        self.bot.notifier.send.assert_not_called()

    def test_expired_source_budget_does_not_stop_another_source(self):
        self.cfg.source_timeout_seconds = 1
        self.cfg.channels = [TelegramChannelSource('slow','slow'),TelegramChannelSource('good','good')]
        clock = [1000.0]
        def fetch(username,**kwargs):
            if username=='slow': clock[0] += 2
            return []
        with patch('app.collectors.base.time.monotonic',side_effect=lambda:clock[0]), \
             patch('app.main.telegram_channel.fetch_channel',side_effect=fetch): self.bot.run_cycle()
        self.assertIn('бюджет',self.bot.db.get_source_error('slow'))
        self.assertFalse(self.bot.db.source_initialized('slow'))
        self.assertTrue(self.bot.db.source_initialized('good'))
        self.assertFalse(self.bot.stop_event.is_set())


if __name__ == '__main__': unittest.main()
