# -*- coding: utf-8 -*-
"""Turn one management message into targeted instructions.

Semantics confirmed by a native reader of the channel:
  1. "לממש את כל העסקה"      -> complete close, not a partial.
  2. "לממש עוד 50%"          -> 50% of what REMAINS after earlier reductions.
  3. "50 אחוז מכל עסקה פתוחה" -> that % off EVERY open position.
  4. "אפשר לסגור"            -> an instruction to follow, not an option.
  5. "זהב וסילבר ..."         -> one message can instruct several instruments.
  6. "לבטל את הטריגר"        -> cancels a PENDING order; it is not a second close.
  7. bare "יש לממש" (no amount stated) -> 100%, a full close.
  8. "מזכיר לכם ..."          -> commentary describing state, not an instruction.

Confirmed by a native reader, precisely because it should NOT be inferred
from rule 4:
 10. "יש לשקול" + [whole-quantity phrase] -> a BINDING full close. "יש ל" carries the
     obligation; "לשקול" is politeness, not a transfer of the decision. This is what
     separates it from the true hedges (שווה לשקול / רצוי ל / מציע ל / שיקול שלכם),
     which do hand the decision over and are treated as unresolved conditions.

Confirmed after an external review argued this one was too confident:
  9. "להפחית הוספה אחרונה"    -> CLOSE the newest position, in full. The phrase names
     which position ("the last addition") and not a quantity, and in this channel that
     means the addition comes off entirely. An amount, when the author wants one, is stated:
     "להפחית חצי מההוספה האחרונה" is a 50% partial on that same position.
"""
import re

from lexicon import TIER_RISK   # CONFIGURE ME lives in lexicon.py


def _tier_risk(clause, prefixes):
    """Risk implied by a conviction tier named in `clause`, or None.

    Empty TIER_RISK (the default) means no channel tier vocabulary is configured, and
    every lookup here returns None - the numeric paths are unaffected.
    """
    for word, risk in TIER_RISK.items():
        for pre in prefixes:
            if pre + word in clause:
                return risk
    return None

# Bare "לסגור" carries the instruction as often as "יש לסגור" does - "יפני לסגור",
# "ביטקוין עסקה אחרונה לסגור". It is listed last so the longer forms match first.
FULL_CLOSE = ["יש לסגור", "לסגור הכל", "סגירת העסקה", "לצאת מהעסקה", "סוגרים",
              "לסגור את העסקה", "יציאה מלאה", "יש לצאת", "לבטל את העסקה",
              "אפשר לסגור", "ניתן לסגור", "כדאי לסגור", "תסגרו", "נסגור",
              "החוצה", "ובחוץ", "לסגור"]
SIZE_UP    = ["יש להוסיף", "להוסיף לעסקה", "להוסיף עסקה", "משודרגת", "לחזק את העסקה",
              "הוספה לעסקה", "יש להגדיל", "להגדיל לסיכון", "תוספת לעסקה"]
SIZE_UP   += ["לחזק"]
#   להפחתי  - a letter transposition of להפחית. It matches nothing else in Hebrew,
#   and it is used on live positions ([symbol] + "יש להפחתי חצי").
SIZE_UP   += ["להחזיר את ההפחתה", "חוזרים על חלק מההפחתה",
              "השלמה חזרה של ההפחתה", "משלימים את ההפחתה",
              "השלמה להפחתה"]
RESTORE_RE = re.compile(
    r"(?:"
    r"(?:להחזיר|נחזיר|להוסיף\s+חזרה).{0,45}ה?הפחתה"
    r"|חוזרים\s+על\s+(?:חלק\s+מ)?ה?הפחתה"
    r"|(?:השלמה\s+חזרה\s+של|משלימים\s+את|השלמה\s+ל)ה?הפחתה"
    r"|מי\s+שהפחית.{0,55}(?:אמור\s+)?להחזיר"
    r")")
PARTIAL    = ["לממש", "להפחית", "להפחתי", "הפחתה", "מימוש חלקי", "לסגור חצי", "הפחית",
              "תפחיתו", "להוריד חצי", "להוריד עוד חצי", "לצמצם", "צמצום", "לצמצמ", "תצמצמו", "להקטין",
              # The bare noun with a quantity attached is an instruction in its own
              # right - "סילבר מימוש חצי". Without a quantity it is almost always
              # descriptive, so only these three forms are listed.
              "מימוש חצי", "מימוש שליש", "מימוש רבע"]
STOP_MOVE  = ["סטופ עובר", "להעביר סטופ", "מעביר סטופ", "סטופ לכניסה", "סטופ בכניסה",
              "לחזק את הסטופ", "הידוק סטופ", "סטופ כל הכמות", "לעדכן stop",
              "לעדכן סטופ", "עדכון סטופ", "עדכון stop", "לקדם סטופ", "לקדם את הסטופ",
              "לצמצם סטופ", "לצמצם את הסטופ", "להעלות סטופ", "לעלות סטופ",
              "סטופ יש לצמצם", "סטופ יש לעדכן",
              # "יש להזיז STOP ל / " + [price] - the same idea as להעביר and
              # לעדכן, and the only spelling that was missing.
              "להזיז סטופ", "להזיז stop"]

# A stop verb can precede or follow the object, and a scope phrase often sits between
# STOP and the verb. Literal phrase lists cannot safely cover those permutations.
STOP_ACTION_RE = re.compile(
    r"(?:"
    r"(?:לצמצם|להעלות|לעלות|לעדכן|לקדם|להזיז|להעביר|לחזק)\s+"
    r"(?:את\s+)?(?:ה\s*)?(?:סטופ|stop)"
    r"|(?:סטופ|stop)(?:\s+\S+){0,7}\s+(?:יש\s+)?(?:לצמצם|מתעדכן|לעדכן|להזיז|לעבור|עובר|עולה|מתקדם)"
    r")", re.IGNORECASE)


def _stop_move_intent(clause):
    return (any(w in clause.lower() for w in STOP_MOVE)
            or bool(STOP_ACTION_RE.search(clause)))

# A bare number on its own line, which is how the author writes the new stop after a clause
# that ends "... יש לעדכן סטופ ל:" - the value belongs to the clause before it.
BARE_NUMBER_RE = re.compile(r"^\d[\d,]*(?:\.\d+)?$")

# A stop move that names no verb at all, only the scope:
#
#     נסדק סטופ        <- instrument and the bare word "stop"
#     כל העסקאות       <- the scope: every open trade
#     [price]          <- the level, alone on its own line
#
# Every keyword in STOP_MOVE requires a verb ("להעביר", "לעדכן", "עובר"), so this
# shape matched nothing and every such instruction was silently discarded. The bare
# word "סטופ" cannot be the trigger on its own - every entry ticket contains it next
# to a number - so the scope phrase is what makes it unambiguous. Entry tickets never
# say "all trades".
ALL_TRADES = ["כל העסקאות", "לכל העסקאות", "כל הפוזיציות", "לכל הפוזיציות",
              "על כל העסקאות", "על כל הפוזיציות", "כל עסקה פתוחה",
              "מכל עסקה פתוחה", "כל פוזיציה פתוחה", "מכל פוזיציה פתוחה",
              "מכל עסקה", "מכל פוזיציה", "שאר העסקאות", "יתר העסקאות",
              "בכל עסקה פתוחה"]
STOP_TOKEN_RE = re.compile(r"סטופ|stop", re.IGNORECASE)

# How many following clauses to search for the level. The author's layout puts the scope on one
# line and the number on the next, so one clause of lookahead is not enough.
BARE_LOOKAHEAD = 3


# Instructions addressed to the whole book rather than to an instrument:
#
#     "אפשר לסגור" + "כל העסקאות הפתוחות"
#     "יש להפחית" + [percent] + "מכל הפוזיציות הפתוחות"
#     "יש לממש" + "כל השורטים הפתוחים"
#
# These name no instrument, so `load_actions` dropped the message entirely. Both parts
# are required: a "every trade/position" scope AND an explicit "open" qualifier. The
# scope alone appears constantly in instrument-scoped messages ("[symbol] STOP לכל
# העסקאות"), and those are excluded anyway because a symbol resolves for them.
#
# A bare plural with neither is handled by the fallback at the end of book_wide_side.
BOOK_WIDE_SCOPE = re.compile(r"כל\s+ה?(עסקאות|פוזיציות|שורטים|לונגים)")
BOOK_WIDE_OPEN = re.compile(r"פתוח")
_BOOK_WIDE_SIDE = ((r"שורטים", -1), (r"לונגים", 1))
BOOK_WIDE_PLURAL = re.compile(r"(פוזיציות|עסקאות)")


def book_wide_side(text):
    """0 for the whole book, -1 for 'all open shorts', +1 for 'all open longs'.

    Returns None when the text is not a book-wide instruction at all.

    Scope and the word "open" must appear in the SAME clause. Testing the whole message
    matched a post that said "all the trades" in one sentence and "open an account" in
    another - because "לפתוח" contains "פתוח".
    """
    for clause in _clauses(text):
        if not (BOOK_WIDE_SCOPE.search(clause) and BOOK_WIDE_OPEN.search(clause)):
            continue
        if _verb_pos(clause) is None:
            continue            # a scope without an instruction is description
        for word, side in _BOOK_WIDE_SIDE:
            if word in clause:
                return side
        return 0

    # A bare plural with no "כל" and no "פתוחות": "יש לסגור פוזיציות". It was excluded
    # as too vague, on the grounds that reading a bare plural as the whole book is a
    # guess; the surrounding messages showed it meant exactly that. The rule is kept
    # this narrow on purpose.
    #
    # Only safe because the caller has already established that no instrument resolves
    # anywhere in the message - "זהב יש לסגור עסקאות" must never reach here.
    for clause in _clauses(text):
        if not BOOK_WIDE_PLURAL.search(clause):
            continue
        if any(x["kind"] in ("full_close", "partial_close") for x in parse(clause)):
            return 0
    return None


# An entry ticket always quotes a target. Management messages that move a stop never
# do. Without this, a ticket that happens to contain a scope phrase produces a stop
# *move* carrying the ticket's own original stop. The normal entry gate keeps such a
# message away from `load_actions` today, so this is defence in depth rather than a
# live defect - but the trigger should not depend on a gate somewhere else.
TARGET_MARKER_RE = re.compile(r"טייק\s*פרופיט|טייק|take\s*profit|(?<![a-z])tp(?![a-z])",
                              re.IGNORECASE)


def _all_trades_stop(clauses, idx):
    """True when clause `idx` is a verb-less 'stop, all trades' instruction.

    The scope phrase may sit in the same clause or the one after it, because the line
    break falls in both places in the author's messages.
    """
    c = clauses[idx]
    if not STOP_TOKEN_RE.search(c):
        return False
    if any(TARGET_MARKER_RE.search(x) for x in clauses):
        return False            # this is an entry ticket, not a stop move
    window = c
    if idx + 1 < len(clauses):
        window = c + " " + clauses[idx + 1]
    return any(w in window for w in ALL_TRADES)


def _scope_only(clause):
    """True if the clause is nothing but a scope phrase ("כל העסקאות")."""
    residue = clause
    for w in ALL_TRADES:
        residue = residue.replace(w, "")
    return not residue.strip(" ,.:;-־")


def _bare_level_after(clauses, idx):
    """First bare number within BARE_LOOKAHEAD clauses after `idx`, or None.

    The scan may only step over a line that is purely a scope phrase. Anything else -
    another instrument, another stop keyword, any prose - halts it. Without that guard
    "זהב סטופ / כל העסקאות / סילבר סטופ / כל העסקאות / [price]" gave the gold
    instruction silver's level.
    """
    for j in range(idx + 1, min(idx + 1 + BARE_LOOKAHEAD, len(clauses))):
        c = clauses[j]
        if BARE_NUMBER_RE.match(c):
            try:
                return float(c.replace(",", ""))
            except ValueError:
                return None
        if not _scope_only(c):
            return None
    return None


# A stop expressed as a distance from the entry rather than as a price:
#   "STOP 10 נקודות מתחת לכניסה"      ten points below entry
#   "יש לעדכן STOP ל [n] נקודות מהכניסה"
#
# These were being read as breakeven, because BREAKEVEN matches "לכניסה" and
# "מתחת לכניסה" contains it. Unguarded, that moves live gold stops to exactly
# the entry price instead of ten points below it - an instruction the engine had
# previously ignored was now being obeyed wrongly, which is worse than missing it.
RELATIVE_STOP_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:נקודות|נקודה|פיפס|pips?)\s*"
    r"(מתחת|מעל)?\s*(?:ל|מ)?ה?כניסה")
GENERIC_STOP_DISTANCE_RE = re.compile(
    r"(?:סטופ|stop).{0,45}?(?:ל\s*)?(\d+(?:\.\d+)?)\s*"
    r"(?:נקודות|נקודה|פיפס|pips?)\b", re.IGNORECASE)
_OFFSET_SIDE = {"מתחת": -1, "מעל": 1}


def relative_stop(clause):
    """(distance, side) for a stop quoted as an offset from entry, else (None, 0).

    `side` is -1 for "below entry", +1 for "above entry", and 0 when unstated - the
    engine then puts the stop on whichever side is protective for that position.
    """
    m = RELATIVE_STOP_RE.search(clause)
    if not m:
        # In the platform's points/pips mode this is a distance from entry, not a
        # literal price: "move STOP to 5 points". Absolute stops are written as a
        # bare level, without a points unit.
        m = GENERIC_STOP_DISTANCE_RE.search(clause)
        if not m:
            return None, 0
        try:
            return float(m.group(1)), 0
        except ValueError:
            return None, 0
    try:
        return float(m.group(1)), _OFFSET_SIDE.get(m.group(2), 0)
    except ValueError:
        return None, 0

def _verb_pos(clause):
    """Where the first management verb starts, or None."""
    best = None
    lowered = clause.lower()
    for w in FULL_CLOSE + SIZE_UP + PARTIAL + STOP_MOVE + RESIZE + CANCEL:
        p = lowered.find(w)
        if p >= 0 and (best is None or p < best):
            best = p
    stop_action = STOP_ACTION_RE.search(clause)
    if stop_action and (best is None or stop_action.start() < best):
        best = stop_action.start()
    return best


def clause_targets(clauses, symbols_of):
    """Resolve which instruments each clause is an instruction *about*.

    Three patterns have to be told apart, and getting them wrong either drops a real
    instruction or invents one:

      "[symbol] הפחתה של חצי"          the instrument precedes the verb - it is the target
      "[symbol]" / "יש לממש עוד 20%"    a bare header line; the verb clause inherits it
      "יש לסגור, ... לפני הדוחות"       the instrument trails the verb inside a reason
                                        clause and is *not* the target

    So: prefer instruments named before the verb; accept one named immediately after it
    (no comma in between); otherwise inherit the running context set by earlier clauses.
    """
    out, context, header = {}, None, []
    for idx, c in enumerate(clauses):
        # "מכיוון שיש חשיפה גם ל[symbol A] ול[symbol B], נקדים את הצמצום ולכן:" explains why the
        # instruction that follows is being given. It names instruments and carries a
        # verb, but instructs nothing - and must not set the subject either.
        if any(c.lstrip().startswith(w) or (" " + w) in c for w in REASON):
            continue
        syms = symbols_of(c)
        vp = _verb_pos(c)
        if vp is None and _all_trades_stop(clauses, idx):
            # "נסדק סטופ" carries no verb, so it would be filed as a bare header and
            # the instruction would never find a subject. The stop token is the anchor.
            m = STOP_TOKEN_RE.search(c)
            vp = m.start() if m else None
        if vp is None:
            # Consecutive bare instrument lines accumulate: "נסדק US100" / "יפני JP225" /
            # "יש להפחית" + [fraction] + "מכל פוזיציה" is one instruction to both.
            if len(c.split()) <= 5:
                header.extend(s for s in syms if s not in header)
            continue
        at = {s: _first_pos(c, s, symbols_of) for s in syms}
        lo = 0
        stripped = c.lstrip()
        if any(stripped.startswith(w) for w in REASON_OPENER):
            comma = c.find(",")
            if 0 <= comma < vp:
                lo = comma + 1          # the reason ends at the comma
        else:
            comma = c.find(",")
            prefix = c[:comma] if comma >= 0 else ""
            if (0 <= comma < vp
                    and any(w in prefix for w in ("לאור", "עקב", "בגלל", "בעיקר"))):
                lo = comma + 1
        # at[s] is the offset just past the instrument's first mention, so a subject
        # sitting immediately before the verb satisfies at[s] <= vp.
        before = [s for s in syms if lo <= at[s] <= vp]
        adjacent = [s for s in syms if at[s] > vp and "," not in c[vp:at[s]]
                    and " ואם " not in c[vp:at[s]]]
        target = before or adjacent or header or context

        if not target:
            # Some posts put the instruction first and its instruments on following
            # terse lines: "reduce last entry for:" / "[symbol]" / "and" / "Gold".
            # Any prose stops the scan so a later market mention cannot become scope.
            forward = []
            for following in clauses[idx + 1:idx + 6]:
                if _verb_pos(following) is not None:
                    break
                following_syms = symbols_of(following)
                terse = len(following.split()) <= 5 or following.startswith("כולל ")
                connector = following.strip(" ,:;- ") in ("ו", "גם", "וכן")
                if following_syms and terse:
                    forward.extend(s for s in following_syms if s not in forward)
                    continue
                if connector:
                    continue
                break
            if forward:
                target = forward

        # A single grammatical object can enumerate several instruments after the
        # verb: "cancel a short on JP and a short on Nasdaq which are pending". The
        # 30-character adjacency rule finds only the first. Repetition of the side+
        # preposition is a strong list marker, so retain every named object here.
        if re.search(r"(?:שורט|לונג)\s+על.+(?:ו|,)\s*(?:שורט|לונג)\s+על", c[vp:]):
            listed = [s for s in syms if at[s] > vp]
            if listed:
                target = listed

        # "סוגרים חוץ מסילבר" names silver only to spare it. Drop anything inside the
        # exception, and if that empties the clause, emit nothing at all - falling back
        # to the running context here would re-close the instrument named just above.
        span = _except_span(c)
        if span and target:
            kept = [s for s in target
                    if not (s in at and span[0] < at[s] <= span[1] + 1)]
            if not kept:
                continue
            target = kept
        if not target:
            continue
        out[c] = target
        context, header = target, []
    return out


def _first_pos(clause, symbol, symbols_of):
    """Offset just past the FIRST mention of `symbol`, via the caller's detector.

    It has to be the first: "יפני לסגור את האחרונה ולהשאיר רק את הקטנה
    ביפני" names יפני twice, and measuring to the second one puts the instrument
    far from the verb and loses it.

    Binary search for the shortest prefix that already contains the symbol. Uses the
    caller's detector so the guarded Hebrew homographs (כסף, זהב, אפל) resolve exactly
    as they do everywhere else.
    """
    lo, hi = 0, len(clause)
    while lo < hi:
        mid = (lo + hi) // 2
        if symbol in (symbols_of(clause[:mid]) or []):
            hi = mid
        else:
            lo = mid + 1
    return lo

# "לעדכן את הסיכון ל 0.3%" changes the size of an order, it does not move a stop.
RESIZE = ["לעדכן סיכון", "לעדכן את הסיכון", "סיכון ל", "להוריד סיכון", "לשנות סיכון"]

# Openers that introduce the *reason* for an instruction. "בגלל התנועה בזהב, סוגרים את
# הסילבר" names gold only to explain why silver is being closed; counting it as the
# subject closes the wrong instrument. When a comma separates the reason from the
# instruction, the reason is trimmed; when there is none, the whole clause is reason.
REASON = ["מכיוון ש", "בגלל ש", "לאחר ש", "אחרי ש", "היות ו"]
REASON_OPENER = ["עקב", "בעקבות", "בגלל", "מכיוון", "לאחר", "אחרי", "היות", "כתוצאה"]

# (7) A bare realise means everything, unless it is explicitly partial.
PARTIAL_MARKERS = ["חלק", "עוד חלק", "חצי", "שליש", "רבע", "2/3", "%", "אחוז"]

# (1) phrases that make a "realise" a complete exit
WHOLE = ["את כל העסקה", "כל העסקה", "כל הכמות", "את כל הפוזיציה", "כל הפוזיציה",
         "מה שפתוח ונשאר", "את כל מה שפתוח", "כל היתרה", "את כל היתרה",
         "כל יתרת", "את יתרת העסקה"]

# ---------------------------------------------------------------------------
# CONFIGURE ME - CHANNEL-SPECIFIC PHRASES.
#
# The lists below ship with GENERIC Hebrew trading vocabulary only. Every channel also
# develops its own turns of phrase, and those are not vocabulary - they are one author's
# wording. Add yours here rather than editing the generic lists, so the two stay apart.
#
# COMMENTARY_EXTRA: phrasings that DESCRIBE a state instead of instructing a change.
#   A channel might habitually say something like "<we took some profit and right now>"
#   or "<in any case that was cancelled and still>" - wording that contains reduction
#   vocabulary but instructs nothing. Collect yours by reading the unresolved queue.
#
# NON_ACTION_REDUCTION: whole constructions that discuss reducing without instructing
#   it - educational advice, past-tense status, hypothetical capacity. Regex fragments.
# ---------------------------------------------------------------------------
COMMENTARY_EXTRA = []
NON_ACTION_REDUCTION = []

# (8) describes a state rather than instructing a change
COMMENTARY = ["נקודת הפחתה", "נקודת מימוש", "אפשרות להפחית", "אפשרות לצמצם",
              "מזכיר לכם", "מזכיר לכולם", "מזכיר ש", "כבר יש stop", "יש כבר stop",
              "אחרי מימוש", "לאחר מימוש", "אחרי המימוש", "לאחר המימוש",
              "ללא מימוש", "אחרי שמימשנו", "שמימשנו",
              "ההמלצה הייתה",
              "אזורים להפחתה", "בהמשך להפחתה", "הייתה הפחתה",
              "נטייה לסגור",
              "פתוחה סטופ", "פתוח סטופ",
              "פשוט להפחית חצי",
              "לאחר עדכון", "אחרי עדכון",
              "כבר ללא סיכון",
              # "לסגור" also describes a candle or a week closing at a level, which is
              # a market observation and not an instruction to close a position.
              "לסגור מעל", "לסגור מתחת", "הולכים לסגור", "איך לסגור",
              "לסגור את השבוע", "לסגור את היום", "לסגור שבועי", "לסגור מעליו",
              "נסגור שבועי",
              "לסגור שבוע ", "שנסגור את היום",
              "סוגרים שבוע",
              "היעד הוא לסגור", "המטרה לסגור", "נצליח לסגור"] + COMMENTARY_EXTRA

# Past-tense status, educational advice and hypothetical capacity all contain the
# same reduction vocabulary as a live command. Keeping these exact constructions out
# of the action stream makes the unresolved queue a useful review list instead of a
# list of sentences that never instructed anyone to trade.
# Built from NON_ACTION_REDUCTION (CONFIGURE ME, above). Two generic constructions ship
# by default; the rest are whatever your own channel says. Empty-safe: with nothing
# configured the pattern below never matches, which is the conservative direction.
_NAR = [r"חייבים\s+לדעת\s+לצמצם", r"גם\s+בהפחתה"] + list(NON_ACTION_REDUCTION)
NON_ACTION_REDUCTION_RE = re.compile("(?:" + "|".join(_NAR) + ")") if _NAR else None

# Hebrew uses one verb for "close a position" and "a candle closes", and the
# first-person forms נסגור / סוגרים are both in FULL_CLOSE. So watching a candle and
# supposing a close each read as an instruction:
#   "נראה איך נסגור את" + [timeframe]       a candle, not a position
#   "אפילו אם נסגור היום את [symbol]"            closed [symbol] positions
#   "רוצה לראות נר יומי של זהב מתחת ל [price]"
#
# Unlike COMMENTARY these cannot be tested against the whole clause. The author routinely gives
# an instruction and then explains themselves in the same breath - "[symbol] לסגור את
# ההוספה, ואז נראה אם הרמה מחזיקה" is a real close - so a marker only disqualifies
# the clause when it sits BEFORE the verb and therefore governs it.
SPECTATOR = ["נראה איך", "לראות איך", "לראות אם", "נראה אם", "גם אם",
             "איך נסגור", "איך סוגרים", "אם נסגור", "אם ייסגר", "אם יסגור",
             "מעניין לראות", "רוצה לראות", "נחכה לראות",
             "סוגר מתחת", "סוגר מעל", "ייסגר מתחת", "ייסגר מעל"]


def _spectator_governs(clause):
    """True when a "let's watch" or "even if" marker precedes the management verb."""
    vp = _verb_pos(clause)
    if vp is None:
        return False
    return any(0 <= clause.find(w) < vp for w in SPECTATOR)

# (6) cancelling a resting order is not an exit
# "עסקה בהמתנה" / "שורט אשר ממתינה" name a resting order just as "טריגר" does.
PENDING = ["טריגר", "לימיט", "limit", "הזמנה ממתינה", "פקודה ממתינה",
           "בהמתנה", "ממתינה", "ממתין"]
CANCEL  = ["לבטל", "מבוטל", "מבוטלת", "בוטל", "בוטלה", "בטלו", "ביטול",
           "נמחק", "נמחקה"]

# What the cancellation is ABOUT decides what it does.
#
# Rule 6 (confirmed by a native reader): "לבטל את הטריגר" cancels a resting order and is
# not a close. But when the author names the TRADE rather than the trigger, and that trade has
# already filled, cancelling it means closing it. Authors gloss this themselves:
#
#   [symbol] + "מבוטלת" ... "מה שפתוח" + [symbol] + "יש לסגור"
#   [symbol] + "מבוטלת כרגע, יש לסגור"
#   "פקודה מבוטלת, יש לסגור"
#   [symbol] + "מבוטל כרגע" ... "משמע לסגור עסקה"
#
# The noun form was also missing from the gate: `ביטול` needed a PENDING word beside it,
# so "סילבר ביטול עסקה אחרונה" produced nothing at all.
# Cancelling the TARGET is not cancelling the trade: the position stays open with no
# take-profit and runs on under its stop.
TAKE_PROFIT = ["טייק פרופיט", "טייק־פרופיט", "טייקפרופיט", "tp", "טארגט"]

CANCEL_TRADE = ["עסקה", "עסקת", "עסקאות", "פוזיציה", "פוזיצית", "פוזיציות",
                "שורט", "לונג", "פקודה"]

# PARTIAL holds noun forms (צמצום, הפחתה, מימוש) beside the verbs, and a noun takes an
# object. When that object is the STOP or the RISK, nothing is being done to the
# position at all:
#   "צמצום הסטופ" as the OBJECT               the stop was tightened
#   "צמצום הסיכון" as the OBJECT               the risk was made smaller
# Both parsed as a FULL close - not even a partial - because no fraction word follows,
# so the bare-"realise" rule promoted them to 100%.
#
# Masked rather than suppressed: the phrase is removed before the keyword tests, so a
# genuine instruction sharing the clause still fires.
NOUN_REDUCTION_RE = re.compile(
    r"(?:צמצום|הפחתת|הפחתה\s+של|מימוש|הקטנת)\s+ה?(?:סטופ|סיכון|סטופים)")

# Unlike the noun forms above, these are real actions on the stop/risk. Remove them
# only from the *position reduction* test; the original clause is still parsed as a
# stop move or a pending-risk update. This prevents "לצמצם סטופ" becoming a full exit.
REDUCTION_OBJECT_RE = re.compile(
    r"(?:"
    r"(?:לצמצם|להפחית|להוריד|להקטין)\s+(?:את\s+)?ה?"
    r"(?:סטופ|stop|סיכון|סטופים)"
    r"|(?:סטופ|stop)\s+(?:יש\s+)?(?:לצמצם|להפחית|להוריד|להקטין)"
    r")", re.IGNORECASE)

# One clause can carry two independent actions: "reduce half ... and stop to entry".
# Splitting only at an explicit stop conjunction is deliberately narrow; a mere
# mention that price nearly reached the stop must not manufacture a second action.
DUAL_STOP_RE = re.compile(
    r"\s+ו(?=(?:(?:להעביר|לקדם|לעדכן|להזיז|להעלות|לעלות)\s+(?:את\s+)?ה?)?"
    r"(?:סטופ|stop)\b)", re.IGNORECASE)
DUAL_REDUCE_RE = re.compile(r"\s+ו(?=(?:להפחית|לצמצם|לממש)\b)")
CLOSE_FRACTION_RE = re.compile(
    r"(?:לסגור|סוגרים)\s*(?:חצי|שליש|רבע|[1-9]\s*/\s*[1-9]|\d{1,3}\s*%)")

CONDITION_RE = re.compile(
    r"(?:^|[,;]\s*|\s)(אם\b|במידה\s+[וש]|כאשר\b|ברגע\s+ש|כש(?=\S)|אוטוטו\s+נוכל\b)")
OPTIONAL_SUFFIX_RE = re.compile(r"אם\s+(?:אתם\s+רוצים|אתם\s+רוצות|נרצה|תרצו)")
PRICE_CONDITION_RE = re.compile(r"(?:מתחת|מעל)\s*\d[\d,.]*")
CHOICE_RE = re.compile(r"(?:אם\s+אתם\s+רוצים|רצוי).{0,120}\sאו\s")
POSITION_GUARD_RE = re.compile(
    r"(?:אם\s+(?:למישהו\s+נכנס|מישהו\s+עדיין\s+מחזיק)|"
    r"אם\s+לא\s+מומש|מומש\s+אצלכם\s+ואם\s+לא)")

PLAN_CANCEL_RE = re.compile(
    r"(?:"
    r"(?:ה?הפחתה|נקוד(?:ה|ת)\s+(?:ל|ה)?הפחתה|ה?מימוש|נקוד(?:ה|ת)\s+(?:ל|ה)?מימוש)"
    r".{0,45}(?:מבוטל(?:ת)?|בוטל(?:ה)?|נמחק(?:ה)?)"
    r"|(?:ביטול|לבטל)\s+(?:ה?הפחתה|ה?מימוש)"
    r"|ביטול\s+על\s+\S+"
    r")")

_TIER_ALT = "|".join(re.escape(w) for w in TIER_RISK)
REDUCE_RISK_RE = re.compile(
    r"(?:להפחית|לצמצם).{0,30}(?:ל|אל)\s*סיכון\s+"
    r"(?:" + (_TIER_ALT + "|" if _TIER_ALT else "") +
    r"\d+(?:\.\d+)?\s*(?:%|אחוז))")


BREAKEVEN = ["לכניסה", "בכניסה", "נקודת הכניסה", "ברייק איבן", "ב.א"]

# "3/4" was missing, and its absence was not harmless: an unmatched fraction falls
# through to 1.0, which the partial-close branch then promotes to a FULL close. So
# "לצמצם ב 3/4" - reduce by three quarters, keeping a quarter - was closing the
# whole position. Ordered longest-first so "3/4" is tested before "4".
WORD_FRACTIONS = [("2/3", 2.0/3.0), ("3/4", 0.75), ("1/3", 1.0/3.0), ("1/4", 0.25),
                  ("1/2", 0.5), ("שלושה רבעים", 0.75), ("שני שליש", 2.0/3.0),
                  ("שליש", 1.0/3.0), ("חצי", 0.5), ("רבע", 0.25)]

WEEKDAY = {"ראשון": 6, "שני": 0, "שלישי": 1, "רביעי": 2, "חמישי": 3,
           "שישי": 4, "שבת": 5}

# percentages that are risk sizing, not exit fractions
# "סיכון ל 0.3%" / "סיכון של 0.3%" / "סיכון 0.3%" all name a risk size.
RISK_IN_CLAUSE = re.compile(r"סיכון\s*(?:של\s*|ל\s*)?(\d+(?:\.\d+)?)\s*(?:%|אחוז)")

_RISK_PCT = re.compile(r"סיכון\s*(?:של\s*)?\d+(?:\.\d+)?\s*(?:%|אחוז)")


URL_RE = re.compile(r"https?://\S+")
# Split on sentence punctuation, but never inside a decimal number: a naive split
# turns "0.3%" into "0" and "3%", which loses the risk size entirely.
SENT_RE = re.compile(r"(?<!\d)[.!?]+(?!\d)")


# "חוץ מסילבר" excludes silver from the close; it does not select it. The lookbehind
# keeps this off the near-identical phrases that mean something else entirely:
#   מחוץ לכסף   "out of the money"       בחוץ   "already out"
#   מדיניות חוץ     "foreign policy"         לא כולל "does not include" (a P&L line)
#   חוץ מזה      "apart from that" - a discourse connective, not a selector
# All were checked in a scope audit and must stay unmatched.
EXCEPT_RE = re.compile(
    r"(?<![א-ת])(?:חוץ\s+מ(?!זה\b|ול\b)|למעט|פרט\s+ל)")


def _except_span(clause):
    """(start, end) of the excluded region, or None.

    The exclusion covers what follows the marker up to the next management verb, comma
    or closing bracket: "חוץ מהזהב סוגרים הכל" excludes the gold, not the closing.
    """
    m = EXCEPT_RE.search(clause)
    if not m:
        return None
    tail = clause[m.end():]
    stop = len(tail)
    for w in FULL_CLOSE + PARTIAL + SIZE_UP + STOP_MOVE:
        p = tail.find(w)
        if 0 <= p < stop:
            stop = p
    for ch in (",", ")"):
        p = tail.find(ch)
        if 0 <= p < stop:
            stop = p
    return m.start(), m.end() + stop


def excluded_symbols(text, symbols_of):
    """Instruments a message names only in order to spare them.

    "חוץ מהזהב סוגרים הכל" - "except gold we are closing everything" - names gold
    solely to exclude it. A caller deciding whether a book-wide instruction can execute
    needs this, because a message whose ONLY named instrument is the excluded one is
    still an instruction to the whole of the rest of the book. Without it such a message
    falls through to per-instrument routing, which correctly refuses to close the
    excluded instrument and then emits nothing at all - so "close everything except
    gold" closed nothing.
    """
    out = set()
    for clause in _clauses(text):
        span = _except_span(clause)
        if not span:
            continue
        out.update(symbols_of(clause[span[0]:span[1] + 1]))
    return sorted(out)


WS_RE = re.compile(r"[ \t ]+")


def _clauses(text):
    # Double spaces are common enough that "נסדק יש  לסגור" fails every literal keyword
    # match. Collapse runs of spaces before anything is matched against them.
    text = WS_RE.sub(" ", URL_RE.sub(" ", text))
    parts = []
    for chunk in text.splitlines():
        # A bracketed aside is normally its own clause, but one carrying an exception
        # belongs to the instruction it qualifies: "להוריד חצי מכל מה שפתוח
        # (חוץ מהשורט החדש)" loses its exception entirely if it is split off.
        for m in re.finditer(r"\(([^)]*)\)", chunk):
            if not EXCEPT_RE.search(m.group(1)):
                parts.append(m.group(1))
        chunk = re.sub(r"\(([^)]*)\)",
                       lambda m: m.group(0) if EXCEPT_RE.search(m.group(1)) else " ",
                       chunk)
        # Different instruments can receive different actions in one sentence:
        # "reduce Bitcoin and strengthen Gold". Splitting at this explicit second
        # action lets the normal clause-target resolver bind each verb to its object.
        for action_chunk in re.split(r"\s+ו(?=לחזק\b)", chunk):
            parts.extend(SENT_RE.split(action_chunk))
    return [p.strip() for p in parts if p.strip()]


def _fraction(clause):
    """Exit fraction of the REMAINING position, and whether it was stated."""
    for w, f in WORD_FRACTIONS:
        if w in clause:
            return f, True
    # numeric percent, ignoring risk-sizing percentages
    masked = _RISK_PCT.sub(" ", clause)
    # Exit fractions are whole numbers ("50%", "30%"). A decimal percentage is
    # risk sizing or a P&L remark ("a small loss of 0.05%"), never an exit size.
    m = re.search(r"(?<![\d.])(\d{1,3})\s*(?:%|אחוז)", masked)
    if m:
        v = int(m.group(1))
        if 10 <= v <= 100:
            return v / 100.0, True
    return None, False


# The author sometimes hands the decision to the reader instead of giving an order.
# Left alone, "worth CONSIDERING reducing half of every position - your call of course"
# reduced every open position by half.
#
# Two shapes, because they sit in different places. A deliberation verb governs from
# BEFORE the management verb, exactly as SPECTATOR does - the author routinely hedges and then
# gives a real instruction in the next clause, so a marker after the verb does not
# disqualify it. An explicit hand-off of the decision ("your call") is a disclaimer and
# is normally appended, so it counts wherever it appears.
#
# `יש לשקול` is deliberately NOT here - rule 10, confirmed by a native reader. It was put
# to the user rather than inferred from rule 4: that ruling was given about "אפשר לסגור",
# a different verb, and stretching it to cover a construct the user was never asked about
# is the overreach this project keeps catching. Asked directly, the answer was that "יש ל"
# carries the obligation and "לשקול" is politeness.
HEDGE_BEFORE_RE = re.compile(
    r"(?<!יש\s)(?:"
    r"שווה\s+לשקול|אפשר\s+לשקול|כדאי\s+לשקול|מומלץ\s+לשקול"
    r"|רצוי\s+ל(?!ה?שאיר)|מציע\s+ל|ממליץ\s+ל"
    r"|הייתי\s+(?:סוגר|מפחית|מממש|שוקל|חושב|יוצא)"
    r")")
HEDGE_ANYWHERE_RE = re.compile(
    r"(?:שיקול\s+שלכ[םן]|שיקול\s+שלך|החלטה\s+שלכ[םן]|כל\s+אחד\s+יחליט"
    r"|לפי\s+שיקול\s+דעתכ[םן])")


def _hedge_governs(clause, vp):
    """The hedge that makes this clause a suggestion, or None."""
    before = HEDGE_BEFORE_RE.search(clause[:vp])
    if before:
        return clause[before.start():].strip(" ,;:")
    anywhere = HEDGE_ANYWHERE_RE.search(clause)
    return clause[anywhere.start():].strip(" ,;:") if anywhere else None


def _condition(clause):
    """Return the unresolved precondition governing the action, if one is stated."""
    vp = _verb_pos(clause)
    if vp is None:
        return None
    prefix = clause[:vp]
    m = CONDITION_RE.search(prefix)
    if m:
        return prefix[m.start():].strip(" ,;:")
    # A hedge is a precondition on the reader's judgement rather than on the market,
    # but it has the same consequence: the action is not deterministic, so it must not
    # mutate the book. Handled here rather than as a separate suppression so it lands
    # in the same audit bucket as every other unresolved condition.
    hedge = _hedge_governs(clause, vp)
    if hedge:
        return hedge
    price_condition = PRICE_CONDITION_RE.search(prefix)
    if price_condition:
        return prefix[price_condition.start():].strip(" ,;:")
    choice = CHOICE_RE.search(clause)
    if choice:
        return clause[choice.start():].strip(" ,;:")
    # In an explicit either/or choice the optional phrase follows the reduction verb:
    # "decide whether you want to reduce half or leave the full size". It is not a
    # deterministic management instruction and therefore cannot mutate the book.
    optional = OPTIONAL_SUFFIX_RE.search(clause)
    return clause[optional.start():].strip(" ,;:") if optional else None


def _condition_type(clause, condition):
    if condition and POSITION_GUARD_RE.search(clause):
        return "position_exists"
    return "unresolved" if condition else None


def _stop_level(clause):
    """Read a price after STOP without mistaking a quoted current price for the stop."""
    masked = _RISK_PCT.sub(" ", clause)
    m_stop = STOP_TOKEN_RE.search(masked)
    if not m_stop:
        return None
    tail = masked[m_stop.end():]
    for scope in ALL_TRADES:
        tail = tail.replace(scope, " ")
    # "STOP in the 2 trades to / [price]": 2 selects positions; the following bare
    # number is the level. It must never become an absolute stop at price 2.
    tail = re.sub(r"\b\d+\s+ה?עסקאות\b", " ", tail)
    # When the verb follows STOP, selector counts and ticker digits before that verb
    # are not price levels: "STOP 3 last trades move to" + [price], "STOP" + [symbol].
    m_action = re.search(
        r"(?:לצמצם|מתעדכן|לעדכן|להזיז|לעבור|עובר|עולה|מתקדם)", tail)
    if m_action:
        tail = tail[m_action.end():]
    m_num = re.search(r"(?<![A-Za-z0-9])\d[\d,]*(?:\.\d+)?(?![A-Za-z0-9])", tail)
    if not m_num:
        return None
    # A widening bug can come from reading "current price [price]" as a stop.
    before = tail[:m_num.start()]
    before_lower = before.lower()
    if ("מחיר נוכחי" in before or "מחיר השוק" in before
            or "current price" in before_lower or "market price" in before_lower):
        return None
    try:
        return float(m_num.group(0).replace(",", ""))
    except ValueError:
        return None


# A trade referred to by WHEN IT WAS POSTED, rather than by ordinal. Always singular.
RECENT_POST = ("לפני כמה דקות", "לפני מספר דקות", "לפני כמה רגעים", "לפני דקות",
               "מלפני כמה דקות", "מלפני דקות", "שעלתה כאן", "שעלתה לפני",
               "שפורסמה כאן", "שפרסמתי כאן")


def _selector(clause):
    c = clause
    if any(w in c for w in (
            "שאר העסקאות", "יתר העסקאות", "שאר הפוזיציות", "יתר הפוזיציות")):
        return {"type": "remainder"}
    if any(w in c for w in ["מה שפתוח", "כל העסקאות", "כל הפוזיציות", "כל מה ש",
                            "מכל עסקה", "מכל פוזיציה", "כל עסקה פתוחה", "כל היתרה",
                            "את כל היתרה", "כל יתרת"]):
        return {"type": "all"}

    if any(w in c for w in ("הכי רווחית", "הרווחית ביותר", "העסקה הרווחית")):
        return {"type": "best"}

    m = re.search(r"(\d+)\s*ה?עסקאות\s*ראשונ", c)
    if m:
        return {"type": "n_oldest", "n": int(m.group(1))}
    m = re.search(r"(\d+)\s*ה?עסקאות\s*אחרונ", c)
    if m:
        return {"type": "n_newest", "n": int(m.group(1))}
    if (("שתי עסקאות" in c or "2 עסקאות" in c or "2 העסקאות" in c)
            and "ראשונ" not in c and "אחרונ" not in c):
        return {"type": "n_newest", "n": 2}

    m = re.search(r"סיכון\s*(?:של\s*)?(\d+(?:\.\d+)?)\s*(?:%|אחוז)", c)
    risk = float(m.group(1)) if m else None

    # "the trade posted here a few minutes ago" names ONE object - the most recent
    # thing the author put up - not the whole book. Without this it fell through to "all",
    # and cancelling a trigger posted minutes earlier closed an older gold position
    # instead.
    if any(w in c for w in RECENT_POST):
        return {"type": "newest", "risk": risk}

    if any(w in c for w in ["אחרונה", "האחרונה", "אחרון", "החדשה", "עסקה חדשה", "הוספה אחרונה",
                            "אחורנה"]):
        return {"type": "newest", "risk": risk}
    if any(w in c for w in ["הראשונה", "ראשונה", "קודמת", "הקודמת", "הישנה", "הראשון",
                            "טרייד קודם", "עסקה קודמת"]):
        return {"type": "oldest", "risk": risk}

    for name, wd in WEEKDAY.items():
        if re.search(r"מ\s*יום\s*" + name + r"\b", c) or re.search(r"\bמ" + name + r"\b", c):
            return {"type": "weekday", "weekday": wd, "risk": risk}
    if "מהיום" in c or "של היום" in c:
        return {"type": "today", "risk": risk}
    if "מאתמול" in c or "של אתמול" in c or "אמש" in c:
        return {"type": "yesterday", "risk": risk}
    if risk is not None:
        return {"type": "risk", "risk": risk}
    return {"type": "all"}


def _side(clause):
    has_l, has_s = "לונג" in clause, "שורט" in clause
    if has_s and not has_l:
        return -1
    if has_l and not has_s:
        return 1
    return 0


def _scoped_side(clause):
    """Direction the instruction applies to, honouring an exception.

    "להוריד חצי מכל מה שפתוח (חוץ מהשורט החדש)" reduces the LONGS. The only
    side word in the clause names the position being spared, so reading it at face
    value reduced precisely the hedge the author had just excluded.
    """
    span = _except_span(clause)
    if span is None:
        return _side(clause)
    stated = _side(clause[:span[0]] + clause[span[1]:])
    if stated:
        return stated                       # an explicit side outside the exception wins
    excluded = _side(clause[span[0]:span[1]])
    return -excluded if excluded else 0


def parse(text, symbols_in_clause=None, default_partial=0.5, target_id=None):
    """Return instruction dicts. `symbols_in_clause` maps clause -> [symbols].

    `target_id` is the id named by the input message's optional `reply_to` field.
    It is carried through untouched so the engine can bind an instruction to
    one specific order rather than to every order on the instrument.
    """
    out = []
    clauses = _clauses(text)
    message_risks = [float(m.group(1)) for clause in clauses
                     for m in [RISK_IN_CLAUSE.search(clause)] if m]
    unique_message_risks = sorted(set(message_risks))
    message_risk = unique_message_risks[0] if len(unique_message_risks) == 1 else None
    total_risk_match = re.search(
        r"סך\s*הכל\s+סיכון\s*(?:של\s*|ל\s*)?(\d+(?:\.\d+)?)\s*(?:%|אחוז)", text)
    message_total_risk = float(total_risk_match.group(1)) if total_risk_match else None
    preserve_pending = any(w in text for w in (
        "הטריגר עדיין רלוונטי", "הטריגר נשאר רלוונטי", "הטריגר רלוונטי"))
    selector_context = {}
    inherited_selector = None
    for clause in clauses:
        if _verb_pos(clause) is None:
            candidate = _selector(clause)
            # Selector inheritance is only for terse headers ("Gold, last trade").
            # Narrative status lines such as "the first JP trade hit TP" must not
            # leak "first" into a later, unrelated cancellation.
            if candidate["type"] != "all" and len(clause.split()) <= 4:
                inherited_selector = candidate
        elif inherited_selector is not None:
            selector_context[clause] = inherited_selector
            inherited_selector = None

    for idx, c in enumerate(clauses):
        raw_clause = c
        inherited_symbols = ((symbols_in_clause or {}).get(raw_clause)
                             or (symbols_in_clause or {}).get(c))
        plan_cancel = bool(PLAN_CANCEL_RE.search(c))
        if (((any(w in c for w in COMMENTARY)
              or (NON_ACTION_REDUCTION_RE is not None
                  and NON_ACTION_REDUCTION_RE.search(c))) and not plan_cancel)
                or _spectator_governs(c)):
            continue
        if (any(c.lstrip().startswith(w) for w in REASON)
                and "," not in c):
            continue

        dual = DUAL_STOP_RE.search(c)
        if dual and any(w in c[:dual.start()] for w in PARTIAL):
            first = c[:dual.start()].strip()
            second = c[dual.start():].strip()
            if second.startswith("ו"):
                second = second[1:].lstrip()
            scoped = {first: inherited_symbols, second: inherited_symbols}
            out.extend(parse(first, scoped, default_partial, target_id))
            out.extend(parse(second, scoped, default_partial, target_id))
            continue
        dual_reduce = DUAL_REDUCE_RE.search(c)
        if dual_reduce and any(w in c[:dual_reduce.start()] for w in FULL_CLOSE):
            first = c[:dual_reduce.start()].strip()
            second = c[dual_reduce.start():].strip()
            if second.startswith("ו"):
                second = second[1:].lstrip()
            scoped = {first: inherited_symbols, second: inherited_symbols}
            out.extend(parse(first, scoped, default_partial, target_id))
            out.extend(parse(second, scoped, default_partial, target_id))
            continue
        # (6) cancelling a resting order is not a position exit - it cancels the
        # pending entry instead, which we now model explicitly. Passive phrasing
        # ("הטרייד מבוטל" - the trade is cancelled) calls off a plan; the active
        # "לסגור" closes a live position and is handled below.
        # "יש לבטל טייק פרופיט" - remove the target and let the remainder run.
        # This is NOT a cancellation of the trade: the position stays open with no
        # take-profit. It parsed to nothing, so such messages left their
        # positions exiting at a target that had explicitly been removed.
        if (any(w in c for w in CANCEL)
                and any(w in c.lower() for w in TAKE_PROFIT)):
            out.append({"kind": "cancel_target", "frac": 0.0, "frac_stated": True,
                        "selector": _selector(c), "side": _side(c), "be": False,
                        "new_stop": None, "clause": c[:160], "target_id": target_id,
                        "condition": None, "condition_type": None,
                        "symbols": (symbols_in_clause or {}).get(c)})
            continue
        passive_cancel = any(w in c for w in (
            "מבוטל", "מבוטלת", "בוטל", "בוטלה", "נמחק", "נמחקה"))
        # Which noun the cancellation actually governs is decided by position, not by
        # mere presence: "ביטול עסקה אחרונה" ... "עולה טריגר" cancels the TRADE
        # and then announces a new trigger, so the later "טריגר" must not scope it.
        _pos = lambda words: min([c.find(w) for w in words if c.find(w) >= 0] or [10**9])
        _p_pending, _p_trade = _pos(PENDING), _pos(CANCEL_TRADE)
        pending_state = (bool(re.search(
            r"(?:עסקה|עסקת|פקודה|הזמנה|שורט|לונג).{0,24}"
            r"(?:בהמתנה|ממתינ(?:ה|ה)?)", c))
            or any(w in c for w in (
                "עסקת הלימיט", "פקודת הלימיט", "הכניסה מבוטלת",
                "הכניסה מבוטל", "לא תפס", "לא נכנס לפעולה",
                "לא נכנסה לפעולה")))
        names_pending = pending_state or (_p_pending < 10**9 and _p_pending < _p_trade)
        names_trade = _p_trade < 10**9
        # ...unless the author says outright what to do with the live position. Position is a
        # heuristic; an explicit close verb is not. In
        #   [symbol] + "מבוטל כרגע" ... [new trigger] ... "משמע לסגור עסקה"
        # the trigger the author names is one the author is about to PUBLISH, so it precedes "עסקה"
        # and the positional rule read it as a pending-order cancellation. The clause
        # ends by spelling out the opposite. An explicit verb wins over word order.
        explicit_close = any(w in c for w in FULL_CLOSE)
        if plan_cancel:
            out.append({"kind": "cancel_scheduled", "frac": 0.0,
                        "frac_stated": True,
                        # "the reduction is cancelled" refers to the latest queued
                        # management plan, not every historical reduction.
                        "selector": selector_context.get(
                            raw_clause, {"type": "newest"}),
                        "side": _side(c), "be": False, "close_filled": False,
                        "new_stop": None, "clause": c[:160], "condition": None,
                        "target_id": target_id, "symbols": inherited_symbols})
            continue
        if preserve_pending and (passive_cancel or any(w in c for w in CANCEL)):
            continue
        if passive_cancel or (any(w in c for w in CANCEL)
                              and (names_pending or names_trade)):
            never_activated = any(w in c for w in (
                "לא נכנסה לפעולה", "לא נכנס לפעולה", "לא הופעלה", "לא הופעל"))
            selector = _selector(c)
            if selector["type"] == "all" and raw_clause in selector_context:
                selector = selector_context[raw_clause]
            condition = _condition(c)
            out.append({"kind": "cancel_pending", "frac": 0.0, "frac_stated": True,
                        "selector": selector if names_trade else {"type": "all"},
                        "side": _side(c), "be": False,
                        # Naming the trigger keeps it to the resting order (rule 6).
                        # Naming the trade closes it too, if it has already filled.
                        "close_filled": (explicit_close or not names_pending)
                                        and not never_activated,
                        "new_stop": None, "clause": c[:160],
                        "condition": condition,
                        "condition_type": _condition_type(c, condition),
                        "target_id": target_id,
                        "symbols": inherited_symbols})
            continue

        # A reduction OF THE STOP or OF THE RISK is not a reduction of the position.
        stop_intent = _stop_move_intent(c)
        position_clause = REDUCTION_OBJECT_RE.sub(" ", c)
        position_clause = NOUN_REDUCTION_RE.sub(" ", position_clause)

        kind = None
        if any(w in c for w in RESIZE) and "לעדכן" in c:
            kind = "update_pending_risk"
        elif RESTORE_RE.search(c):
            kind = "restore_reduction"
        elif REDUCE_RISK_RE.search(c):
            kind = "reduce_risk"
        elif any(w in position_clause for w in SIZE_UP):
            kind = "size_up"
        elif (_fraction(position_clause)[0] is not None
              and (any(w in position_clause for w in PARTIAL)
                   or CLOSE_FRACTION_RE.search(position_clause))):
            # A stated exit quantity outranks generic close vocabulary: "אפשר לסגור
            # חצי" is a half close. "בחצי הפסד" does not contain a partial verb and
            # therefore remains a full exit.
            kind = "partial_close"
        elif any(w in position_clause for w in FULL_CLOSE):
            kind = "full_close"
        elif any(w in position_clause for w in PARTIAL):
            kind = "partial_close"
        elif stop_intent:
            kind = "stop_move"
        elif _all_trades_stop(clauses, idx):
            # Checked last so an explicit verb always wins: "יש לממש חצי מכל עסקה
            # פתוחה" is a partial close, not a stop move, even though the scope
            # phrase matches.
            kind = "stop_move"
        if kind is None:
            continue

        m_risk = RISK_IN_CLAUSE.search(c)
        add_risk = float(m_risk.group(1)) if m_risk else None
        risk_target = None
        if kind == "reduce_risk":
            if add_risk is not None:
                risk_target, add_risk = add_risk, None
            else:
                risk_target = _tier_risk(c, ("סיכון ",))
        elif kind == "size_up":
            if (add_risk is not None and re.search(
                    r"(?:להגדיל\s+(?:את\s+)?ה?סיכון\s+ל|לסיכון)", c)):
                risk_target, add_risk = add_risk, None
            elif message_total_risk is not None:
                risk_target, add_risk = message_total_risk, None
            elif _tier_risk(c, ("רמת ", "לסיכון ", "לעסקת ")) is not None:
                risk_target = _tier_risk(c, ("רמת ", "לסיכון ", "לעסקת "))
                add_risk = None
            elif (add_risk is None and message_risk is not None
                  and any(w in c for w in ("להוסיף עסקת", "הוספה לעסקה"))):
                add_risk = message_risk
            if add_risk is None and risk_target is None:
                # The direction is clear but the amount is not. Defaulting to another
                # full-risk leg invents exposure, so surface it for adjudication.
                kind = "ambiguous_size_up"

        frac, stated = 1.0, True
        if kind == "partial_close":
            # (1) "the whole trade" makes it a full close
            if any(w in c for w in WHOLE):
                kind, frac = "full_close", 1.0
            else:
                f, st = _fraction(c)
                if f is not None:
                    frac, stated = f, st
                elif not any(w in c for w in PARTIAL_MARKERS):
                    selector_type = _selector(c)["type"]
                    concrete_position = selector_type not in ("all", "risk", "remainder")
                    if "לממש" in c or concrete_position:
                        # Confirmed semantics: a bare "realise" is everything, and
                        # "reduce the last addition" removes that selected leg in full.
                        kind, frac = "full_close", 1.0
                    else:
                        # "prefer to reduce risk" gives no executable quantity. The
                        # old 100% default invented a full liquidation; keep it visible
                        # for audit but make it non-mutating.
                        kind, frac, stated = "ambiguous_reduction", 0.0, False
                else:
                    frac, stated = default_partial, False   # "עוד חלק", no amount
                if kind == "partial_close" and frac >= 1.0:
                    kind = "full_close"

        new_stop = None
        stop_offset, offset_side = None, 0
        if kind == "stop_move":
            # A distance from entry is not a price and must be recognised before
            # anything tries to read it as one.
            stop_offset, offset_side = relative_stop(c)
            if stop_offset is None:
                new_stop = _stop_level(c)
                if new_stop is None:
                    # [symbol] + "יש לעדכן סטופ ל:" / newline / [price] - the splitter
                    # cuts the value onto its own line, which silently turned the whole
                    # instruction into a no-op. The scope may take a line of its own
                    # too ("סטופ" / "כל העסקאות" / [price]).
                    new_stop = _bare_level_after(clauses, idx)
                if new_stop is None:
                    # A scope phrase and a verb between the keyword and the level put
                    # ~20 characters between them. Rather than widening the window - which
                    # swallowed a quoted current price after the scope - delete the scope
                    # phrase and re-run the narrow search on what is left.
                    stripped = _RISK_PCT.sub(" ", c)
                    for w in ALL_TRADES:
                        stripped = stripped.replace(w, " ")
                    m = re.search(r"(?:סטופ|STOP)\D{0,14}(\d[\d,\.]*)", stripped,
                                  re.IGNORECASE)
                    if m:
                        try:
                            new_stop = float(m.group(1).replace(",", ""))
                        except ValueError:
                            new_stop = None

        # "מתחת לכניסה" contains "לכניסה", so a relative stop looks like breakeven to
        # a substring test. An explicit distance always wins.
        be = any(b in c for b in BREAKEVEN) and stop_offset is None

        if kind == "stop_move" and new_stop is None and stop_offset is None and not be:
            # A stop move that names neither a level nor breakeven changes nothing, and
            # emitting it is not harmless: the engine advances the position cursor for
            # every instruction it processes, so a no-op still skips the bars it steps
            # over and can turn a stop into a timeout under delayed execution.
            continue

        selector = _selector(c)
        if selector["type"] == "all" and raw_clause in selector_context:
            selector = selector_context[raw_clause]

        condition = _condition(c)
        # Realise AT take profit describes a future exit, not an immediate trim.
        if kind in ("full_close", "partial_close", "ambiguous_reduction") and re.search(
                r"(?:לממש|לסגור).*?ב(?:טייק\s*פרופיט|יעד|TP\b)", c, re.IGNORECASE):
            continue
        if kind == "restore_reduction" and condition is None:
            # The restore condition is sometimes stated after the verb: "should add
            # it back once futures cross the failure point".
            trailing = re.search(r"(?:ברגע\s+ש|אם\b|כאשר\b|כש(?=\S)).*$", c)
            if trailing:
                condition = trailing.group(0).strip(" ,;:")
        out.append({"kind": kind, "frac": frac,
                    "frac_stated": (stated if kind in (
                        "partial_close", "ambiguous_reduction") else True),
                    "selector": selector, "side": _scoped_side(c),
                    "be": be, "new_stop": new_stop,
                    "stop_offset": stop_offset, "offset_side": offset_side,
                    "add_risk": add_risk,
                    "risk_target": risk_target,
                    "new_risk": add_risk,
                    "clause": c[:160],
                    "condition": condition,
                    "condition_type": _condition_type(c, condition),
                    "target_id": target_id,
                    "symbols": inherited_symbols,
                    # An exception clause must never broaden to "every instrument in
                    # this message": that is exactly how "סוגרים חוץ מסילבר" closed
                    # the silver it was written to spare.
                    "scope_excluded": bool(_except_span(c))})
    return out


def select(positions, sel, msg_time, price=None):
    pool = list(positions)
    risk = sel.get("risk")
    if risk is not None:
        exact = [p for p in pool if abs(p["risk"] - risk) < 1e-6]
        pool = exact
    t = sel["type"]
    if t in ("all", "risk"):
        return pool
    if t == "newest":
        return pool[-1:] if pool else []
    if t == "oldest":
        return pool[:1] if pool else []
    if t == "best":
        if not pool:
            return []
        if price is None:
            # Callers selecting pending orders have no mark-to-market price. No such
            # real selector exists in the corpus; remaining conservative is safer than
            # silently choosing an arbitrary order.
            return []
        def open_pnl(p):
            return (p.get("units", 1.0) * p.get("remaining", 1.0)
                    * p.get("dir", 0) * (price - p.get("entry", price)))
        return [max(pool, key=open_pnl)]
    if t == "n_newest":
        return pool[-sel["n"]:] if pool else []
    if t == "n_oldest":
        return pool[:sel["n"]] if pool else []
    if t == "remainder":
        # The engine supplies the positions already addressed earlier in the same
        # message. Without that context the least surprising standalone behaviour is
        # to leave the pool untouched.
        return pool
    if t == "today":
        return [p for p in pool if p["ts"].date() == msg_time.date()]
    if t == "yesterday":
        return [p for p in pool if (msg_time.date() - p["ts"].date()).days == 1]
    if t == "weekday":
        return [p for p in pool if p["ts"].weekday() == sel["weekday"]]
    return pool
