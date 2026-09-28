# -*- coding: utf-8 -*-
"""Hebrew trading vocabulary used by the signal parser."""
import re

# Canonical instrument -> match terms. Order matters: first hit wins.
SYMBOLS = [
    ("XAUUSD", ["זהב", "xauusd", "gold", "גולד"]),
    ("XAGUSD", ["סילבר", "xagusd", "silver", "כסף"]),   # 'כסף' is ambiguous - see is_silver_metal()
    ("US100",  ["נסדק", "נאסדק", "נאסד", "נסדר", "nas100", "us100", "us 100",
                "ustec", "nq"]),
    ("JP225",  ["יפני", "ניקיי", "jp225", "nikkei"]),
    ("BTCUSD", ["ביטקוין", "בטקוין", "ביטקיון", "btcusd", "bitcoin", "btc"]),
    ("US2000", ["ראסל", "russell", "rus2000"]),
    ("US500",  ["אסאנפי", "s&p", "ספי", "us500"]),
    ("XTIUSD", ["נפט", "oil", "wti"]),
    ("TSLA",   ["טסלה", "tsla"]),

    # A wider universe: indices, single stocks and crypto. Latin tickers here are matched on word
    # boundaries (see ASCII_BOUNDED): "arm", "snow", "sol" and "meta" are ordinary
    # English substrings and would otherwise fire inside unrelated words.
    ("META",   ["meta", "meat", "מטא"]),
    ("ETHUSD", ["איתריום", "אתריום", "איתרים", "ethereum", "etherum",
                "ethusd", "eth"]),
    ("NVDA",   ["nvda", "אנבידיה"]),
    ("PLTR",   ["pltr", "פלנטיר"]),
    ("US30",   ["דאו", "dow", "us30"]),
    ("GER40",  ["דקס", "דאקס", "גרמני", "dax", "ger40", "ger 40"]),
    ("MSTR",   ["mstr", "mstu"]),
    ("GOOG",   ["גוגל", "goog", "googl"]),
    ("MSFT",   ["msft", "מיקרוסופט"]),
    ("AAPL",   ["aapl", "appl", "אפל"]),          # 'אפל' also means 'dark' - see is_apple()
    ("NFLX",   ["nflx", "נטפליקס"]),
    ("ADAUSD", ["קרדנו", "cardano", "adausd", "ada"]),
    ("AVGO",   ["avgo", "ברודקום"]),
    ("QCOM",   ["qcom"]),
    ("INTC",   ["intc", "אינטל"]),
    ("SOLUSD", ["סולנה", "solana", "solusd", "sol"]),
    ("AMZN",   ["amzn", "אמזון"]),
    ("SNOW",   ["snow"]),
    ("ARM",    ["arm"]),
    ("ORCL",   ["orcl", "אורקל"]),
    ("XRPUSD", ["ריפל", "xrpusd", "xrp"]),
    ("ASML",   ["asml"]),
]

# Latin terms that must match as whole words. The original eight are matched as bare
# substrings ("btc" has to find "btcusd"), which is safe for those tickers but not for
# these: unbounded "arm"/"sol"/"eth"/"meta"/"ada" fire inside ordinary words.
ASCII_BOUNDED = {
    "meta", "eth", "nvda", "pltr", "dow", "us30", "dax", "ger40", "ger 40", "mstr",
    "goog", "googl", "msft", "aapl", "nflx", "cardano", "ada", "avgo", "qcom",
    "intc", "solana", "sol", "amzn", "snow", "arm", "orcl", "xrp", "asml",
    "mstu", "ethusd", "adausd", "solusd", "xrpusd",
    # Spelling variants seen in the wild. "us 100" is bounded so it cannot
    # fire inside "plus 100"; "meat"/"appl" are bounded so they cannot fire inside
    # ordinary words. "ethereum"/"etherum" are needed because bounded "eth" will
    # not match inside them.
    "ethereum", "etherum", "meat", "appl", "us 100",
}

# Short Hebrew tickers that are substrings of ordinary words and must therefore match
# only as whole words. "ספי" for S&P fired inside מספיק ("enough") and נוספים
# ("additional") repeatedly, and never once on an actual S&P mention.
HEB_BOUNDED = {"ספי", "דאו"}
_HEB = "֐-׿"

# CONFIGURE ME - CONVICTION TIERS.
#
# Some channels grade conviction with a word that is ALSO an instrument name. Where
# that happens the tier sense has to be excluded, or a message about one instrument
# opens a phantom position in another.
#
# Leave both lists empty unless your channel does this. Empty means every mention of
# an instrument word is read as the instrument, which is the safe default. If your
# channel does grade, list the phrases that mark the tier sense here, and put the risk
# each tier implies in parse_signals.TIER_RISK.
TIER_CONTEXT = []
GOLD_TIER_CONTEXT = []

# {tier word: risk percent it implies}. Empty disables every tier rule in the
# parser. Example shape:
#   TIER_RISK = {"<low tier word>": <risk %>, "<high tier word>": <risk %>}
TIER_RISK = {}
MONEY_CONTEXT = ["בכסף", "לכסף", "הכסף", "בתוך הכסף", "עמוק בכסף", "עמקו בכסף",
                 "מהכסף", "בכיס"]

LONG  = ["לונג", "long", "קנייה", "קניה"]
SHORT = ["שורט", "short", "מכירה"]

IMMEDIATE = ["כניסה מיידית", "כניסה עכשיו"]

# Fast typing produces spelling variants, and a fixed string list cannot keep up with
# them - every variant it misses is a message silently dropped as a non-entry. Match
# the SHAPE of the phrase instead of enumerating its spellings.
IMMEDIATE_RE = re.compile(r"כנ[יד]?[סד]ה\s+מי*ד+י*ת|כניסה\s+עכשיו")

# Trade-management verbs, grouped by what they do to an open position.
MANAGE = {
    "partial_close": ["לממש חצי", "לממש עוד חלק", "לממש חלק", "להפחית", "לממש",
                      "הפחתה", "לסגור חצי", "מימוש חלקי"],
    "full_close":    ["יש לסגור", "לסגור הכל", "סגירת העסקה", "לצאת מהעסקה",
                      "סוגרים", "לסגור את העסקה", "יציאה מלאה"],
    "stop_move":     ["סטופ עובר", "להעביר סטופ", "סטופ ל", "מעביר סטופ",
                      "סטופ לכניסה", "סטופ בכניסה", "לחזק את הסטופ", "הידוק סטופ"],
    "size_up":       ["להוסיף", "משודרגת", "לחזק", "הוספה לעסקה", "תוספת"],
}

BREAKEVEN = ["לכניסה", "בכניסה", "נקודת הכניסה", "ברייק איבן", "ב.א"]


def is_gold_metal(text, pos):
    """True if the 'זהב' at `pos` is the metal, not the conviction tier."""
    window = text[max(0, pos - 14):pos + 8]
    return not any(t in window for t in GOLD_TIER_CONTEXT)


def is_apple(text, pos):
    """True if the 'אפל' at `pos` is Apple the company, not the adjective 'dark'.

    'אפל' is a real Hebrew word meaning dark/gloomy, and its inflections (אפלה,
    אפלים, אפלות) are common in market commentary. Only the bare form is a ticker.
    """
    tail = text[pos + 3:pos + 4]
    if tail in ("ה", "ים", "ות", "י"):
        return False
    return True


def is_silver_metal(text, pos):
    """True if the 'כסף' at `pos` is the metal, not the tier and not plain money."""
    window = text[max(0, pos - 12):pos + 6]
    if any(t in window for t in TIER_CONTEXT):
        return False
    if any(t in window for t in MONEY_CONTEXT):
        return False
    return True


# Plausible price envelopes, used to catch silent misparses. Widen them for other periods.
# Deliberately wide - this is a sanity net, not a filter on market views.
PLAUSIBLE = {
    "XAUUSD": (1800, 7000),
    "XAGUSD": (18, 150),
    "US100":  (12000, 40000),
    "JP225":  (25000, 90000),
    "BTCUSD": (15000, 300000),
    "US2000": (1200, 5000),
    "US500":  (3500, 12000),
    "XTIUSD": (30, 150),
    "TSLA":   (80, 900),
    "US30":   (20000, 100000),
    "GER40":  (10000, 40000),
    "ETHUSD": (500, 15000),
    "ADAUSD": (0.05, 20),
    "SOLUSD": (5, 1500),
    "META":   (40, 2000),
    "NVDA":   (5, 2000),
    "GOOG":   (40, 1000),
    "MSFT":   (50, 2000),
    "AAPL":   (30, 1000),
    "PLTR":   (3, 1000),
    "MSTR":   (20, 5000),
    "NFLX":   (30, 3000),
    "INTC":   (3, 300),
    "AVGO":   (10, 2000),
    "QCOM":   (20, 1000),
    "ARM":    (10, 1000),
    "SNOW":   (20, 1500),
}

def plausible(symbol, value):
    lo, hi = PLAUSIBLE.get(symbol, (0, float("inf")))
    return lo <= value <= hi


def resolve_by_levels(candidates, levels):
    """Pick the instrument whose price envelope actually contains the levels.

    A signal card is often built by copying the previous one and retyping the numbers,
    which makes the *ticker* the token most likely to be stale while the numbers stay
    fresh. A card can therefore name one metal and carry the other one's levels:

        <metal A> / <METAL B TICKER> / Stop <b-level> / TP <b-level>

    Whichever token a parser prefers, it is wrong half the time. The levels usually are
    not ambiguous, because the two price envelopes do not overlap. Returns the sole
    candidate whose envelope holds every level, or None when the levels do not
    separate them.
    """
    vals = [v for v in levels if v is not None]
    if not vals or not candidates:
        return None
    fits = [c for c in candidates if all(plausible(c, v) for v in vals)]
    return fits[0] if len(fits) == 1 else None


def direction_from_levels(stop, target):
    """'long' when the target is above the stop, 'short' when below, else None.

    Only valid for two ABSOLUTE levels. Distances are magnitudes and carry no side,
    and a distance stop against an absolute target is not on a comparable scale.
    """
    if stop is None or target is None or stop == target:
        return None
    return "long" if target > stop else "short"


_BOUNDED_RE = {}

# Terms whose surface form has a non-instrument sense, and the test that separates them.
GUARDS = {"כסף": is_silver_metal, "זהב": is_gold_metal, "אפל": is_apple}


def term_pos(text, low, term):
    """Position of `term` in `low`, or -1, applying every disambiguation guard.

    Single place for match semantics so the parser and the engine cannot drift apart.
    """
    if term in ASCII_BOUNDED:
        rx = _BOUNDED_RE.get(term)
        if rx is None:
            rx = _BOUNDED_RE[term] = re.compile(r"(?<![A-Za-z0-9])" + re.escape(term) +
                                                r"(?![A-Za-z0-9])")
        m = rx.search(low)
        return m.start() if m else -1

    if term in HEB_BOUNDED:
        rx = _BOUNDED_RE.get(term)
        if rx is None:
            rx = _BOUNDED_RE[term] = re.compile(
                "(?<![%s])%s(?![%s])" % (_HEB, re.escape(term), _HEB))
        m = rx.search(low)
        return m.start() if m else -1

    guard = GUARDS.get(term)
    start = 0
    while True:
        pos = low.find(term, start)
        if pos < 0:
            return -1
        # Scan every occurrence, not just the first: one message can use 'זהב' as a
        # conviction tier and then name the metal, or the reverse.
        if guard is None or guard(text, pos):
            return pos
        start = pos + 1
