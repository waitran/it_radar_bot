from datetime import datetime, timezone
from tests.personal_support import PersonalCase

class AgendaTests(PersonalCase):
    def test_week_boundaries_timezone_sort_and_unknown(self):
        prefs=self.db.preferences(101);prefs['timezone']='Asia/Tokyo';self.db.set_preferences(101,prefs)
        texts=['Митап 2027-06-01 в 18:00','Митап 2027-06-07 в 18:00','Митап 2027-06-08 в 18:00',
               'Митап 2027-05-31','Митап скоро','Митап 2027-06-01 в 00:30']
        for i,text in enumerate(texts):self.event(str(i),text)
        now=datetime(2027,5,31,20,tzinfo=timezone.utc) # June 1 in Tokyo
        rows=self.db.personal_page(101,'week',now=now)
        self.assertEqual([r['external_id'] for r in rows],['5','0']) # June 7 18 Moscow = June 8 Tokyo, outside
        self.assertTrue(any('idx_agenda' in str(tuple(r)) for r in self.db._conn.execute('EXPLAIN QUERY PLAN SELECT * FROM event_metadata WHERE event_at>=? AND event_at<?',(0,1))))

    def test_filters_pagination_baseline_no_broadcast(self):
        for i in range(5):
            item,ref=self.event(str(i));self.db.revise_item(item,item.content_hash,state='baseline')
        self.assertEqual(len(self.db.personal_page(101,'week')),4)
        self.assertEqual(len(self.db.personal_page(101,'week',1)),2)
        prefs=self.db.preferences(202);prefs['topics']=['backend'];self.db.set_preferences(202,prefs)
        self.assertEqual(self.db.personal_page(202,'week'),[])
        self.command('/week');self.bot.notifier.send.assert_not_called()
