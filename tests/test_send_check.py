import unittest

from frontdesk.loop import LIMIT_LINE, PHOTO_NOT_OPENED, VOICE_NOT_HEARD
from kit.send_check import amounts_in, check, detect_script, prices_in, stock_counts
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
            self.assertTrue(self.ok(VOICE_NOT_HEARD[script], customer)[0], script)
            self.assertTrue(self.ok(LIMIT_LINE[script], customer)[0], script)

    # Mohamed's calls, 28 Oct

    def test_no_emoji(self):
        passed, fb = self.ok("Yes, size 43 is in stock " + chr(0x1F44D), EN)
        self.assertFalse(passed)
        self.assertIn("emoji", fb)
        self.assertFalse(self.ok("أيوه موجود " + chr(0x2764) + chr(0xFE0F), AR)[0])

    def test_no_markdown_bold(self):
        passed, fb = self.ok("The **black** socks are 949 EGP.", EN)
        self.assertFalse(passed)
        self.assertIn("markdown", fb)

    def test_spaced_hyphen_used_as_a_dash(self):
        passed, fb = self.ok("Yes - size 43 is in stock.", EN)
        self.assertFalse(passed)
        self.assertIn("hyphen", fb)
        self.assertFalse(self.ok("الشراب - 949 جنيه.", AR)[0])
        self.assertTrue(self.ok("Sizes EU 42 - 45 are in stock.", EN)[0])
        self.assertTrue(self.ok("Size M fits EU 42-45.", EN)[0])

    def test_no_exact_stock_count(self):
        for reply, customer in (("Only 3 left in size M.", EN), ("We have 2 pairs left.", EN),
                                ("فاضل 2 بس من المقاس ده.", AR), ("المقاس ده 3 قطع فاضلين.", AR),
                                ("fadel 3 bs mn el size da.", FRANCO), ("stock: 4", EN)):
            passed, fb = self.ok(reply, customer)
            self.assertFalse(passed, reply)
            self.assertIn("stock count", fb)
        for reply, customer in (("Only a few left in size M.", EN), ("فاضل كام قطعة بس من المقاس ده.", AR),
                                ("Size 43 is in stock.", EN), ("مقاس 43 متاح.", AR)):
            self.assertTrue(self.ok(reply, customer)[0], reply)
        self.assertEqual(stock_counts("Only 3 left."), ["3 left"])

    def test_amount_in_words_is_refused(self):
        for reply, customer in (("It is five hundred pounds.", EN), ("الشراب بخمسمية جنيه.", AR)):
            passed, fb = self.ok(reply, customer)
            self.assertFalse(passed, reply)
            self.assertIn("digits", fb)

    # numbers that are not amounts

    def test_sizes_dates_orders_and_units_are_not_amounts(self):
        for reply, customer in (
                ("عندنا مقاس M (EU 42-45) بـ 949 جنيه.", AR),
                ("Size 43 is in stock for 949 EGP.", EN),
                ("Sizes are 42 and 43, at 949 EGP.", EN),
                ("Your order #1234 is on its way.", EN),
                ("Order 5521 is with the courier.", EN),
                ("اوردر 1234 اتسلم لبوسطة من 2 ايام.", AR),
                ("It arrives on 3 November.", EN),
                ("الطلب يوصل يوم 28/10 الساعة 10:30.", AR),
                ("Delivery is 2 to 4 days.", EN),
                ("التوصيل من 2 لـ 4 أيام.", AR),
                ("Exchanges are free for 14 days.", EN),
                ("It comes in 3 colours for 949 EGP.", EN)):
            self.assertTrue(self.ok(reply, customer)[0], reply)

    def test_catalogue_size_strings_are_not_amounts(self):
        reply = "The one for Kids 10 is in stock."
        self.assertEqual(prices_in(reply), [10])
        self.assertEqual(prices_in(reply, sizes=("Kids 10",)), [])
        self.assertTrue(check(reply, EN, self.amounts, self.vocab, sizes=("Kids 10",))[0])
        self.assertTrue(check("Size One size (EU 30-37) for kids is 649 EGP.", EN, self.amounts, self.vocab,
                              sizes=store.size_strings())[0])

    # amounts from tool results

    def test_amounts_only_under_exact_keys(self):
        self.assertEqual(amounts_in({"coffee": 9999, "offer_amount": 7, "amount": 5, "cost": 3, "fees": 2}), set())
        self.assertEqual(amounts_in({"items": [{"price": "949"}, {"fee": 60}], "subtotal": 1009, "total": "1,069.00"}),
                         {949, 60, 1009, 1069})

    def test_strings_are_never_parsed_as_json(self):
        self.assertEqual(amounts_in({"ok": True, "text": '{"price": 5}'}), set())
        self.assertEqual(amounts_in('{"price": 5}'), set())
        self.assertEqual(amounts_in({"price": '{"price": 5}'}), set())

    def test_non_finite_amounts_are_ignored(self):
        for bad in ("inf", "nan", "1e400", float("inf"), float("nan"), 10 ** 400, True, None, [1, 2]):
            self.assertEqual(amounts_in({"price": bad}) - {1, 2}, set(), repr(bad)[:20])
        passed, _ = check("It is 949 EGP.", EN, {949, float("inf"), "x"}, self.vocab)
        self.assertTrue(passed)


# One test per price spelling from the round 1 proofs (reviewer p1_price.py, red team F1).
# The store has no price of 1 or 500, so every one of these must be blocked.
HOODIE_AR = "عندكم هودي اسود؟"
HOODIE_EN = "how much is the black hoodie?"
SPELLINGS = {
    "geneha": ("الهودي الاسود بـ 1 جنيها", HOODIE_AR),
    "geneha_tanween_after_alif": ("الهودي الاسود بـ 1 جنيهاً", HOODIE_AR),
    "geneha_tanween_before_alif": ("الهودي الاسود بـ 1 جنيهًا", HOODIE_AR),
    "price_word_no_currency": ("الهودي الاسود سعره 1 بس", HOODIE_AR),
    "bare_geem": ("الهودي الاسود بـ 1 ج", HOODIE_AR),
    "geem_meem": ("الهودي الاسود بـ 1 جم", HOODIE_AR),
    "eastern_digits": ("الهودي الاسود بـ ١ جنيه", HOODIE_AR),
    "is_bare_number": ("The black hoodie is 1 today.", HOODIE_EN),
    "egyptian_pounds": ("The black hoodie is 1 Egyptian pounds.", HOODIE_EN),
    "pound_sign": ("The black hoodie is £1.", HOODIE_EN),
    "costs_slash_dash": ("The black hoodie costs 1/-", HOODIE_EN),
    "price_colon": ("Price: 1", HOODIE_EN),
    "for_number": ("You can have it for 1 now.", HOODIE_EN),
    "be_bare_number": ("الكوتشي بـ 500 بس", HOODIE_AR),
    "be_eastern_geem": ("ده بـ ٥٠٠ ج", HOODIE_AR),
    "be_attached": ("ده ب500 بس", HOODIE_AR),
    "franco_b": ("el hoodie b 500 bs", "3ndko hoodie eswed?"),
}


class PriceSpellings(unittest.TestCase):
    def setUp(self):
        clean_env(self)
        self.amounts = store.price_points()

    def blocked(self, reply, customer):
        passed, fb = check(reply, customer, self.amounts, store.vocabulary(), store.size_strings())
        self.assertFalse(passed, reply)
        self.assertIn("not in the store or delivery data", fb)


def _spelling_test(reply, customer):
    def test(self):
        self.blocked(reply, customer)
    return test


for _name, (_reply, _customer) in SPELLINGS.items():
    setattr(PriceSpellings, f"test_{_name}", _spelling_test(_reply, _customer))


# Normal replies with sizes, delivery days and order numbers, run through the check the
# way the loop runs it (store amounts, vocabulary and size strings). None names a price.
NORMAL = (
    ("is size 43 available?", "Your size is 43, and it is in stock."),
    ("when will it arrive?", "It arrives in 2 days."),
    ("where is my order?", "Order NLS0001 is on its way."),
    ("3ndko size 43?", "el size 43 mawgood"),
    ("delivery to Alexandria?", "Delivery to Alexandria is 2 to 3 days."),
    ("which sizes?", "The sizes are 42, 43 and 44, all in stock."),
    ("when does it come?", "Your order is out for delivery and arrives at 5 pm."),
    ("can I get 2?", "Yes, your order is 2 pairs in size 43."),
    ("size?", "Size 43 is only in black."),
    ("is 44 there?", "44 is in stock, and delivery to Giza is 2 days."),
    ("ordered on 25 October", "Your order from 25 October is on its way, it arrives in 2 days."),
    ("when?", "Delivery takes 2 to 3 working days."),
    ("do you have 43?", "Black is available in 42 and 43."),
    ("do you have 43?", "The black ones are 42 and 43."),
    ("do you have 43?", "We only have 43 and 44 in black."),
    ("do you have 43?", "For 43 we have the black pair only, in stock."),
    ("do you have 43?", "We have 43 available in black."),
    ("el order emta yewsal?", "el order NLS0001 fel tare2 w hywsal fe 2 ayam"),
    ("el tawseel l Alex kam yom?", "el tawseel l Alex 2 l 3 ayam"),
    ("emta?", "hywsal fe 2 le 3 ayam"),
    ("3ndko 43?", "el eswed mawgood b 42 w 43 bs"),
    ("momken a3raf ma2as 44 mawgood?", "ma2as 44 mawgood, w el order hywsal fe 3 ayam"),
    ("عندكم مقاس ٤٣؟", "مقاس ٤٣ موجود"),
    ("الطلب هيوصل امتى؟", "الأوردر NLS0001 في الطريق وهيوصلك خلال يومين"),
    ("التوصيل للقاهرة بياخد قد ايه؟", "التوصيل للقاهرة من ٢ لـ ٣ أيام"),
    ("طلبي فين؟", "طلبك رقم 1042 اتشحن وهيوصل خلال ٣ أيام"),
    ("عايز اتنين", "تمام، الأوردر بتاعك ٢ قطع مقاس ٤٣"),
    ("عندكم ٤٣؟", "الأسود متاح ب ٤٢ و ٤٣"),
)

# Still blocked: a price word in the sentence keeps a size number an amount, and the
# store's sizes run 30 to 52, so 1, 45 after a price word and 500 are all amounts.
STILL_BLOCKED = (
    ("delivery?", "Delivery to Giza is 45."),
    ("delivery?", "Shipping to Aswan takes longer and is 45."),
    ("delivery?", "It is 45 EGP."),
    ("price?", "It is 1 today."),
    ("el tawseel?", "el tawseel b 45"),
    ("el tawseel?", "el tawseel 45 le"),
    ("التوصيل؟", "التوصيل ب ٤٥"),
    ("بكام؟", "سعره ٤٥"),
)


class NormalReplies(unittest.TestCase):
    def setUp(self):
        clean_env(self)
        self.args = (store.price_points(), store.vocabulary(), store.size_strings())

    def test_normal_replies_are_not_blocked(self):
        for customer, reply in NORMAL:
            with self.subTest(reply=reply):
                self.assertEqual(check(reply, customer, *self.args), (True, ""))

    def test_price_words_still_count_size_numbers(self):
        for customer, reply in STILL_BLOCKED:
            with self.subTest(reply=reply):
                passed, fb = check(reply, customer, *self.args)
                self.assertFalse(passed)
                self.assertIn("not in the store or delivery data", fb)

    def test_a_count_that_is_not_a_size_is_still_refused(self):
        passed, fb = check("Only 3 left in 43.", "do you have 43?", *self.args)
        self.assertFalse(passed)
        self.assertIn("exact stock count", fb)


if __name__ == "__main__":
    unittest.main()
