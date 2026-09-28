# -*- coding: utf-8 -*-
"""Book-level simulation.

The author manages exposure per instrument, not per ticket: "gold, take half off" applies to
every open gold position. So positions are simulated as a book walked chronologically,
with each management message applied to all open positions in that instrument.
"""
import csv, io, os, re, sys, datetime as dt
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import futures_prices as prices   # NOT `prices`: stocks/scripts/prices.py is a
                                 # different module with that name
import instructions as ins
import lexicon
import message_input
from lexicon import plausible, SYMBOLS, is_silver_metal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARSED = os.path.join(ROOT, "data", "parsed")
# Per-side cost in PRICE units (half-spread + commission). Provisional values are
# replaced by broker truth as soon as data/prices/ctrader/_specs.csv exists - see
# scripts/specs.py and tools/cbot/SpecExporter.cs.
COST = {"XAUUSD": 0.15, "XAGUSD": 0.010, "US100": 0.50, "JP225": 3.00, "BTCUSD": 12.00,
        "US2000": 0.30, "XTIUSD": 0.015, "TSLA": 0.05}
COMMISSION_PCT = {k: 0.0 for k in COST}
try:
    from specs import build_cost_tables
    COST, COMMISSION_PCT = build_cost_tables(COST)
except Exception as _e:
    # Never swallow this. A failure here silently reverts every instrument to the
    # provisional cost estimates, and the book still runs and still prints a number.
    sys.stderr.write("book_engine: WARNING - broker specs not applied (%s: %s); "
                     "costs are provisional estimates.\n" % (type(_e).__name__, _e))


def side_cost(symbol, price):
    """Cost of one side, in price units per unit traded.

    Spread is a fixed price offset; commission is a percentage of notional, so it
    scales with price. Bitcoin is the case that makes the distinction matter: its
    commission is worth ~70x its half-spread.
    """
    return COST[symbol] + abs(price) * COMMISSION_PCT.get(symbol, 0.0) / 100.0
DEFAULT_RISK = 0.25
MAX_HOLD_MIN = 30 * 24 * 60
PENDING_MAX_MIN = 7 * 24 * 60   # a resting order expires unfilled after a week
INITIAL = 100000.0

# How much of the *remaining* position each instruction takes off.
FRACTIONS = [("2/3", 2.0/3.0), ("שני שליש", 2.0/3.0), ("שליש", 1.0/3.0),
             ("חצי", 0.5), ("רבע", 0.25)]
DEFAULT_PARTIAL = 0.5   # overridable for sensitivity testing


def _ts(s):
    return dt.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")


# CONFIGURE ME: directory containing platform-neutral message JSON, and the logical
# streams this run is about. Keep these values aligned with parse_signals.py.
INPUT_DIR = os.environ.get("ENGINE_MESSAGE_DIR", os.path.join(ROOT, "data", "raw"))
MAIN_STREAM = "YOUR_MAIN_STREAM_ID"
SECOND_STREAM = "YOUR_SECOND_STREAM_ID"
SOURCE_STREAMS = frozenset(s for s in (MAIN_STREAM, SECOND_STREAM) if s)


# ---------------------------------------------------------------------------
# Optional per-message overrides, for entries whose price the parser could not read
# (a trigger stated before its label, for instance). Keyed by message id:
#   "<message id>": (entry trigger, corrected stop or None)
# Empty by default - fill it in only for messages you have checked by hand.
# ---------------------------------------------------------------------------
CORRECTIONS_ON = True

ENTRY_FIXES = {}

# {message id: target message id} - see the stop_move block in run().
STOP_TARGET_OVERRIDES = {}

# An instruction that names a logical stream is about positions opened in THAT stream.
#
# Stream scoping is deliberately NOT the general rule. One book can be run from several
# streams, and a message in one stream saying "take 20% off every open Nasdaq position"
# correctly reduces positions opened from the other. Only an explicit stream name in the
# words narrows the scope. Add (phrase, stream id) pairs here for the wording your own
# source uses.
STREAM_SCOPE = ()

_SIDE_WORD = re.compile("שורט|לונג|short|long", re.I)
_SIDE_OF = (("שורט", -1), ("short", -1), ("לונג", 1), ("long", 1))


def corrected_entry(row):
    """The entry row with its stated trigger honoured, and a note - or (row, None)."""
    fix = ENTRY_FIXES.get(row.get("id"))
    if not fix or not CORRECTIONS_ON:
        return row, None
    row = dict(row)
    row["entry_type"] = "trigger"
    row["trigger"] = repr(fix[0])
    note = ("corrected: the stated trigger %g is now honoured instead of opening at "
            "the message timestamp" % fix[0])
    if fix[1] is not None:
        note += "; the protective stop is %g, not the stop-limit entry price" % fix[1]
        row["stop"] = repr(fix[1])
    return row, note


def sole_side(txt):
    """The one direction this message is about, or None when it is not unambiguous."""
    low, found = txt.lower(), set()
    for word, d in _SIDE_OF:
        if word in low:
            found.add(d)
    return found.pop() if len(found) == 1 else None


def correct_instructions(txt, instr, entry_streams=None):
    """Corrections that need the whole message, not one clause.

    They live here rather than in `instructions.parse` because each needs context the
    parser does not have: the other clauses and the stream a ticket was posted in.
    """
    if not instr or not CORRECTIONS_ON:
        return instr
    # 1. Side does not survive a clause boundary, so "regarding the Nasdaq short /
    #    cancel the last trade" reaches longs as well. Only carry it when the message is
    #    about ONE instrument: "bitcoin long, close it / gold can be closed too" must not
    #    hand that long to the gold clause.
    syms = [x for x in detect_symbols(txt) if x in COST]
    d = sole_side(txt) if len(set(syms)) == 1 else None
    if d:
        for i in instr:
            if not i.get("side") and not _SIDE_WORD.search(i.get("clause") or ""):
                i["side"] = d
                i["side_carried"] = True
    # 2. An instruction scoped to a logical stream reaches only that stream's tickets.
    if entry_streams:
        for phrase, stream in STREAM_SCOPE:
            if phrase not in txt:
                continue
            scope = frozenset(e for e, s in entry_streams.items() if s == stream)
            for i in instr:
                if not i.get("target_id"):
                    i["scope_ids"] = scope
                    i["stream_scoped"] = True
    return instr


def _source_messages(streams=None):
    """Normalized messages supplied by the operator.

    `streams=None` means "whatever SOURCE_STREAMS says right now". Binding it as a
    default argument instead would freeze the value at import time, so repointing the
    engine - which is what demo/build_demo.py does to stay independent of your
    configuration - would silently keep reading the old streams.
    """
    return message_input.load(INPUT_DIR,
                              SOURCE_STREAMS if streams is None else streams)


def _full_texts(streams=None):
    """id -> (content, timestamp) from the platform-neutral input."""
    return {m["id"]: (m["content"], m["timestamp"])
            for m in _source_messages(streams)}


def _full_records(streams=None):
    """id -> full raw record, not just text.

    `_full_texts` keeps only content and timestamp, which is enough for the engine but
    not for a human reviewer: it silently drops reply references, attachments and the
    source stream. Some messages have no text at all and exist purely as an
    attachment, and many more carry one. Those appear as blank messages unless the attachment
    is surfaced.
    """
    out = {}
    for m in _source_messages(streams):
        atts = [(a["name"], a["url"]) for a in m["attachments"]]
        out[m["id"]] = {
            "id": m["id"], "content": m["content"], "ts": m["timestamp"],
            "reply_to": m["reply_to"], "attachments": atts,
            "stream_id": m["stream_id"], "stream_name": m["stream_name"],
            "edited": m["edited"],
        }
    return out


def _reply_map(streams=None):
    """message id -> the id of the message named by its optional reply_to field.

    This is the only hard link between an instruction and one specific order. The author rarely
    uses it, so most instructions still have to be bound by wording alone - but where
    the link exists it is authoritative and must beat any heuristic.
    """
    return {m["id"]: m["reply_to"] for m in _source_messages(streams)
            if m["reply_to"]}


def detect_symbols(text):
    """Every instrument named in `text`, canonical, in order of appearance."""
    low = text.lower()
    hits = []
    for canon, terms in SYMBOLS:
        for w in terms:
            pos = lexicon.term_pos(text, low, w)
            if pos >= 0:
                hits.append((pos, canon))
                break
    hits.sort()
    return [c for _, c in hits]


def load_actions(default_partial=DEFAULT_PARTIAL):
    """One chronological stream of entries and management instructions."""
    if PARSED == os.path.join(ROOT, "data", "parsed"):
        import parse_signals
        parse_signals.ensure_current()
    acts = []
    entry_seq = {}
    entry_streams = {}
    for e in csv.DictReader(io.open(os.path.join(PARSED, "entries.csv"), encoding="utf-8-sig")):
        e = corrected_entry(e)[0]
        entry_streams[e["id"]] = e.get("stream_id") or ""
        if e["symbol"] not in COST:
            continue
        # Message id identifies the source message, not the ticket within it. Some
        # posts contain two entries, so carry a stable row sequence as well.  Keeping
        # `id` unchanged preserves reply/management semantics; `entry_seq` is solely
        # the source-ticket identity used by diagnostics and review accounting.
        seq = entry_seq.get(e["id"], 0)
        entry_seq[e["id"]] = seq + 1
        acts.append({"kind": "entry", "ts": _ts(e["ts"]), "symbol": e["symbol"],
                     "id": e["id"], "entry_seq": seq,
                     "dir": 1 if e["direction"] == "long" else -1,
                     "risk": float(e["risk_pct"]) if e["risk_pct"] else DEFAULT_RISK,
                     "level_type": e["level_type"],
                     "stop": float(e["stop"]),
                     "tp": float(e["tp"]) if e.get("tp") else None,
                     "stop_type": e.get("stop_type") or e["level_type"],
                     "tp_type": e.get("tp_type") or e["level_type"],
                     "entry_type": e.get("entry_type", "market"),
                     "trigger": float(e["trigger"]) if e.get("trigger") else None})
    entry_keys = {(a["id"], a["symbol"]) for a in acts}
    # Management: scan every message independently of whether it also contains an
    # entry. Real posts sometimes close or modify an existing trade and publish its
    # replacement in the same message; treating the two classes as exclusive silently
    # discarded the management half.
    texts = _full_texts()
    replies = _reply_map()
    for mid, (txt, ts) in texts.items():
        if not txt.strip():
            continue
        msg_syms = [x for x in detect_symbols(txt) if x in COST]
        # Instruments named only inside an exception are named to be SPARED, so for the
        # purpose of "does this message route to a single instrument" they do not count.
        excluded = [x for x in ins.excluded_symbols(txt, detect_symbols) if x in COST]
        if not msg_syms or set(msg_syms) <= set(excluded):
            # A message naming no instrument is normally not an instruction we can
            # route. The exception is an instruction addressed to the whole book -
            # "close all open trades", "take all open shorts off" - which is
            # meaningful precisely because it names nothing.
            #
            # The same is true of one whose only named instrument is the exclusion:
            # "חוץ מהזהב סוגרים הכל" is a book-wide close that spares gold. It used
            # to fall through to per-instrument routing, which correctly declined to
            # close gold and then emitted nothing, so the instruction closed nothing at
            # all and the rest of the book ran on.
            side = ins.book_wide_side(txt)
            if side is None:
                continue
            wide = [x for x in correct_instructions(
                        txt, ins.parse(txt), entry_streams)
                    if x["kind"] in ("full_close", "partial_close")]
            if not wide:
                continue
            for x in wide:
                x["side"] = side
            acts.append({"id": mid, "ts": _ts(ts), "kind": "manage", "symbol": None,
                         "book_wide": True, "exclude": excluded, "instr": wide})
            continue
        # Resolve each clause to the instrument it is *about*, so a bare header line
        # ("[symbol]" / "יש לממש עוד 20%") binds to that instrument instead of falling back
        # to every instrument the message happens to mention.
        # Resolution runs over *every* instrument, not just the priced ones: a clause
        # naming an unpriced instrument ("Meta יש לסגור") must break the running
        # context, or its close is inherited by whatever was named before it. Only
        # after the target is settled is it filtered down to what we can price.
        resolved = ins.clause_targets(ins._clauses(txt), detect_symbols)
        clause_syms = {c: [x for x in s if x in COST]
                       for c, s in resolved.items() if any(x in COST for x in s)}
        instr = ins.parse(txt, clause_syms, default_partial,
                          target_id=replies.get(mid))
        instr = correct_instructions(txt, instr, entry_streams)
        # A message that both opens a new ticket and moves a stop can name an EARLIER
        # ticket by its time ("the 17:39 one"), not the ticket it just opened and not
        # every open leg on that instrument. There is no general rule for it, so map
        # such messages here by hand:  {message id: target message id}
        for _src, _dst in STOP_TARGET_OVERRIDES.items():
            if mid == _src and CORRECTIONS_ON:
                for it in instr:
                    if it["kind"] == "stop_move":
                        it["target_id"] = _dst
                        it["target_from_named_time"] = True
        if not instr:
            continue
        for it in instr:
            # A clause with an exception resolves to its own symbols or to nothing;
            # widening it to every instrument mentioned in the message would re-apply
            # the action to the instrument the exception exists to protect.
            fallback = []
            if not it.get("scope_excluded"):
                if len(msg_syms) == 1:
                    fallback = msg_syms
                elif any(scope in (it.get("clause") or "")
                         for scope in ins.ALL_TRADES):
                    # An explicit "every open position" can deliberately govern all
                    # instruments named elsewhere in the post. Generic prose cannot.
                    fallback = msg_syms
            for sym in (it.get("symbols") or fallback):
                if (it["kind"] in ("size_up", "ambiguous_size_up", "restore_reduction")
                        and (mid, sym) in entry_keys):
                    # The ticket itself is the added exposure. Executing a second
                    # synthetic size-up for wording such as "add a long" doubles it.
                    continue
                acts.append({"kind": "manage", "ts": _ts(ts), "symbol": sym,
                             "id": mid, "instr": [it]})
    acts.sort(key=lambda a: a["ts"])
    return acts


def _in_scope(position, instruction):
    """Is this position inside the instruction's explicit scope?

    Positions created by a size-up carry a suffixed id ("<entry>_add"); they inherit the
    scope of the ticket they were added to.
    """
    scope = instruction.get("scope_ids")
    if scope is None:
        return True
    return str(position["id"]).split("_")[0] in scope


def _first_touch(arr, level, above, lo, hi):
    if lo >= hi:
        return -1
    seg = arr[lo:hi]
    hits = (seg >= level) if above else (seg <= level)
    k = int(hits.argmax())
    return lo + k if hits[k] else -1


class Book:
    def __init__(self):
        self.series = {sym: prices.get(sym) for sym in COST}
        self.open_pos = []
        self.log = []
        self.realised = []
        self.realised_total = 0.0
        self.rejected_stops = 0
        self.stop_crossed_updates = 0
        self.pending = []
        self.pending_filled = 0
        # Actions that could not be executed because the instrument has no price
        # file. Without this they are scheduled at datetime.max and vanish: the
        # book quietly gets smaller and nothing says so.
        # The message currently being applied. None while the book is merely being
        # advanced through time, so a stop or target that fires on its own is
        # correctly recorded as caused by nothing.
        self.cause = None
        # cause id -> {effect name: count}, for instructions that change state
        # without closing anything.
        self.effects = {}
        self.unpriced = []
        self.pending_expired = 0
        self.pending_cancelled = 0
        self.pending_void = 0
        self.pending_resized = 0
        self.pending_resize_ambiguous = 0
        self.pending_resize_void = 0
        self.conditional_unresolved = 0
        self.ambiguous_unresolved = 0
        self.scheduled_cancelled = 0
        self.book_wide_hits = 0
        self.cancel_closed = 0
        self.targets_cancelled = 0
        self.size_ups = 0
        self.size_ups_void = 0
        self.restores = 0
        self.restores_void = 0
        self.audit = []
        self._next_pos = 0

    def new_pos_id(self):
        self._next_pos += 1
        return "P%d" % self._next_pos

    def equity_at(self, t):
        """Account EQUITY at `t`: realised balance plus floating P&L on open positions.

        RULES.md sizes each trade as a percentage of current equity, so open exposure
        has to be marked. _advance() has already closed everything resolvable up to
        `t`, so realised is a running total; only the still-open book needs marking.
        """
        eq = INITIAL + self.realised_total
        for p in self.open_pos:
            ps = self.series.get(p["symbol"])
            if ps is None or p["remaining"] <= 0 or p["ts"] > t:
                continue
            j = int(np.searchsorted(ps.t, int((t - prices.EPOCH).total_seconds()), side="right")) - 1
            if j < 0:
                continue
            # Mark at the bar OPEN, not its close: we are acting at the open of this
            # bar, and using the close would let sizing see the rest of the minute.
            mark = ps.o[j] if ps.time_at(j) == t else ps.c[j]
            eq += p["units"] * p["remaining"] * p["dir"] * (float(mark) - p["entry"])
        return eq

    def _effect(self, name):
        """Record a state change that produces no leg, against the causing message."""
        if self.cause is not None:
            self.effects.setdefault(self.cause, {})
            self.effects[self.cause][name] = self.effects[self.cause].get(name, 0) + 1

    def _resolve_unprompted(self, pos, s, lo, hi):
        """`_resolve` for a stretch no message caused: stops and targets that fire while
        an instruction is waiting for its execution bar belong to nobody."""
        prev, self.cause = self.cause, None
        try:
            return self._resolve(pos, s, lo, hi)
        finally:
            self.cause = prev

    def _book(self, pos, px, idx, s, reason, frac, same_bar=False):
        d = pos["dir"]
        pnl_pts = frac * (d * (px - pos["entry"]) - side_cost(pos["symbol"], px))
        rec = {"entry_id": pos["id"], "symbol": pos["symbol"], "ts": pos["ts"],
               "entry_seq": pos.get("entry_seq"),
               "exit_ts": s.time_at(idx), "reason": reason, "frac": frac,
               "r": pnl_pts / pos["stop_dist"], "units": pos["units"],
               "pnl": pnl_pts * pos["units"], "same_bar": same_bar,
               "entry_px": pos["entry"], "dir": pos["dir"],
               "pos_id": pos.get("pos_id"),
               "hold_min": int(idx - pos["entry_idx"]), "risk": pos["risk"],
               "cause_id": self.cause}
        self.log.append(rec)
        self.realised.append((rec["exit_ts"], rec["pnl"]))
        self.realised_total += rec["pnl"]

    def _resolve(self, pos, s, lo, hi):
        """Close pos if stop or target is touched in [lo,hi)."""
        d = pos["dir"]
        fs = _first_touch(s.l if d == 1 else s.h, pos["stop"], d == -1, lo, hi)
        ft = (-1 if pos["tp"] is None else
              _first_touch(s.h if d == 1 else s.l, pos["tp"], d == 1, lo, hi))
        cands = [x for x in (fs, ft) if x >= 0]
        if not cands:
            return False
        first = min(cands)
        same_bar = (fs == ft and fs >= 0)
        hit_stop = (fs >= 0 and (ft < 0 or fs <= ft))    # conservative on ties
        px = pos["stop"] if hit_stop else pos["tp"]
        # Gaps: if the bar already opened beyond the level, that level never traded -
        # the fill is the open. Worse than asked for on a stop, better on a target.
        # The same rule _fill_pending applies to resting orders; it was missing here,
        # which credited every gapped stop with a fill at a price that did not exist.
        op = float(s.o[first])
        beyond = (op < px if d == 1 else op > px) if hit_stop else \
                 (op > px if d == 1 else op < px)
        if beyond:
            px = op
        self._book(pos, px, first, s, "stop" if hit_stop else "target",
                   pos["remaining"], same_bar)
        pos["remaining"] = 0.0
        return True

    def _fill_pending(self, upto_time):
        """Resting orders fill only when price genuinely reaches them.

        The order type follows from where the trigger sat relative to the market when
        it was announced:
            long,  trigger above market -> buy stop   (fills on high >= trigger)
            long,  trigger below market -> buy limit  (fills on low  <= trigger)
            short, trigger below market -> sell stop  (fills on low  <= trigger)
            short, trigger above market -> sell limit (fills on high >= trigger)

        Gaps: if the filling bar opens already beyond the trigger, the fill happens at
        the open, not the trigger - worse than requested for stops, better for limits.
        That is how a real broker fills, and it avoids inventing prices that never traded.
        """
        def touch(q):
            ps = self.series[q["symbol"]]
            boundary = min(upto_time, q["expires_at"])
            hi = ps.idx_at_or_after(boundary)
            hi = len(ps.t) if hi < 0 else hi
            k = _first_touch(ps.h if q["fill_on_high"] else ps.l,
                             q["trigger"], q["fill_on_high"], q["cursor"], hi)
            return ps.time_at(k) if k >= 0 else dt.datetime.max

        # Size fills in market-time order, after resolving earlier exits. Message
        # insertion order and unrelated no-op messages must not change allocation.
        for q in sorted(self.pending, key=touch):
            ps = self.series[q["symbol"]]
            j = ps.idx_at_or_after(min(upto_time, q["expires_at"]))
            if j < 0:
                j = len(ps.t)
            hi = min(j, len(ps.t))

            want_up = q["fill_on_high"]
            arr = ps.h if want_up else ps.l
            k = _first_touch(arr, q["trigger"], want_up, q["cursor"], hi)

            if k >= 0:
                self.pending.remove(q)
                op = float(ps.o[k])
                # gap through the level -> fill at the open
                fill = op if ((want_up and op >= q["trigger"]) or
                              (not want_up and op <= q["trigger"])) else q["trigger"]
                d = q["dir"]
                entry = fill + d * side_cost(q["symbol"], fill)
                ok = ((q["stop"] < entry and (q["tp"] is None or entry < q["tp"]))
                      if d == 1 else
                      (entry < q["stop"] and (q["tp"] is None or q["tp"] < entry)))
                if not ok:
                    self.pending_void += 1
                    continue
                sd = abs(entry - q["stop"])
                if sd <= 0:
                    self.pending_void += 1
                    continue
                self._advance_open(ps.time_at(k))
                self.open_pos.append({"id": q["id"], "symbol": q["symbol"], "dir": d,
                    "entry_seq": q.get("entry_seq"),
                    "ts": ps.time_at(k), "entry": entry, "stop": q["stop"], "tp": q["tp"],
                    "stop_dist": sd, "entry_idx": k, "cursor": k, "remaining": 1.0,
                    "units": (self.equity_at(ps.time_at(k)) * q["risk"] / 100.0) / sd,
                    "risk": q["risk"], "pos_id": self.new_pos_id()})
                self.pending_filled += 1
                continue

            # expiry is calendar time from creation, not a window that slides with the cursor
            if upto_time >= q["expires_at"]:
                self.pending.remove(q)
                self.pending_expired += 1
            else:
                q["cursor"] = max(q["cursor"], min(hi, len(ps.t)))

    def _advance(self, upto_time):
        """Resolve levels / timeouts on every open position up to `upto_time`.

        Anything booked in here fired on its own - a stop, a target, a timeout - so the
        cause is cleared for the duration and restored afterwards.
        """
        prev, self.cause = self.cause, None
        try:
            self._fill_pending(upto_time)
            self._advance_open(upto_time)
        finally:
            self.cause = prev

    def _advance_open(self, upto_time):
        """Resolve existing exposure without recursively filling pending orders."""
        for p in list(self.open_pos):
            ps = self.series[p["symbol"]]
            j = ps.idx_at_or_after(upto_time)
            if j < 0:
                j = len(ps.t)
            cap = calendar_cap(p, ps)
            hi = min(j, cap)
            if self._resolve(p, ps, p["cursor"], hi):
                self.open_pos.remove(p)
            elif hi >= cap:
                self._book(p, float(ps.c[cap - 1]), cap - 1, ps, "timeout", p["remaining"])
                self.open_pos.remove(p)
            else:
                p["cursor"] = hi


def calendar_cap(position, series):
    """Exclusive bar boundary for the calendar holding limit."""
    cutoff = position["ts"] + dt.timedelta(minutes=MAX_HOLD_MIN)
    index = series.idx_at_or_after(cutoff)
    return (min(max(position["entry_idx"] + 1, index), len(series.t))
            if index >= 0 else len(series.t))


def resolve_levels(a, anchor, d):
    """(stop, target) in price units for an entry anchored at `anchor`.

    Three notations coexist and all three are the author's:
      absolute    "STOP" + [price]             the price itself
      distance    "STOP 600 נקודות"            points away from entry
      r_multiple  "פוטנציאל 1 ל 4"             a reward-to-risk ratio, so the target
                                               is only knowable once the stop is
    """
    stop = anchor - d * a["stop"] if a["stop_type"] == "distance" else a["stop"]
    if a.get("tp") is None or a.get("tp_type") == "none":
        tp = None
    elif a["tp_type"] == "r_multiple":
        tp = anchor + d * a["tp"] * abs(anchor - stop)
    elif a["tp_type"] == "distance":
        tp = anchor + d * a["tp"]
    else:
        tp = a["tp"]
    return stop, tp


LAST_BOOK = None

def run(acts, entry_delay=0, exit_delay=0, take=None, delays=None):
    """`delays`: optional per-action delay in whole M1 bars, overriding the scalars.
    `take`: optional per-action boolean. Callers that model a missed *message* must
    set the same value for every action sharing a message id."""
    b = Book()

    # A delay changes when an action exists, not just the price used for it. Processing
    # in source-message order allowed a close scheduled for minute 3 to close an entry
    # that was not scheduled to open until minute 6. Build one execution queue first.
    scheduled = []
    for k, a in enumerate(acts):
        if take is not None and not take[k]:
            continue
        lag = delays[k] if delays is not None else (
            entry_delay if a["kind"] == "entry" else exit_delay)
        if a.get("book_wide"):
            # Book-wide actions still price each position on its own series below.
            # This timestamp is only their stable place in the global queue.
            execute_at = a["ts"] + dt.timedelta(minutes=lag)
        else:
            ps = b.series.get(a["symbol"])
            if ps is None:
                b.unpriced.append({"id": a.get("id"), "ts": a["ts"],
                                   "symbol": a.get("symbol"), "kind": a["kind"]})
            j = ps.idx_at_or_after(a["ts"]) if ps is not None else -1
            j = j + lag if j >= 0 else -1
            execute_at = ps.time_at(j) if ps is not None and 0 <= j < len(ps.t) else dt.datetime.max
        scheduled.append({"index": k, "action": a, "lag": lag,
                          "execute_at": execute_at})

    # A cancellation posted before the bar where an earlier signal would first be
    # executable cancels the queued signal itself. Otherwise the loop would open the
    # trade at the bar and immediately close it, inventing exposure that never existed.
    suppressed = set()
    consumed_cancel = set()
    selected_in_message = {}
    by_message_time = sorted(scheduled, key=lambda x: (x["action"]["ts"], x["index"]))
    for cancel_item in by_message_time:
        ca = cancel_item["action"]
        if ca["kind"] != "manage" or ca.get("book_wide"):
            continue
        for instruction_index, ist in enumerate(ca["instr"]):
            if (ist["kind"] not in ("cancel_pending", "cancel_scheduled")
                    or ist.get("condition")):
                continue
            wanted_kind = "entry" if ist["kind"] == "cancel_pending" else "manage"
            candidates = [item for item in by_message_time
                          if item["index"] not in suppressed
                          and item["action"]["kind"] == wanted_kind
                          and item["action"]["symbol"] == ca["symbol"]
                          and item["action"]["ts"] < ca["ts"]
                          and item["execute_at"] >= cancel_item["execute_at"]
                          and (wanted_kind != "entry" or ist["side"] == 0
                               or item["action"]["dir"] == ist["side"])]
            if wanted_kind == "manage":
                candidates = [item for item in candidates
                              if any(x["kind"] not in (
                                  "cancel_pending", "cancel_scheduled")
                                  for x in item["action"].get("instr", []))]
            scope_ids = ist.get("scope_ids")
            if scope_ids is not None:
                candidates = [item for item in candidates
                              if item["action"]["id"] in scope_ids]
            target_id = ist.get("target_id")
            if target_id:
                candidates = [item for item in candidates
                              if item["action"]["id"] == target_id]
            else:
                views = []
                for item in candidates:
                    view = dict(item["action"])
                    view["_scheduled_item"] = item
                    views.append(view)
                candidates = [view["_scheduled_item"]
                              for view in ins.select(views, ist["selector"], ca["ts"])]
            if not candidates:
                continue
            for item in candidates:
                suppressed.add(item["index"])
                b.scheduled_cancelled += 1
                trigger_entry = (item["action"]["kind"] == "entry"
                                 and item["action"].get("entry_type") == "trigger")
                if trigger_entry:
                    b.pending_cancelled += 1
                # This pass runs before the main loop, so there is no current cause to
                # inherit: name the cancelling message explicitly.
                b.cause = str(ca["id"])
                b._effect("pending_cancelled" if trigger_entry else "scheduled_cancelled")
                b.cause = None
                b.audit.append({"type": "scheduled_cancelled",
                                "action_id": item["action"]["id"],
                                "by_id": ca["id"], "symbol": ca["symbol"],
                                "kind": item["action"]["kind"]})
            consumed_cancel.add((cancel_item["index"], instruction_index))

    scheduled.sort(key=lambda x: (
        x["execute_at"], x["action"]["ts"],
        # In a mixed post, management describes what to do with the old trade and
        # the entry is its replacement. Apply that management before opening the new
        # ticket, even though entries were loaded into the stream first.
        0 if x["action"]["kind"] == "manage" else 1,
        x["index"]))
    for item in scheduled:
        # Everything applied below is attributable to this message. _advance() clears
        # it again for the stretches where the book is only moving through time.
        b.cause = str(item["action"].get("id") or "") or None
        k, a, lag = item["index"], item["action"], item["lag"]
        if k in suppressed:
            continue
        if a.get("book_wide"):
            # No single instrument, so no single price series. Advance the book to the
            # message and then close each open position against its own series.
            #
            # The exit delay has to be applied here too. Every other exit in this loop
            # is lagged by `delays[k]`/`exit_delay`, and the sensitivity and Monte-Carlo
            # callers rely on that; a book-wide close executing at zero lag would be the
            # only instruction in the book granted a perfect fill.
            active_instr = []
            for instruction_index, ist in enumerate(a["instr"]):
                if (k, instruction_index) in consumed_cancel:
                    continue
                if ist["kind"] == "cancel_scheduled":
                    continue
                if ist["kind"] in ("ambiguous_reduction", "ambiguous_size_up"):
                    b.ambiguous_unresolved += 1
                    b.audit.append({"type": ist["kind"],
                                    "action_id": a["id"], "symbol": a.get("symbol"),
                                    "clause": ist.get("clause")})
                    continue
                if (ist.get("condition")
                        and ist.get("condition_type") != "position_exists"):
                    b.conditional_unresolved += 1
                    b.audit.append({"type": "condition_unresolved",
                                    "action_id": a["id"], "symbol": a.get("symbol"),
                                    "clause": ist.get("clause")})
                else:
                    active_instr.append(ist)
            if not active_instr:
                b._advance(a["ts"])
                continue
            b._advance(a["ts"])
            spared = set(a.get("exclude") or ())
            for ist in active_instr:
                for p in list(b.open_pos):
                    # "close everything except gold" must leave gold alone.
                    if p["symbol"] in spared:
                        continue
                    if ist["side"] != 0 and p["dir"] != ist["side"]:
                        continue
                    ps = b.series[p["symbol"]]
                    if ps is None:
                        continue
                    j = ps.idx_at_or_after(a["ts"])
                    if j < 0:
                        continue
                    j += lag
                    if j >= len(ps.t):
                        continue
                    # Same causality rule as the per-instrument path: if this position
                    # met its stop or target before the delayed execution bar, it is
                    # already closed and there is nothing left to close.
                    cap = calendar_cap(p, ps)
                    if b._resolve_unprompted(p, ps, p["cursor"], min(j, cap)):
                        b.open_pos.remove(p)
                        continue
                    q = float(ps.o[j])
                    if ist["kind"] == "full_close":
                        b._book(p, q, j, ps, "manual_close", p["remaining"])
                        p["remaining"] = 0.0
                        b.open_pos.remove(p)
                    else:
                        take_f = p["remaining"] * ist["frac"]
                        b._book(p, q, j, ps, "partial", take_f)
                        p["remaining"] -= take_f
                        if p["remaining"] <= 1e-9:
                            b.open_pos.remove(p)
                        else:
                            # The surviving remainder must not be re-resolved from
                            # before this exit. Without advancing the cursor a delayed
                            # partial books at minute 4 and the remainder then stops
                            # out at minute 3 - an order of events that cannot happen.
                            p["cursor"] = j
                    b.book_wide_hits += 1
            continue

        s = b.series[a["symbol"]]
        i = s.idx_at_or_after(a["ts"]) if s is not None else -1
        i = i + lag if i >= 0 else -1
        if s is None or i < 0 or i >= len(s.t):
            if a["kind"] == "entry" and a.get("entry_type") == "trigger":
                b.pending_void += 1
                b.audit.append({"type": "pending_unpriced", "action_id": a["id"],
                                "symbol": a["symbol"], "reason": "outside price coverage"})
            continue
        now = s.time_at(i)

        # Actions are now globally ordered by their executable timestamp, so advancing
        # the whole book to `now` cannot reveal the future relative to another queued
        # action. This also resolves stops that genuinely happened during a delay.
        b._advance(now)

        if a["kind"] == "entry":
            d = a["dir"]
            if a.get("entry_type") == "trigger" and a.get("trigger") is not None:
                tg = a["trigger"]
                mkt = float(s.o[i])
                # buy-stop / sell-limit wait for price to rise to the trigger;
                # buy-limit / sell-stop wait for it to fall.
                fill_on_high = (tg > mkt)
                b.pending.append({"id": a["id"], "symbol": a["symbol"], "dir": d,
                                  "entry_seq": a.get("entry_seq"),
                                  "trigger": tg, "fill_on_high": fill_on_high,
                                  "stop": resolve_levels(a, tg, d)[0],
                                  "tp":   resolve_levels(a, tg, d)[1],
                                  "risk": a["risk"], "cursor": i,
                                  "ts": now,
                                  "created_at": now,
                                  "expires_at": now + dt.timedelta(minutes=PENDING_MAX_MIN)})
                continue
            raw_px = float(s.o[i])
            entry = raw_px + d * side_cost(a["symbol"], raw_px)
            stop, tp = resolve_levels(a, entry, d)
            ok = ((stop < entry and (tp is None or entry < tp)) if d == 1
                  else (entry < stop and (tp is None or tp < entry)))
            if not ok:
                continue                                 # void: already past a level
            sd = abs(entry - stop)
            if sd <= 0:
                continue
            eq = b.equity_at(now)
            # Every position carries a pos_id, not just the ones opened by size_up.
            # `entry_id` is the MESSAGE id, and a message can carry two signals, so it
            # does not identify a position - which made ledger invariants ("a position
            # is never closed for more than 100%") impossible to state.
            b.open_pos.append({"id": a["id"], "symbol": a["symbol"], "dir": d, "ts": now,
                               "entry_seq": a.get("entry_seq"),
                               "entry": entry, "stop": stop, "tp": tp, "stop_dist": sd,
                               "entry_idx": i, "cursor": i, "remaining": 1.0,
                               "units": (eq * a["risk"] / 100.0) / sd, "risk": a["risk"],
                               "pos_id": b.new_pos_id()})
        else:
            active_instr = []
            for instruction_index, ist in enumerate(a["instr"]):
                if (k, instruction_index) in consumed_cancel:
                    continue
                if ist["kind"] == "cancel_scheduled":
                    continue
                if ist["kind"] in ("ambiguous_reduction", "ambiguous_size_up"):
                    b.ambiguous_unresolved += 1
                    b.audit.append({"type": ist["kind"],
                                    "action_id": a["id"], "symbol": a.get("symbol"),
                                    "clause": ist.get("clause")})
                    continue
                if (ist.get("condition")
                        and ist.get("condition_type") != "position_exists"):
                    b.conditional_unresolved += 1
                    b.audit.append({"type": "condition_unresolved",
                                    "action_id": a["id"], "symbol": a.get("symbol"),
                                    "clause": ist.get("clause")})
                else:
                    active_instr.append(ist)
            if not active_instr:
                continue
            # Causality. `_advance` resolved the book only as far as the MESSAGE, but
            # under delay this instruction executes at bar `i`, later than that. A
            # position whose stop was hit in between is already gone and cannot be
            # managed: without this, a standing stop at minute 3 and a delayed partial
            # at minute 4 produced "50% partial at minute 4, then 50% timeout" instead
            # of "100% stopped at minute 3".
            #
            # Only this action's own instrument is advanced. Advancing the whole book to
            # a fill that can be hours away is what the comment above `_advance` warns
            # against - a stock instruction posted pre-open would resolve stops on
            # unrelated instruments before they had happened.
            for p in list(b.open_pos):
                if p["symbol"] != a["symbol"]:
                    continue
                cap = calendar_cap(p, s)
                if b._resolve_unprompted(p, s, p["cursor"], min(i, cap)):
                    b.open_pos.remove(p)

            px = float(s.o[i])
            for ist in active_instr:
                pool = [q for q in b.open_pos
                        if q["symbol"] == a["symbol"]
                        and (ist["side"] == 0 or q["dir"] == ist["side"])
                        and _in_scope(q, ist)]
                selection_key = (a["id"], a["symbol"])
                already_selected = selected_in_message.setdefault(selection_key, set())
                if ist.get("target_id"):
                    targets = [p for p in pool if str(p["id"]).split("_")[0] == ist["target_id"]]
                elif ist["selector"].get("type") == "remainder":
                    targets = [p for p in pool if p.get("pos_id") not in already_selected]
                else:
                    targets = ins.select(pool, ist["selector"], now, px)
                if ist["kind"] in ("full_close", "partial_close", "stop_move"):
                    already_selected.update(p.get("pos_id") for p in targets)
                if ist["kind"] == "reduce_risk":
                    current_risk = sum(p["risk"] * p["remaining"] for p in targets)
                    target_risk = ist.get("risk_target")
                    if current_risk > 1e-9 and target_risk is not None:
                        close_share = max(0.0, min(1.0,
                                                  (current_risk - target_risk)
                                                  / current_risk))
                        for p in list(targets):
                            if close_share <= 1e-9 or p not in b.open_pos:
                                continue
                            take_f = p["remaining"] * close_share
                            b._book(p, px, i, s, "partial", take_f)
                            p["remaining"] -= take_f
                            if p["remaining"] <= 1e-9:
                                b.open_pos.remove(p)
                            else:
                                p["cursor"] = i
                    continue
                if ist["kind"] == "size_up":
                    # Add exposure alongside the newest position, inheriting its levels.
                    base = pool[-1] if pool else None
                    if base is not None:
                        risk_target = ist.get("risk_target")
                        if risk_target is not None:
                            existing_risk = sum(p["risk"] * p["remaining"] for p in pool)
                            risk = max(0.0, risk_target - existing_risk)
                        else:
                            risk = ist.get("add_risk") or base["risk"]
                        d2 = base["dir"]
                        entry2 = px + d2 * side_cost(base["symbol"], px)
                        # The addition enters at today's price, so its risk must be sized
                        # on its OWN distance to the inherited stop, not the original one.
                        ok2 = ((base["stop"] < entry2
                                and (base["tp"] is None or entry2 < base["tp"]))
                               if d2 == 1 else
                               (entry2 < base["stop"]
                                and (base["tp"] is None or base["tp"] < entry2)))
                        sd2 = abs(entry2 - base["stop"])
                        if ok2 and sd2 > 0 and risk > 1e-9:
                            b.open_pos.append({"id": a["id"] + "_add", "symbol": base["symbol"],
                                "dir": d2, "ts": now, "entry": entry2,
                                "stop": base["stop"], "tp": base["tp"], "stop_dist": sd2,
                                "entry_idx": i, "cursor": i, "remaining": 1.0,
                                "units": (b.equity_at(now) * risk / 100.0) / sd2, "risk": risk, "pos_id": b.new_pos_id()})
                            b.size_ups += 1
                            b._effect("size_up")
                        else:
                            b.size_ups_void += 1
                    continue
                if ist["kind"] == "restore_reduction":
                    restored_any = False
                    for base in targets:
                        missing = max(0.0, 1.0 - base["remaining"])
                        if missing <= 1e-9:
                            continue
                        d2 = base["dir"]
                        entry2 = px + d2 * side_cost(base["symbol"], px)
                        ok2 = ((base["stop"] < entry2
                                and (base["tp"] is None or entry2 < base["tp"]))
                               if d2 == 1 else
                               (entry2 < base["stop"]
                                and (base["tp"] is None or base["tp"] < entry2)))
                        sd2 = abs(entry2 - base["stop"])
                        if not ok2 or sd2 <= 0:
                            continue
                        # Restore the quantity actually removed from this position,
                        # not another full risk-sized leg.
                        units2 = base["units"] * missing
                        eq = b.equity_at(now)
                        risk2 = 100.0 * units2 * sd2 / eq if eq > 0 else 0.0
                        b.open_pos.append({
                            "id": a["id"] + "_restore", "symbol": base["symbol"],
                            "dir": d2, "ts": now, "entry": entry2,
                            "stop": base["stop"], "tp": base["tp"],
                            "stop_dist": sd2, "entry_idx": i, "cursor": i,
                            "remaining": 1.0, "units": units2, "risk": risk2,
                            "pos_id": b.new_pos_id()})
                        restored_any = True
                        b.restores += 1
                        b._effect("restored")
                    if not restored_any:
                        b.restores_void += 1
                    continue
                if ist["kind"] == "update_pending_risk":
                    nr = ist.get("new_risk") or ist.get("add_risk")
                    if nr:
                        cands = [x for x in b.pending if x["symbol"] == a["symbol"]]
                        tid = ist.get("target_id")
                        if tid:
                            # An explicit reply names one order. It wins outright.
                            cands = [x for x in cands if x["id"] == tid]
                        if len(cands) == 1:
                            cands[0]["risk"] = nr
                            b.pending_resized += 1
                            b._effect("pending_resized")
                        elif len(cands) > 1:
                            # Several resting orders on this instrument and nothing
                            # picks one out. Resizing all of them invents an
                            # instruction the author did not give, so record and do nothing.
                            b.pending_resize_ambiguous += 1
                        else:
                            b.pending_resize_void += 1
                    continue
                if ist["kind"] == "cancel_pending":
                    pend = [x for x in b.pending if x["symbol"] == a["symbol"]
                            and (ist["side"] == 0 or x["dir"] == ist["side"])
                            and _in_scope(x, ist)]
                    target_id = ist.get("target_id")
                    close_filled = bool(ist.get("close_filled"))
                    sel_type = (ist["selector"] or {}).get("type")
                    if target_id:
                        # An explicit reply_to binding is exact. If it names no current
                        # pending order, do not silently broaden back to the symbol.
                        pend_sel = [x for x in pend if x["id"] == target_id]
                        pos_sel = ([p for p in targets if p["id"] == target_id]
                                   if close_filled else [])
                    elif close_filled and sel_type in ("newest", "oldest"):
                        # ONE PHRASE NAMES ONE OBJECT.
                        #
                        # "סילבר ביטול עסקה אחרונה" - "cancel the last silver trade" -
                        # means the trade the author posted moments ago, which had already
                        # filled. Selecting resting orders first found an OLDER pending
                        # silver order, cancelled that, and returned before touching the
                        # position. The live trade then kept running.
                        #
                        # A singular selector has to choose across both populations by
                        # time, or "the last trade" silently means "the last of whichever
                        # list I happened to search first".
                        union = ([("pend", x, x["ts"]) for x in pend]
                                 + [("pos", q, q["ts"]) for q in targets
                                    if q in b.open_pos])
                        pend_sel, pos_sel = [], []
                        if union:
                            union.sort(key=lambda z: z[2])
                            kind_, obj, _ = union[-1] if sel_type == "newest" else union[0]
                            if kind_ == "pend":
                                pend_sel = [obj]
                            else:
                                pos_sel = [obj]
                    else:
                        # "all", risk-scoped, dated: act on everything it names. A
                        # cancellation that names the TRIGGER never closes positions.
                        pend_sel = ins.select(pend, ist["selector"], now)
                        pos_sel = targets if close_filled else []
                    for q in pend_sel:
                        b.pending.remove(q)
                        b.pending_cancelled += 1
                        b._effect("pending_cancelled")
                    # The author calls off a trade that has already filled by "cancelling" it,
                    # and says so outright: [symbol] + "מבוטל כרגע" ... "משמע לסגור עסקה".
                    for p in pos_sel:
                        if p not in b.open_pos:
                            continue
                        b._book(p, px, i, s, "manual_close", p["remaining"])
                        p["remaining"] = 0.0
                        b.open_pos.remove(p)
                        b.cancel_closed += 1
                    continue
                for p in targets:
                    if p not in b.open_pos:
                        continue
                    if ist["kind"] == "cancel_target":
                        # The target is withdrawn; the position keeps running under its
                        # stop until the author closes it or the hold cap expires.
                        p["tp"] = None
                        b.targets_cancelled += 1
                        b._effect("target_cancelled")
                        continue
                    if ist["kind"] == "full_close":
                        b._book(p, px, i, s, "manual_close", p["remaining"])
                        p["remaining"] = 0.0
                        b.open_pos.remove(p)
                        continue
                    if ist["kind"] == "partial_close":
                        take_f = p["remaining"] * ist["frac"]
                        b._book(p, px, i, s, "partial", take_f)
                        p["remaining"] -= take_f
                        if p["remaining"] <= 1e-9:
                            b.open_pos.remove(p)
                            continue
                    if ist["kind"] == "stop_move":
                        off = ist.get("stop_offset")
                        if off is not None:
                            # "ten points below entry" is a distance, not a price.
                            # An unstated side goes to whichever is protective for
                            # this position: below entry for a long, above for a short.
                            sign = ist.get("offset_side") or -p["dir"]
                            ns = p["entry"] + sign * off
                            if (ns < px if p["dir"] == 1 else ns > px):
                                p["stop"] = ns
                                b._effect("stop_moved")
                            else:
                                # The requested protection was valid when sent but the
                                # first executable quote is already through it (often
                                # a weekend gap). A stop fills at that quote; retaining
                                # the old, looser stop invents exposure after the level
                                # the author ordered has been crossed.
                                b._book(p, px, i, s, "stop", p["remaining"])
                                p["remaining"] = 0.0
                                b.open_pos.remove(p)
                                b.stop_crossed_updates += 1
                                b.audit.append({"type": "stop_crossed_at_update",
                                                "action_id": a["id"],
                                                "symbol": a["symbol"], "value": ns,
                                                "fill": px,
                                                "clause": ist.get("clause")})
                        elif ist["be"]:
                            p["stop"] = p["entry"]
                            b._effect("stop_to_breakeven")
                        elif ist["new_stop"] is not None:
                            ns = ist["new_stop"]
                            # A stop must sit on the losing side of the current price and be a
                            # real price for the instrument. Messages that quote a *distance*
                            # ("stop 100 points") would otherwise be read as an absolute level.
                            good = plausible(p["symbol"], ns) and (
                                ns < px if p["dir"] == 1 else ns > px)
                            if good:
                                p["stop"] = ns
                                b._effect("stop_moved")
                            elif plausible(p["symbol"], ns):
                                b._book(p, px, i, s, "stop", p["remaining"])
                                p["remaining"] = 0.0
                                b.open_pos.remove(p)
                                b.stop_crossed_updates += 1
                                b.audit.append({"type": "stop_crossed_at_update",
                                                "action_id": a["id"],
                                                "symbol": a["symbol"], "value": ns,
                                                "fill": px,
                                                "clause": ist.get("clause")})
                            else:
                                b.rejected_stops += 1
                                b.audit.append({"type": "stop_rejected",
                                                "action_id": a["id"],
                                                "symbol": a["symbol"], "value": ns,
                                                "clause": ist.get("clause")})
                    if p in b.open_pos:
                        p["cursor"] = i

    # The loop leaves the last message as the current cause. Nothing below was caused
    # by it: without this, every position flushed at the end of the sample is recorded
    # as closed by whichever message happened to be last.
    b.cause = None

    # Advance to the end of the data before flushing. Without this a resting order
    # placed after the last management message would never be given the chance to
    # fill - it would simply be swept up as expired.
    last = None
    for sym in COST:
        ps = b.series.get(sym)
        if ps is not None and len(ps.t):
            t_end = ps.time_at(len(ps.t) - 1)
            last = t_end if last is None else max(last, t_end)
    if last is not None:
        b._advance(last + dt.timedelta(minutes=1))

    # flush anything still open at the end of the sample
    for p in list(b.open_pos):
        ps = b.series[p["symbol"]]
        cap = calendar_cap(p, ps)
        if not b._resolve(p, ps, p["cursor"], cap):
            b._book(p, float(ps.c[cap - 1]), cap - 1, ps, "timeout", p["remaining"])
        b.open_pos.remove(p)
    b.pending_expired += len(b.pending)
    b.pending = []
    global LAST_BOOK
    LAST_BOOK = b
    return b.log
