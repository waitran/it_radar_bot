from tests.personal_support import PersonalCase

class CallbacksTests(PersonalCase):
    def test_sender_cannot_change_another_users_settings(self):
        self.callback('pref:202:topics:python:1')
        self.assertEqual(self.db.preferences(202)['topics'],[])
        self.assertEqual(self.db.preferences(101)['topics'],[])
        self.callback('pref:101:topics:python:1')
        self.assertEqual(self.db.preferences(101)['topics'],['python'])
        self.callback('pref:101:topics:python:1')
        self.assertEqual(self.db.preferences(101)['topics'],['python'])

    def test_invalid_data_safe_and_acknowledged(self):
        for data in ['', 'save:0','save:abc','x'*65,'page:101:week:-1','unsave:101:'+'9'*50,None]:self.callback(data)
        self.assertEqual(self.bot.notifier.call.call_count,7)
        self.bot.notifier.send.assert_not_called()

    def test_callback_is_acked_without_locks(self):
        def ack(*args,**kwargs):
            self.assertTrue(self.db._lock.acquire(False));self.db._lock.release()
            self.assertTrue(self.bot._lock.acquire(False));self.bot._lock.release()
        self.bot.notifier.call.side_effect=ack
        self.callback('pref:101:topics:python:1')

    def test_private_chat_required(self):
        self.callback('pref:101:topics:python:1',chat=202)
        self.assertEqual(self.db.preferences(101)['topics'],[])

    def test_city_timezone_input_and_command_markup(self):
        self.command('/settings');row=self.db.pending_reply(10**12);self.assertIn('inline_keyboard',row['reply_markup'])
        self.callback('input:101:city');self.command('Санкт-Петербург')
        self.assertEqual(self.db.preferences(101)['city'],'Санкт-Петербург')
        self.callback('input:101:timezone');self.command('Bad/Zone')
        self.assertEqual(self.db.preferences(101)['timezone'],'Europe/Moscow')
        self.command('Asia/Tokyo');self.assertEqual(self.db.preferences(101)['timezone'],'Asia/Tokyo')

    def test_markup_sent_through_durable_command_outbox(self):
        self.command('/settings');self.bot.dispatch_pending()
        call=self.bot.notifier.send.call_args
        self.assertEqual(call.kwargs['chat_id'],101)
        self.assertIn('inline_keyboard',call.kwargs['reply_markup'])

    def test_notifier_serializes_markup(self):
        from app.notify import TelegramNotifier
        from tests.test_delivery import api_response
        from unittest.mock import patch
        from app.keyboards import save_keyboard
        with patch('app.notify.requests.post',return_value=api_response()) as post:
            TelegramNotifier('synthetic',101).send('hello',reply_markup=save_keyboard(1))
        self.assertEqual(post.call_args.kwargs['json']['reply_markup'],save_keyboard(1))
