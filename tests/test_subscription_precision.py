"""Subscription boundaries must retain sub-millisecond precision on fast runners."""
from datetime import datetime, timedelta, timezone
import unittest
from app.db import Database
from app.models import Item


class SubscriptionPrecisionTests(unittest.TestCase):
    def setUp(self):
        self.db=Database(':memory:');self.addCleanup(self.db.close)
        self.boundary=datetime(2026,10,25,10,0,0,123456,tzinfo=timezone.utc)
        self.db.user_control(101,active=True)
        self.db._conn.execute('UPDATE users SET subscribed_at=?',(self.boundary.timestamp(),));self.db._conn.commit()

    def add(self, key, published):
        entry=Item('source','Source',key,'https://example.test/jobs/'+key,'Python','','',published)
        self.db.add_item(entry,category='internships',eligible_at=self.boundary.timestamp()+10)
        return entry

    def test_one_microsecond_after_subscription_is_eligible_but_before_and_equal_are_not(self):
        before=self.add('before',self.boundary-timedelta(microseconds=1))
        equal=self.add('equal',self.boundary)
        after=self.add('after',self.boundary+timedelta(microseconds=1))
        self.db.fill_user_queue()
        self.assertIsNone(self.db.user_delivery(101,before.item_key))
        self.assertIsNone(self.db.user_delivery(101,equal.item_key))
        self.assertIsNotNone(self.db.user_delivery(101,after.item_key))

    def test_iso_timezone_offset_keeps_the_same_precise_boundary(self):
        published=(self.boundary+timedelta(microseconds=1)).astimezone(timezone(timedelta(hours=3)))
        entry=self.add('offset',published)
        self.db.fill_user_queue()
        self.assertIsNotNone(self.db.user_delivery(101,entry.item_key))

    def test_malformed_legacy_time_is_quarantined_without_blocking_healthy_item(self):
        malformed=self.add('bad',self.boundary+timedelta(seconds=1))
        good=self.add('good',self.boundary+timedelta(seconds=1))
        self.db._conn.execute('UPDATE seen_items SET opportunity_at=? WHERE item_key=?',('not-a-date',malformed.item_key));self.db._conn.commit()
        self.db.fill_user_queue()
        self.assertIsNone(self.db.user_delivery(101,malformed.item_key))
        self.assertEqual(self.db.item_row(malformed.item_key)['delivery_state'],'legacy_review')
        self.assertIsNotNone(self.db.user_delivery(101,good.item_key))
