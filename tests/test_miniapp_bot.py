"""Mini App launch is queued; all Telegram I/O is mocked."""
import json
import os
import unittest
from unittest.mock import Mock, patch
from app.config import Config, ConfigurationError
from app.db import Database
from app.main import Bot, handle_update
from app.notify import DeliveryResult, BotAPIError
from tests import test_multiuser
from tests.test_delivery import ROOT

URL='https://radar.example.org/app'


class MiniAppBotTests(unittest.TestCase):
    def setUp(self):
        self.fixture=test_multiuser.MultiuserTests();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.bot=self.fixture.bot
        self.bot.cfg.miniapp_url=URL
        self.bot.cfg.miniapp_menu_button=False

    def row(self,uid=101):
        return self.bot.db._conn.execute('SELECT * FROM command_replies WHERE telegram_id=?',(uid,)).fetchone()

    def test_app_is_private_and_only_persists_a_web_app_keyboard(self):
        self.fixture.command(101,'/app')
        markup=json.loads(self.row()['reply_markup'])
        self.assertEqual(markup,{'inline_keyboard':[[{'text':'📱 Открыть IT Radar','web_app':{'url':URL}}]]})
        self.assertIsNone(self.bot.db.user(101))
        self.bot.notifier.send.assert_not_called();self.bot.service_message.assert_not_called()
        handle_update(self.bot,{'update_id':2,'message':{'chat':{'type':'group','id':-1},'from':{'id':202},'text':'/app'}})
        self.assertIsNone(self.row(202))

    def test_start_and_help_include_launch_button_but_stop_preserves_semantics(self):
        for command in ['/start','/help']:
            self.fixture.command(101,command);self.assertIsNotNone(self.row()['reply_markup'])
        self.fixture.command(101,'/stop')
        self.assertIsNone(self.row()['reply_markup']);self.assertFalse(self.bot.db.user(101)['is_active'])

    def test_no_url_has_no_fake_button(self):
        self.bot.cfg.miniapp_url=''
        for command in ['/app','/start','/help']:
            self.fixture.command(101,command);self.assertIsNone(self.row()['reply_markup'])
        self.fixture.command(101,'/app');self.assertIn('пока не настроен',self.row()['text'])

    def test_restart_and_duplicate_update_preserve_queued_markup(self):
        self.fixture.command(101,'/app');before=dict(self.row())
        self.bot.db.close();self.bot.db=Database(self.bot.cfg.db_path)
        self.assertEqual(before,dict(self.row()))
        handle_update(self.bot,{'update_id':1,'message':{'chat':{'type':'private','id':101},'from':{'id':101},'text':'/app'}})
        self.assertEqual(before,dict(self.row()))

    def test_dispatch_preserves_markup_429_and_uncertain_without_sqlite_lock(self):
        self.bot.service_message=Bot.service_message.__get__(self.bot,Bot)
        self.fixture.command(101,'/app')
        def send(*args,**kwargs):
            self.assertTrue(self.bot.db._lock.acquire(blocking=False))
            self.bot.db._lock.release()
            self.assertEqual(kwargs['reply_markup']['inline_keyboard'][0][0]['web_app']['url'],URL)
            return DeliveryResult('retry',429,120)
        self.bot.notifier.send.side_effect=send
        self.bot.flush_command_reply()
        self.assertEqual(self.row()['status'],'pending')
        self.assertGreater(float(self.bot.db.get_flag('send_after')),__import__('time').time()+100)
        self.bot.db._conn.execute('UPDATE command_replies SET available_at=0')
        self.bot.db._conn.execute('DELETE FROM command_reply_limits');self.bot.db._conn.commit()
        self.bot.db.set_flag('send_after','0')
        self.bot.notifier.send.side_effect=None
        self.bot.notifier.send.return_value=DeliveryResult('uncertain')
        self.bot.flush_command_reply();self.assertEqual(self.row()['status'],'uncertain')
        self.bot.db.set_flag('send_after','0');self.bot.flush_command_reply()
        self.assertEqual(self.bot.notifier.send.call_count,2)

    def test_optional_menu_runs_outside_sqlite_lock(self):
        self.bot.cfg.miniapp_menu_button=True
        def call(method,*args,**kwargs):
            self.assertTrue(self.bot.db._lock.acquire(blocking=False));self.bot.db._lock.release()
            if method=='getMe':return {'id':123,'username':'test_radar_bot'}
            if method=='getWebhookInfo':return {'url':''}
            return True
        self.bot.notifier.call=Mock(side_effect=call)
        self.bot.initialize()
        self.bot.notifier.call.assert_any_call('setChatMenuButton',{'menu_button':{'type':'web_app','text':'📱 Открыть IT Radar','web_app':{'url':URL}}})

    def test_menu_is_not_called_without_opt_in(self):
        self.bot.notifier.call=Mock(side_effect=lambda method,*a,**k: {'id':123,'username':'test_radar_bot'} if method=='getMe' else {'url':''} if method=='getWebhookInfo' else True)
        self.bot.initialize()
        self.assertNotIn('setChatMenuButton',[call.args[0] for call in self.bot.notifier.call.call_args_list])

    def test_config_url_is_optional_and_invalid_values_never_echo(self):
        with patch.dict(os.environ,{},clear=True):
            self.assertEqual(Config.load(str(ROOT/'config.yaml')).miniapp_url,'')
        for url in ['http://example.org','javascript:private','https://user:private@example.org','https://example.org/#private']:
            with patch.dict(os.environ,{'MINIAPP_URL':url},clear=True):
                with self.assertRaises(ConfigurationError) as caught:Config.load(str(ROOT/'config.yaml'))
                self.assertNotIn('private',str(caught.exception))
