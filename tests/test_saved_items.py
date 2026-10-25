from app.db import Database
from app.agenda import render
from tests.personal_support import PersonalCase

class SavedTests(PersonalCase):
    def test_isolation_idempotency_and_restart(self):
        item,ref=self.event()
        self.callback(f'save:{ref}');self.callback(f'save:{ref}')
        self.assertEqual(len(self.db.personal_page(101,'saved')),1)
        self.assertEqual(len(self.db.personal_page(202,'saved')),0)
        self.callback(f'unsave:101:{ref}',uid=202)
        self.assertEqual(len(self.db.personal_page(101,'saved')),1)
        self.db.user_control(101,active=False);self.db.close();self.bot.db=Database(self.cfg.db_path);self.db=self.bot.db
        self.assertEqual(len(self.db.personal_page(101,'saved')),1)

    def test_saved_pagination_html_and_short_callbacks(self):
        for i in range(5):
            _,ref=self.event(str(i));self.db.save_item(101,ref)
        text,markup=render(self.db,101,'saved')
        self.assertIn('Оригинал',text);self.assertEqual(len(self.db.personal_page(101,'saved',1)),2)
        self.assertLess(len(text.encode('utf-16-le'))//2,4096)
        for row in markup['inline_keyboard']:
            for b in row:self.assertLessEqual(len(b['callback_data'].encode()),64)

    def test_long_urls_and_escaped_text_fit_telegram(self):
        from app.formatting import units
        for i in range(3):
            item,ref=self.event(str(i));item.source_key='website';item.url='https://example.com/'+('a'*1150)
            # Long URL fallback belongs to website events, not canonical Telegram posts.
            self.db.add_item(item,category='events');ref=self.db.item_ref(item.item_key)
            item.title='<>&'*300;item.source_name='<>&'*300
            self.db.revise_item(item,item.content_hash,state='pending');self.db.save_item(101,ref)
        text,markup=render(self.db,101,'saved')
        self.assertLessEqual(units(text),4096)
        self.assertIn('&lt;',text);self.assertEqual(sum('url' in b for row in markup['inline_keyboard'] for b in row),3)
