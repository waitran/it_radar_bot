from datetime import datetime, timezone
from app.event_index import extract
from tests.personal_support import PersonalCase

class EventIndexTests(PersonalCase):
    def test_deadline_distinct_from_event_and_precision(self):
        item,_=self.event(text='Митап состоится 2027-06-10 в 18:00.\nРегистрация до 2027-06-08.')
        result=extract(item);self.assertEqual(result['event_day'],'2027-06-10');self.assertEqual(result['deadline_day'],'2027-06-08')
        self.assertEqual(result['event_precision'],'time');self.assertEqual(result['deadline_precision'],'date')

    def test_ambiguous_and_absent_dates_not_invented(self):
        for text in ['Митап скоро','Митап 2027-06-10 и 2027-06-11','Python 3.12']:
            item,_=self.event(str(len(text)),text=text);self.assertIsNone(extract(item)['event_at'])
        item,_=self.event('missingyear',text='Митап 10 июня');item.published_at=None
        self.assertIsNone(extract(item)['event_at'])

    def test_source_deadline_and_closed_revision(self):
        item,ref=self.event();item.source_deadline=datetime(2027,6,8,12,tzinfo=timezone.utc)
        self.db.revise_item(item,item.content_hash,state='pending')
        m=self.db._conn.execute('SELECT * FROM event_metadata WHERE item_id=?',(ref,)).fetchone()
        self.assertEqual(m['deadline_precision'],'time')
        item.explicitly_closed=True;self.db.revise_item(item,item.content_hash,state='rejected')
        self.assertEqual(self.db._conn.execute('SELECT registration_status FROM event_metadata WHERE item_id=?',(ref,)).fetchone()[0],'closed')
