import unittest

from frontdesk.loop import PHOTO_NOT_OPENED
from kit.send_check import amounts_in, check, detect_script, prices_in
from kit.tools import store
from kit.tools.handoff import TAKING_OVER
from tests.fake_model import clean_env

AR = "عندكم شرابات مقاس ٤٣؟"
FRANCO = "3ndko sharabat size 43?"
EN = "Do you have socks in size 43?"


class SendCheck(unittest.TestCase):
    def setUp(self):
        clean_env(self)
        self.amounts = store.price_points()
        self.vocab = store.vocabulary()

    def ok(self, reply, customer, amounts=None):
        return check(reply, customer, self.amounts if amounts is None else amounts, self.vocab)

    # script match

    def test_detect(self):
        self.assertEqual(detect_script(AR), "arabic")
        self.assertEqual(detect_script(FRANCO), "franco")
        self.assertEqual(detect_script("enta bot wala eh?"), "franco")
        self.assertEqual(detect_script(EN), "english")
        self.assertIsNone(detect_script("؟ 43"))

    def test_arabic_in_arabic_out(self):
        self.assertTrue(self.ok("أيوه عندنا مقاس 43، الشراب بـ 949 جنيه.", AR)[0])
        self.assertTrue(self.ok("عندنا Nilestep Socks الأسود مقاس M بـ 949 جنيه.", AR)[0])
        passed, fb = self.ok("Yes, we have size 43 for 949 EGP.", AR)
        self.assertFalse(passed)
        self.assertIn("Arabic letters", fb)

    def test_franco_in_latin_out(self):
        self.assertTrue(self.ok("aywa 3andena size 43, b 949 geneh.", FRANCO)[0])
        passed, fb = self.ok("أيوه عندنا مقاس 43.", FRANCO)
        self.assertFalse(passed)
        self.assertIn("Franco", fb)

    def test_english_in_english_out(self):
        self.assertTrue(self.ok("Yes, size 43 is in stock at 949 EGP.", EN)[0])
        self.assertFalse(self.ok("أيوه عندنا.", EN)[0])
        passed, fb = self.ok("aywa 3andena el size da.", EN)
        self.assertFalse(passed)
        self.assertIn("plain English", fb)

    # banned words and dashes

    def test_banned_words(self):
        for reply, word in (("949 جنيه للجوز", "الجوز"), ("949 geneh el goz", "el goz"), ("هيناسبك 43 مرتاح", "مرتاح")):
            customer = FRANCO if "geneh" in reply else AR
            passed, fb = self.ok(reply, customer)
            self.assertFalse(passed, reply)
            self.assertIn(word, fb)

    def test_sympathy_lines(self):
        passed, fb = self.ok("مقدر إنك متضايق وحقك تزعل، الاوردر في الطريق.", AR)
        self.assertFalse(passed)
        self.assertIn("sympathy", fb)
        self.assertFalse(self.ok("I understand your frustration. It ships tomorrow.", EN)[0])

    def test_dashes(self):
        for dash in ("\u2014", "\u2013"):
            passed, fb = self.ok(f"Yes {dash} size 43 is in stock.", EN)
            self.assertFalse(passed)
            self.assertIn("dashes", fb)
        self.assertTrue(self.ok("Sizes EU 42-45 and 45-52 are in stock.", EN)[0])

    # prices

    def test_price_must_be_in_the_data(self):
        passed, fb = self.ok("الشراب بـ 349 جنيه.", AR)
        self.assertFalse(passed)
        self.assertIn("349", fb)
        for good in ("الشراب بـ 949 جنيه.", "الشراب بـ ٩٤٩ جنيه.", "الشحن 60 جنيه للقاهرة."):
            self.assertTrue(self.ok(good, AR)[0], good)
        self.assertTrue(self.ok("It is EGP 949, delivery 85 EGP.", EN)[0])
        self.assertTrue(self.ok("Delivery takes 2 to 4 days, exchanges within 14 days.", EN)[0])

    def test_totals_pass_only_from_tool_results(self):
        self.assertFalse(self.ok("Your total is 1,009 EGP.", EN)[0])
        amounts = self.amounts | amounts_in({"ok": True, "order": {"total": "1009.00"}})
        self.assertTrue(self.ok("Your total is 1,009 EGP.", EN, amounts)[0])

    def test_prices_in(self):
        self.assertEqual(prices_in("949 جنيه و 60 جنيه شحن"), [949, 60])
        self.assertEqual(prices_in("EGP 1,009 total, 14 days"), [1009])
        self.assertEqual(prices_in("size 43, 2 pairs"), [])

    def test_empty_reply(self):
        self.assertFalse(self.ok("  ", EN)[0])

    # our own fixed replies pass too

    def test_fixed_replies_pass(self):
        for script, customer in (("arabic", AR), ("franco", FRANCO), ("english", EN)):
            self.assertTrue(self.ok(PHOTO_NOT_OPENED[script], customer)[0], script)
            self.assertTrue(self.ok(TAKING_OVER[script], customer)[0], script)


if __name__ == "__main__":
    unittest.main()
