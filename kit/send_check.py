"""The send check. Every reply passes it before it goes out, whatever the model decided.

check() returns (passed, feedback). The feedback is written for the model, so a
failed reply can be retried with it.

Rules (from the store rules and the 27 Oct morning review):
1. Script match: Arabic in, Arabic out; Franco in, Latin letters out; English in, English out.
2. Banned words: الجوز / el goz, مرتاح, sympathy lines. No em or en dashes.
3. Every price or fee number must exist in the store or delivery data.
"""
import json
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

CURRENCY = r"(?:جنيه|جنية|جنيهات|ج\.م|egp|le|l\.e|pounds?|geneh|genih|gneh|gnih|ginih|gnyh)"
NUMBER = r"(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
PRICE = re.compile(
    rf"{NUMBER}\s*{CURRENCY}(?![\w])|(?<![\w]){CURRENCY}\s*{NUMBER}", re.IGNORECASE)
AMOUNT_KEYS = re.compile(r"price|fee|total|amount|subtotal|cost", re.IGNORECASE)


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


def prices_in(reply):
    """Every number written next to a currency word, as ints."""
    text = reply.translate(EASTERN_DIGITS)
    out = []
    for m in PRICE.finditer(text):
        raw = m.group(1) or m.group(2)
        out.append(_number(raw))
    return out


def amounts_in(obj):
    """Numbers under price, fee or total keys anywhere in a tool result."""
    found = set()
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except ValueError:
            return found
    if isinstance(obj, dict):
        for k, v in obj.items():
            if AMOUNT_KEYS.search(str(k)):
                for x in (v if isinstance(v, list) else [v]):
                    if isinstance(x, (int, float, str)) and not isinstance(x, bool):
                        try:
                            found.add(int(float(str(x).replace(",", ""))))
                        except ValueError:
                            pass
                if isinstance(v, (dict, list)):
                    found |= amounts_in(v)
            else:
                found |= amounts_in(v)
    elif isinstance(obj, list):
        for v in obj:
            found |= amounts_in(v)
    return found


def check(reply, customer_text="", amounts=(), vocabulary=frozenset()):
    """(passed, feedback) for one reply.

    amounts: every price and fee in the store and delivery data, plus amounts from this turn's tool results.
    vocabulary: lowercase words from product names and sizes, allowed in Latin letters inside an Arabic reply.
    """
    problems = []
    if not (reply or "").strip():
        return False, "The reply was empty. Write a reply to the customer."
    fb = _script_feedback(reply, customer_text, vocabulary)
    if fb:
        problems.append(fb)
    low = reply.lower()
    for pattern, fix in BANNED:
        if pattern.search(low):
            problems.append(fix)
    for line in SYMPATHY:
        if line in low:
            problems.append(f'Remove the sympathy line ("{line}"). No sympathy lines; just answer.')
            break
    if DASHES.search(reply):
        problems.append("Never use em dashes or en dashes. Use a comma or a full stop.")
    allowed = {int(a) for a in amounts}
    wrong = sorted({p for p in prices_in(reply) if p not in allowed})
    if wrong:
        nums = ", ".join(str(p) for p in wrong)
        problems.append(f"These amounts are not in the store or delivery data: {nums}. "
                        "Use only prices and fees from tool results, or say you will check with the team.")
    if problems:
        return False, " ".join(dict.fromkeys(problems))
    return True, ""
