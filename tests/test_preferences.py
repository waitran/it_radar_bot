from datetime import datetime, timezone
from app.preferences import matches
from tests.personal_support import PersonalCase

class PreferencesTests(PersonalCase):
    def test_defaults_and_independent_filters(self):
        item,ref=self.event();prefs=self.db.preferences(101)
        self.assertTrue(matches(prefs,item));prefs['topics']=['backend'];self.db.set_preferences(101,prefs)
        self.assertFalse(matches(self.db.preferences(101),item))
        self.assertTrue(matches(self.db.preferences(202),item))
        self.bot.fill_delivery_queue(datetime.now(timezone.utc))
        self.assertIsNone(self.db.user_delivery(101,item.item_key))
        self.assertIsNotNone(self.db.user_delivery(202,item.item_key))

    def test_unknown_dimensions_conservatively_excluded(self):
        item,_=self.event(text='Митап по программированию без уточнения формата')
        item.title='Митап без уточнения направления'
        for field,value in [('topics',['python']),('format','offline'),('city','Москва')]:
            prefs=self.db.preferences(101);prefs[field]=value;self.assertFalse(matches(prefs,item))

    def test_change_does_not_replay_archive(self):
        item,_=self.event();prefs=self.db.preferences(101);prefs['topics']=['backend'];self.db.set_preferences(101,prefs)
        self.bot.fill_delivery_queue(datetime.now(timezone.utc))
        prefs['topics']=[];self.db.set_preferences(101,prefs)
        self.bot.fill_delivery_queue(datetime.now(timezone.utc))
        self.assertIsNone(self.db.user_delivery(101,item.item_key))

    def test_recheck_before_send(self):
        item,_=self.event();self.bot.fill_delivery_queue(datetime.now(timezone.utc))
        prefs=self.db.preferences(101);prefs['topics']=['backend'];self.db.set_preferences(101,prefs)
        self.assertFalse(self.bot.deliver(item,telegram_id=101));self.bot.notifier.send.assert_not_called()
