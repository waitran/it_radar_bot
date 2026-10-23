"""Migration, delivery ambiguity, commands and concurrency; all HTTP is mocked."""
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import requests

from app.config import Config, TelegramChannelSource, HttpSource
from app.db import Database, SCHEMA
from app.dedup import canonical_url
from app.formatting import units
from app.main import Bot, handle_update, poll_updates
from app.models import Item
from app.notify import TelegramNotifier, DeliveryResult, BotAPIError
from app.collectors.telegram_channel import fetch_post
from tests.test_telegram import page, response

ROOT=Path(__file__).resolve().parents[1]
NOW=datetime.now(timezone.utc)


def item(identifier='1', source='tg_example'):
    return Item(source,'Example',identifier,f'https://t.me/example/{identifier}',
                f'Открыт набор на стажировку Python {identifier}','Для студентов без опыта',
                f'https://jobs.example/apply/{identifier}',NOW,raw_text='Для студентов без опыта',
                deadline=NOW+timedelta(days=2),categories=['internships'],kind='vacancy',experience='0-1 год')


def api_response(status=200, data=None):
    r=Mock(status_code=status)
    r.json.return_value=data if data is not None else {'ok':True,'result':{'message_id':42}}
    return r


class APITests(unittest.TestCase):
    def setUp(self):
        self.sender=TelegramNotifier('synthetic-test-value',123)

    def test_success_records_message_id_with_one_request(self):
        with patch('app.notify.requests.post',return_value=api_response()) as post:
            result=self.sender.send('test')
        self.assertEqual(result,DeliveryResult('sent',message_id=42))
        self.assertEqual(post.call_count,1)
        self.assertFalse(post.call_args.kwargs['allow_redirects'])

    def test_connect_timeout_is_safe_to_defer(self):
        with patch('app.notify.requests.post',side_effect=requests.ConnectTimeout()) as post:
            result=self.sender.send('test')
        self.assertEqual(result.status,'retry');self.assertEqual(post.call_count,1)

    def test_read_timeout_and_connection_loss_are_never_retried_blindly(self):
        for exception in [requests.ReadTimeout(),requests.ConnectionError()]:
            with patch('app.notify.requests.post',side_effect=exception) as post:
                result=self.sender.send('test')
            self.assertEqual(result.status,'uncertain');self.assertEqual(post.call_count,1)

    def test_429_is_deferred_without_sleep_or_second_request(self):
        r=api_response(429,{'ok':False,'error_code':429,'parameters':{'retry_after':47}})
        with patch('app.notify.requests.post',return_value=r) as post:
            result=self.sender.send('test')
        self.assertEqual(result,DeliveryResult('retry',code=429,retry_after=47))
        self.assertEqual(post.call_count,1)

    def test_500_invalid_json_and_missing_message_id_are_uncertain(self):
        malformed=api_response();malformed.json.side_effect=ValueError()
        for r in [api_response(500,{'ok':False,'error_code':400}),malformed,
                  api_response(200,{'ok':True,'result':{}}),api_response(302)]:
            with patch('app.notify.requests.post',return_value=r) as post:
                self.assertEqual(self.sender.send('test').status,'uncertain')
            self.assertEqual(post.call_count,1)

    def test_definite_rejection_is_terminal(self):
        for code in [400,401,403]:
            with patch('app.notify.requests.post',return_value=api_response(code,{'ok':False,'error_code':code})):
                self.assertEqual(self.sender.send('test'),DeliveryResult('failed',code=code))

    def test_get_updates_conflict_is_typed_and_contains_no_url(self):
        with patch('app.notify.requests.post',return_value=api_response(409,{'ok':False,'error_code':409})):
            with self.assertRaises(BotAPIError) as caught:
                self.sender.call('getUpdates')
        self.assertEqual(caught.exception.code,409)
        self.assertNotIn(self.sender.bot_token,str(caught.exception))


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=str(Path(self.tmp.name)/'state.db')

    def test_migration_backs_up_preserves_rows_and_quarantines_legacy_queue(self):
        old=sqlite3.connect(self.path);old.executescript(SCHEMA)
        for key,notified,baseline in [('pending',0,0),('sent',1,0),('baseline',1,1)]:
            old.execute("INSERT INTO seen_items(item_key,source_key,external_id,url,category,first_seen_at,notified,baseline) VALUES(?,?,?,?,?,?,?,?)",
                        (key,'legacy',key,'https://example.org/job/'+key,'internships',NOW.isoformat(),notified,baseline))
        old.execute("INSERT INTO flags VALUES('enabled','1')");old.commit();old.close()
        db=Database(self.path)
        try:
            self.assertEqual(db.count_items(),3)
            self.assertEqual(db.get_flag('enabled'),'1')
            self.assertEqual(db.delivery_counts(),{'baseline':1,'legacy_review':1,'sent':1})
            self.assertEqual(db.pending_unnotified(),[])
            self.assertTrue(Path(db.backup_path).is_file())
            backup=sqlite3.connect(db.backup_path)
            self.assertEqual(backup.execute('SELECT COUNT(*) FROM seen_items').fetchone()[0],3)
            self.assertNotIn('delivery_state',[r[1] for r in backup.execute('PRAGMA table_info(seen_items)')])
            backup.close()
        finally:db.close()
        db=Database(self.path)
        self.assertIsNone(db.backup_path);db.close()

    def test_full_payload_roundtrip_after_restart(self):
        db=Database(self.path);original=item();db.add_item(original,category='internships');db.close()
        db=Database(self.path)
        try:self.assertEqual(db.deserialize(db.item_row(original.item_key)['payload']),original)
        finally:db.close()

    def test_interrupted_send_becomes_uncertain_after_restart(self):
        db=Database(self.path);original=item();db.add_item(original,category='internships')
        self.assertTrue(db.claim_delivery(original.item_key,NOW));db.close()
        db=Database(self.path)
        try:
            self.assertEqual(db.item_row(original.item_key)['delivery_state'],'uncertain')
            self.assertEqual(db.pending_unnotified(),[])
        finally:db.close()

    def test_second_process_instance_cannot_open_same_database(self):
        db=Database(self.path)
        try:
            with self.assertRaisesRegex(RuntimeError,'already in use'):Database(self.path)
        finally:db.close()
        db=Database(self.path);db.close()

    def test_lock_is_enforced_across_processes(self):
        db=Database(self.path)
        command=[sys.executable,'-c','import sys; from app.db import Database; db=Database(sys.argv[1]); db.close()',self.path]
        try:
            result=subprocess.run(command,cwd=ROOT,capture_output=True,text=True,timeout=10)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('already in use',result.stderr)
        finally:
            db.close()
        result=subprocess.run(command,cwd=ROOT,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_owner_and_bot_binding_prevent_accidental_reuse(self):
        db=Database(self.path)
        try:
            db.bind_identity('owner_id',1);db.bind_identity('owner_id',1)
            with self.assertRaises(RuntimeError):db.bind_identity('owner_id',2)
            db.bind_identity('bot_id',10)
            with self.assertRaises(RuntimeError):db.bind_identity('bot_id',11)
        finally:db.close()

    def test_claim_and_pause_are_atomic_state_changes(self):
        db=Database(self.path);original=item();db.add_item(original,category='internships')
        try:
            db.apply_control(enabled=False,offset=20)
            self.assertEqual(db.get_flag('offset'),'20')
            self.assertEqual(db.item_row(original.item_key)['delivery_state'],'skipped')
            self.assertFalse(db.claim_delivery(original.item_key,NOW))
        finally:db.close()

    def test_manual_resolution_and_safe_cli_guard(self):
        db=Database(self.path);original=item();db.add_item(original,category='internships')
        db.set_delivery(original.item_key,'uncertain','timeout');db.close()
        env={'DB_PATH':self.path,'CONFIG_PATH':str(ROOT/'config.yaml'),'PYTHONDONTWRITEBYTECODE':'1'}
        command=[sys.executable,'-m','app.main','--resolve-item',original.item_key,'--resolution','retry']
        r=subprocess.run(command,cwd=ROOT,env=env,capture_output=True,text=True,timeout=10)
        self.assertNotEqual(r.returncode,0)
        r=subprocess.run(command+['--allow-duplicate-risk'],cwd=ROOT,env=env,capture_output=True,text=True,timeout=10)
        self.assertEqual(r.returncode,0,r.stderr)
        db=Database(self.path)
        try:self.assertEqual(db.item_row(original.item_key)['delivery_state'],'pending')
        finally:db.close()


class BotFixture(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.cfg=Config.load(str(ROOT/'config.yaml'))
        self.cfg.db_path=str(Path(self.tmp.name)/'state.db');self.cfg.owner_id=123
        self.cfg.channels=[TelegramChannelSource('configured_key','example')];self.cfg.websites=[]
        self.bot=Bot(self.cfg);self.addCleanup(lambda:self.bot.db.close())
        self.bot.notifier.send=Mock(return_value=DeliveryResult('sent',message_id=42))
        self.bot.set_enabled(True)
        self.bot._available_sources={'tg_example'};self.bot._last_scan=NOW.timestamp()

    def queue(self,identifier='1'):
        it=item(identifier);self.bot.db.add_item(it,category='internships');self.bot._fresh_items[it.item_key]=it
        return it

class DeliveryTests(BotFixture):
    def test_success_is_persisted_and_not_sent_twice(self):
        it=self.queue();self.assertTrue(self.bot.deliver(it,now=NOW))
        self.assertFalse(self.bot.deliver(it,now=NOW+timedelta(seconds=2)))
        row=self.bot.db.item_row(it.item_key)
        self.assertEqual(row['delivery_state'],'sent');self.assertEqual(row['message_id'],42)
        self.assertEqual(self.bot.notifier.send.call_count,1)

    def test_queued_expired_opportunity_is_not_sent(self):
        it=self.queue();it.deadline=NOW-timedelta(seconds=1)
        self.assertFalse(self.bot.deliver(it,now=NOW))
        self.assertEqual(self.bot.db.item_row(it.item_key)['delivery_state'],'expired')
        self.bot.notifier.send.assert_not_called()

    def test_changed_rules_are_applied_before_delivery(self):
        it=self.queue();self.cfg.rules.categories={}
        self.bot.drain_pending(now=NOW)
        self.bot.notifier.send.assert_not_called()
        self.assertEqual(self.bot.db.item_row(it.item_key)['delivery_state'],'expired')

    def test_uncertain_send_is_not_automatically_repeated(self):
        it=self.queue();self.bot.notifier.send.return_value=DeliveryResult('uncertain')
        self.bot.drain_pending(now=NOW);self.bot.drain_pending(now=NOW+timedelta(seconds=100))
        self.assertEqual(self.bot.notifier.send.call_count,1)
        self.assertEqual(self.bot.db.item_row(it.item_key)['delivery_state'],'uncertain')

    def test_429_delays_the_entire_queue(self):
        it=self.queue('1');self.queue('2')
        self.bot.notifier.send.return_value=DeliveryResult('retry',code=429,retry_after=30)
        self.bot.drain_pending(now=NOW);self.bot.drain_pending(now=NOW+timedelta(seconds=29))
        self.assertEqual(self.bot.notifier.send.call_count,1)
        available=self.bot.db.item_row(it.item_key)['available_at']
        self.assertGreaterEqual(available,NOW.timestamp()+30)
        self.bot.notifier.send.return_value=DeliveryResult('sent',message_id=42)
        self.bot.drain_pending(now=datetime.fromtimestamp(available+1,timezone.utc))
        self.assertEqual(self.bot.notifier.send.call_count,2)

    def test_safe_connection_failures_back_off(self):
        it=self.queue();self.bot.notifier.send.return_value=DeliveryResult('retry',retry_after=10)
        self.bot.deliver(it,now=NOW)
        available=self.bot.db.item_row(it.item_key)['available_at']
        next_time=datetime.fromtimestamp(available+1,timezone.utc)
        self.bot.deliver(it,now=next_time)
        row=self.bot.db.item_row(it.item_key)
        self.assertEqual(row['attempts'],2)
        self.assertGreaterEqual(row['available_at'],next_time.timestamp()+20)

    def test_blocked_owner_pauses_queue_and_401_stops_process(self):
        it=self.queue();self.bot.notifier.send.return_value=DeliveryResult('failed',code=403)
        self.bot.deliver(it,now=NOW)
        self.assertFalse(self.bot.is_enabled())
        self.bot.set_enabled(True);it=self.queue('2')
        self.bot.notifier.send.return_value=DeliveryResult('failed',code=401)
        self.bot.deliver(it,now=NOW)
        self.assertTrue(self.bot.stop_event.is_set())
        self.assertIsNotNone(self.bot.fatal_error)

    def test_source_outage_and_stale_snapshot_hold_queue(self):
        self.queue();self.bot._available_sources.clear()
        self.bot.drain_pending(now=NOW)
        self.bot._available_sources={'tg_example'};self.bot._last_scan=NOW.timestamp()-100000
        self.bot.drain_pending(now=NOW)
        self.bot.notifier.send.assert_not_called()

    def test_removed_source_is_skipped(self):
        it=self.queue();self.cfg.channels=[];self.bot.drain_pending(now=NOW)
        self.assertEqual(self.bot.db.item_row(it.item_key)['delivery_state'],'skipped')
        self.bot.notifier.send.assert_not_called()

    def test_queued_telegram_post_is_refreshed_and_closed_post_rejected(self):
        it=self.queue();self.bot._fresh_items.clear()
        fresh=copy.deepcopy(it);fresh.raw_text='Прием заявок закрыт'
        with patch('app.main.telegram_channel.fetch_post',return_value=fresh) as fetch:
            self.bot.drain_pending(now=NOW)
        fetch.assert_called_once_with('example',1)
        self.bot.notifier.send.assert_not_called()
        self.assertEqual(self.bot.db.item_row(it.item_key)['delivery_state'],'expired')

    def test_missing_post_and_missing_vacancy_are_expired(self):
        it=self.queue();self.bot._fresh_items.clear()
        with patch('app.main.telegram_channel.fetch_post',return_value=None):self.bot.drain_pending(now=NOW)
        self.assertEqual(self.bot.db.item_row(it.item_key)['delivery_state'],'expired')
        self.cfg.websites=[HttpSource('web','Web','https://careers.croc.ru/vacancies/','croc_vacancies')]
        old=item('2','web');self.bot.db.add_item(old,category='internships');self.bot._available_sources.add('web')
        self.bot.drain_pending(now=NOW)
        self.assertEqual(self.bot.db.item_row(old.item_key)['delivery_state'],'expired')

    def test_stop_during_collection_cannot_send_later_results(self):
        self.bot.db.save_telegram_cursor('tg_example',1)
        def collect(*args,**kwargs):
            self.bot.set_enabled(False)
            return [item('2')]
        with patch('app.main.telegram_channel.fetch_channel',side_effect=collect):self.bot.run_cycle()
        self.bot.notifier.send.assert_not_called()
        self.assertEqual(self.bot.db.item_row('tg_example:2')['delivery_state'],'baseline')

    def test_stop_discards_pending_without_archiving_for_next_start(self):
        it=self.queue();self.bot.set_enabled(False);self.bot.set_enabled(True)
        self.bot.drain_pending(now=NOW)
        self.bot.notifier.send.assert_not_called()
        self.assertEqual(self.bot.db.item_row(it.item_key)['delivery_state'],'skipped')

    def test_resume_baselines_unseen_pause_history_then_sends_future_posts(self):
        self.bot.db.save_telegram_cursor('tg_example',1)
        self.bot.set_enabled(False);self.bot.set_enabled(True)
        with patch('app.main.telegram_channel.fetch_channel',return_value=[item('2')]):
            self.bot.run_cycle()
        self.bot.notifier.send.assert_not_called()
        self.assertEqual(self.bot.db.item_row('tg_example:2')['delivery_state'],'baseline')
        with patch('app.main.telegram_channel.fetch_channel',return_value=[item('3')]):
            self.bot.run_cycle()
        self.assertEqual(self.bot.notifier.send.call_count,1)
        self.assertEqual(self.bot.db.item_row('tg_example:3')['delivery_state'],'sent')

    def test_stop_waits_for_inflight_request_but_blocks_next(self):
        first=self.queue('1');second=self.queue('2')
        entered,released,stopped=threading.Event(),threading.Event(),threading.Event()
        def send(text):
            entered.set();released.wait(3)
            return DeliveryResult('sent',message_id=42)
        self.bot.notifier.send.side_effect=send
        sender=threading.Thread(target=lambda:self.bot.deliver(first,now=NOW));sender.start()
        self.assertTrue(entered.wait(2))
        stopper=threading.Thread(target=lambda:(self.bot.set_enabled(False),stopped.set()));stopper.start()
        self.assertFalse(stopped.wait(.05))
        released.set();sender.join(3);stopper.join(3)
        self.assertTrue(stopped.is_set())
        self.assertFalse(self.bot.deliver(second,now=NOW+timedelta(seconds=5)))
        self.assertEqual(self.bot.notifier.send.call_count,1)

    def test_restart_uses_public_refresh_before_sending_pending(self):
        it=self.queue();self.bot.db.close();self.bot=Bot(self.cfg)
        self.bot.notifier.send=Mock(return_value=DeliveryResult('sent',message_id=99))
        with patch('app.main.telegram_channel.fetch_channel',return_value=[]),patch('app.main.telegram_channel.fetch_post',return_value=it) as refresh:
            self.bot.run_cycle()
        refresh.assert_called_once()
        self.assertEqual(self.bot.db.item_row(it.item_key)['delivery_state'],'sent')


class CommandTests(BotFixture):
    def update(self,identifier,text,**changes):
        message={'chat':{'id':123,'type':'private'},'from':{'id':123},'text':text}
        message.update(changes)
        return {'update_id':identifier,'message':message}

    def test_command_is_persisted_before_acknowledgement_failure(self):
        self.bot.notifier.send.side_effect=RuntimeError('lost acknowledgement')
        with self.assertRaises(RuntimeError):handle_update(self.bot,self.update(10,'/stop'))
        self.assertFalse(self.bot.is_enabled());self.assertEqual(self.bot.db.get_flag('offset'),'11')
        handle_update(self.bot,self.update(9,'/start'))
        self.assertFalse(self.bot.is_enabled())

    def test_only_exact_owner_private_commands_are_accepted(self):
        self.bot.service_message=Mock()
        for update in [self.update(1,'/stopper'),self.update(2,'/stop',chat={'id':123,'type':'group'}),
                       self.update(3,'/stop',**{'from':{'id':999}}),self.update(4,'/stop@other'),
                       {'update_id':5,'edited_message':self.update(5,'/stop')['message']}]:
            handle_update(self.bot,update)
        self.assertTrue(self.bot.is_enabled());self.bot.service_message.assert_not_called()
        self.bot.bot_username='mybot';handle_update(self.bot,self.update(6,'/stop@mybot'))
        self.assertFalse(self.bot.is_enabled())

    def test_409_terminates_polling(self):
        self.bot.notifier.call=Mock(side_effect=BotAPIError(409))
        poll_updates(self.bot,self.bot.stop_event)
        self.assertTrue(self.bot.stop_event.is_set())
        self.assertIn('409',self.bot.fatal_error)

    def test_startup_rejects_webhook_without_deleting_it(self):
        self.bot.notifier.call=Mock(side_effect=[{'id':42,'username':'mybot'},{'url':'https://example.org/webhook'}])
        with self.assertRaises(RuntimeError):self.bot.initialize()
        self.assertEqual([c.args[0] for c in self.bot.notifier.call.call_args_list],['getMe','getWebhookInfo'])


class DedupAndFormatTests(unittest.TestCase):
    def setUp(self):
        self.db=Database(':memory:');self.addCleanup(self.db.close)

    def test_same_title_at_different_companies_is_not_a_duplicate(self):
        first=item();self.db.add_item(first,category='internships')
        other=item('2','other');other.title=first.title;other.url='https://other.example/job/2';other.apply_url=None
        self.assertFalse(self.db.is_duplicate(other))

    def test_tracking_and_fragment_do_not_defeat_strong_url_dedup(self):
        first=item();self.db.add_item(first,category='internships')
        other=item('2','other');other.apply_url=first.apply_url+'?utm_source=channel#form'
        self.assertTrue(self.db.is_duplicate(other))
        self.assertEqual(canonical_url(first.apply_url),canonical_url(other.apply_url))

    def test_homepages_and_rejected_items_do_not_suppress_opportunities(self):
        first=item();first.url='https://company.example/';first.apply_url=None
        self.db.add_item(first,baseline=True)
        other=item('2','other');other.url=first.url;other.apply_url=None
        self.assertFalse(self.db.is_duplicate(other))
        self.assertIsNone(canonical_url('https://company.example/'))

    def test_different_deadlines_or_old_occurrences_do_not_collapse(self):
        first=item();self.db.add_item(first,category='internships')
        other=item('2','other');other.apply_url=first.apply_url;other.deadline=first.deadline+timedelta(days=7)
        self.assertFalse(self.db.is_duplicate(other))
        other.deadline=first.deadline
        self.assertFalse(self.db.is_duplicate(other,now=NOW+timedelta(days=31)))

    def test_html_is_escaped_bounded_and_unsafe_links_removed(self):
        cfg=Config.load(str(ROOT/'config.yaml'));bot=Bot.__new__(Bot);bot.cfg=cfg
        it=item();it.title='<script>'+('😀<&'*4000);it.summary='😀<&'*4000
        it.source_name='<b>untrusted</b>';it.apply_url='javascript:alert(1)'
        rendered=bot.format_message(it)
        self.assertLessEqual(units(rendered),4096)
        self.assertNotIn('<script>',rendered);self.assertNotIn('javascript:',rendered)
        self.assertIn('&lt;script&gt;',rendered);self.assertIn('Оригинал',rendered)

    def test_refresh_reads_only_the_public_page(self):
        with patch('app.collectors.telegram_channel.http_get',return_value=response(page([1,2]))) as get:
            result=fetch_post('example',2)
        self.assertEqual(result.external_id,'2')
        get.assert_called_once_with('https://t.me/s/example?before=3',allow_redirects=False)

    def test_refresh_accepts_real_forward_pagination_without_following(self):
        html=page([1,2])+'<a class="tme_messages_more js-messages_more" data-after="2" href="/s/example?after=2"></a>'
        with patch('app.collectors.telegram_channel.http_get',return_value=response(html)) as get:
            result=fetch_post('example',2)
        self.assertEqual(result.external_id,'2')
        self.assertEqual(get.call_count,1)

    def test_refresh_rejects_external_forward_pagination(self):
        html=page([1,2])+'<a class="tme_messages_more" data-after="2" href="https://other.example/s/example?after=2"></a>'
        with patch('app.collectors.telegram_channel.http_get',return_value=response(html)) as get:
            with self.assertRaises(RuntimeError):fetch_post('example',2)
        self.assertEqual(get.call_count,1)


if __name__=='__main__':unittest.main()
