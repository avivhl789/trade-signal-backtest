# -*- coding: utf-8 -*-
"""Track the HELD FRACTION per ticker, and derive trades from changes in it.

Why state and not events. Extracting trades from verbs alone leaves most actions
unmatched: exits with no prior open, opens never closed, adds and cuts on tickers that
were never opened. Authors do not narrate every entry. What they do narrate, constantly,
is where they stand:

    [symbol] בפנים שליש כמות    in, one third
    [symbol] כמות מלאה           full
    [symbol] בחוץ                out

Such declarations can outnumber the actions themselves. Each one SETS the level; the implied trade is the difference from the previous level. Orphan
cuts and exits then resolve themselves, because a cut from a level we already know about
needs no matching open.

Sizing (operator's stated working assumptions):
  * equal NOTIONAL per unit of position - "full quantity" is a fixed dollar amount.
    Risk-based sizing is impossible here: very few messages name a stop, though exits
    are announced, which is what makes the notional model workable.
  * fractional shares allowed, so a third of a position needs no rounding.
  * no account limit; concurrent positions are unconstrained.

THE ONE GRAMMATICAL RULE THAT CHANGES NUMBERS

    הפחתתי חצי          reduced BY half   ->  level *= 0.5
    אפחית לחצי          reduce TO half    ->  level  = 0.5

A single letter, ל, separates them. Getting it backwards on a position held at 1.0 is
the difference between ending at 0.5 and ending at 0.5 - harmless - but from 0.6 it is
0.3 versus 0.5, and the error compounds across every later trade on that ticker.
"""
import datetime as dt
import re

# Defaults for the vague quantities. "חלק" and "קצת" are common and they carry
# no number. These are assumptions, isolated here so their effect can be measured by
# changing them rather than by re-reading the code.
DEFAULT_ADD = 0.25          # "הוספתי עוד חלק"  - added another part
DEFAULT_CUT = 0.5           # "מימשתי חלק"      - realised a part
DEFAULT_ENTRY = 0.5         # "[symbol] בפנים"      - in, size unstated

# "to" rather than "by": לחצי, לשליש, לכמות קטנה, ירדתי ל...
TO_LEVEL_RE = re.compile(r"\bל\s*(?:חצי|שליש|רבע|כמות|שלושה|[0-9]+\s*/\s*[0-9]+)")

# Order matters: "לא בפנים" contains "בפנים", so the negative forms must be tested
# first. Without that, "[symbol A] [symbol B] כרגע לא בפנים" - currently NOT inside - opened a
# half position in both.
OUT_WORDS = ["לא בפנים", "לא מחזיק", "לא נמצא", "בחוץ", "אין לי", "סגרתי הכל",
             # "[symbol] עדיין לא נכנס, רק [price] בכמות קטנה" - has NOT entered; the
             # quantity is what would be taken. The negation sits on the verb, so
             # classify() returned None and the trailing "כמות קטנה" opened 25%
             # of a position that never existed, and it never closed.
             "לא נכנס", "לא נכנסתי", "טרם נכנס", "לא קניתי",
             "יצאתי לגמרי", "אאוט", "מחוץ לעסקה"]
IN_WORDS = ["בפנים", "מחזיק"]


# A REMINDER IS NOT A TRADE.
#
# "[symbol] נכנסתי בחצי כמות"          entered, HALF quantity          -> 0.50
# "[symbol] אני בכמות קטנה מזכיר"      I'm in a small quantity, NB     -> 0.25, a 25% sale
#
# Hours apart, describing one position. The second sentence contains no verb and
# announces nothing; "מזכיר" - I remind you - asserts that nothing has changed. But
# size vocabulary is loose, "half" and "small" can be the same holding, and
# treating the looser word as a precise level booked a sale that never happened.
#
# The rule is not "reminders are inert". Some reminders in a window are the
# only evidence their position exists at all - "[symbol] מזכיר שנשארתי בפנים בכמות קטנה
# מהטריגר של אתמול" is how we learn the author holds [symbol] - and suppressing those would delete real
# positions. What outranks what is the point: an explicit VERB is precise, a reminder is
# not, so a reminder cannot overrule a verb that is still fresh.
#
# Fresh means 48 hours. A reminder days after the verb is old enough that
# an unannounced trim is a real possibility, and it is left alone.
RESTATEMENT = ["מזכיר", "להזכיר", "תזכורת", "למען ההבהרה", "לחידוד", "מדגיש"]
RESTATE_WINDOW = dt.timedelta(hours=48)
VERBS = ("open", "add", "cut", "exit")


def is_restatement(text):
    return any(w in (text or "") for w in RESTATEMENT)


def _when(ts):
    try:
        return dt.datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _clamp(x):
    return max(0.0, min(1.0, x))


def apply(level, rec, text):
    """Return the new held fraction after this record, given the current one.

    `rec` is a record from actions.extract(); `text` is the raw message, needed for the
    to/by distinction and for the bare state words.
    """
    kind = rec["kind"]
    size = rec["size_level"]

    # Plans and ambiguous present-tense forms never move the book. This is the whole
    # point of the tense rule in actions.py; state must not quietly undo it.
    if kind in ("plan", "ambiguous"):
        return level

    if (kind == "exit" and size is not None and 0 < size < 1
            and re.search(r"חצי|רבע|שליש|\d\s*/\s*\d", rec.get("size_word") or "")):
        return _clamp(size if TO_LEVEL_RE.search(text) else level * (1.0 - size))
    if kind == "exit" or any(w in text for w in OUT_WORDS):
        return 0.0

    if kind == "open":
        return _clamp(size if size is not None else DEFAULT_ENTRY)

    if kind == "add":
        if TO_LEVEL_RE.search(text) and size is not None:
            return _clamp(size)
        return _clamp(level + (size if size is not None else DEFAULT_ADD))

    if kind == "cut":
        if TO_LEVEL_RE.search(text) and size is not None:
            return _clamp(size)
        frac = size if size is not None else DEFAULT_CUT
        return _clamp(level * (1.0 - frac))

    return level


def declared_level(text, size_level):
    """The level a bare STATE declaration asserts, or None if it is not one.

    "[symbol] בפנים שליש כמות" -> 0.334    "[symbol] כמות מלאה" -> 1.0    "[symbol] בחוץ" -> 0.0
    """
    if any(w in text for w in OUT_WORDS):
        return 0.0
    if any(w in text for w in IN_WORDS):
        return size_level if size_level is not None else DEFAULT_ENTRY
    if size_level is not None:
        return size_level
    return None


def walk(records):
    """Replay records in time order; yield one implied trade per level change.

    `records` are dicts carrying at least: ts, ticker, kind, size_level, text, and a
    boolean `state_only` marking bare declarations that have no verb.

    Yields dicts: ts, ticker, delta (signed change in held fraction), level_before,
    level_after, reason.
    """
    level = {}
    direction = {}
    last_verb = {}                  # ticker -> when an explicit verb last set the level
    # A flat-book record has no ticker. Sorting it LAST within its timestamp means a
    # close stated in the same message ("[symbol] סגרתי, כרגע ללא עסקאות") is applied first
    # and the sweep then collects whatever that message did not name.
    for r in sorted(records, key=lambda x: (x["ts"], x["ticker"] or "￿")):
        t = r["ticker"]

        # A flat declaration: the author states they hold nothing across the whole book.
        # Every position still open is closed here. This is the only rule in the study
        # that acts on tickers its own message does not mention, which is exactly why
        # it is worth what it is worth - it is the only thing that ever closes a
        # position the author opened and then went silent about.
        if r.get("kind") == "flat":
            for t2 in sorted(level):
                before2 = level[t2]
                if before2 <= 1e-9:
                    continue
                level[t2] = 0.0
                yield dict(ts=r["ts"], ticker=t2, delta=-before2,
                           direction=direction.get(t2, "long"),
                           level_before=before2, level_after=0.0, reason="flat",
                           id=r.get("id"), text=r.get("text", "")[:200],
                           leveraged=None, inverse=None)
            continue

        before = level.get(t, 0.0)
        if r.get("kind") in ("plan", "ambiguous"):
            continue
        old_side = direction.get(t, "long")
        stated_side = r.get("direction")
        # An exit explicitly naming another side cannot close this position.
        if before > 1e-9 and r.get("kind") in ("cut", "exit") and stated_side and stated_side != old_side:
            continue

        # Read the record's OWN clause, never the whole message. Scoping the size but
        # not the state left [symbol] opening at the default level, because declared_level
        # found "בפנים" sitting in another ticker's clause three sentences away.
        scope = r.get("clause") or r["text"]

        if r.get("state_only"):
            # A declaration with no verb states where the author stands. It is authoritative:
            # if it disagrees with our running level, the author is right and we are wrong -
            # UNLESS it is a reminder arriving hot on the heels of an explicit verb,
            # in which case it is re-describing what the verb already told us.
            if is_restatement(scope):
                seen_at, now = last_verb.get(t), _when(r["ts"])
                if seen_at and now and (now - seen_at) <= RESTATE_WINDOW:
                    continue
            after = declared_level(scope, r["size_level"])
            if after is None:
                continue
            reason = "declared"
        else:
            after = apply(before, r, scope)
            reason = r["kind"]

        after = _clamp(after)
        new_side = stated_side or (old_side if before > 1e-9 else "long")
        if before > 1e-9 and after > 1e-9 and new_side != old_side:
            # A reversal is a close followed by a new entry, even at equal size.
            yield dict(ts=r["ts"], ticker=t, delta=-before, direction=old_side,
                       level_before=before, level_after=0.0, reason="reverse_close",
                       id=r.get("id"), text=r.get("text", "")[:200],
                       leveraged=r.get("leveraged"), inverse=r.get("inverse"))
            before = 0.0
        if abs(after - before) < 1e-9:
            continue
        level[t] = after
        direction[t] = new_side
        if reason in VERBS:
            when = _when(r["ts"])
            if when:
                last_verb[t] = when
        yield dict(ts=r["ts"], ticker=t, delta=after - before,
                   direction=new_side,
                   level_before=before, level_after=after, reason=reason,
                   id=r.get("id"), text=r.get("text", "")[:200],
                   leveraged=r.get("leveraged"), inverse=r.get("inverse"))
