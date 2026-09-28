# -*- coding: utf-8 -*-
"""Parse user-provided messages into structured signals + management events.

Writes:
  data/parsed/entries.csv     one row per entry signal
  data/parsed/events.csv      one row per management instruction
  data/parsed/unparsed.csv    entry-looking messages we could not fully read
  data/parsed/coverage.json   headline coverage numbers
"""
import json, io, glob, os, re, sys, csv, collections, hashlib, contextlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lexicon
import instructions as ins
import message_input
from lexicon import (SYMBOLS, LONG, SHORT, IMMEDIATE, MANAGE, BREAKEVEN,
                     is_silver_metal, plausible)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT  = os.path.join(ROOT, "data", "parsed")
# CONFIGURE ME: directory containing platform-neutral message JSON, and the logical
# stream ids to read. These must match the values in book_engine.py. The input files
# must already contain only messages the operator is authorised to analyse.
INPUT_DIR = os.environ.get("ENGINE_MESSAGE_DIR", os.path.join(ROOT, "data", "raw"))
SOURCE_STREAMS = frozenset(("YOUR_MAIN_STREAM_ID", "YOUR_SECOND_STREAM_ID"))

# --- number extraction -------------------------------------------------------
# Levels appear after a keyword, often on the following line, sometimes with a
# stray space inside the number ("82 5" for 82.5) or a thousands comma.
NUM_BODY = r"[0-9][0-9,\. ]{0,12}[0-9]|[0-9]"
NUM = r"(" + NUM_BODY + r")"

def clean_number(raw):
    """Return (value, flag). flag is set when the text was ambiguous."""
    s = raw.strip().replace(",", "")
    if re.fullmatch(r"\d+\.\d+", s):
        return float(s), ""
    if re.fullmatch(r"\d+", s):
        return float(s), ""
    m = re.fullmatch(r"(\d+)\s+(\d+)", s)      # "82 5" / "4 275"
    if m:
        a, b = m.groups()
        if len(b) == 1:                         # most likely a decimal typo
            return float(a + "." + b), "space_decimal"
        return float(a + b), "space_thousands"  # e.g. "4 275" -> 4275
    m = re.fullmatch(r"(\d+)\.\s+(\d+)", s)   # "73. 5" -> 73.5
    if m:
        return float(m.group(1) + "." + m.group(2)), "space_decimal"
    return None, "unreadable"

DIST_WORD = r"(?:פיפס|נקודות|נקודה|pips?|points?)"
DIST_MARK = re.compile(r"\s*(?:עד\s*(?:" + NUM_BODY + r")\s*)?" + DIST_WORD,
                       re.IGNORECASE)

def find_level(text, keywords):
    """Return (value, flag, is_distance).

    Two formats coexist: absolute levels ("STOP 4275") and distances in points
    ("STOP 600 נקודות"). Distances are feed-independent, so keep them tagged.
    """
    for kw in keywords:
        pat = (kw + r"\s*[:\-–]?\s*\n{0,2}\s*"
               r"(?:(?P<distance_before>" + DIST_WORD +
               r"(?:\s+" + DIST_WORD + r")?)|(?:סופר\s+קצר)|"
               r"(?:לפי\s*(?:חשבון\s+הערכה|evaluation)))?"
               r"\s*\n{0,2}\s*(?P<number>" + NUM_BODY + r")")
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val, flag = clean_number(m.group("number"))
            is_dist = (m.group("distance_before") is not None
                       or bool(DIST_MARK.match(text[m.end():m.end() + 40])))
            return val, flag, is_dist
    return None, "", False


def repair_scaled_price(symbol, value):
    """Repair one missing/extra order of magnitude when exactly one repair is sane.

    This is intentionally unavailable for multi-instrument prose: a Nasdaq level can
    look like a silver price after division, and the level resolver must choose the
    instrument before any typo repair is attempted.
    """
    if symbol is None or value is None or plausible(symbol, value):
        return value, ""
    candidates = []
    for factor in (0.1, 10.0, 100.0, 1000.0):
        candidate = value * factor
        if plausible(symbol, candidate):
            candidates.append(candidate)
    if len(candidates) == 1:
        return candidates[0], "scaled_price_typo"
    return value, ""

# A trigger keyword and its price are often separated by an inline aside, e.g.
# "לימיט לכניסה (כלומר מחכים שהמחיר יגיע)" with the number on the next line.
# Bounded: no digits, at most 60 characters, at most two line breaks.
TRIG_GAP = r"[^\d\n]{0,60}\s*\n{0,2}\s*"


def find_trigger(text, symbol=None):
    """Trigger/limit price, allowing a short aside between keyword and number.

    Stops and targets are matched tightly, but the author routinely explains a limit order
    inline before giving the price. Because the pattern is loose it can pick up a
    neighbouring number ("LIMIT RANGE 60"), so every candidate is checked against the
    instrument's plausibility envelope and the first credible one wins.
    """
    fallback = None
    for kw in TRIG_KW:
        pat = kw + TRIG_GAP + NUM
        for m in re.finditer(pat, text, re.IGNORECASE):
            val = clean_number(m.group(1))[0]
            if val is None:
                continue
            if symbol is None or plausible(symbol, val):
                return val
            if fallback is None:
                fallback = val
    return None


TRIG_KW = [r"טריגר\s*לכניסה", r"טריגר", r"לימיט", r"limit", r"כניסה\s*ב"]
# "פוטנציאל 1 ל 4", "פוטנציאל 1 ל 3 עד 1 ל 4", "יחס סיכון סיכוי 1:5"
RATIO_RE = re.compile(r"(?:פוטנציאל|יחס[^\d\n]{0,20})\D{0,12}1\s*(?:ל|:)\s*(\d+(?:\.\d+)?)"
                      r"(?:[^\d\n]{0,10}1\s*(?:ל|:)\s*(\d+(?:\.\d+)?))?")
RATIO_MIN, RATIO_MAX = 1.0, 20.0
STOP_KW = [r"STOP", r"סטופ", r"ס\.?ט\.?ו\.?פ"]
TP_LABEL = r"(?:[123]|[ \t]+[123](?=[ \t]*(?:\n|:)))?"
TP_KW   = [r"TP" + TP_LABEL + r"(?![0-9])",
           r"טייק\s*פרופיט" + TP_LABEL, r"טייק", r"מטרה", r"יעד", r"פוטנציאל"]
# "סיכון" is commonly mistyped as "סיכום", and the percent sign is often omitted
# ("סיכום 0.35"), so both are accepted and the value is range-checked instead.
RISK_RE = re.compile(
    r"סיכ(?:ון|ום|ן)\s*(?:(?:קטן|ברמת\s+\S+)\s*)?"
    r"(?:של\s*|ל\s*)?[\s:;,\-–]*([0-9]+(?:\.[0-9]+)?)\s*(?:%|אחוז)?")
# The number is often written first: "0.5 אחוז סיכון".
RISK_REV_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*(?:%|אחוז)\s*סיכ(?:ון|ום|ן)")
RISK_MIN, RISK_MAX = 0.01, 5.0

# Conviction tiers live in lexicon.py - see the CONFIGURE ME note there. Used only
# to sanity-check a parsed risk, never to invent one.
from lexicon import TIER_RISK  # noqa: E402


def _risk_value(raw):
    """Float for a risk token, repairing a missing decimal point.

    "סיכון 025 אחוז" means 0.25%, not 25%. A leading zero on a whole number is
    only ever a dropped decimal point - nobody writes "04" to mean four.

    The repair must not be gated on the value looking too large: "04 אחוז" parses to
    4.0, which sits under RISK_MAX and so was accepted as a 4% trade. It was a 0.4%
    trade, sized at ten times what the author stated.
    """
    try:
        v = float(raw)
    except ValueError:
        return None
    if "." not in raw and raw.startswith("0") and len(raw) > 1:
        try:
            v = float("0." + raw[1:])
        except ValueError:
            return None
    return v


def find_risk(text):
    for rx in (RISK_RE, RISK_REV_RE):
        for m in rx.finditer(text):
            v = _risk_value(m.group(1))
            if v is not None and RISK_MIN <= v <= RISK_MAX:
                return v
    # Where a channel defines tiers (TIER_RISK in lexicon.py), a named tier is a numeric
    # sizing instruction, not a qualitative adjective. Only explicit trade/tier
    # constructions qualify; an ordinary use of the same word does not.
    for tier, value in TIER_RISK.items():
        if re.search(r"(?:עסקה\s+ברמת|עסקה\s+בסיכון|עסקת)\s*" + tier, text):
            return value
    return None

def detect_symbol(text):
    low = text.lower()
    hits = []
    for canon, terms in SYMBOLS:
        for t in terms:
            p = lexicon.term_pos(text, low, t)
            if p >= 0:
                hits.append((p, canon))
                break
    if not hits:
        return None, []
    hits.sort()
    return hits[0][1], [h[1] for h in hits]

def detect_direction(text):
    low = text.lower()
    lp = min([low.find(w) for w in LONG  if low.find(w) >= 0] or [10**9])
    sp = min([low.find(w) for w in SHORT if low.find(w) >= 0] or [10**9])
    if lp == sp == 10**9: return None
    return "long" if lp < sp else "short"

def detect_manage(text):
    out = []
    for kind, words in MANAGE.items():
        if any(w in text for w in words):
            out.append(kind)
    # `lexicon.MANAGE` gates whether a message is management at all, while
    # `instructions.STOP_MOVE` parses it - two lists that have drifted before. A
    # verb-less "stop / all trades / <level>" matches neither, so real stop moves
    # never reached the parser. Ask the parser's own detector instead of adding
    # another spelling to a list that will drift again.
    if "stop_move" not in out:
        clauses = ins._clauses(text)
        if any(ins._all_trades_stop(clauses, i) for i in range(len(clauses))):
            out.append("stop_move")
    # The instruction parser is the semantic authority. Keep the broad lexicon as a
    # cheap discovery aid, but never let a newly-supported wording (for example
    # "להוריד חצי" or "החוצה") remain invisible to the event export.
    for item in ins.parse(text):
        if item["kind"] not in out:
            out.append(item["kind"])
    return out


def manage_rows(r, t, primary, manage):
    """One row per (message, instrument), in order of first mention.

    The author routinely manages two instruments in one message - "[symbol A] להפחית
    חצי / [symbol B] להפחית חצי מההוספה האחרונה" - and binding a single symbol per message
    silently discarded every instruction after the first. A clause that names no
    instrument of its own belongs to the message's primary instrument.
    """
    clauses = ins._clauses(t)
    targets = ins.clause_targets(clauses, lambda c: detect_symbol(c)[1])
    parsed = ins.parse(t, targets)

    # One row per (instruction, instrument), not one per instrument. Collapsing them
    # lost the second action whenever a message carried two: a gold
    # message became a single row holding only one level, hiding the relative stop on the
    # remaining tickets entirely. `events.csv` is the ruler used to score the parser,
    # so anything it cannot represent cannot be reviewed.
    #
    # There is deliberately no fallback. It used to synthesise a row from the
    # `lexicon.MANAGE` keyword hit whenever `ins.parse` returned nothing, which
    # manufactured "ghost" rows for the very clauses the parser had just rejected as
    # commentary - and made the ruler disagree with the engine it was meant to score.
    rows = []
    for it in parsed:
        for symbol in it.get("symbols") or ([primary] if primary else [""]):
            rows.append({
                "symbol": symbol,
                "kinds": it["kind"],
                "to_breakeven": bool(it.get("be")),
                "new_stop": "" if it.get("new_stop") is None else it["new_stop"],
                "stop_offset": "" if it.get("stop_offset") is None else it["stop_offset"],
                "offset_side": it.get("offset_side", 0) if it.get("stop_offset") is not None else "",
                "frac": round(it.get("frac", 1.0), 4),
                "clause": (it.get("clause") or "")[:160],
            })

    return [{**{k: r[k] for k in ("id", "ts", "edited", "stream_id", "reply_to")},
             "seq": seq, "text": t[:500], **row}
            for seq, row in enumerate(rows)]


SEP_RE = re.compile(r"^\s*[*_=-]{1,}\s*$")

def split_signal_blocks(text):
    """Split a message that carries several signals into one block per signal.

    The author separates them either with a line of asterisks, or simply by starting a new
    instrument once the previous signal is complete. A block is considered complete
    when it already has an instrument, a stop and a target; a new instrument after
    that starts the next block.
    """
    lines = text.split(chr(10))
    blocks, cur = [], []

    def state(buf):
        joined = chr(10).join(buf)
        sym, _ = detect_symbol(joined)
        return (sym,
                bool(find_level(joined, STOP_KW)[0] is not None),
                bool(find_level(joined, TP_KW)[0] is not None))

    for line in lines:
        if SEP_RE.match(line) and cur:
            blocks.append(cur); cur = []
            continue
        sym_now, has_stop, has_tp = state(cur) if cur else (None, False, False)
        if sym_now and has_stop and has_tp:
            line_sym, _ = detect_symbol(line)
            if line_sym and line_sym != sym_now:
                blocks.append(cur); cur = []
        cur.append(line)
    if cur:
        blocks.append(cur)

    out = [chr(10).join(b).strip() for b in blocks]
    return [b for b in out if b]


# --- load --------------------------------------------------------------------
def load():
    """All selected, user-provided messages in chronological order."""
    rows = []
    for m in message_input.load(INPUT_DIR, SOURCE_STREAMS):
        rows.append({
            "id": m["id"], "ts": m["timestamp"], "edited": m["edited"],
            "stream_id": m["stream_id"], "stream": m["stream_name"],
            "text": m["content"], "n_att": len(m["attachments"]),
            "reply_to": m["reply_to"],
        })
    return rows

def source_signature():
    """Fingerprint parser sources and raw-export versions for cache invalidation."""
    raw = sorted(glob.glob(os.path.join(INPUT_DIR, "**", "*.json"), recursive=True))
    digest = hashlib.sha256()
    # The selection is part of the input. Without it, correcting a wrong stream id
    # leaves the signature unchanged and the engine quietly reuses the parse built
    # from the wrong one - and a cached empty parse looks exactly like a source with
    # nothing in it.
    digest.update(repr((os.path.abspath(INPUT_DIR),
                        tuple(sorted(SOURCE_STREAMS)))).encode())
    for path in raw:
        stat = os.stat(path)
        digest.update(repr((os.path.relpath(path, ROOT), stat.st_size, stat.st_mtime_ns)).encode())
    for path in (__file__, ins.__file__, lexicon.__file__, message_input.__file__):
        with open(path, "rb") as fh:
            digest.update(fh.read())
    return digest.hexdigest()


def ensure_current():
    """Refresh derived entries when exports or parser rules change."""
    try:
        with open(os.path.join(OUT, "parse_manifest.json"), encoding="utf-8") as fh:
            saved = json.load(fh)
        with open(os.path.join(OUT, "entries.csv"), "rb") as fh:
            entries_hash = hashlib.sha256(fh.read()).hexdigest()
        if saved == {"source": source_signature(), "entries": entries_hash}:
            return
    except (OSError, ValueError):
        pass
    with contextlib.redirect_stdout(sys.stderr):
        main()


def main():
    os.makedirs(OUT, exist_ok=True)
    msgs = load()
    if not msgs:
        # Writing this parse would cache "nothing" against the current signature, and
        # every later run would reuse it. An empty selection is a configuration
        # mistake, not a result.
        raise SystemExit("parse_signals: no messages selected - check SOURCE_STREAMS / INPUT_DIR, and "
                         "that the input files are where you think they are. "
                         "Nothing written.")
    entries, events, unparsed, near_miss = [], [], [], []
    stats = collections.Counter()

    for r in msgs:
        whole = r["text"]
        if not whole.strip():
            stats["empty"] += 1
            continue

        blocks = split_signal_blocks(whole)
        if len(blocks) > 1:
            complete = 0
            for b in blocks:
                sym_b, _ = detect_symbol(b)
                if sym_b and find_level(b, STOP_KW)[0] is not None                         and find_level(b, TP_KW)[0] is not None:
                    complete += 1
            if complete < 2:
                blocks = [whole]
        else:
            blocks = [whole]
        if len(blocks) > 1:
            stats["multi_signal_messages"] += 1

        for t in blocks:

            sym, all_syms = detect_symbol(t)
            direction     = detect_direction(t)
            trig          = find_trigger(t, sym)
            # A stated trigger/limit price is itself an entry instruction. The author often
            # separates the word from the number with an aside:
            #   "לימיט לכניסה (כלומר מחכים שהמחיר יגיע)" then the price on
            #   the following line, so the word and the number are not adjacent.
            has_entry_kw  = (any(k in t for k in IMMEDIATE)
                             or bool(lexicon.IMMEDIATE_RE.search(t))
                             or bool(re.search(r"כניסה\s*(?:ב|ל|:)?\s*\d", t))
                             or trig is not None)
            stop, stop_f, stop_d = find_level(t, STOP_KW)
            tp,   tp_f,  tp_d    = find_level(t, TP_KW)
            target_declared = any(re.search(kw, t, re.IGNORECASE) for kw in TP_KW)

            # Some older Nasdaq cards quote one absolute level from the futures
            # contract and explicitly say to convert it for the evaluation feed, but also give
            # the complete feed-independent distances in prose:
            #   "stop" + [futures price] + "(futures prices; adjust to evaluation),"
            #   + [n] + "point stop, potential" + [n] + "+".
            # Using the futures level directly on the CFD feed is wrong, while dropping the
            # ticket loses the position and every later management instruction.  A
            # number immediately followed by "points stop" is an explicit distance;
            # only under the stated futures-to-evaluation warning may it replace the printed
            # absolute stop.  The implausibly small potential is then the paired
            # target distance.
            futures_to_evaluation = bool(re.search(
                r"(?:מחירי\s+חוזים|חוזים\s+עתידיים).{0,100}"
                r"(?:חשבון\s+הערכה|evaluation)", t,
                re.IGNORECASE | re.DOTALL))
            distance_stop = re.search(
                r"([0-9]+(?:\.[0-9]+)?)\s*נקודות\s*סטופ", t,
                re.IGNORECASE)
            if sym and futures_to_evaluation and distance_stop:
                stop = float(distance_stop.group(1))
                stop_d = True
                stop_f = ";".join(x for x in
                                  (stop_f, "distance_from_futures_card") if x)
                if tp is not None and not tp_d and not plausible(sym, tp):
                    tp_d = True
                    tp_f = ";".join(x for x in
                                    (tp_f, "distance_from_futures_card") if x)

            # Some tickets label the stop distance simply "risk /" + [n] + "points".
            # The percent risk appears inline earlier; only a standalone risk header
            # followed by a points value qualifies for this repair.
            if stop is None and has_entry_kw:
                stop, stop_f, stop_d = find_level(t, [r"סיכון(?=\s*\n)"])

            # An R-ratio written as "1 ל 5" is not a price level - but it is still a
            # target. "פוטנציאל 1 ל 4" says the target sits four stop-widths away, which
            # is fully specified once the stop is known, so keep the ratio and let the
            # engine convert it at fill time. Discarding it dropped complete tickets
            # into unparsed.csv for want of a number the author never intended to write.
            tp_r = None
            if (tp is not None and tp < 10 and not tp_d
                    and re.search(r"\d\s*ל\s*\d", t)):
                tp, tp_f = None, "ratio_not_level"
            if tp is None:
                m_r = RATIO_RE.search(t)
                if m_r:
                    # "1 ל 3 עד 1 ל 4" states a range; take the nearer target.
                    ratios = [float(g) for g in m_r.groups() if g]
                    tp_r = min(ratios) if ratios else None
                    if tp_r is not None and RATIO_MIN <= tp_r <= RATIO_MAX:
                        tp, tp_f = tp_r, "r_multiple"
                    else:
                        tp_r = None

            if tp is not None and not tp_d and tp_f != "r_multiple" and len(all_syms) == 1:
                tp, scale_flag = repair_scaled_price(sym, tp)
                if scale_flag:
                    tp_f = ";".join(x for x in (tp_f, scale_flag) if x)

            # In the later compact cards the author writes the unit once, on the target:
            # "STOP /" + [n] + "/ TP /" + [n] + "points". A value outside the instrument's
            # price envelope cannot be an absolute stop, so the matched target unit
            # safely disambiguates both fields as distances.
            if (sym and stop is not None and not stop_d and tp_d
                    and not plausible(sym, stop) and stop > 0):
                stop_d = True
                stop_f = ";".join(x for x in (stop_f, "distance_from_paired_target") if x)

            risk_val      = find_risk(t)
            risk          = risk_val
            manage        = detect_manage(t)

            # The levels outrank both the Hebrew name and the printed ticker when they
            # disagree, because the numbers are the only part the author retypes. See
            # lexicon.resolve_by_levels for the ambiguous messages that motivate
            # this. Only absolute levels can adjudicate; distances carry no scale.
            level_flag = ""
            if len(all_syms) > 1:
                abs_levels = [v for v, is_dist in
                              ((stop, stop_d), (tp, tp_d or tp_f == "r_multiple"))
                              if v is not None and not is_dist]
                picked = lexicon.resolve_by_levels(all_syms, abs_levels)
                if picked and picked != sym:
                    level_flag = "symbol_from_levels_over_%s" % sym
                    sym = picked

            # A stated side that contradicts the level ordering is a misparse, not a
            # trade. Recording it keeps the failure loud instead of silent.
            dir_flag = ""
            if (stop is not None and tp is not None and not stop_d and not tp_d
                    and tp_f != "r_multiple"):
                implied = lexicon.direction_from_levels(stop, tp)
                if implied and direction and implied != direction:
                    dir_flag = "direction_contradicts_levels"
                elif implied and not direction:
                    direction = implied
                    dir_flag = "direction_from_levels"

            # With an R-multiple target there is no target price to order against, but
            # the stop still sits on the far side of the entry: a trigger above the stop
            # is a long. "יפני בפריצה / טריגר [price] / סטופ [price]" states no side at all.
            if direction is None and trig is not None and stop is not None and not stop_d:
                implied = lexicon.direction_from_levels(stop, trig)
                if implied:
                    direction = implied
                    dir_flag = "direction_from_trigger_and_stop"

            # The author's standard signal card is an order ticket whether or not the author remembers
            # to write "כניסה מיידית": instrument, direction, a stated risk, and a stop
            # and target that both sit inside the instrument's price envelope. Many such
            # messages were sitting in near_miss.csv, found because a blind reader
            # reconstructed trades the parser had never heard of.
            # Absolute levels only - a stop quoted as a distance is handled separately.
            # Two notations, each self-consistent. Absolute levels must sit inside the
            # instrument's envelope; a pair quoted in points ("סטופ 25 נקודות / טייק
            # פרופיט 100 נקודות") carries no scale to check, so it is accepted on the
            # strength of being a matched pair and converted at fill time. A mixed pair
            # is not accepted - that combination has only ever been a misparse.
            have_levels = stop is not None and tp is not None
            card_absolute = (have_levels and sym and not stop_d and not tp_d
                             and plausible(sym, stop) and plausible(sym, tp))
            card_distance = (have_levels and stop_d and tp_d and stop > 0 and tp > 0)
            card_mixed = (have_levels and sym and stop > 0 and tp > 0
                          and ((stop_d and not tp_d and plausible(sym, tp))
                               or (tp_d and not stop_d and plausible(sym, stop))))
            signal_card = (sym and direction and risk_val is not None
                           and (card_absolute or card_distance or card_mixed))
            looks_entry = (has_entry_kw or signal_card) and direction and sym

            # An unpriced "add another position" is represented by the management
            # engine, which inherits the live trade's levels. Treating the same prose
            # as a second market ticket as well would double the addition.
            if (not has_entry_kw
                    and any(k in ("size_up", "restore_reduction") for k in manage)):
                looks_entry = False

            # Some cancellation posts quote the complete old ticket so readers know
            # exactly which resting order is being removed. Its stop/target/risk do
            # not republish it. A replacement is different and remains an entry.
            parsed_manage = ins.parse(t)
            has_cancel = any(i["kind"] == "cancel_pending" for i in parsed_manage)
            quotes_cancelled_ticket = any(w in t for w in (
                "זו העסקה שבהמתנה", "זאת העסקה שבהמתנה",
                "העסקה שבהמתנה:", "הפקודה שבהמתנה:"))
            publishes_replacement = any(w in t for w in (
                "מעלה חדשה", "מעלה עסקה חדשה", "במקום זה", "במקומה"))
            if has_cancel and quotes_cancelled_ticket and not publishes_replacement:
                looks_entry = False

            # Diagnostic bucket. A message carrying instrument, direction, stop AND
            # target is an order ticket in all but name; if it fails the entry-keyword
            # gate that is nearly always a spelling not seen before. These used to fall
            # into `other`, which nothing reviews - a misspelled "כניסה מיידית" hid
            # entries there, and every management message that
            # followed them was orphaned. Recorded, not reclassified.
            if (not looks_entry and sym and direction
                    and stop is not None and tp is not None):
                near_miss.append({"id": r["id"], "ts": r["ts"], "symbol": sym,
                                  "direction": direction, "stop": stop, "tp": tp,
                                  "reason": "no_entry_keyword", "text": t[:500]})

            if (trig is not None and stop is not None and tp is not None
                    and stop_d is False and tp_d is False and direction
                    and tp_f != "r_multiple"):
                between = (stop < trig < tp) if direction == "long" else (tp < trig < stop)
                if not between:
                    trig = None
                    stats["trigger_rejected"] += 1

            implausible = ""
            if sym:
                # Check each field on its own: a distance-quoted stop must not exempt an
                # absolute target from the plausibility envelope.
                # An R-multiple target is a ratio, not a price; range-checking it
                # against the instrument's envelope rejected every ticket that used one.
                tp_rel = tp_d or tp_f == "r_multiple"
                for nm, v, is_dist in (("stop", stop, stop_d), ("tp", tp, tp_rel)):
                    if v is not None and not is_dist and not plausible(sym, v):
                        implausible = nm + "_out_of_range"

            classified_as_management = False
            # Plausibility is an entry-ticket gate, not a reason to promote ordinary
            # management/commentary into the rejected-entry queue.  "stop 3 latest
            # JP trades ->" + [price] used to record the selector count (3) as an
            # impossible entry stop; market outlooks saying "3%-4% potential" did the
            # same with a portfolio percentage.  Keep a complete malformed card loud,
            # but do not manufacture a rejected ticket when no direction/card exists.
            malformed_ticket = bool(implausible and
                                    (looks_entry or
                                     (sym and direction and stop is not None
                                      and tp is not None)))
            if malformed_ticket:
                unparsed.append({"id": r["id"], "ts": r["ts"], "symbol": sym or "",
                    "direction": direction or "", "stop": stop if stop is not None else "",
                    "tp": tp if tp is not None else "", "reason": implausible, "text": t[:500]})
                stats["implausible_level"] += 1
            elif looks_entry and stop is not None:
                entries.append({**{k: r[k] for k in ("id","ts","edited","stream_id","reply_to")},
                    "symbol": sym, "direction": direction, "risk_pct": risk,
                    "entry_type": "market" if (any(k in t for k in IMMEDIATE) or lexicon.IMMEDIATE_RE.search(t)) else ("trigger" if trig is not None else "unspecified"),
                    "trigger": trig if trig is not None else "",
                    "stop": stop, "tp": tp if tp is not None else "",
                    "level_type": ("distance" if (stop_d or tp_d) else "absolute"),
                    "stop_type": ("distance" if stop_d else "absolute"),
                    "tp_type": ("none" if tp is None
                                else "r_multiple" if tp_f == "r_multiple"
                                else "distance" if tp_d else "absolute"),
                    "r_multiple": (round(tp / stop, 2) if (stop_d and tp_d and stop) else ""),
                    "flags": ";".join(x for x in (stop_f, tp_f, level_flag, dir_flag) if x),
                    "multi_symbol": ";".join(all_syms) if len(all_syms) > 1 else "",
                    "text": t[:500]})
                stats["full_entry"] += 1
            elif looks_entry:
                unparsed.append({**{k: r[k] for k in ("id","ts")}, "symbol": sym or "",
                    "direction": direction or "", "stop": stop if stop is not None else "",
                    "tp": tp if tp is not None else "",
                    "reason": "missing_" + ("stop" if stop is None else "") + ("tp" if tp is None else ""),
                    "text": t[:500]})
                stats["entry_incomplete"] += 1
            else:
                classified_as_management = bool(manage)
                if not classified_as_management:
                    stats["other"] += 1

            # Entry and management are independent dimensions. A post can close an
            # old position and publish a replacement, or publish a limit while moving
            # the stop on an existing trade. Both records must survive.
            if manage:
                rows = manage_rows(r, t, sym, manage)
                if looks_entry:
                    rows = [row for row in rows
                            if not (row["kinds"] in (
                                "size_up", "ambiguous_size_up", "restore_reduction")
                                    and row["symbol"] == sym)]
                if rows:
                    events.extend(rows)
                    stats["management"] += 1

    def dump(name, rows):
        p = os.path.join(OUT, name)
        if not rows:
            # An empty reparse must not retain rows from an older input snapshot.
            with io.open(p, "w", encoding="utf-8-sig", newline=""):
                pass
            return
        with io.open(p, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader(); w.writerows(rows)
        print("wrote %-14s %5d rows" % (name, len(rows)))

    dump("entries.csv", entries); dump("events.csv", events); dump("unparsed.csv", unparsed)
    dump("near_miss.csv", near_miss)
    cov = {"messages": len(msgs), **dict(stats)}
    json.dump(cov, io.open(os.path.join(OUT, "coverage.json"), "w", encoding="utf-8"), indent=2)
    with open(os.path.join(OUT, "entries.csv"), "rb") as fh:
        entries_hash = hashlib.sha256(fh.read()).hexdigest()
    with open(os.path.join(OUT, "parse_manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"source": source_signature(), "entries": entries_hash}, fh, indent=2)

    print("\n=== COVERAGE ===")
    for k, v in sorted(stats.items(), key=lambda kv: -kv[1]):
        print("  %-18s %5d  %5.1f%%" % (k, v, 100.0 * v / len(msgs)))
    ec = collections.Counter(e["symbol"] for e in entries)
    print("\n=== ENTRIES BY SYMBOL ===")
    for s, n in ec.most_common(): print("  %-8s %4d" % (s, n))
    fl = collections.Counter(e["flags"] for e in entries if e["flags"])
    if fl: print("\n  ambiguous numbers:", dict(fl))
    print("\n  entries with risk%%:  %d / %d" % (sum(1 for e in entries if e["risk_pct"]), len(entries)))

if __name__ == "__main__":
    main()
