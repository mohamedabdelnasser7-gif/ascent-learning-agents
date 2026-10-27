import os
import unittest
from unittest import mock

from kit.db import Db
from kit.limits import DAY, Limits, OverLimit
from tests.fake_model import FakeModel, say


class LimitsTest(unittest.TestCase):
    def setUp(self):
        self.db = Db(":memory:")
        self.addCleanup(self.db.close)
        self.now = [5000.0]

    def make(self, **kw):
        return Limits(self.db, clock=lambda: self.now[0], **kw)

    def test_defaults_and_env(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            lim = self.make()
        self.assertEqual((lim.per_customer, lim.window, lim.daily), (20, 600, 1000))
        env = {"LIMIT_CUSTOMER_CALLS": "5", "LIMIT_CUSTOMER_SECONDS": "60", "LIMIT_DAILY_CALLS": "50"}
        with mock.patch.dict(os.environ, env, clear=True):
            lim = self.make()
        self.assertEqual((lim.per_customer, lim.window, lim.daily), (5, 60, 50))
        with mock.patch.dict(os.environ, {"LIMIT_CUSTOMER_CALLS": "lots"}, clear=True):
            self.assertEqual(self.make().per_customer, 20)

    def test_twenty_calls_per_ten_minutes(self):
        lim = self.make()
        for _ in range(20):
            lim.take("whatsapp:a")
        self.assertEqual(lim.over("whatsapp:a"), "customer")
        self.assertIsNone(lim.over("whatsapp:b"))
        with self.assertRaises(OverLimit):
            lim.take("whatsapp:a")
        self.now[0] += 601
        self.assertIsNone(lim.over("whatsapp:a"))

    def test_daily_ceiling_counts_everyone(self):
        lim = self.make(daily=3)
        for who in "abc":
            lim.take(who)
        self.assertEqual(lim.over("d"), "daily")
        self.now[0] += DAY + 1
        self.assertIsNone(lim.over("d"))

    def test_counts_live_in_sqlite(self):
        self.make(per_customer=1).take("a")
        self.assertEqual(self.make(per_customer=1).over("a"), "customer")

    def test_notice_once_per_window(self):
        lim = self.make()
        self.assertEqual([lim.notice_due("a", "customer") for _ in range(2)], [True, False])
        self.now[0] += 601
        self.assertTrue(lim.notice_due("a", "customer"))
        self.assertTrue(lim.notice_due("b", "daily"))
        self.now[0] += 601
        self.assertFalse(lim.notice_due("b", "daily"))

    def test_metered_model_counts_before_calling(self):
        lim = self.make(per_customer=1)
        model = FakeModel(say("hi"), say("never"))
        metered = lim.meter(model, "a")
        metered.chat([])
        with self.assertRaises(OverLimit):
            metered.chat([])
        self.assertEqual(len(model.calls), 1)


if __name__ == "__main__":
    unittest.main()
