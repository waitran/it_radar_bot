"""A01: a source outage must not occupy ready capacity or destroy delivery history."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock

from app.config import Config, TelegramChannelSource
from app.db import Database
from app.main import Bot
from app.notify import DeliveryResult
from tests.test_delivery import ROOT, item


class QueueIsolationTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(':memory:')
        self.addCleanup(self.db.close)
        self.db.user_control(101, active=True)
        self.db._conn.execute('UPDATE users SET subscribed_at=1')
        self.db._conn.commit()
        self.now = datetime.now(timezone.utc)

    def add(self, key, source):
        publication = item(key, source)
        publication.published_at = self.now
        self.db.add_item(publication, category='internships')
        return publication

    def test_full_default_capacity_from_dead_source_does_not_block_healthy_delivery(self):
        cfg = Config.load(str(ROOT/'config.yaml'))
        cfg.db_path = ':memory:'; cfg.owner_id = 0; cfg.websites = []
        cfg.channels = [TelegramChannelSource('dead','dead'), TelegramChannelSource('good','good')]
        bot = Bot(cfg)
        self.addCleanup(bot.db.close)
        bot.notifier.send = Mock(return_value=DeliveryResult('sent',message_id=42))
        bot.db._conn.executemany('INSERT INTO users VALUES(?,NULL,1,0,0,1)', ((uid,) for uid in range(1,10001)))
        bot.db._conn.commit()
        dead = item('1','tg_dead'); dead.published_at = self.now
        bot.db.add_item(dead,category='internships')
        for _ in range(20):
            bot.db.fill_user_queue()
        self.assertEqual(bot.db.multi_delivery_counts(), {'pending':10000})
        good = item('2','tg_good'); good.published_at = self.now
        bot.db.add_item(good,category='internships')
        bot._fresh_items = {good.item_key:good}
        bot._available_sources = {'tg_good'}; bot._last_scan = time.time()
        self.assertEqual(bot.drain_pending(),1)
        counts = bot.db.multi_delivery_counts()
        self.assertEqual(counts['suspended'],10000)
        self.assertLessEqual(counts.get('pending',0)+counts.get('sending',0),10000)
        self.assertEqual(bot.db.user_delivery(1,good.item_key)['status'],'sent')
        bot.notifier.send.assert_called_once()

    def test_recovery_retains_attempts_retry_deadline_and_terminal_states(self):
        publications = [self.add(str(n),'dead') for n in range(4)]
        self.db.fill_user_queue()
        first = self.db.user_delivery(101,publications[0].item_key)
        self.db.claim_user_delivery(first['id'],self.now)
        retry_at = self.now.timestamp()+3600
        self.db.finish_user_delivery(first['id'],'pending','telegram_retry',available_at=retry_at)
        for publication,state in zip(publications[1:],('sent','uncertain','skipped')):
            row = self.db.user_delivery(101,publication.item_key)
            self.db.finish_user_delivery(row['id'],state,'test')
        self.db.fill_user_queue(available_sources=set())
        self.assertEqual(self.db.user_delivery(101,publications[0].item_key)['status'],'suspended')
        self.db.fill_user_queue(available_sources={'dead'})
        restored = self.db.user_delivery(101,publications[0].item_key)
        self.assertEqual((restored['id'],restored['attempts'],restored['available_at']), (first['id'],1,retry_at))
        self.assertEqual(restored['status'],'pending')
        self.assertEqual(self.db.user_pending(self.now),[])
        self.assertEqual([self.db.user_delivery(101,p.item_key)['status'] for p in publications[1:]], ['sent','uncertain','skipped'])

    def test_stop_cancels_suspended_and_resume_does_not_replay_it(self):
        publication = self.add('1','dead'); self.db.fill_user_queue()
        self.db.fill_user_queue(available_sources=set())
        self.db.user_control(101,active=False)
        self.db.user_control(101,active=True)
        self.db.fill_user_queue(available_sources={'dead'})
        self.assertEqual(self.db.user_delivery(101,publication.item_key)['status'],'skipped')
        self.assertEqual(self.db.user_pending(self.now),[])

    def test_recovered_and_new_source_share_limited_ready_capacity(self):
        for n in range(4): self.add(str(n),'dead')
        self.db.fill_user_queue()
        self.db.fill_user_queue(available_sources=set())
        fresh = self.add('good','good')
        self.db.fill_user_queue(available_sources={'dead','good'},capacity=2,batch=2)
        counts = self.db.multi_delivery_counts()
        self.assertEqual(counts['pending'],2)
        self.assertEqual(self.db.user_delivery(101,fresh.item_key)['status'],'pending')
        self.assertEqual(counts['suspended'],3)

    def test_one_slot_rotates_between_sources(self):
        for source in ('a','b','c'):
            for n in range(3): self.add(str(n),source)
        visited = []
        for _ in range(3):
            self.db.fill_user_queue(available_sources={'a','b','c'},capacity=1,batch=1)
            ready = self.db.user_pending(self.now)
            self.assertEqual(len(ready),1)
            visited.append(ready[0]['source_key'])
            self.db.finish_user_delivery(ready[0]['delivery_id'],'sent','test')
        self.assertEqual(set(visited),{'a','b','c'})

    def test_sending_is_not_suspended_and_removed_source_is_skipped(self):
        first = self.add('1','dead'); second = self.add('2','dead')
        self.db.fill_user_queue()
        claimed = self.db.user_delivery(101,first.item_key)
        self.db.claim_user_delivery(claimed['id'],self.now)
        self.db.fill_user_queue(available_sources=set(),configured_sources=set())
        self.assertEqual(self.db.user_delivery(101,first.item_key)['status'],'sending')
        self.assertEqual(self.db.user_delivery(101,second.item_key)['status'],'skipped')

    def test_suspended_tasks_survive_restart_and_recover_within_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory)/'state.db')
            db = Database(path)
            try:
                db.user_control(101,active=True)
                publication = item('1','dead'); publication.published_at = datetime.now(timezone.utc)+timedelta(seconds=1)
                db.add_item(publication,category='internships'); db.fill_user_queue()
                db.fill_user_queue(available_sources=set())
            finally: db.close()
            db = Database(path)
            try:
                self.assertEqual(db.user_delivery(101,publication.item_key)['status'],'suspended')
                db.fill_user_queue(available_sources={'dead'},capacity=1,batch=1)
                self.assertEqual(db.user_delivery(101,publication.item_key)['status'],'pending')
                db.fill_user_queue(available_sources={'dead'},capacity=1,batch=1)
                self.assertEqual(db.multi_delivery_counts(),{'pending':1})
            finally: db.close()


if __name__ == '__main__': unittest.main()
