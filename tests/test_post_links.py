"""All links describing a Telegram source lead to the actual event post."""
import time
from bs4 import BeautifulSoup
from app.agenda import render
from app.formatting import original_url, source_text
from app.reminders import format_reminder
from tests.personal_support import PersonalCase


class PostLinkTests(PersonalCase):
    def legacy_event(self):
        item,ref=self.event()
        item.source_name='Telegram-канал t.me/example'
        item.url='https://t.me/example'
        self.db.revise_item(item,item.content_hash,state='pending')
        return item,ref

    def assert_post_links(self, text):
        parsed=BeautifulSoup(text,'html.parser')
        urls=[a['href'] for a in parsed.select('a[href]')]
        self.assertTrue(urls)
        self.assertTrue(all(url=='https://t.me/example/1' for url in urls),urls)
        self.assertNotIn('t.me/example',parsed.get_text())

    def test_notification_source_and_original_link_to_post(self):
        item,_=self.legacy_event()
        self.assert_post_links(self.bot.format_message(item))

    def test_saved_and_week_use_post_for_existing_rows(self):
        item,ref=self.legacy_event();self.db.save_item(101,ref)
        for kind in ('saved','week'):
            text,markup=render(self.db,101,kind)
            self.assert_post_links(text)

    def test_reminder_links_to_post(self):
        item,ref=self.legacy_event();self.db.save_item(101,ref);self.db.enable_reminder(101,ref,1)
        self.db._conn.execute('UPDATE reminder_jobs SET due_at=0');self.db._conn.commit()
        job=self.db.claim_reminder(time.time())
        self.assertIsNotNone(job)
        self.assert_post_links(format_reminder(item,job,self.db.preferences(101)))

    def test_unknown_post_id_never_falls_back_to_channel(self):
        item,_=self.legacy_event();item.external_id='unknown'
        self.assertIsNone(original_url(item))
        self.assertNotIn('href=',source_text(item))
        self.assertNotIn('t.me/',source_text(item))

    def test_website_url_and_registration_form_stay_distinct(self):
        item,_=self.event();item.source_key='company';item.url='https://example.org/event/123'
        self.assertEqual(original_url(item),item.url)
        item.source_key='tg_example';item.apply_url='https://example.org/register'
        self.assertIn('href="https://example.org/register"',self.bot.format_message(item))
