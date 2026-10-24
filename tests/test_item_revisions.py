"""A04: reconsider changed rejected publications without replaying delivery history."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from app.config import Config, TelegramChannelSource
from app.main import Bot
from app.notify import DeliveryResult
from app.collectors.telegram_channel import fetch_channel
from tests.test_delivery import ROOT, item
from tests.test_telegram import page, response


class RevisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base = time.time(); self.clock = self.base-60
        self.clock_patch = patch('app.main.time.time',side_effect=lambda:self.clock)
        self.clock_patch.start(); self.addCleanup(self.clock_patch.stop)
        self.cfg = Config.load(str(ROOT/'config.yaml'))
        self.cfg.db_path = str(Path(self.tmp.name)/'state.db'); self.cfg.owner_id = 0
        self.cfg.channels = [TelegramChannelSource('example','example')]; self.cfg.websites = []
        self.bot = Bot(self.cfg); self.addCleanup(lambda:self.bot.db.close())
        self.bot.notifier.send = Mock(return_value=DeliveryResult('sent',message_id=42))
        for uid in (101,202): self.bot.db.user_control(uid,active=True)
        self.bot.db._conn.execute('UPDATE users SET subscribed_at=?',(self.base-100,)); self.bot.db._conn.commit()
        self.bot._cycle_started = self.base-60; self.bot._undated_cutoff = self.base-120
        self.bot._available_sources = {'tg_example'}; self.bot._last_scan = self.base

    def publication(self, closed=False):
        publication = item('9'); publication.published_at = datetime.fromtimestamp(self.base-500,timezone.utc)
        publication.raw_text = ('Приём заявок закрыт. Стажировка Python для студентов без опыта' if closed
                                else 'Открыт набор на стажировку Python для студентов без опыта')
        return publication

    def next_snapshot(self, cutoff=None):
        self.clock = self.base
        self.bot._cycle_started = self.base
        self.bot._undated_cutoff = self.base-50 if cutoff is None else cutoff

    def fill(self):
        self.bot.fill_delivery_queue(datetime.fromtimestamp(self.clock,timezone.utc))

    def test_hash_excludes_filter_mutations_and_source_display_name(self):
        publication = self.publication()
        publication.deadline = None
        publication.raw_text += '\nПриём заявок до 30 декабря 2026'
        fingerprint = publication.content_hash
        self.bot.filters.check(publication)
        publication.source_name = 'Новое отображаемое название'
        self.assertIsNotNone(publication.deadline)
        self.assertEqual(publication.content_hash,fingerprint)

    def test_changed_rejection_becomes_eligible_for_two_users_once(self):
        self.bot.process_item(self.publication(True),baseline=False)
        before = self.bot.db.item_row('tg_example:9')
        self.next_snapshot()
        self.assertTrue(self.bot.process_item(self.publication(),baseline=False))
        self.fill()
        row = self.bot.db.item_row('tg_example:9')
        self.assertEqual(row['revision'],2)
        self.assertEqual(row['first_seen_at'],before['first_seen_at'])
        self.assertEqual(row['published_at'],before['published_at'])
        self.assertEqual(row['eligible_at'],self.base-50)
        for uid in (101,202):
            self.assertEqual(self.bot.db.user_delivery(uid,'tg_example:9')['status'],'pending')
        self.assertFalse(self.bot.process_item(self.publication(),baseline=False))
        self.assertEqual(self.bot.db.item_row('tg_example:9')['revision'],2)

    def test_unchanged_rejection_does_not_create_revisions_or_jobs(self):
        self.bot.process_item(self.publication(True),baseline=False)
        self.next_snapshot()
        self.assertFalse(self.bot.process_item(self.publication(True),baseline=False))
        self.fill()
        self.assertEqual(self.bot.db.item_row('tg_example:9')['revision'],1)
        self.assertEqual(self.bot.db.multi_delivery_counts(),{})

    def test_accepted_edit_does_not_expand_old_audience_or_replay_sent(self):
        original = self.publication(); original.published_at = datetime.fromtimestamp(self.base-55,timezone.utc)
        self.bot.db.user_control(202,active=False)
        self.bot.process_item(original,baseline=False); self.fill()
        self.assertTrue(self.bot.deliver(original,telegram_id=101,now=datetime.fromtimestamp(self.clock,timezone.utc)))
        self.clock = self.base-10; self.bot.db.user_control(202,active=True)
        self.next_snapshot()
        changed = copy.deepcopy(original); changed.title += ' — уточнение программы'
        self.bot.process_item(changed,baseline=False); self.fill()
        self.assertEqual(self.bot.db.user_delivery(101,original.item_key)['status'],'sent')
        self.assertIsNone(self.bot.db.user_delivery(202,original.item_key))
        self.assertEqual(self.bot.notifier.send.call_count,1)

    def test_closed_revision_expires_only_unsent_and_reopening_restores_them(self):
        original = self.publication(); original.published_at = datetime.fromtimestamp(self.base-55,timezone.utc)
        self.bot.process_item(original,baseline=False); self.fill()
        self.bot.deliver(original,telegram_id=101,now=datetime.fromtimestamp(self.clock,timezone.utc))
        closed = copy.deepcopy(original); closed.raw_text = 'Приём заявок закрыт. Стажировка Python для студентов без опыта'
        self.bot.process_item(closed,baseline=False)
        self.assertEqual(self.bot.db.user_delivery(101,original.item_key)['status'],'sent')
        self.assertEqual(self.bot.db.user_delivery(202,original.item_key)['status'],'expired')
        self.next_snapshot()
        self.assertTrue(self.bot.process_item(original,baseline=False)); self.fill()
        self.assertEqual(self.bot.db.user_delivery(101,original.item_key)['status'],'sent')
        self.assertEqual(self.bot.db.user_delivery(202,original.item_key)['status'],'pending')
        self.bot.deliver(original,telegram_id=202,now=datetime.fromtimestamp(self.clock,timezone.utc))
        self.assertEqual(self.bot.notifier.send.call_count,2)

    def test_uncertain_failed_and_paused_tasks_are_not_automatically_revived(self):
        for state in ('uncertain','failed','skipped'):
            with self.subTest(state=state):
                original = self.publication(); original.external_id = state; original.title += ' '+state
                original.url = f'https://t.me/example/{state}'; original.apply_url = f'https://jobs.example/{state}'
                original.published_at = datetime.fromtimestamp(self.base-55,timezone.utc)
                self.bot.process_item(original,baseline=False); self.fill()
                row = self.bot.db.user_delivery(101,original.item_key)
                self.bot.db.finish_user_delivery(row['id'],state,'test')
                closed = copy.deepcopy(original); closed.raw_text = 'Приём заявок закрыт. Стажировка Python для студентов без опыта'
                self.bot.process_item(closed,baseline=False)
                self.next_snapshot()
                self.bot.process_item(original,baseline=False); self.fill()
                self.assertEqual(self.bot.db.user_delivery(101,original.item_key)['status'],state)

    def test_reopening_preserves_attempts_and_future_retry_deadline(self):
        original = self.publication(); original.published_at = datetime.fromtimestamp(self.base-55,timezone.utc)
        self.bot.process_item(original,baseline=False); self.fill()
        row = self.bot.db.user_delivery(101,original.item_key)
        self.bot.db.claim_user_delivery(row['id'],datetime.fromtimestamp(self.clock,timezone.utc))
        retry_at = self.base+3600
        self.bot.db.finish_user_delivery(row['id'],'pending','telegram_retry',available_at=retry_at)
        closed = copy.deepcopy(original); closed.raw_text = 'Приём заявок закрыт'
        self.bot.process_item(closed,baseline=False)
        self.next_snapshot(); self.bot.process_item(original,baseline=False); self.fill()
        restored = self.bot.db.user_delivery(101,original.item_key)
        self.assertEqual((restored['attempts'],restored['available_at']),(1,retry_at))
        self.assertFalse(self.bot.deliver(original,telegram_id=101,now=datetime.fromtimestamp(self.clock,timezone.utc)))
        self.bot.notifier.send.assert_not_called()

    def test_recent_subscriber_does_not_receive_unobserved_edit_archive(self):
        self.bot.process_item(self.publication(True),baseline=False)
        self.clock = self.base-25; self.bot.db.user_control(303,active=True)
        self.next_snapshot()
        self.bot.process_item(self.publication(),baseline=False); self.fill()
        self.assertIsNone(self.bot.db.user_delivery(303,'tg_example:9'))
        self.assertIsNotNone(self.bot.db.user_delivery(101,'tg_example:9'))

    def test_accepted_baseline_edit_and_initial_snapshot_reconsideration_do_not_broadcast(self):
        original = self.publication(); self.bot.process_item(original,baseline=True)
        changed = copy.deepcopy(original); changed.title += ' — обновление'
        self.next_snapshot(); self.bot.process_item(changed,baseline=False); self.fill()
        self.assertEqual(self.bot.db.item_row(original.item_key)['delivery_state'],'baseline')
        self.assertEqual(self.bot.db.multi_delivery_counts(),{})
        other = self.publication(True); other.external_id = '10'
        other.url = 'https://t.me/example/10'; other.apply_url = 'https://jobs.example/10'
        self.bot.process_item(other,baseline=True)
        opened = copy.deepcopy(other); opened.raw_text = self.publication().raw_text
        self.bot.process_item(opened,baseline=True); self.fill()
        self.assertEqual(self.bot.db.item_row(other.item_key)['delivery_state'],'baseline')
        self.assertEqual(self.bot.db.multi_delivery_counts(),{})

    def test_structured_deadline_change_is_a_revision_even_without_text_changes(self):
        original = self.publication(); original.source_deadline = original.deadline = datetime.fromtimestamp(self.base-1,timezone.utc)
        self.bot.process_item(original,baseline=False)
        self.next_snapshot()
        changed = copy.deepcopy(original); changed.source_deadline = changed.deadline = datetime.fromtimestamp(self.base+86400,timezone.utc)
        self.assertTrue(self.bot.process_item(changed,baseline=False)); self.fill()
        self.assertEqual(self.bot.db.user_delivery(101,original.item_key)['status'],'pending')
        self.assertEqual(self.bot.db.deserialize(self.bot.db.item_row(original.item_key)['payload']).source_deadline,changed.source_deadline)

    def test_reopened_duplicate_of_another_event_stays_rejected(self):
        rejected = self.publication(True); self.bot.process_item(rejected,baseline=False)
        other = self.publication(); other.source_key = 'other'; other.external_id = 'other'
        self.bot.db.add_item(other,category='internships')
        self.next_snapshot()
        self.assertFalse(self.bot.process_item(self.publication(),baseline=False)); self.fill()
        self.assertEqual(self.bot.db.item_row(rejected.item_key)['delivery_state'],'rejected')
        self.assertIsNone(self.bot.db.user_delivery(101,rejected.item_key))

    def test_closed_previously_sent_event_still_suppresses_cross_source_duplicate(self):
        original = self.publication(); original.published_at = datetime.fromtimestamp(self.base-55,timezone.utc)
        self.bot.process_item(original,baseline=False); self.fill()
        self.bot.deliver(original,telegram_id=101,now=datetime.fromtimestamp(self.clock,timezone.utc))
        closed = copy.deepcopy(original); closed.raw_text = 'Приём заявок закрыт'
        self.bot.process_item(closed,baseline=False)
        duplicate = copy.deepcopy(original); duplicate.source_key = 'other'; duplicate.external_id = 'copy'
        self.assertTrue(self.bot.db.is_duplicate(duplicate))
        self.assertFalse(self.bot.process_item(duplicate,baseline=False))

    def test_changed_legacy_expired_item_can_reopen_without_replaying_sent(self):
        closed = self.publication(True); closed.published_at = datetime.fromtimestamp(self.base-55,timezone.utc)
        self.bot.db.add_item(closed,category='internships'); self.fill()
        for uid in (101,202):
            row = self.bot.db.user_delivery(uid,closed.item_key)
            self.bot.db.finish_user_delivery(row['id'],'expired','legacy_closed')
        self.bot.db.set_delivery(closed.item_key,'expired','legacy_closed')
        self.next_snapshot()
        opened = copy.deepcopy(closed); opened.raw_text = self.publication().raw_text
        self.assertTrue(self.bot.process_item(opened,baseline=False)); self.fill()
        self.assertEqual(self.bot.db.user_delivery(101,closed.item_key)['status'],'pending')
        self.assertEqual(self.bot.db.item_row(closed.item_key)['revision'],2)

    def test_changed_aliases_replace_old_urls(self):
        rejected = self.publication(True); self.bot.process_item(rejected,baseline=False)
        opened = self.publication(); opened.apply_url = 'https://new.example/form/42'
        self.next_snapshot(); self.bot.process_item(opened,baseline=False)
        old = self.publication(); old.source_key = 'other'; old.url = 'https://other.example/old/1'
        self.assertFalse(self.bot.db.is_duplicate(old))
        old.apply_url = opened.apply_url
        self.assertTrue(self.bot.db.is_duplicate(old))

    def test_bot_revisits_known_post_and_cursor_never_moves_backwards(self):
        self.bot.process_item(self.publication(True),baseline=False)
        self.bot.db.save_telegram_cursor('tg_example',10)
        self.bot.db.set_source_state('example',initialized=True)
        self.bot.db.set_flag('source_snapshot:example',str(self.base-50))
        self.next_snapshot()
        with patch('app.main.telegram_channel.fetch_channel',return_value=[self.publication()]) as fetch:
            report = self.bot.run_cycle()
        self.assertTrue(fetch.call_args.kwargs['include_recent'])
        self.assertEqual(self.bot.db.telegram_cursor('tg_example'),10)
        self.assertEqual(report['matched'],1)
        self.assertEqual(self.bot.notifier.send.call_count,1)
        self.clock += 2
        self.bot.db.set_flag('send_after','0')  # run_cycle uses real datetime, skip the synthetic HTTP cooldown
        with patch('app.main.telegram_channel.fetch_channel',return_value=[self.publication()]): self.bot.run_cycle()
        self.assertEqual(self.bot.notifier.send.call_count,2)  # second subscriber, no duplicate for first
        self.assertEqual(self.bot.db.item_row('tg_example:9')['revision'],2)

    def test_v3_migration_backfills_hash_without_replaying_existing_event(self):
        original = self.publication(); original.published_at = datetime.fromtimestamp(self.base-55,timezone.utc)
        self.bot.process_item(original,baseline=False); self.fill()
        self.bot.deliver(original,telegram_id=101,now=datetime.fromtimestamp(self.clock,timezone.utc))
        saved = dict(self.bot.db.item_row(original.item_key))
        for name in ('content_hash','revision','opportunity_at','content_updated_at'):
            self.bot.db._conn.execute('ALTER TABLE seen_items DROP COLUMN '+name)
        payload = json.loads(saved['payload']); payload.pop('source_deadline',None)
        self.bot.db._conn.execute('UPDATE seen_items SET payload=?',(json.dumps(payload),))
        self.bot.db._conn.execute('PRAGMA user_version=3'); self.bot.db._conn.commit()
        self.bot.db.close(); self.bot = Bot(self.cfg)
        self.assertTrue(Path(self.bot.db.backup_path).is_file())
        backup = sqlite3.connect(self.bot.db.backup_path)
        self.assertEqual(backup.execute('PRAGMA user_version').fetchone()[0],3); backup.close()
        self.assertFalse(self.bot.process_item(original,baseline=False))
        row = self.bot.db.item_row(original.item_key)
        self.assertEqual(row['revision'],1)
        self.assertEqual(row['first_seen_at'],saved['first_seen_at'])
        self.assertEqual(self.bot.db.user_delivery(101,original.item_key)['telegram_message_id'],42)
        self.bot.db.close(); self.bot = Bot(self.cfg)
        self.assertIsNone(self.bot.db.backup_path)
        self.assertEqual(self.bot.db.item_row(original.item_key)['revision'],1)


class TelegramRecentTests(unittest.TestCase):
    def test_known_posts_from_latest_page_are_optional_and_need_no_extra_request(self):
        for recent,expected in ((False,['11']),(True,['9','10','11'])):
            with self.subTest(recent=recent), patch('app.collectors.telegram_channel.http_get',return_value=response(page([9,10,11],before=9))) as get:
                result = fetch_channel('example',since_id=10,include_recent=recent)
                self.assertEqual([entry.external_id for entry in result],expected)
                get.assert_called_once()


if __name__ == '__main__': unittest.main()
