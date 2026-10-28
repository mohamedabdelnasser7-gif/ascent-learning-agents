"""The send check. Every reply passes it before it goes out, whatever the model decided.

check() returns (passed, feedback). The feedback is written for the model, so a
failed reply can be retried with it.

Rules (from the store rules, the 27 Oct morning review and Mohamed's calls on 28 Oct):
1. Script match: Arabic in, Arabic out; Franco in, Latin letters out; English in, English out.
2. Banned words: الجوز / el goz, مرتاح, sympathy lines.
3. Style: no em or en dashes, no hyphen with spaces used as a dash, no emoji, no markdown **.
4. Every number that reads as an amount must exist in the store or delivery data. A number
   reads as an amount when it sits next to a currency word or sign, or comes after a price
   word (سعر, بـ, price, costs, for, is and a few more). Sizes, size ranges, dates, order
   numbers and numbers with a unit (days, pairs, MB) are left out. After a weak word (is,
   for, ب), a catalogue size number is a size unless a price word comes earlier in the
   sentence. An amount written in words next to a currency word is refused, so the model
   writes it in digits.
5. No exact stock count. The store tool gives a level (in stock, few left, out), never a number.
"""
import math
import re

# Arabic letters only: Arabic punctuation and digits do not count as a script.
ARABIC = re.compile(r"[\u0621-\u064A\u0671-\u06D3\u06FA-\u06FC\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFC]")
LATIN = re.compile(r"[A-Za-z]")
WORD = re.compile(r"[\w\u0600-\u06ff]+", re.UNICODE)
EASTERN_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

# Franco: Egyptian Arabic in Latin letters, with 2 3 5 7 8 9 standing for Arabic sounds.
FRANCO_DIGIT_WORD = re.compile(r"\b(?=[a-z0-9]*[a-z])(?=[a-z0-9]*[235789])[a-z0-9]{2,}\b")
ORDINAL = re.compile(r"^\d+(st|nd|rd|th)$")
FRANCO_WORDS = {
    "ana", "enta", "enti", "ahlan", "momken", "bs", "bas", "keda", "kda", "ezay", "ezzay", "fe", "fi",
    "msh", "mesh", "mish", "ya", "el", "w", "wala", "aywa", "la2", "lw", "leh",
    "tamam", "khalas", "awi", "awy", "ba2a", "kman", "kaman", "delwa2ty", "dlw2ty", "bta3", "beta3",
    "3ayez", "3ayz", "3andena", "3ndena", "fein", "fen", "emta", "amta", "eih", "agib", "yom",
}

BANNED = [
    (re.compile(r"(?:ال|لل|بال)جوز"), "Never write الجوز. Say the price plainly, the number then جنيه."),
    (re.compile(r"\b(?:el|l|ll|lel|bel)\s?goz\b"), "Never write el goz. Say the price plainly."),
    (re.compile(r"مرتاح"), "Never use مرتاح or filler like it."),
]
SYMPATHY = [
    "مقدر إن", "مقدر ان", "متضايق", "حقك تزعل", "حقك تضايق", "أنا آسف إنك", "انا اسف انك",
    "i understand your frustration", "i understand how you feel", "sorry to hear",
    "sorry for the inconvenience", "i can imagine how", "m2adar", "mtday2", "7a2ak tez3al",
]
DASHES = re.compile("[\u2012\u2013\u2014\u2015]")


def _ranges(*pairs):
    return "".join(f"{chr(a)}-{chr(b)}" for a, b in pairs)


# Emoji and pictographs, built from code points so the source stays plain.
EMOJI = re.compile("[" + _ranges((0x1F000, 0x1FAFF), (0x2600, 0x27BF), (0x2B00, 0x2BFF), (0x2300, 0x23FF),
                                 (0xE0020, 0xE007F)) + chr(0xFE0F) + chr(0x3030) + chr(0x303D) + "]")
MARKDOWN_BOLD = re.compile(r"\*\*")
# A hyphen with spaces on both sides. Between two digits it is a range (42 - 45), not a dash.
SPACED_HYPHEN = re.compile(r"(?<=(\S))[ \t]+-[ \t]+(?=(\S))")

LETTER_BEFORE = r"(?<![^\W\d_])"
NO_LETTER_AFTER = r"(?![^\W\d_])"
MASK = chr(1)  # stands in for a number that is not an amount (a size, a date, an order number)

CURRENCY = (r"(?:جنيهات|جنيهاً|جنيهًا|جنيها|جنيه|جنية|ج\.م|جم|ج|egp|l\.e|le|egyptian\s+pounds?|pounds?"
            r"|geneh|genih|gneh|gnih|ginih|gnyh|£)")
NUMBER = r"(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
PRICE = re.compile(
    rf"{NUMBER}\s*{CURRENCY}(?![\w])|(?<![\w]){CURRENCY}{NO_LETTER_AFTER}\s*{NUMBER}", re.IGNORECASE)

# Words after which a number reads as an amount. PRICE_WORDS always name money. WEAK_WORDS
# (is, for, ب) also come before sizes, so after one of them a catalogue size number (43 from
# "EU 42-45") is read as a size, unless a price word comes earlier in the same sentence.
PRICE_WORDS = (
    r"(?:و|ف)?(?:بال|ال|ب)?سعر(?:ه|ها|هم|و)?", r"(?:و|ف)?(?:بال|ال|ب)?تمن(?:ه|ها|هم|و)?",
    r"(?:و|ف)?(?:ال)?(?:شحن|توصيل|مصاريف|رسوم|اجمالي|إجمالي|مجموع|حساب)",
    r"(?:el\s?|b)?se3r\w*", r"(?:el\s?)?taman\w*", r"(?:el\s?)?(?:sha7n|tawsil|tawseel)",
    r"prices?", r"priced", r"pricing", r"costs?", r"costing",
    r"total", r"subtotal", r"fees?", r"shipping", r"delivery", r"pay", r"charged?", r"charges",
)
WEAK_WORDS = (r"بـ", r"ب", r"b", r"bi", r"be", r"for", r"is", r"are", r"at", r"only", r"just")
CUES = PRICE_WORDS + WEAK_WORDS
WEAK_CUE = re.compile("(?:" + "|".join(WEAK_WORDS) + ")", re.IGNORECASE)
PRICE_WORD = re.compile(LETTER_BEFORE + "(?:" + "|".join(PRICE_WORDS) + ")" + NO_LETTER_AFTER, re.IGNORECASE)
SENTENCE_END = re.compile(r"[.!?؟\n](?!\d)")
FILLER_WORD = r"[^\s\d" + MASK + r".!?؟،,;]+"
CUE_AMOUNT = re.compile(
    LETTER_BEFORE + "(?P<cue>" + "|".join(CUES) + ")" + NO_LETTER_AFTER
    + r"(?:\s+" + FILLER_WORD + r"){0,3}?\s*[:=]?\s*(?<![\d.,])(?P<n>\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?![\w%])",
    re.IGNORECASE)

SEP = r"(?:-|to|and|or|/|,|،|و|او|أو|لـ|ل|l|w|aw|le)"
UNITS = (r"(?:days?|yom|youm|ayam|ayyam|أيام|ايام|يومين|يوم|hours?|hrs?|ساعات|ساعة|ساعه|sa3a|sa3at|weeks?"
         r"|اسبوع|أسبوع|اسابيع|أسابيع|months?|شهور|شهر|minutes?|mins?|دقيقة|دقيقه|دقايق|pairs?|pieces?|pcs"
         r"|items?|قطعة|قطعه|قطع|7etta|7etat|colou?rs?|ألوان|الوان|لون|sizes?|مقاسات|cm|mm|سم|mb|ميجا|mega"
         r"|kg|كيلو|am|pm|times|مرات|مرة|مره|years?|سنين|سنة|سنه)")
UNIT_AFTER = re.compile(rf"\s*(?:{SEP}\s*\d+\s*)?(?:(?:working|business)\s+)?{UNITS}{NO_LETTER_AFTER}", re.IGNORECASE)
# Franco "2 le 3 ayam" (2 to 3 days): this "le" means "to", not the currency LE.
LE_RANGE = re.compile(rf"(?<=\d)(\s*)le(?=\s*\d+\s*(?:(?:working|business)\s+)?{UNITS}{NO_LETTER_AFTER})",
                      re.IGNORECASE)

# Numbers that are not amounts: sizes, dates and order numbers.
SIZE_WORDS = r"(?:sizes?|مقاسات|مقاس(?:ه|ها|ك)?|المقاس|بمقاس|ma2as(?:at)?|eu)"
SIZE_CTX = re.compile(
    rf"{LETTER_BEFORE}{SIZE_WORDS}{NO_LETTER_AFTER}(?:\s*(?:is|are|:|=))?\s*\d+(?:\s*{SEP}\s*\d+)*", re.IGNORECASE)
MONTHS = (r"(?:jan(?:uary)?|feb(?:ruary)?|march|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?"
          r"|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|يناير|فبراير|مارس|ابريل|أبريل|إبريل|مايو|يونيو|يونيه"
          r"|يوليو|يوليه|اغسطس|أغسطس|سبتمبر|اكتوبر|أكتوبر|نوفمبر|ديسمبر)")
DATES = re.compile(
    r"(?<!\d)\d{1,2}/\d{1,2}(?:/\d{2,4})?(?!\d)|(?<!\d)\d{4}-\d{1,2}-\d{1,2}(?!\d)|(?<!\d)\d{1,2}:\d{2}(?!\d)"
    rf"|(?<![\d.,])\d{{1,2}}(?:st|nd|rd|th)?(?:\s+of)?\s+{MONTHS}{NO_LETTER_AFTER}"
    rf"|{LETTER_BEFORE}{MONTHS}\s+\d{{1,2}}(?!\d)", re.IGNORECASE)
ORDER_NUMBER = re.compile(
    r"#\s*\d+|" + LETTER_BEFORE
    + r"(?:orders?|اوردر|أوردر|الاوردر|الأوردر|اوردرك|أوردرك|طلب|طلبك|الطلب|رقم|ordr)" + NO_LETTER_AFTER
    + r"(?:\s*(?:number|no\.?|num|#|رقم|:))*\s*\d+", re.IGNORECASE)

# An amount written in words next to a currency word cannot be checked, so it is refused.
NUMBER_WORDS = (r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty"
                r"|fifty|sixty|seventy|eighty|ninety|hundred|thousand|alf|meya|mya|mit|واحد|اتنين|اثنين|تلاتة|ثلاثة"
                r"|اربعة|أربعة|خمسة|ستة|سبعة|تمانية|ثمانية|تسعة|عشرة|عشرين|تلاتين|ثلاثين|اربعين|أربعين|خمسين"
                r"|ستين|سبعين|تمانين|ثمانين|تسعين|مية|ميه|مائة|ميت|ميتين|الف|ألف|آلاف|الاف|تلاف)")
SPELLED_AMOUNT = re.compile(rf"{NUMBER_WORDS}\s*{CURRENCY}(?![\w])", re.IGNORECASE)

# An exact stock count, in English, Arabic or Franco.
STOCK_COUNT = re.compile(
    r"\d+\s+(?:(?:pairs?|pieces?|items?|units?|more)\s+)?(?:left|remaining|in\s+stock|available)" + NO_LETTER_AFTER
    + r"|" + LETTER_BEFORE + r"stock\s*(?:of|is|:|=)?\s*\d+"
    + r"|(?:فاضل|فاضلين|فاضله|فاضلة|باقي|باقى|باقيين|متبقي|متبقى|المتبقي|المتاح|المخزون|الكمية|الكميه|ستوك"
      r"|fadel|fadl|fadlin|fadleen|ba2y|ba2i|ba2yin|motba2y)\s*(?:منه|منها|منهم|بس|meno|menha|menhom|bs)?\s*\d+"
    + r"|\d+\s*(?:(?:قطعة|قطعه|قطع|حتة|حتت|بس|7etta|7etat|bs)\s*)?"
      r"(?:فاضلين|فاضلة|فاضله|فاضل|باقيين|باقي|متبقيين|متبقي|في المخزن|في الستوك|متاحين|موجودين"
      r"|fadlin|fadleen|fadel|fadl|ba2yin|ba2y|mawgodin|mwgodin|fel stock)",
    re.IGNORECASE)

AMOUNT_KEYS = frozenset({"price", "fee", "total", "subtotal"})
AMOUNT_TEXT = re.compile(r"\s*\d[\d,]*(?:\.\d+)?\s*")


def detect_script(text):
    """'arabic', 'franco', 'english', or None when there are no letters."""
    text = text or ""
    ar = len(ARABIC.findall(text))
    la = len(LATIN.findall(text))
    if ar == 0 and la == 0:
        return None
    if ar >= la:
        return "arabic"
    return "franco" if looks_franco(text) else "english"


def looks_franco(text):
    words = [w for w in WORD.findall(text.lower()) if not ARABIC.search(w)]
    digit_words = [w for w in words if FRANCO_DIGIT_WORD.fullmatch(w) and not ORDINAL.match(w)]
    common = [w for w in words if w in FRANCO_WORDS]
    return len(digit_words) >= 1 or len(common) >= 2


def _script_feedback(reply, customer_text, vocabulary):
    want = detect_script(customer_text)
    if want is None:
        return None
    if want == "arabic":
        words = WORD.findall(reply)
        ar = sum(1 for w in words if ARABIC.search(w))
        latin = sum(1 for w in words if LATIN.search(w) and not ARABIC.search(w)
                    and w.lower() not in vocabulary and not re.fullmatch(r"[A-Z]{2,}\d+", w))
        if ar == 0 or latin > ar / 4:
            return "The customer wrote in Arabic letters. Reply in Egyptian Arabic, in Arabic letters."
    elif want == "franco":
        if ARABIC.search(reply):
            return "The customer wrote in Franco. Reply in Franco, in Latin letters and numbers, no Arabic letters."
    elif want == "english":
        if ARABIC.search(reply):
            return "The customer wrote in English. Reply in English."
        if looks_franco(reply):
            return "The customer wrote in English. Reply in plain English, not Franco."
    return None


def _number(raw):
    return int(raw.replace(",", ""))


def _size_strings(sizes):
    """The catalogue's size strings, and the part in brackets of each ("EU 42-45" from "M (EU 42-45)")."""
    out = set()
    for s in sizes or ():
        s = str(s).translate(EASTERN_DIGITS).strip()
        if any(ch.isdigit() for ch in s):
            out.add(s)
            out |= {inner.strip() for inner in re.findall(r"\(([^()]*\d[^()]*)\)", s)}
    return sorted(out, key=len, reverse=True)


def _size_numbers(sizes):
    """Whole numbers the catalogue uses as sizes: 42 to 45 from "EU 42-45", 43 from "43".

    Measures like "12x8 cm" are left out.
    """
    out = set()
    for s in _size_strings(sizes):
        for a, b in re.findall(r"(?<![\dx.])(\d{1,3})\s*-\s*(\d{1,3})(?![\dx.])", s, re.IGNORECASE):
            lo, hi = int(a), int(b)
            if lo <= hi <= lo + 40:
                out.update(range(lo, hi + 1))
        for n in re.findall(r"(?<![\dx.-])(\d{1,3})(?![\dx.-])(?!\s*(?:cm|mm|سم))", s, re.IGNORECASE):
            out.add(int(n))
    return out


def _weak_size(text, m, size_numbers):
    """True when a cue match is a weak word, then a catalogue size number, with no price word
    earlier in the same sentence ("Black is available in 42 and 43")."""
    if _number(m.group("n")) not in size_numbers or not WEAK_CUE.fullmatch(m.group("cue")):
        return False
    start = max((e.end() for e in SENTENCE_END.finditer(text, 0, m.start())), default=0)
    return PRICE_WORD.search(text, start, m.start("n")) is None


def _mask(text, sizes=()):
    """The text with sizes, dates and order numbers replaced by MASK, so no rule reads them as amounts."""
    for s in _size_strings(sizes):
        text = re.sub(re.escape(s), MASK, text, flags=re.IGNORECASE)
    for pattern in (SIZE_CTX, DATES, ORDER_NUMBER):
        text = pattern.sub(MASK, text)
    return text


def prices_in(reply, sizes=()):
    """Every number in the reply that reads as an amount, as ints.

    A number next to a currency word or sign always counts. A number after a price word
    counts unless it is a size, a date, an order number or has a unit after it (2 days).
    sizes: the catalogue's size strings, so "M (EU 42-45)" is never read as an amount, and
    a size number after a weak word ("is available in 43") is read as a size.
    """
    text = LE_RANGE.sub(r"\1to", (reply or "").translate(EASTERN_DIGITS))
    out = [_number(m.group(1) or m.group(2)) for m in PRICE.finditer(text)]
    masked = _mask(PRICE.sub(MASK, text), sizes)
    size_numbers = _size_numbers(sizes)
    for m in CUE_AMOUNT.finditer(masked):
        if UNIT_AFTER.match(masked, m.end()) or _weak_size(masked, m, size_numbers):
            continue
        out.append(_number(m.group("n")))
    return out


def stock_counts(reply, sizes=()):
    """Exact stock counts stated in the reply ("3 left", "فاضل 2", "fadel 3").

    A catalogue size number is a size, not a count ("43 available").
    """
    masked = _mask((reply or "").translate(EASTERN_DIGITS), sizes)
    size_numbers = _size_numbers(sizes)
    return [m.group(0) for m in STOCK_COUNT.finditer(masked)
            if not all(int(n) in size_numbers for n in re.findall(r"\d+", m.group(0)))]


def _amount(x):
    """x as an int amount, or None for anything that is not a plain finite number."""
    if isinstance(x, bool):
        return None
    if isinstance(x, str):
        if not AMOUNT_TEXT.fullmatch(x):
            return None
        x = x.replace(",", "")
    elif not isinstance(x, (int, float)):
        return None
    try:
        f = float(x)
        if not math.isfinite(f):
            return None
        return int(f)
    except (ValueError, OverflowError):
        return None


def amounts_in(obj):
    """Numbers under the exact keys price, fee, total or subtotal anywhere in a tool result.

    Strings are never parsed as JSON, so free text inside a result adds nothing.
    """
    found = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in AMOUNT_KEYS:
                for x in (v if isinstance(v, list) else [v]):
                    n = _amount(x)
                    if n is not None:
                        found.add(n)
            if isinstance(v, (dict, list)):
                found |= amounts_in(v)
    elif isinstance(obj, list):
        for v in obj:
            found |= amounts_in(v)
    return found


def style_problems(text):
    """Feedback for the house style: banned words, dashes, spaced hyphens, emoji, markdown bold."""
    problems = []
    low = (text or "").lower()
    for pattern, fix in BANNED:
        if pattern.search(low):
            problems.append(fix)
    if DASHES.search(text or ""):
        problems.append("Never use em dashes or en dashes. Use a comma or a full stop.")
    if any(not (a.isdigit() and b.isdigit()) for a, b in
           ((m.group(1), m.group(2)) for m in SPACED_HYPHEN.finditer(text or ""))):
        problems.append("Never use a hyphen with spaces as a dash. Use a comma or a full stop.")
    if EMOJI.search(text or ""):
        problems.append("No emoji.")
    if MARKDOWN_BOLD.search(text or ""):
        problems.append("No markdown. WhatsApp shows the ** as they are.")
    return problems


def check(reply, customer_text="", amounts=(), vocabulary=frozenset(), sizes=()):
    """(passed, feedback) for one reply.

    amounts: every price and fee in the store and delivery data, plus amounts from this turn's store tools.
    vocabulary: lowercase words from product names and sizes, allowed in Latin letters inside an Arabic reply.
    sizes: the catalogue's size strings, so a size is never read as an amount.
    """
    problems = []
    if not (reply or "").strip():
        return False, "The reply was empty. Write a reply to the customer."
    fb = _script_feedback(reply, customer_text, vocabulary)
    if fb:
        problems.append(fb)
    problems += style_problems(reply)
    low = reply.lower()
    for line in SYMPATHY:
        if line in low:
            problems.append(f'Remove the sympathy line ("{line}"). No sympathy lines; just answer.')
            break
    allowed = {n for n in (_amount(a) for a in amounts) if n is not None}
    wrong = sorted({p for p in prices_in(reply, sizes) if p not in allowed})
    if wrong:
        nums = ", ".join(str(p) for p in wrong)
        problems.append(f"These amounts are not in the store or delivery data: {nums}. "
                        "Use only prices and fees from tool results, or say you will check with the team. "
                        "If a number is not a price, write its unit with it (size 43, 2 days).")
    if SPELLED_AMOUNT.search(reply.translate(EASTERN_DIGITS)):
        problems.append("Write every amount in digits, the number then جنيه, so it can be checked.")
    if stock_counts(reply, sizes):
        problems.append("Never give the exact stock count. If the store says few left, say only a few are left; "
                        "otherwise say it is in stock or out of stock.")
    if problems:
        return False, " ".join(dict.fromkeys(problems))
    return True, ""
