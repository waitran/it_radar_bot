"""A05: catalogue completeness and individual detail failures are separate facts."""
import copy
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from app.collectors.croc import CrocCollector
from app.collectors.k2 import K2Collector
from app.collectors.website_http import WebsiteSnapshot, WebsiteSourceError
from app.config import Config
from app.main import Bot
from app.notify import DeliveryResult
from tests.test_delivery import ROOT, item
from tests.test_websites import fixture, catalog, croc_source, k2_source


def snapshot(items, failed=()):
    entries = {entry.item_key:(entry.external_id,entry.url) for entry in items}
    entries.update({entry.item_key:(entry.external_id,entry.url) for entry in failed})
    return WebsiteSnapshot(items,catalog=entries,failed_details={entry.item_key:'HTTP 404' for entry in failed})


class CollectorPartialTests(unittest.TestCase):
    def test_snapshot_cannot_claim_completeness_with_unaccounted_cards(self):
        with self.assertRaises(ValueError):
            WebsiteSnapshot([],catalog={'k2:missing':('missing','https://example.test/missing')},failed_details={})

    def test_croc_one_bad_detail_keeps_other_twelve_and_full_catalogue(self):
        calls = []
        def read(url):
            calls.append(url)
            if len(calls)==1:return fixture('croc_listing.html')
            if urlsplit(url).path=='/vacancies/':return fixture('croc_filtered.html')
            if len(calls)==4:raise WebsiteSourceError('HTTP 404')
            return fixture('croc_detail.html')
        with patch('app.collectors.croc.read_text',side_effect=read): result = CrocCollector(croc_source()).fetch()
        self.assertEqual((len(result),len(result.catalog),len(result.failed_details)),(12,13,1))
        self.assertEqual(len(calls),15)
        self.assertFalse(set(result.failed_details) & {entry.item_key for entry in result})

    def test_k2_one_bad_detail_keeps_other_and_validates_all_urls_first(self):
        data = catalog(); other = copy.deepcopy(data['products'][0])
        other['uid']='second'; other['url']='https://careers.cybersecurity.k2.tech/second'
        data['products'].append(other); data['total']=2
        with patch('app.collectors.k2.read_text',side_effect=[fixture('k2_listing.html'),WebsiteSourceError('bad detail'),fixture('k2_detail.html')]), \
             patch('app.collectors.k2.read_json',return_value=data): result = K2Collector(k2_source()).fetch()
        self.assertEqual((len(result),len(result.catalog),len(result.failed_details)),(1,2,1))
        data['products'][1]['url']='https://untrusted.example/second'
        with patch('app.collectors.k2.read_text',return_value=fixture('k2_listing.html')) as read, \
             patch('app.collectors.k2.read_json',return_value=data):
            with self.assertRaises(WebsiteSourceError):K2Collector(k2_source()).fetch()
        self.assertEqual(read.call_count,1)


class PartialDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.cfg = Config.load(str(ROOT/'config.yaml'))
        self.cfg.db_path = str(Path(self.tmp.name)/'state.db'); self.cfg.owner_id=0
        self.cfg.channels=[]; self.cfg.websites=[k2_source()]
        self.bot=Bot(self.cfg); self.addCleanup(lambda:self.bot.db.close())
        self.bot.notifier.send=Mock(return_value=DeliveryResult('sent',message_id=42))
        self.bot.db.user_control(101,active=True)
        self.bot.db._conn.execute('UPDATE users SET subscribed_at=?',(time.time()-100,)); self.bot.db._conn.commit()

    def publication(self, key):
        entry=item(key,'k2'); entry.published_at=None
        return entry

    def cycle(self, result):
        self.bot.db.set_flag('send_after','0')
        with patch.object(K2Collector,'fetch',return_value=result):self.bot.run_cycle()

    def initialized(self):
        self.bot.db.set_source_state('k2',initialized=True)
        self.bot.db.set_flag('source_snapshot:k2',str(time.time()-50))

    def test_partial_snapshot_sends_good_and_suspends_bad_without_expiring_it(self):
        self.initialized(); good,bad=self.publication('good'),self.publication('bad')
        self.bot.db.add_item(bad,category='internships'); self.bot.db.fill_user_queue()
        self.cycle(snapshot([good],[bad]))
        self.assertEqual(self.bot.db.user_delivery(101,good.item_key)['status'],'sent')
        self.assertEqual(self.bot.db.user_delivery(101,bad.item_key)['status'],'suspended')
        self.assertIn('HTTP 404',self.bot.db.get_source_error('k2'))
        self.assertIn('k2',self.bot._available_sources)
        self.bot.notifier.send.assert_called_once()

    def test_initial_bad_cards_are_baseline_and_future_new_good_card_is_delivered(self):
        old,new=self.publication('old'),self.publication('new')
        self.cycle(snapshot([],[old]))
        self.assertTrue(self.bot.db.source_initialized('k2'))  # complete membership, even though no detail was read
        self.assertEqual(self.bot.db.item_row(old.item_key)['delivery_state'],'baseline')
        self.cycle(snapshot([old,new]))
        self.assertIsNone(self.bot.db.user_delivery(101,old.item_key))
        self.assertEqual(self.bot.db.user_delivery(101,new.item_key)['status'],'sent')
        self.assertEqual(self.bot.notifier.send.call_count,1)
        self.assertFalse(self.bot.db.get_source_error('k2'))

    def test_unread_new_card_retains_first_admission_across_more_failures_and_restart(self):
        self.initialized(); bad=self.publication('bad')
        old_cutoff=float(self.bot.db.get_flag('source_snapshot:k2'))
        self.cycle(snapshot([],[bad]))
        self.assertEqual(self.bot.db.item_row(bad.item_key)['delivery_state'],'awaiting_detail')
        self.bot.db.user_control(202,active=True)
        self.cycle(snapshot([],[bad]))
        self.bot.db.close();self.bot=Bot(self.cfg)
        self.bot.notifier.send=Mock(return_value=DeliveryResult('sent',message_id=43))
        self.cycle(snapshot([bad]))
        self.assertEqual(self.bot.db.item_row(bad.item_key)['eligible_at'],old_cutoff)
        self.assertEqual(self.bot.db.user_delivery(101,bad.item_key)['status'],'sent')
        self.assertIsNone(self.bot.db.user_delivery(202,bad.item_key))

    def test_read_failure_after_restart_preserves_attempts_and_retry_deadline(self):
        self.initialized(); bad=self.publication('bad')
        self.bot.db.add_item(bad,category='internships');self.bot.db.fill_user_queue()
        row=self.bot.db.user_delivery(101,bad.item_key)
        self.bot.db.claim_user_delivery(row['id'],datetime.now(timezone.utc))
        retry_at=time.time()+3600
        self.bot.db.finish_user_delivery(row['id'],'pending','retry',available_at=retry_at)
        self.cycle(snapshot([],[bad]))
        self.bot.db.close();self.bot=Bot(self.cfg)
        self.bot.notifier.send=Mock(return_value=DeliveryResult('sent',message_id=43))
        self.cycle(snapshot([],[bad]))
        self.assertEqual(self.bot.db.user_delivery(101,bad.item_key)['status'],'suspended')
        self.cycle(snapshot([bad]))
        restored=self.bot.db.user_delivery(101,bad.item_key)
        self.assertEqual((restored['status'],restored['attempts'],restored['available_at']),('pending',1,retry_at))
        self.bot.notifier.send.assert_not_called()

    def test_verified_absence_can_expire_a_previously_failed_card(self):
        self.initialized();bad=self.publication('bad')
        self.bot.db.add_item(bad,category='internships');self.bot.db.fill_user_queue()
        self.cycle(snapshot([],[bad]))
        self.cycle(snapshot([]))
        self.assertEqual(self.bot.db.user_delivery(101,bad.item_key)['status'],'expired')

    def test_failed_listing_does_not_initialize_or_advance_boundary(self):
        with patch.object(K2Collector,'fetch',side_effect=WebsiteSourceError('catalogue incomplete')):self.bot.run_cycle()
        self.assertFalse(self.bot.db.source_initialized('k2'))
        self.assertIsNone(self.bot.db.get_flag('source_snapshot:k2'))
        self.assertEqual(self.bot.db.count_items(),0)

    def test_blocked_card_frees_active_capacity_for_same_source(self):
        self.initialized();bad,good=self.publication('bad'),self.publication('good')
        self.bot.db.add_item(bad,category='internships');self.bot.db.fill_user_queue(capacity=1,batch=1)
        self.bot.db.add_item(good,category='internships')
        self.bot.db.fill_user_queue(capacity=1,batch=1,available_sources={'k2'},blocked_items={bad.item_key})
        self.assertEqual(self.bot.db.user_delivery(101,bad.item_key)['status'],'suspended')
        self.assertEqual(self.bot.db.user_delivery(101,good.item_key)['status'],'pending')

    def test_stop_cancels_failed_detail_task_and_recovery_does_not_replay(self):
        self.initialized();bad=self.publication('bad')
        self.bot.db.add_item(bad,category='internships');self.bot.db.fill_user_queue()
        self.cycle(snapshot([],[bad]))
        self.bot.db.user_control(101,active=False);self.bot.db.user_control(101,active=True)
        self.cycle(snapshot([bad]))
        self.assertEqual(self.bot.db.user_delivery(101,bad.item_key)['status'],'skipped')
        self.bot.notifier.send.assert_not_called()


if __name__=='__main__':unittest.main()
