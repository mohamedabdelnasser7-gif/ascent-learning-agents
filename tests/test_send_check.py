import unittest
from decimal import Decimal
from unittest import mock

from frontdesk.loop import LIMIT_LINE, MODEL_DOWN, PHOTO_NOT_OPENED, VOICE_NOT_HEARD
from kit.send_check import amounts_in, check, detect_script, prices_in, size_number_amounts, stock_counts
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
            self.assertTrue(self.ok(MODEL_DOWN[script], customer)[0], script)

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
    # No price word before the number, and only 1 digit, so only the currency list can catch these.
    "no_cue_geneha": ("الهودي الاسود 1 جنيها", HOODIE_AR),
    "no_cue_geneha_tanween_after_alif": ("الهودي الاسود 1 جنيهاً", HOODIE_AR),
    "no_cue_geneha_tanween_before_alif": ("الهودي الاسود 1 جنيهًا", HOODIE_AR),
    "no_cue_bare_geem": ("الهودي الاسود ١ ج", HOODIE_AR),
    "no_cue_geem_meem": ("الهودي الاسود 1 جم", HOODIE_AR),
    "no_cue_egyptian_pounds": ("Black hoodie: 1 Egyptian pounds.", HOODIE_EN),
    "no_cue_pound_sign": ("Black hoodie: £1.", HOODIE_EN),
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
    # A bare number of 3 or more digits is an amount (28 Oct), but order numbers with #,
    # Egyptian phone numbers, years with a year word or month, times, percentages and
    # product codes still pass.
    ("where is my order?", "Your order #10234 is with the courier."),
    ("الاوردر فين؟", "الأوردر #1042 اتشحن وهيوصل بكرة"),
    ("el order fen?", "el order #1042 etsha7an w hywsal bokra"),
    ("what is your number?", "Call us on 01012345678."),
    ("what is your number?", "Our WhatsApp is +20 10 1234 5678."),
    ("what is your number?", "Write to 00201012345678 or +201012345678."),
    ("رقمكم كام؟", "كلمنا على ٠١٠١٢٣٤٥٦٧٨"),
    ("momken rakamko? 3ayez akalemko", "kalemna 3la 010 1234 5678"),
    ("since when?", "We have sold these socks since May 2019."),
    ("is it new?", "This is the 2026 model."),
    ("موديل سنة كام؟", "ده موديل 2026"),
    ("اتعمل امتى؟", "ده اتعمل سنة ٢٠٢٤"),
    ("el model bta3 sana kam?", "el model 2026 mawgood"),
    ("when will it come?", "It arrives on Oct 30, 2026."),
    ("when do you answer?", "We answer from 10:00 to 22:00."),
    ("بتردوا امتى؟", "بنرد من الساعة ٩:٣٠ لحد ١٠:٠٠ بالليل"),
    ("what are they made of?", "They are 100% cotton."),
    ("which one?", "Code BO-2936845 is in stock."),
    ("how much?", "The black socks, 949."),
    ("how much?", "They are 949.00 EGP."),
)

# Mohamed's call, 28 Oct: a standalone number of 3 or more digits is an amount, with or
# without a currency or price word, and a unit after it does not change that.
BARE_BLOCKED = (
    ("how much?", "Hoodie: 500."),
    ("بكام؟", "الهودي ٥٠٠ بس"),
    ("el hoodie b kam ya basha?", "el hoodie 500 bas"),
    ("how much?", "The black socks, 500."),
    ("how much?", "Black socks 500 only today"),
    ("el socks b kam ya basha?", "el socks 500 bs ya basha"),
    ("بكام؟", "الكوتشي 500 بس"),
    ("بكام؟", "ده ٥٠٠ بس"),
    ("delivery?", "Shipping 500 le 2 days."),
    ("el tawseel b kam ya basha?", "el tawseel 500 le 3 ayam"),
    ("how long?", "You pay for 500 working days."),
    ("how long?", "It is 700 business days."),
    ("how much?", "Hoodie: 1,500."),
    ("how much?", "Socks 649-700."),
    ("size 43?", "Size 43, 500."),
    ("how much?", "It is 2000."),
    # A 4 digit number from 1900 to 2099 is a year only with a year word or a month next
    # to it, or inside a date (Mohamed, 28 Oct). Anything else is an amount.
    ("how much?", "Hoodie: 1999."),
    ("since when?", "We have sold these socks since 2019."),
    ("is it new?", "Yes, the 2026 collection is in stock."),
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

    def test_bare_numbers_of_three_digits_are_amounts(self):
        for customer, reply in BARE_BLOCKED:
            with self.subTest(reply=reply):
                passed, fb = check(reply, customer, *self.args)
                self.assertFalse(passed)
                self.assertIn("not in the store or delivery data", fb)
        self.assertEqual(len(NORMAL), 48)  # the 28 from round 2, plus 20 for the bare number and year rules


class NumbersAsWritten(unittest.TestCase):
    def setUp(self):
        clean_env(self)

    def test_emphasis_marks_and_invisible_characters_do_not_hide_an_amount(self):
        # Red team round 3: WhatsApp shows _500_ as an italic 500, and a zero width space splits 500.
        for customer, reply in (
                (EN, "The price is _500_ EGP."), (EN, "The black socks, ~500~."), (EN, "It is `500` EGP."),
                ("بكام الشراب؟", "الكوتشي _500_ جنيه"), ("bkam el socks?", "el sha7n _70_ gneh"),
                (EN, "Black socks 5\u200b00."), ("بكام الكوتشي؟", "الكوتشي ٥\u200d٠٠")):
            with self.subTest(reply=reply):
                passed, fb = check(reply, customer, store.price_points())
                self.assertFalse(passed)
                self.assertIn("not in the store or delivery data", fb)
        self.assertTrue(check("The black socks are _649 EGP_.", EN, store.price_points())[0])
        self.assertFalse(check("el socks b meteen geneh", "bkam el socks?", store.price_points())[0])

    def test_decimals_count_as_written(self):
        self.assertEqual(prices_in("It is 649.99 EGP."), [Decimal("649.99")])
        self.assertEqual(prices_in("الشراب ٦٤٩٫٩٩ جنيه"), [Decimal("649.99")])
        self.assertFalse(check("It is 649.99 EGP.", EN, {649, 949})[0])
        self.assertFalse(check("The price is 649.99", EN, {649})[0])
        self.assertTrue(check("It is 949.00 EGP.", EN, {949})[0])
        self.assertTrue(check("It is 949.50 EGP.", EN, {"949.50"})[0])
        self.assertFalse(check("It is 949 EGP.", EN, {"949.50"})[0])
        self.assertEqual(amounts_in({"price": "949.50", "fee": 60.0}), {Decimal("949.5"), 60})

    def test_store_prices_keep_their_fractions(self):
        items = [{"title": "T", "variants": [{"size": "M", "price": "949.50"}, {"size": "L", "price": "949.00"}]}]
        with mock.patch.object(store, "catalogue", return_value=items):
            points = store.price_points()
        self.assertEqual(points, {Decimal("949.5"), 949, 60, 85})  # 60 and 85: the demo delivery fees

    def test_amounts_with_k_are_refused(self):
        for reply in ("It costs 1k EGP.", "Only 1.5k.", "EGP 2k", "The price is 1k"):
            with self.subTest(reply=reply):
                passed, fb = check(reply, EN, store.price_points())
                self.assertFalse(passed)
                self.assertIn("digits", fb)
        self.assertTrue(check("We have 1k followers on Instagram.", EN, store.price_points())[0])

    def test_a_size_string_never_hides_part_of_a_bigger_number(self):
        self.assertEqual(prices_in("The hoodie is 430.", sizes=("43",)), [430])
        self.assertEqual(prices_in("Hoodie: 1430.", sizes=("43",)), [1430])
        self.assertEqual(prices_in("Size 43 is in stock.", sizes=("43",)), [])

    def test_a_size_list_goes_on_only_with_small_numbers(self):
        self.assertEqual(prices_in("Sizes 42, 43 and 44 are in stock."), [])
        self.assertEqual(prices_in("Size 43, 500."), [500])

    def test_phones_codes_and_years_with_a_year_word_are_not_amounts(self):
        for reply in ("01012345678", "+20 10 1234 5678", "00201012345678", "201012345678", "+20 2 2345 6789",
                      "BO-2936845", "NLS0001", "100%", "١٠٠٪", "model 2019", "the 2019 model", "Model: 2026",
                      "سنة ٢٠١٩", "موديل 2026", "el model 2026", "May 2019", "30 Oct 2026", "Oct 30, 2026",
                      "٢٨ أكتوبر ٢٠٢٦", "28/10/2026", "2026-10-28", "2026/10/28"):
            with self.subTest(reply=reply):
                self.assertEqual(prices_in(reply), [])
        for reply, n in (("01012345", 1012345), ("1999.5", Decimal("1999.5")), ("2,019", 2019), ("2100", 2100),
                         ("since 2019", 2019), ("Hoodie: 1999.", 1999), ("the 2026 collection", 2026),
                         ("1999 years", 1999), ("Price is 1999, model 2026.", 1999)):
            with self.subTest(reply=reply):
                self.assertEqual(prices_in(reply), [n])

    def test_size_numbers_that_are_also_prices(self):
        self.assertEqual(size_number_amounts({45, 949, "60"}, ("M (EU 42-45)", "Kids 60")), [45, 60])
        self.assertEqual(size_number_amounts(store.price_points(), store.size_strings()), [])


if __name__ == "__main__":
    unittest.main()
