# -*- coding: utf-8 -*-
"""Extract executed trade actions from the stocks channel.

Self-contained. Imports nothing from the futures/CFD engine: that engine assumes
tickets with a stop, a target and a risk percentage, and none of those exist here.

THE CENTRAL RULE - tense separates fact from plan.

    past, first person     נכנסתי / הוספתי / סגרתי / החזרתי / הפחתתי   -> the author did it
    future, first person   אכנס  / אוסיף  / אסגור  / אפחית  / אממש     -> the author might

The future forms almost always carry a price condition:

    "מעל [price] אוסיף עוד חלק"  above a level I will add another part
    "מתחת ל [price] אסגור אותה"  below a level I will close it

Treating those as trades is the defect an external review found in the other engine,
where conditional language executed immediately. They are captured with kind="plan" and
excluded here.

Sizes are ORDINAL, never absolute. The author says "full quantity", "half", "a third",
"a small quantity", "a part". The ladder below maps those to fractions purely so
actions can be ordered by conviction; it is not a claim about position size, and no
P&L is ever computed from it.
"""
import re

# --- quantity ladder ---------------------------------------------------------
# Ordinal only. "part" and "a bit" are deliberately vague in the source and stay vague
# here: they are recorded as a level of None so nothing downstream can pretend to know.
SIZE_WORDS = [
    ("כמות מלאה", 1.0), ("כמויות מלאות", 1.0), ("כמות שלמה", 1.0),
    # The author drops the noun: "[symbol] מלאה" is a full position, the adjective agreeing with
    # the share rather than with כמות. Listed AFTER the two-word forms so those still
    # win, and it must also be a STATE_PHRASE below or the message carries no state.
    ("מלאה", 1.0), ("מלאים", 1.0),
    ("שלושה רבעים", 0.75), ("3/4", 0.75),
    ("חצי כמות", 0.5), ("חצי", 0.5), ("1/2", 0.5),
    ("שליש כמות", 0.334), ("שליש", 0.334), ("1/3", 0.334),
    ("רבע כמות", 0.25), ("רבע", 0.25), ("1/4", 0.25),
    ("כמות ממש קטנה", 0.15), ("כמות קטנה", 0.25), ("כמות קטנטנה", 0.15),
    # "חלק אחרון" is the LAST part - the remainder - not another vague part.
    # Falling through to ("חלק", None) sent it to DEFAULT_CUT and left a quarter open
    # forever, carrying unrealised P&L to the end of the window on exactly that.
    ("חלק אחרון", 1.0), ("החלק האחרון", 1.0),
    # "[symbol] הוחזרה כמות מקורית" - restored to the ORIGINAL quantity, i.e. all of it.
    ("כמות מקורית", 1.0), ("הכמות המקורית", 1.0),
    ("חלק קטן", 0.25), ("קצת", None), ("חלק", None), ("כמות", None),
    # "מימשתי הכל" - I realised EVERYTHING - was a cut with no stated size, so it fell
    # to DEFAULT_CUT and left half the position open forever.
    ("את כל הכמות", 1.0), ("כל הכמות", 1.0), ("כל היתרה", 1.0), ("את היתרה", 1.0),
    ("היתרה", 1.0), ("הכל", 1.0),
]

# --- verbs -------------------------------------------------------------------
# Past tense, first person: an action the author states the author took.
OPEN_PAST = ["פתחתי", "נכנסתי", "קניתי", "לקחתי", "בניתי", "פתחתי פוזיציה",
             # "התחלתי לבנות" - I started building - opens a position the author intends
             # to scale into, and it is a common entry idiom that a naive list misses.
             # "בניתי" is the obvious form to list; this inflection is easy to leave out,
             # and every message using it is then unread. Found by auditing the positions
             # the flat-book sweep closed and the author then reduced without a visible entry.
             "התחלתי לבנות", "התחלנו לבנות",
             # Confirmed by a native reader. "נכנסה" is PAST feminine - the
             # position was filled - not the present participle "נכנסת". It was reaching
             # the participle branch only because "נכנס" is a prefix of it, and coming
             # out "ambiguous", so real entries were never opened. Listed before the participle test so the past reading wins.
             "נכנסה"]
ADD_PAST = ["הוספתי", "החזרתי", "חזרתי", "הגדלתי", "חיזקתי", "הכפלתי", "נחזרתי",
            "מחזיר", "החזרתי כמות",
            # Confirmed by a native reader. Passive "was brought back": the
            # position is restored to the size the author names. "[symbol A] הוחזרה כמות מקורית" puts
            # it back to the ORIGINAL quantity; "[symbol B] הוחזר חלק קטן" adds a small part
            # back. An add rather than an open, because the author only ever writes it about a
            # position the author had already reduced.
            "הוחזרה", "הוחזר", "הוחזרו"]
CUT_PAST = ["הפחתתי", "צמצמתי", "מימשתי", "הורדתי", "מכרתי חלק", "הפחתתתי",
            # Passive. The author narrates the position as much as the author's own hand:
            # "[symbol] הופחתה לכמות קטנה" - reduced to a small quantity.
            "הופחתה", "הופחת", "צומצמה", "צומצם", "מומשה", "מומש"]
EXIT_PAST = ["סגרתי", "יצאתי", "מכרתי", "נסגרתי", "סגרתי הכל", "יצאתי לגמרי",
             "נסגרה", "נסגר", "נמכרה", "הסתיימה",
             # Authors type fast and do not correct themselves.
             "סגריתי",
             # Feminine, of the share: "[symbol] יצאה בכניסה" - it exited at our entry.
             # Safe only because NOT_AN_EXIT strips "יצאה מסיכון" before any verb match.
             "יצאה", "יצאו"]

# The rule above has a few exceptions:
#
#   "לא נכנסה" after a negator   has NOT entered yet           - negated
#   "[symbol] נכנסה יפה לכסף"        entered nicely INTO PROFIT    - the P&L, not a fill
#   "נכנסה תנודתיות"             VOLATILITY entered            - not a position at all
#
# The user ruled that "נכנסה" reports a fill. That ruling is about the verb, not about
# every sentence containing it, and reading these three as entries would open positions
# on a denial, a profit remark and the weather. Stripped before verb matching, the same
# way NOT_AN_EXIT is.
NOT_AN_ENTRY_RE = re.compile(
    r"(?:לא|טרם|עוד\s+לא)\s+(?:\S+\s+){0,2}?נכנסה"          # negated
    r"|נכנסה\s+(?:יפה\s+)?ל(?:כסף|רווח|ירוק)"                 # entered into profit
    r"|נכנסה\s+(?:תנודתיות|עוצמה|כסף|מומנטום|נפח|קונים|מוכרים)")  # abstract subject

# A realisation the author marks as OLD is not a realisation now, though it reads like
# one that closes a whole position:
#
#   "[symbol] ... נשארתי בכמות קטנה (רוב הכמות כבר מומשה מזמן)"
#        I am left with a SMALL quantity (most of the trade was realised LONG AGO)
#
# The parenthetical explains why the quantity is small. Read as a live instruction it
# booked a 100% close, on the same message whose main clause declares the author still holds
# part of it. The two readings cannot both be right, and the bracket is the giveaway.
HISTORICAL_RE = re.compile(r"\([^)]*\bמזמן\b[^)]*\)|כבר\s+\S+\s+מזמן")


# A past verb carrying the relative prefix "ש" is SUBORDINATE - it describes context,
# not what the author did. Such clauses were misparsed, one of them inverting the trade
# completely:
#
#   "[symbol A] אחרי סגירה בכניסה, חוזרים בחצי כמות"
#        after I CLOSED at entry, I am coming back at half size   -> read as exit 0.5
#   "[symbol B] נחזיר בהמשך החצי שמומש"
#        I will bring back the half that WAS REALISED             -> read as cut 0.5
#   "[symbol C] מחזיקים את הרבע שמומש עמוק בכסף"
#        I am HOLDING the quarter I realised                      -> read as cut 0.25
#
# In every one the main verb is elsewhere in the clause and the "ש" verb is a relative
# clause about an earlier action. Found while checking the flat-book rule: [symbol] is a
# re-entry, and reading it as an exit is what made a flat declaration look contradicted.
SUBORDINATE_PAST_RE = None          # built after the verb lists exist


# "יצאה מסיכון" is NOT an exit. It means the trade is out of RISK - the stop has been
# moved to breakeven - and the position is still open. Without this guard, every
# risk-free trade would be booked as closed at the moment it became safe.
NOT_AN_EXIT = ["יצאה מסיכון", "יצאנו מסיכון", "יצא מסיכון", "מחוץ לסיכון"]

# ...and the author puts words between the verb and the risk:
#
#   "[symbol A] יצאה עכשיו כמעט מסיכון"      now almost out of RISK
#   "[symbol B] יצאה סופית מסיכון, נשארת כמות מלאה"
#   "נסדק שיצא רשמית מסיכון"           officially out of RISK
#
# Risk-free messages are often phrased that way, and none of them
# matches a fixed string. They only became dangerous when "יצאה" was added
# to EXIT_PAST: before that the verb was invisible and the miss was harmless. Adding a
# verb form and leaving its exception literal turned still-open positions into
# closures at the moment the stop moved to breakeven.
NOT_AN_EXIT_RE = re.compile(
    r"(?:יצא|יצאה|יצאו|יצאנו|יצאתי|שיצא|שיצאה)"
    r"(?:\s+\S+){0,3}"
    r"\s+מ?ה?סיכון")

# Future tense, first person: a plan, not a trade.
FUTURE = ["אכנס", "אקח", "אוסיף", "אפחית", "אממש", "אסגור", "אתחיל", "אחזיר",
          "אגדיל", "אצא", "אקנה", "אמכור", "אבנה", "אשקול", "ארד", "אעלה"]

# Present participles are ambiguous in Hebrew ("קונה" = buying / I buy / the author buys) and
# are NOT treated as executed. They are recorded as kind="ambiguous".
#
# "ממש" is deliberately absent. It is a prefix of ממשיך / ממשיכה ("continues"), so it
# matched most ambiguous records on pure commentary - "[symbol] ממשיכה לפי תוכנית".
# It is also an ordinary adverb meaning "really". No form of it is worth the noise.
PRESENT = ["קונה", "מוכר", "סוגר", "נכנס", "יוצא", "מוסיף", "מפחית"]

# What a present participle becomes once the clause proves it is about the author's position.
# See the subject test at the end of classify().
PRESENT_KIND = {"קונה": "open", "נכנס": "open", "מוסיף": "add",
                "מפחית": "cut", "סוגר": "exit", "יוצא": "exit", "מוכר": "exit"}

# NARROWED after review found the previous version wrong in BOTH directions.
#
# It used to be a list of anything position-flavoured - "אני", "כמות", "לפי תוכנית".
# That was a heuristic pretending to be a rule, and it:
#   over-fired  "[symbol A] סוגרת ... לכן אני מפחית"  -> exit, when the action is a reduction
#               an educational passage containing "אני" opened several instruments
#               "[symbol B] סוגרת היום חזק מאוד" -> exit, describing the chart
#   under-fired "[symbol C] סוגר", "[symbol D] סוגר כרגע"    -> still ambiguous, and they are exits
#
# Three attempts at a general discriminator have now failed: grammatical gender (some
# tickers are masculine and take the author's own form), position vocabulary (above), and exact-form
# matching ("[symbol] סוגר מעל קו המגמה" is exact and is chart commentary). I do not
# have a general rule, so this is deliberately not one.
#
# What remains is a single idiom that cannot mean anything else. "בכניסה" / "ברווח" /
# "בהפסד" report where a trade FINISHED relative to its entry - "[symbol] בכניסה סוגר",
# "[symbol] סוגר ברווח קטן". A chart does not close at your entry; only a position does.
#
# This is strictly conservative: it leaves "[symbol] סוגר" ambiguous and unclosed, which
# can leave a position open too long. That is the right way to be wrong while the general case is
# unsolved, and the blind recall sample should size this family before anyone writes a
# fourth rule.
POSITION_CONTEXT = ["בכניסה", "בכניסות", "ברווח", "בהפסד"]

# ADDED AFTER A TEST AUDIT.
#
# The rule above leaves "[symbol A] [symbol B] אני מפחית" - "[symbol A] [symbol B] I am reducing" - ambiguous,
# so an explicit executed reduction on two live positions reached the book as nothing.
#
# The discriminator is NOT the bare presence of "אני" anywhere in the message; that was
# the earlier heuristic, and it opened several instruments off an educational passage. It
# is "אני" DIRECTLY governing the participle. A chart cannot be the subject of "אני
# מפחית", and the educational passage survives untouched because its own "אני" governs
# רוצה / יכול / לוקח, none of which are in PRESENT, while its PRESENT verb (קונה) is
# governed by the conjunction "ו".
#
# Measured before it was written: very few clauses change classification, and the
# conditional case is handled by the guard below.
FIRST_PERSON = "אני"
IF_WORD = "אם"


def _first_person_subject(text, pos):
    """True when the present participle at `pos` is directly governed by "אני"."""
    before = text[:pos].rstrip()
    if not before.endswith(FIRST_PERSON):
        return False
    # "[symbol] אם יורדת לכניסה מפחיתים" is a condition, and CONDITION_RE does not catch
    # a bare "אם" followed by a verb outside its list. Widening that regex would change
    # the classifier everywhere and needs measuring on its own, so the guard is local.
    return IF_WORD not in before


def _governed_present(text):
    """The first present participle whose own subject is explicitly "אני", if any.

    "[symbol] סוגרת חלש היום ולכן מפחיתים את ההוספה" holds two participles: the
    share closes (the chart) and then THE AUTHOR reduces. Taking the earliest, as _first_hit
    does, reads the clause as being about סוגרת and loses the reduction entirely - the
    defect this function exists to correct. An explicitly first-person verb outranks an
    earlier one whose subject is unstated, because only one of the two is unambiguous.
    """
    best = (None, None)
    for word in PRESENT:
        start = 0
        while True:
            i = text.find(word, start)
            if i < 0:
                break
            if not _negated(text, i) and _first_person_subject(text, i):
                if best[0] is None or i < best[0]:
                    best = (i, word)
                break
            start = i + 1
    return best

# --- direction ---------------------------------------------------------------
SHORT_WORDS = ["שורט", "שורטים", "בשורט"]
LONG_WORDS = ["לונג", "לונגים", "בלונג"]

# Inverse/short ETFs: an "open" on these is bearish exposure to the underlying, which
# matters when interpreting a direction call but NOT when measuring the ticker's own
# forward return. Recorded as a flag, not as a direction flip.
INVERSE_ETFS = {"SQQQ", "SOXS", "SPXS", "SDOW", "TZA", "MSTZ", "BITI", "LABD", "SRTY"}
LEVERAGED_ETFS = {"TQQQ", "SOXL", "TSLL", "TNA", "BITX", "PLTU", "UPRO", "SPXL",
                  "LABU", "NVDL", "MSTU", "FNGU", "USD", "CONL"} | INVERSE_ETFS

# Uppercase tokens that are not tickers.
NOISE = {
    "TP", "STOP", "LIMIT", "RANGE", "EVAL", "USD", "ETF", "ETFS", "CPI", "AI", "US",
    "IPO", "CEO", "CFO", "PE", "EPS", "YTD", "ATH", "PPI", "FED", "GDP", "NYSE", "EU",
    "UK", "ER", "IV", "OK", "PM", "AM", "ET", "USA", "FOMC", "QE", "QT", "SL", "RR",
    "EMA", "SMA", "RSI", "VWAP", "OB", "FVG", "HTF", "LTF", "NFP", "ISM", "PCE",
    # "RR" is NOT here: see AMBIGUOUS_NOISE below.
    "Q1", "Q2", "Q3", "Q4", "H1", "H2", "TA", "IL", "GG", "LOL", "BTW", "FYI",
    "CAPEX",
}
NOISE.discard("RR")

# Tokens that are jargon in one sentence and a ticker in the next.
#
# "RR" is risk/reward - and it is also a real ticker. Listing it as
# noise silenced every message in which it was the ticker, followed by a state or a
# verb like any other. Listing it as a ticker would read every mention of a
# risk/reward ratio as a position.
#
# The two uses look nothing alike. Jargon comes with a ratio or the word יחס:
# "יחס RR", "RR של 1:3", "RR 2.5". The ticker comes with a state or a verb, like any
# other ticker, and the rest of the pipeline is already good at spotting those.
AMBIGUOUS_NOISE = {"RR"}
RATIO_RE = re.compile(r"\d\s*[:/]\s*\d|יחס|ריסק|תשואה\s*סיכון")


BARE_NUMBER_RE = re.compile(r"^\s{0,2}\d+(?:[.,]\d+)?")


def _jargon_here(text, start, end):
    """True if this occurrence reads as jargon rather than as a ticker.

    Two signals. A ratio nearby ("יחס RR", "RR של 1:3"), or a bare number immediately
    after it ("RR 2.5") - because a quoted risk/reward is a number, while the ticker is
    followed by Hebrew: "RR בפנים", "RR פורצת". A number later in the sentence is not a
    signal at all; "RR בפנים כמות קטנה STOP [price]" is a position with a stop.
    """
    window = text[max(0, start - 22):min(len(text), end + 22)]
    if RATIO_RE.search(window):
        return True
    return bool(BARE_NUMBER_RE.match(text[end:end + 8]))
TICKER_RE = re.compile(r"(?<![A-Za-z0-9$])\$?([A-Z]{2,5})(?![A-Za-z0-9])")
# A condition attached to an instruction. Numeric - "מעל [price]", "מתחת ל [price]", "ב [price]" -
# but also verbal, which the numeric-only version missed entirely:
#   "[symbol A] שליש כמות מעל גבוה"      a third, ABOVE THE HIGH
#   "[symbol B] שליש מהכמות רק אם תעבור [price]" a third, IF IT CROSSES
# Those were being executed on the spot as present holdings.
CONDITION_RE = re.compile(
    r"(?:(?:מעל|מתחת\s*ל?|ב|אם)\s*\d)"
    r"|(?:מעל\s+(?:גבוה|הגבוה|נמוך|הנמוך|רמת|אזור))"
    r"|(?:מתחת\s+ל?(?:גבוה|הגבוה|נמוך|הנמוך|רמת|אזור))"
    r"|(?:אם\s+(?:תעבור|יעבור|תחזיק|יחזיק|תשבור|ישבור|נראה|תסגור|יסגור))"
    r"|(?:בפריצה|בשבירה|בסגירה\s+מעל|בסגירה\s+מתחת)")


def tickers(text):
    """Uppercase tokens that look like tickers, in order, de-duplicated."""
    out = []
    for m in TICKER_RE.finditer(text):
        t = m.group(1)
        if t in NOISE or t in out:
            continue
        if t in AMBIGUOUS_NOISE and _jargon_here(text, m.start(), m.end()):
            continue
        out.append(t)
    return out


# AUTHORS DO NOT SHIFT-LOCK. This is the single largest defect of a naive parser.
#
# TICKER_RE demands [A-Z]{2,5}, so "[symbol A] מומשה ברווח קטן" - [symbol B] was realised at a
# small profit - was invisible, and with it a large share of everything the author
# wrote. Whole trades existed only in mixed case. Positions were opened,
# resized and closed without one of those messages being seen, and the model was
# left holding positions with no ending because the ending was spelled "[symbol]".
#
# Lower-casing the pattern outright is not the fix: "be", "top", "net", "man", "key"
# and "are" are all real tickers somewhere, and the author's text is full of those words in
# English. Two guards make it safe:
#
#   1. A candidate must already be known FROM ITS OWN ALL-CAPS USE, at least twice.
#      A word can only be read as a ticker if the author has unambiguously written it as one.
#      At a sensible floor every recovered ticker is genuine; the
#      cost is the few messages whose ticker never appears in caps at all.
#   2. URLs are removed first. Every single false positive found at any floor came
#      from one source: the "be" in a youtu.be link.
MIN_CAPS = 2

# English words that are also real tickers somewhere. They may appear in ALL-CAPS as
# genuine tickers, but they may never be PROMOTED from lower or mixed case, because the author
# writes English prose too: "looking good" made GOOD a ticker, the name of a charting
# tool made PRICE one, and an English news quote made TO one. None of the three
# reached the book - no size word or verb sat in their clause - but a fourth one might.
# The vocabulary is built over the author's whole history, so this list guards the widest exposure.
PROMOTE_STOP = {
    "TO", "GOOD", "PRICE", "ON", "IN", "AT", "IS", "AS", "AN", "BE", "IT", "SO", "NO",
    "UP", "US", "WE", "DO", "GO", "MY", "ME", "ALL", "AND", "ARE", "BUY", "CALL", "CAR",
    "CAT", "DAY", "END", "FAST", "FLY", "FOR", "GAP", "HIGH", "HOLD", "HOT", "KEY",
    "LOW", "MAN", "MAX", "MIN", "MORE", "NET", "NEW", "NICE", "NOW", "ONE", "OPEN",
    "OUT", "PLAN", "PRE", "PUT", "RED", "RISK", "RUN", "SAFE", "SEE", "SELL", "SET",
    "SIZE", "SLOW", "SOON", "TEST", "TIME", "TOP", "TWO", "VIEW", "WAIT", "WATCH",
    "WEEK", "WIN", "WORK", "YES", "BLOCK", "STARS", "PILOT", "HTML", "ORDER", "TRUST",
}
URL_RE = re.compile(r"https?://\S+|www\.\S+|\S+\.(?:com|net|org|be|co|io)/\S*")
ANY_CASE_RE = re.compile(r"(?<![A-Za-z0-9$])\$?([A-Za-z]{2,5})(?![A-Za-z0-9])")


def strip_urls(text):
    """Blank out links, preserving nothing. A link never carries a trade."""
    return URL_RE.sub(" ", text or "")


def build_vocab(texts, floor=MIN_CAPS):
    """Tickers written in unambiguous ALL-CAPS at least `floor` times."""
    seen = {}
    for t in texts:
        for k in tickers(strip_urls(t)):
            seen[k] = seen.get(k, 0) + 1
    return set(k for k, n in seen.items() if n >= floor)


def normalise(text, vocab):
    """Upper-case every known ticker, whatever case the author typed it in.

    Deliberately a text-to-text transform rather than a change to `tickers`: it is
    length-preserving, so clause splitting, the negation window and every other
    offset-sensitive rule downstream behave exactly as they did before. Only the
    spelling changes.
    """
    if not vocab:
        return text

    def up(m):
        w = m.group(1)
        u = w.upper()
        if u in PROMOTE_STOP or u not in vocab:
            return m.group(0)
        return m.group(0).replace(w, u)

    return ANY_CASE_RE.sub(up, text or "")


# A size word can be measuring something other than the position. "יחידת סיכון" is the author's
# RISK unit, and "הפסד" + [fraction] + "יחידת סיכון" - a loss of part of a risk unit - is a
# sentence about how much the author lost, not about how much the author holds. Stripped before any size
# matching, the same way NOT_AN_EXIT is stripped before verb matching.
NOT_A_SIZE_RE = re.compile(
    r"יחיד(?:ת|ות)\s+ה?סיכון"                # yehidat (ha)sikun - risk unit
    r"(?:\s+כמעט)?"                           # ...almost...
    r"(?:\s+(?:מלאה|שלמה|רגילה|חצי|קטנה))?")   # ...full / half / small



# Confirmed by a native reader. "כרגע ללא עסקאות" / "אני ללא פוזיציות" is a
# statement that the author is FLAT ACROSS THE WHOLE BOOK, and it closes every position still
# open at that moment. Messages that say it may also name the
# final close in the same breath - "סגרתי את [symbol], אין לי עסקאות פתוחות",
# "[symbol] נסגרה בכניסה, ללא פוזיציות כרגע" - which settles it as a book state, not a mood.
#
# It is the single most consequential rule here, because it is the only thing that closes
# positions the author opened and then never mentioned again. A position opened at full size
# and never named again would otherwise mark to the end of the window.
#
# The one exclusion is the author's own: "בלי עסקאות חדשות" - without NEW trades - says the author is not
# opening anything, not that the author closed what the author had. Without the exclusion it would
# flatten the book on a sentence that means the opposite.
FLAT_BOOK_RE = re.compile(
    r"(?:ללא|בלי|בלא|אין\s+לי)\s+(?:ה?עסקאות|ה?פוזיציות|עסקה|פוזיציה)")
FLAT_EXCEPT_RE = re.compile(r"(?:עסקאות|פוזיציות)\s+חדשות")


def flat_declaration(text):
    """True when the author states the author is flat across the whole book.

    Deliberately whole-message rather than clause-scoped: the declaration and the close
    that produced it routinely sit in different clauses of one message, and there is no
    ticker to bind it to in any case.
    """
    if not text:
        return False
    if FLAT_EXCEPT_RE.search(text):
        return False
    return bool(FLAT_BOOK_RE.search(text))


def size_of(text):
    """The ordinal size named in the text, or (None, None) if none is named.

    Longest phrase wins, so "כמות ממש קטנה" is not read as "כמות".
    """
    text = re.sub(r"(?:חצי|רבע|שליש|\d+(?:/\d+)?)\s+יחיד(?:ת|ות)\s+ה?סיכון", " ", text)
    text = NOT_A_SIZE_RE.sub(" ", text)
    # Longest wins, BUT a word that names a fraction beats a longer word that does not.
    # "[symbol] מומש 3/4 כמות" contains both "3/4" (0.75) and "כמות" (vague); by length alone
    # the vague one won and the stated three quarters was thrown away.
    best = None
    for word, level in SIZE_WORDS:
        if text.find(word) < 0:
            continue
        if best is None:
            best = (word, level)
            continue
        better = ((level is not None, len(word)) > (best[1] is not None, len(best[0])))
        if better:
            best = (word, level)
    return (best[0], best[1]) if best else (None, None)


def direction(text, ticker=None):
    """Stated direction, or None. Most messages state none and are long by default."""
    if ticker is not None:
        # Parentheses commonly explain bearish ETF exposure, not the direction in
        # which its shares are held. Bind direction to the traded ticker itself.
        clean = re.sub(r"\([^)]*\)", " ", text)
        t = re.escape(ticker)
        if re.search(r"לונג\s+(?:ל\s*)?" + t + r"\b|\b" + t + r"\s+(?:בפנים\s+)?לונג", clean):
            return "long"
        if ticker in INVERSE_ETFS:
            # 'short Nasdaq through an inverse ETF' and 'that ETF short is closed' describe the
            # fund's bearish exposure. Only an explicit short SALE changes its side.
            if re.search(r"(?:נכנסתי\s+לשורט|מכרתי\s+בחסר)\s+" + t + r"\b", clean):
                return "short"
            return "long"
        direct_short = re.search(r"שורט\s+(?:ל\s*)?" + t + r"\b|\b" + t + r"\s+שורט(?![א-ת])", clean)
        if direct_short:
            return "short"
        # The only named ticker can precede a verb ('[symbol] closed the short').
        # Do not transfer 'short Nasdaq/Tesla' across a clause to an unrelated share.
        if re.search(r"שורט\s+(?:על\s+)?(?:נסדק|לנסדק|טסלה|שבבים|השוק)", clean):
            return None
        if len(tickers(clean)) != 1:
            return None
        if not re.search(r"(?:לונג|שורט)(?![א-ת])", clean):
            return None
        text = clean
    if any(w in text for w in SHORT_WORDS):
        return "short"
    if any(w in text for w in LONG_WORDS):
        return "long"
    return None


# A negator immediately before the verb reverses it. Hebrew puts it there:
#   "[symbol] לא הוספתי עדיין"   I have NOT added it yet
#   "[symbol] לא מכרתי כלום"     I sold NOTHING
# Both booked a trade before this existed. The window is deliberately short - in
# "אם לא תחזיק 11 אסגור" the לא governs תחזיק, not אסגור, and must not reach it.
NEGATORS = ["לא", "טרם", "בלי"]
NEG_WINDOW = 12


def _negated(text, pos):
    """True if a negator sits immediately before the verb at `pos`."""
    back = text[max(0, pos - NEG_WINDOW):pos]
    for n in NEGATORS:
        i = back.find(n)
        if i < 0:
            continue
        # Must be a whole word, and nothing but spaces between it and the verb.
        before_ok = i == 0 or not back[i - 1].isalpha()
        between = back[i + len(n):]
        if before_ok and between.strip() == "":
            return True
    return False


def _first_hit(text, words):
    """Earliest position of any non-negated word, and the word, or (None, None)."""
    best = (None, None)
    for w in words:
        start = 0
        while True:
            i = text.find(w, start)
            if i < 0:
                break
            if not _negated(text, i):
                if best[0] is None or i < best[0]:
                    best = (i, w)
                break
            start = i + 1          # this occurrence is negated; look for another
    return best


def classify(text):
    """Return (kind, verb) for the message.

    kind is one of:
      open / add / cut / exit    - executed, past tense
      plan                       - future tense, a conditional intention
      ambiguous                  - present participle, cannot tell
      None                       - no trade language
    """
    # Strip the risk-free idiom before any verb matching, so "יצאה מסיכון" cannot be
    # read as an exit while still allowing a real "יצאה" elsewhere in the message.
    for phrase in NOT_AN_EXIT:
        text = text.replace(phrase, " ")
    text = NOT_AN_EXIT_RE.sub(" ", text)
    # Same treatment for the three things "נכנסה" does not open. See NOT_AN_ENTRY_RE.
    text = NOT_AN_ENTRY_RE.sub(" ", text)
    # A "ש"-prefixed past verb is a relative clause, not the action. See above.
    text = SUBORDINATE_PAST_RE.sub(" ", text)
    # A realisation the author dates to "long ago" is context, not an instruction. See above.
    text = HISTORICAL_RE.sub(" ", text)
    # A previous trade's closure does not close the current position.
    text = re.sub(r"(?:טרייד|עסקה|העסקה)\s+(?:קודם|קודמת|הקודם|הקודמת)\b[^.!?\n]*", " ", text)

    pos_f, verb_f = _first_hit(text, FUTURE)
    hits = []
    for kind, words in (("open", OPEN_PAST), ("add", ADD_PAST),
                        ("cut", CUT_PAST), ("exit", EXIT_PAST)):
        p, w = _first_hit(text, words)
        if p is not None:
            hits.append((p, kind, w))

    if hits:
        hits.sort()
        # A past-tense verb wins even if a future verb also appears: "הוספתי, ומעל 12
        # אוסיף עוד" is an executed add followed by a plan.
        return hits[0][1], hits[0][2]
    if verb_f is not None:
        return "plan", verb_f
    pos_p, verb_p = _first_hit(text, PRESENT)
    # An explicitly first-person participle wins over an earlier one whose subject is
    # unstated: in "[symbol] סוגרת ... לכן אני מפחית" the reduction is the action and the
    # closing is the chart.
    governed_pos, governed_verb = _governed_present(text)
    if governed_pos is not None:
        pos_p, verb_p = governed_pos, governed_verb
    if pos_p is not None:
        # A present participle hanging off a price condition is an intention, not a
        # report: "[symbol] מעל [price] נכנס חצי כמות" - at that level I enter half. Tense alone
        # cannot separate these; the condition can.
        if has_condition(text):
            return "plan", verb_p
        # Otherwise the question is WHO the verb's subject is, and tense cannot answer
        # it. Both readings are common in channels like this:
        #
        #   [symbol C] סוגרת פער פתיחה     the share closes a gap    - the CHART
        #   [symbol A] סוגר מעל קו המגמה  the ticker closes above the line - the CHART
        #   [symbol D] בכניסה סוגר        closing at our entry      - the POSITION
        #   [symbol B] נכנסת בחצי כמות    the ticker enters, half size - the POSITION
        #
        # Gender does not separate them: some tickers are masculine and take the same
        # form the author uses for themselves, while others are feminine and are still the author's
        # trades - the author narrates the fill from the share's side, exactly as the author does with
        # the past-tense passives already in CUT_PAST ("מומשה", "הופחתה").
        #
        # What separates them is whether the clause talks about a POSITION at all. A
        # size, an entry price, a P&L, a plan, or an explicit "I" means the author is reporting
        # the author's own book. Without one of those it is market commentary, and reading it as
        # a trade opened positions on gap-fills that were never the author's.
        #
        # This rule is symmetric by construction - it turns on position vocabulary, not
        # on the direction of the verb - which matters because a missed exit flatters
        # nothing while a missed entry deletes a trade. Applying it to exits alone would
        # have been favourable selection dressed up as a fix.
        if (_first_hit(text, POSITION_CONTEXT)[0] is not None
                or _first_person_subject(text, pos_p)):
            return PRESENT_KIND.get(verb_p, "ambiguous"), verb_p
        return "ambiguous", verb_p
    return None, None


# "STOP מעל [price]" quotes a protective level. It is not a trigger for entering, and a
# holding stated beside one is still a holding: "[symbol] עדיין בפנים, STOP מעל [price]".
# Rejecting those lost valid state declarations.
STOP_WORDS = ("STOP", "stop", "Stop", "סטופ")


def _build_subordinate_re():
    """Compile SUBORDINATE_PAST_RE once every verb list is populated.

    NARROWED after review. The first version blanked EVERY past verb carrying "ש",
    which is structurally wrong: "ש" also introduces a complement clause after a verb
    of reporting, and there the clause is exactly what the author is asserting.

        [symbol] מעדכן שסגרתי הכל       reporting THAT I closed everything - a real exit
        שימו לב שסגרתי את [symbol]      note THAT I closed it              - a real exit

    Both lost their exit. Only two constructions are contextual:

        אחרי שסגרתי ברווח           AFTER I closed          - temporal subordinator
        גם החצי שמומש               the HALF that was...    - relative clause on a noun
        גם הרבע שמימשתי             the QUARTER that I...

    So the "ש" must be preceded either by a temporal/causal subordinator or by a
    definite quantity noun. A bare "ש" after anything else is left alone.
    """
    verbs = sorted(set(OPEN_PAST + ADD_PAST + CUT_PAST + EXIT_PAST),
                   key=len, reverse=True)
    alt = "|".join(re.escape(v) for v in verbs)
    lead = (r"(?:אחרי|לפני|מאז|למרות|בעקבות|בגלל|כפי)\s+"        # temporal / causal
            r"|ה?(?:חצי|רבע|שליש|יתרה|כמות|חלק|שאר)\s+")          # definite quantity
    return re.compile(r"(?<![א-ת])(?:%s)ש(?:%s)(?![א-ת])" % (lead, alt))


SUBORDINATE_PAST_RE = _build_subordinate_re()


def has_condition(text):
    """True only for a condition that gates the instruction, not for a quoted stop."""
    for m in CONDITION_RE.finditer(text):
        before = text[max(0, m.start() - 14):m.start()]
        if any(w in before for w in STOP_WORDS):
            continue                      # this condition describes a stop level
        return True
    return False


# Commas split clauses too. Without that, "[symbol] עכשיו בכמות מלאה, מתחת לשפל היומי
# אפחית חצי" was ONE clause: the verb came from the second half (a future plan) and the size
# from the first (a current full position), and the engine emitted a single record that
# was neither. A blind recall sample found this repeatedly; same-message composition
# was the largest family of missed assertions in it.
CLAUSE_SPLIT_RE = re.compile(r"[\n\r]+|(?<=[.!?;,])\s+")



def _sole_ticker(text):
    """[ticker] when the whole message names exactly one, else [].

    Splitting on commas isolates sizes correctly but strands the half that carries no
    ticker: "[symbol] מומש עוד חלק, נשאר רבע אחרון" - leaving a last quarter - states the
    RESULTING position, and it is authoritative in a way the vague "חלק" is not.
    Dropping it sent the cut to DEFAULT_CUT and left the wrong fraction open.

    The single-ticker guard is what makes this safe. With two tickers in a message there
    is no way to know which one a bare clause continues, and guessing is how sizes leaked
    across tickers in the first place - the defect this file's clause scoping exists to
    prevent. So the inheritance applies only where there is nothing to be ambiguous
    about.
    """
    found = tickers(text)
    return [found[0]] if len(set(found)) == 1 else []


def _clauses(text):
    """Split into clauses. Sizes bind within a clause, never across one."""
    return [c for c in CLAUSE_SPLIT_RE.split(text) if c and c.strip()]


# Prefixes that mark a clause as continuing the previous one. Written as literal
# strings rather than a regex: right-to-left text makes an alternation with an
# optional prefix easy to get silently wrong.
CONTINUATION_PREFIXES = ("וגם", "גם", "כנל")


def _is_continuation(clause):
    """A clause that carries no verb of its own but continues the previous one.

    "[symbol A] מימשתי חצי / וגם [symbol B]" - "and also [symbol B]". Pure clause isolation
    would lose [symbol B] entirely, so a conjunction-led clause inherits the preceding kind.
    """
    return clause.lstrip().startswith(CONTINUATION_PREFIXES)


# "מומש" / "מומשה" - it WAS REALISED. Unlike "הופחתה" (was reduced), which names an
# amount that remains, realisation with no partitive means the trade is finished.
#
# Measure it rather than trusting intuition: of the messages using the passive, most name
# the part explicitly - "[symbol A] מומש חצי", "[symbol B] חלק מומש", "מומש 3/4 כמות". The author marks a
# partial when the author means one. The bare ones read as complete every time:
# "[symbol C] מומש הכל", "[symbol D] מומשה ברווח יפה".
#
# Falling to DEFAULT_CUT leaves a quarter of the position open to the end of the window,
# on a message that says it was realised at a small profit. A position that never closes
# carries its unrealised P&L into every figure after it.
REALISED_PASSIVE = ("מומשה", "מומש")


def _record(t, clause, kind, verb):
    word, level = size_of(clause)
    # NOT "level is None". A level of None has two very different causes: no size word
    # at all, and a size word that is deliberately vague ("חלק", "קצת"). Testing the
    # level conflated them, so "[symbol] מומש עוד חלק" - another PART was realised - closed
    # the whole position. Only the absence of any size word means all of it.
    if word is None and verb in REALISED_PASSIVE:
        word, level = verb, 1.0
    if (kind == "exit" and level is not None and 0 < level < 1
            and re.search(r"חצי|רבע|שליש|\d\s*/\s*\d", word or "")):
        kind = "cut"
    return dict(ticker=t, kind=kind, verb=verb, size_word=word, size_level=level,
                direction=direction(clause, t), conditional=has_condition(clause),
                leveraged=t in LEVERAGED_ETFS, inverse=t in INVERSE_ETFS,
                clause=clause)


def traded_text(text):
    """Remove descriptions of an ETF's underlying exposure before binding tickers.

    The stated long/short on the ETF itself remains intact. An unnamed leveraged
    vehicle is not silently replaced with a trade in its underlying stock.
    """
    exposure = r"(?:שורט|שוט|לונג)\s+ה?ממונף\s+(?:(?:על|אל)\s+)?[A-Z]{2,5}\b"
    text = re.sub(r"\(\s*" + exposure + r"\s*\)", " ", text)
    text = re.sub(r"ממונפת\s+שורט\s+של\s+[A-Z]{2,5}\b", " ", text)
    return re.sub(r"(?:שזה\s+)?" + exposure, " ", text)


def extract(text):
    """One record per (clause, ticker). EVERYTHING is scoped to the ticker's clause.

    Scoping only the size was half a fix. The kind, the state words, the direction and
    the condition all leaked across tickers too:

      * "סגרתי [symbol B], פתחתי [symbol A]" gave both tickers the same verb.
      * [symbol B] kept opening at the default level because declared_level() searched the
        whole message and found "בפנים" sitting in another ticker's clause.

    A ticker now inherits nothing from a clause it does not appear in, except through
    the explicit continuation rule above.
    """
    text = traded_text(text)
    out = []
    last_kind = last_verb = None
    sole = _sole_ticker(text)
    seen_ticker = False
    for clause in _clauses(text):
        kind, verb = classify(clause)
        explicit = tickers(clause)
        ts = explicit or (sole if seen_ticker else [])
        seen_ticker = seen_ticker or bool(explicit)
        if kind is None and ts and _is_continuation(clause) and last_kind:
            kind, verb = last_kind, last_verb
        if kind is not None:
            last_kind, last_verb = kind, verb
        if kind is None or not ts:
            continue
        for t in ts:
            out.append(_record(t, clause, kind, verb))
    return out


# Bare position-state phrases. A message carrying one of these and a ticker, but no
# verb, is a STATE DECLARATION: it says where the author stands rather than what the author just did.
# There are more of these in the corpus than there are verbs, and they are what makes
# the book reconstructable - see state.py.
STATE_PHRASES = ["בפנים", "בחוץ", "מחזיק", "לא מחזיק", "אין לי", "יצאה מסיכון",
                 "כמות מלאה", "כמויות מלאות", "חצי כמות", "שליש כמות", "רבע כמות",
                 "כמות קטנה", "כמות ממש קטנה",
                 # bare "מלאה" - see the note in SIZE_WORDS. Without it, positions
                 # declared full were not heard.
                 "מלאה"]

# ...but the holding described may not be THE AUTHOR'S. "מי ש" - whoever - hands the state to
# the reader:
#   "[symbol A] 🔥 ברכות למי שבפנים"          congratulations to WHOEVER IS IN
#   "[symbol B] השלימה את התוכנית למי שבפנים"
# Both congratulate readers on a plan that worked, and both opened a half position that
# then never closed, because nothing later in the channel could close a position the
# author never took. An unclosed phantom position carries unrealised P&L to the end of
# the window and quietly distorts everything after it.
#
# Deliberately narrow, for the reason the restatement rule is narrow: a bare
# declaration is often the only evidence a real position exists, so a rule that
# silences declarations has to earn every message it takes. Every message it matches
# is about members - congratulations, holding a trading ACCOUNT rather than a position,
# or questions from members.
THIRD_PERSON = ["מי ש"]
THIRD_PERSON_WINDOW = 6


def _attributed_to_others(text, pos):
    """True if the state phrase at `pos` describes the reader's position, not the author's."""
    back = text[max(0, pos - THIRD_PERSON_WINDOW):pos]
    for marker in THIRD_PERSON:
        i = back.rfind(marker)
        if i >= 0 and back[i + len(marker):].strip() == "":
            return True
    return False


HEB_LETTER = re.compile(r"[א-ת]")


def _states_his_own(clause):
    """The state phrases in `clause` that the author is claiming for themselves.

    A phrase followed by another Hebrew letter is a DIFFERENT WORD, and usually one with
    a different subject. "מחזיק" is the author holding a position; "מחזיקה" is the share holding
    a level - "[symbol A] מחזיקה את ה[price]", "[symbol B] מחזיקה את הרמה", "[symbol C] מחזיקה את התמיכה".
    Substring matching read many of those as the author's positions, including one that was
    an explicit conditional plan: "[symbol D] מחזיקה את האזור, נחזור לגודל מלא בפריצה" was
    booked as a full position the author already held.

    Found by a blind recall sample, as the same defect in the verb tables was
    ("סוגר" inside "סוגרת"). Substring matching against an unvocalised language will keep
    producing this until every table is boundary-checked.
    """
    out = []
    for w in STATE_PHRASES:
        for m in re.finditer(re.escape(w), clause):
            if re.search(r"(?:ה?שוק)\s*$", clause[:m.start()]):
                continue
            after = clause[m.end():m.end() + 1]
            if after and HEB_LETTER.match(after):
                continue
            if not _attributed_to_others(clause, m.start()):
                out.append(w)
    return out


def declaration(text):
    """State-declaration records, one per (clause, ticker), or [].

    Per CLAUSE, not per message. Rejecting a whole message because it contained any
    condition threw away valid statements - "currently holding only [symbol A] and [symbol B]"
    was discarded because the same message mentioned a session time, and
    "[symbol] עדיין בפנים, STOP מעל [price]..." because the stop is a condition. A holding is still
    a holding when a stop is quoted beside it; only the conditional clause is inert.
    """
    text = traded_text(text)
    out = []
    sole = _sole_ticker(text)
    seen_ticker = False
    for clause in _clauses(text):
        explicit = tickers(clause)
        ts = explicit or (sole if seen_ticker else [])
        seen_ticker = seen_ticker or bool(explicit)
        if classify(clause)[0] is not None:
            continue                      # a verb clause is an action, not a state
        if re.search(r"רוצה\s+(?:לפתוח|לקנות|להיכנס|להוסיף)", clause):
            continue
        if not _states_his_own(clause):
            continue
        if has_condition(clause):
            continue                      # an intention, not a holding
        if not ts:
            continue
        word, level = size_of(clause)
        for t in ts:
            out.append(dict(ticker=t, kind=None, verb=None, size_word=word,
                            size_level=level, direction=direction(clause, t),
                            conditional=False, leveraged=t in LEVERAGED_ETFS,
                            inverse=t in INVERSE_ETFS, state_only=True,
                            clause=clause))
    return out
