# -*- coding: utf-8 -*-
"""Replay the stocks channel as a book and report the result.

Model, per the operator's stated working assumptions:

  * held fraction per ticker, driven by state.py
  * EQUAL NOTIONAL: a full position is UNIT dollars, so a fraction f holds f*UNIT
  * fractional shares, no account limit, stated long/short, inverse ETFs held as themselves
  * commission per action = max(PER_SHARE * shares, MIN_DOLLAR)
  * SPREAD_BPS charged per action as a separate, explicitly variable cost

There is no stop in this model. Not because the author never names one - messages in the
window quote a level, usually as "STOP [price]" - but because the author almost never says it was
HIT. A modelled stop would therefore close positions the author went on discussing, inventing
exits the record does not contain. Positions change only when the author says they change,
which is why the never-closed positions have to be adjudicated by a reader instead.

Usage:
    python stocks/scripts/run_study.py [--since YYYY-MM-DD] [--fetch]
"""
import argparse
import bisect
import collections
import csv
import datetime as dt
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STOCKS = os.path.dirname(HERE)
PROJECT = os.path.dirname(STOCKS)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(PROJECT, "scripts"))

import actions as A     # noqa: E402
import message_input as MI  # noqa: E402
import prices as P      # noqa: E402
import state as S       # noqa: E402

# CONFIGURE ME: directory containing platform-neutral message JSON and the logical
# stream that carries stock messages. Input must already contain only messages the
# operator is authorised to analyse.
INPUT_DIR = os.environ.get("STOCK_MESSAGE_DIR", os.path.join(PROJECT, "data", "raw"))
STREAM_ID = "YOUR_STOCKS_STREAM_ID"

UNIT = 10000.0          # dollars in a full position
PER_SHARE = 0.005       # commission per share
MIN_DOLLAR = 1.0        # commission floor per action
SPREAD_BPS = 0.0        # half-spread per action, in basis points; varied in sensitivity


def money(v):
    """Old-style %-formatting has no thousands flag; this does."""
    return format(v, "+,.0f")


def load_records(since=None):
    """Executed actions and state declarations, in time order, deduplicated.

    Returns (msgs, recs, vocab). Each msg is (ts, id, src, text, norm): `text` is what
    the author actually typed and is what any review page must display, `norm` is the same
    message with known tickers upper-cased and is the only thing ever parsed. Showing
    a reader "[symbol A]" where the author wrote "[symbol B]" would misrepresent the evidence they are
    being asked to rule on, so the two are carried separately rather than merged.
    """
    msgs = []
    try:
        supplied = MI.load(INPUT_DIR, (STREAM_ID,))
    except MI.InputFormatError as exc:
        raise SystemExit("invalid message input; refusing to run on a partial corpus -- %s" % exc)
    for m in supplied:
        ts = m["timestamp"][:19].replace("T", " ")
        msgs.append((ts, m["id"], m["stream_id"], m["content"].strip()))
    msgs.sort()

    # The case vocabulary is built from EVERY message the author ever posted, including those
    # before the study window. Evidence that "[symbol]" is a ticker does not expire, and a
    # wider base makes the twice-in-caps floor harder to reach by accident.
    vocab = A.build_vocab([t for _, _, _, t in msgs])
    msgs = [(ts, mid, src, text, A.normalise(A.strip_urls(text), vocab))
            for ts, mid, src, text in msgs
            if not (since and ts[:10] < since)]

    recs = []
    for ts, mid, src, text, norm in msgs:
        got = A.extract(norm)
        for r in got:
            r.update(ts=ts, id=mid, text=text, state_only=False, src=src)
        recs.extend(got)

        # A message can do both, and the old code stopped at the first action it found:
        #
        #   [symbol A] כמות מלאה          [symbol A] is at full quantity   <- a state
        #   מעל גבוה אקח [symbol B]       above the high I will take [symbol B]
        #
        # extract() returned the [symbol B] plan, so the [symbol A] declaration was dropped and the
        # position stayed at whatever the engine last believed. Declarations are only
        # taken for tickers no action already covered in this message - an explicit verb
        # still outranks a bare state, which is the rule the restatement guard rests on.
        claimed = {r["ticker"] for r in got if r["kind"] not in ("plan", "ambiguous")}
        for r in A.declaration(norm):
            if r["ticker"] in claimed:
                continue
            r.update(ts=ts, id=mid, text=text, src=src)
            recs.append(r)

        # A flat-book declaration names no ticker, so it cannot be one of
        # the per-ticker records above; it is carried as a single record with ticker
        # None and resolved against the running book in state.walk.
        if A.flat_declaration(norm):
            recs.append(dict(ticker=None, kind="flat", size_level=None,
                             size_word=None, verb=None, state_only=False,
                             clause=None, direction=None, conditional=False,
                             leveraged=False, inverse=False,
                             ts=ts, id=mid, text=text, src=src))
    return msgs, recs, vocab


def price_at(ts_list, px_list, when, shift=0):
    """Close of the bar CONTAINING `when`. None if outside coverage.

    Second look-ahead bug, found by asking whether hourly resolution is good enough.
    A bar stamped 14:30 spans 14:30-15:30 and its Close is the 15:30 print. Taking the
    first bar at-or-AFTER the message therefore used the bar starting 15:30, whose close
    is 16:30 - a message at 14:52 was priced 1h38m later.

    Using the containing bar caps the lag at one hour instead of two. It is still
    forward-looking by roughly half an hour on average, which matters more than it
    sounds when the author posts while a move is under way: a systematic lag captures
    more of the run than the author could have. Minute bars would remove it; hourly is
    what is available for a multi-month window without a paid feed.
    """
    if not ts_list:
        return None
    step = (ts_list[1] - ts_list[0]) if len(ts_list) > 1 else dt.timedelta(hours=1)
    i = bisect.bisect_right(ts_list, when) - 1

    # DURING a session: the bar CONTAINING the message, i.e. the one still open when the author
    # wrote. The test is that the bar has not closed yet - `when < stamp + step` - not
    # a tolerance in hours. A six-hour tolerance let a midnight message use the
    # previous evening's final bar, whose close printed 3.5 hours EARLIER, handing the author
    # the overnight gap for free.
    if 0 <= i < len(px_list) and when < ts_list[i] + step:
        k = i + shift
        return px_list[k] if 0 <= k < len(px_list) else None

    # OUTSIDE a session - after hours, overnight, a weekend, a holiday. The author cannot
    # trade at the moment the author writes, so the first tradeable price is the next bar.
    # This is not look-ahead: it is the earliest fill actually available to the author.
    # Many such messages exist in a window; discarding them would throw away real
    # decisions, most of them evening posts before the next morning's open.
    j = bisect.bisect_left(ts_list, when) + shift
    if 0 <= j < len(px_list):
        return px_list[j]
    return None


def fill_index(ts_list, when, shift=0):
    """The bar index price_at would use, or None. Kept beside it so they cannot drift.

    Exists so the fill TIME can be checked. A bar stamped 14:30 prints its close at
    15:30, so the fill instant is the bar's stamp plus one interval - not the stamp.
    Measuring the stamp makes every fill look like it happened before the message,
    which is the one alarm nobody should raise falsely: filling an hour early can move
    realised P&L materially, so a spurious early-fill report
    would send someone hunting a bug that is not there, and a real one must not hide
    among false ones.
    """
    if not ts_list:
        return None
    step = (ts_list[1] - ts_list[0]) if len(ts_list) > 1 else dt.timedelta(hours=1)
    i = bisect.bisect_right(ts_list, when) - 1
    if 0 <= i < len(ts_list) and when < ts_list[i] + step:
        k = i + shift
        return k if 0 <= k < len(ts_list) else None
    j = bisect.bisect_left(ts_list, when) + shift
    return j if 0 <= j < len(ts_list) else None


def fill_time(ts_list, when, shift=0):
    """When the chosen bar actually prints: its stamp plus one bar interval."""
    i = fill_index(ts_list, when, shift)
    if i is None:
        return None
    step = (ts_list[1] - ts_list[0]) if len(ts_list) > 1 else dt.timedelta(hours=1)
    return ts_list[i] + step


def main():
    ap = argparse.ArgumentParser()
    # CONFIGURE ME: the date your own window starts.
    ap.add_argument("--since", default="")
    ap.add_argument("--fetch", action="store_true", help="download missing price data")
    ap.add_argument("--unit", type=float, default=UNIT)
    ap.add_argument("--per-share", type=float, default=PER_SHARE)
    ap.add_argument("--min-commission", type=float, default=MIN_DOLLAR)
    ap.add_argument("--spread-bps", type=float, default=SPREAD_BPS)
    ap.add_argument("--default-add", type=float, default=None)
    ap.add_argument("--default-cut", type=float, default=None)
    ap.add_argument("--default-entry", type=float, default=None)
    ap.add_argument("--min-records", type=int, default=2,
                    help="corroboration threshold; 1 disables it")
    ap.add_argument("--no-declarations", action="store_true",
                    help="ignore bare state declarations entirely")
    ap.add_argument("--bar-shift", type=int, default=0,
                    help="deliberately fill N bars later (or earlier); measures how "
                         "much the result depends on fill timing at all")
    # OFF BY DEFAULT, deliberately.
    #
    # An account size quoted from anywhere other than your own records is not a
    # calibration for the unit this study uses: it may describe a different account than
    # the one these messages are about. Do not rest the unit on it.
    ap.add_argument("--account", type=float, default=None,
                    help="quote the return against a stated account size, if one is "
                         "known. Not defaulted: see the note in the source.")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--dump-open", metavar="PATH",
                    help="write the open-position detail as JSON, for reviewing "
                         "positions never seen closed. Written from THIS replay so it "
                         "can never disagree with the headline number.")
    ap.add_argument("--dump-pnl", metavar="PATH",
                    help="write realised and unrealised P&L per ticker as CSV. The "
                         "headline mixes the two, and they are not equally knowable: "
                         "realised is a completed round trip, unrealised is a position "
                         "we never saw closed and is only as good as our reading of "
                         "why it stayed open.")
    args = ap.parse_args()

    # The vague-quantity defaults live in state.py so they can be varied from here.
    if args.default_add is not None:
        S.DEFAULT_ADD = args.default_add
    if args.default_cut is not None:
        S.DEFAULT_CUT = args.default_cut
    if args.default_entry is not None:
        S.DEFAULT_ENTRY = args.default_entry

    msgs, recs, vocab = load_records(args.since)

    # CORROBORATION RULE.
    #
    # A ticker whose entire history is one record is not a position, it is a mention.
    # "[symbol]" appears once in a message carrying a size word, and the state machine
    # dutifully opens half a position that then runs to the end of the window, because
    # the ticker is never spoken of again. A large share of "open" positions can be
    # this, and a single phantom like it can end up the biggest line in the book.
    #
    # Requiring a second record keeps only tickers that were returned to. It discards some
    # genuine one-and-done trades, which is the conservative direction: an unclosed
    # position contributes only unrealised P&L, priced off an entry we had to guess.
    if args.no_declarations:
        recs = [r for r in recs if not r.get("state_only")]
    # A flat-book declaration ("כרגע ללא עסקאות") carries ticker None: it is not a
    # mention of anything, it is the instruction that closes the whole book. Counting it
    # like a ticker dropped it whenever it appeared fewer than `min-records` times - and
    # then crashed on sorting None beside real tickers.
    counts = collections.Counter(r["ticker"] for r in recs if r["ticker"] is not None)
    dropped = sorted(t for t, n in counts.items() if n < args.min_records)
    recs = [r for r in recs
            if r["ticker"] is None or counts[r["ticker"]] >= args.min_records]

    trades = list(S.walk(recs))
    tickers = sorted({t["ticker"] for t in trades})
    if dropped and not args.quiet:
        print("dropped %d single-mention tickers (not positions): %s"
              % (len(dropped), " ".join(dropped)))
    if not args.quiet:
        print("messages %d | records %d | implied trades %d | tickers %d"
              % (len(msgs), len(recs), len(trades), len(tickers)))

    if args.fetch:
        print("\nfetching hourly bars ...")
        P.main(tickers)

    series, missing = {}, []
    for t in tickers:
        ts, px = P.load(t)
        if ts:
            series[t] = (ts, px)
        else:
            missing.append(t)
    if missing:
        print("\nNO PRICE DATA for %d tickers: %s" % (len(missing), " ".join(missing)))
        print("  Their trades are excluded and reported, not silently dropped.")

    # One valuation instant, never earlier than a priced trade. Stale instruments
    # carry their last available quote at that instant and are identified below.
    as_of = max((s[0][-1] for s in series.values()), default=None)

    # Replay. Position value is fraction * UNIT, so a change of `delta` trades
    # |delta| * UNIT dollars at that moment's price.
    held = collections.defaultdict(float)        # ticker -> held fraction, 0..1
    shares = collections.defaultdict(float)      # ticker -> shares (fractional allowed)
    basis = collections.defaultdict(float)       # ticker -> cost basis in dollars
    directions = {}
    realised = 0.0
    costs = 0.0
    skipped = 0
    per_ticker = collections.defaultdict(float)
    log = []

    for tr in trades:
        t = tr["ticker"]
        if t not in series:
            skipped += 1
            continue
        when = dt.datetime.strptime(tr["ts"], "%Y-%m-%d %H:%M:%S")
        if as_of is not None and when > as_of:
            skipped += 1
            continue
        px = price_at(series[t][0], series[t][1], when, args.bar_shift)
        if px is None or px <= 0:
            skipped += 1
            continue

        delta = tr["delta"]
        if delta > 0:
            # A BUY of delta * UNIT dollars at this price.
            dollars = delta * args.unit
            qty = dollars / px
            basis[t] += dollars
            shares[t] += qty
            held[t] += delta
            directions[t] = -1 if tr.get("direction") == "short" else 1
        else:
            # A SELL of the share count proportional to the fraction being given up.
            # Proceeds are shares * price, NOT delta * UNIT: the position has moved
            # since it was opened, which is the entire thing being measured.
            frac_sold = min(1.0, abs(delta) / held[t]) if held[t] > 1e-9 else 1.0
            qty = shares[t] * frac_sold
            dollars = qty * px
            cost_out = basis[t] * frac_sold
            pnl = directions.get(t, 1) * (dollars - cost_out)
            realised += pnl
            per_ticker[t] += pnl
            basis[t] -= cost_out
            shares[t] -= qty
            held[t] = max(0.0, held[t] + delta)
        if qty <= 1e-12:
            continue
        comm = max(args.per_share * qty, args.min_commission)
        slip = dollars * args.spread_bps / 10000.0
        costs += comm + slip
        log.append(dict(ts=tr["ts"], ticker=t, delta=round(delta, 3), px=px,
                        direction="short" if directions.get(t, 1) == -1 else "long",
                        level=round(tr["level_after"], 3), reason=tr["reason"],
                        comm=round(comm, 2), id=tr.get("id"),
                        said=(tr.get("text") or "")[:600]))

    # Mark whatever is still open, at ONE common instant.
    #
    # Carry the last available quote to the common endpoint and disclose stale marks.
    # Moving the endpoint backwards to the earliest cache would value later entries
    # before they were opened and put the capital timeline out of order.
    open_val = 0.0
    still = []
    stale = []
    for t, f in held.items():
        if f <= 1e-9 or t not in series or shares[t] <= 1e-12:
            continue
        mark_idx = bisect.bisect_right(series[t][0], as_of) - 1
        px_end = series[t][1][mark_idx]
        if series[t][0][mark_idx] < as_of:
            stale.append(t)
        val = shares[t] * px_end
        unreal = directions.get(t, 1) * (val - basis[t])
        open_val += unreal
        per_ticker[t] += unreal
        still.append((t, f, unreal))

    # A percentage needs a denominator. "Percent of one unit" is meaningless when up to
    # forty units are deployed at once. Capital actually at work is the honest base.
    #
    # It must be TIME-weighted. Averaging the level after each action weights by how
    # often the author writes, so a chatty afternoon on a small book counts as heavily
    # as a month holding a large one, and the reported return moves with posting
    # frequency rather than with exposure.
    running = collections.defaultdict(float)
    marks = []                      # (timestamp, capital deployed at that moment)
    for r in log:
        running[r["ticker"]] = r["level"]
        marks.append((dt.datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S"),
                      sum(running.values()) * args.unit))
    if marks and as_of is not None:
        marks.append((as_of, marks[-1][1]))
    area = span = 0.0
    for (t0, cap), (t1, _) in zip(marks, marks[1:]):
        secs = max(0.0, (t1 - t0).total_seconds())
        area += cap * secs
        span += secs
    avg_cap = (area / span) if span > 0 else (marks[0][1] if marks else args.unit)
    peak_cap = max((c for _, c in marks), default=args.unit)
    net = realised + open_val - costs

    if args.dump_pnl:
        unreal_by = collections.defaultdict(float)
        for t, f, v in still:
            unreal_by[t] += v
        with io.open(args.dump_pnl, "w", encoding="utf-8-sig", newline="") as fh:
            wr = csv.writer(fh)
            wr.writerow(["ticker", "realised", "unrealised", "total", "still_open"])
            for t in sorted(per_ticker):
                u = unreal_by.get(t, 0.0)
                wr.writerow([t, round(per_ticker[t] - u, 2), round(u, 2),
                             round(per_ticker[t], 2), int(t in unreal_by)])

    if args.dump_open:
        # Everything a human needs to rule on a position the model never saw closed.
        #
        # The decisive evidence is what the author said about the ticker AFTER our last
        # recorded trade in it. If the author went on discussing it for months, the mark is
        # probably legitimate; if the author named it once more in a way we failed to parse,
        # that message is the missed exit and its date is where the position ended.
        by_ticker = collections.defaultdict(list)
        for r in log:
            by_ticker[r["ticker"]].append(r)

        rows = []
        for t, f, unreal in sorted(still, key=lambda z: z[2]):
            mine = by_ticker[t]
            last_ts = mine[-1]["ts"] if mine else args.since
            # Every later mention is a CANDIDATE EXIT, so price each one: what the
            # position would have been worth had the author closed it at that message. The
            # reader is being asked when it ended, and cannot answer that without
            # seeing what each possible ending was worth.
            after = []
            for ts, mid, src, text, norm in msgs:
                if ts <= last_ts or t not in A.tickers(norm):
                    continue
                when = dt.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                mpx = price_at(series[t][0], series[t][1], when, args.bar_shift)
                # Did the extractor SEE this message, or walk past it? A mention
                # that produced a record and simply left the level unchanged is
                # already understood; a mention that produced nothing is where a
                # missed exit hides. The reader needs those two marked differently,
                # or every later message looks equally suspicious.
                got = A.extract(norm)
                mine_r = [g for g in got if g["ticker"] == t]
                if not got:
                    mine_r = [g for g in A.declaration(norm) if g["ticker"] == t]
                parsed = [dict(kind=g.get("kind"),
                               state_only=bool(g.get("state_only")),
                               size=g.get("size_level"),
                               clause=g.get("clause")) for g in mine_r]
                after.append(dict(ts=ts, id=mid, src=src, text=text, px=mpx,
                                  pnl=(directions.get(t, 1) * (shares[t] * mpx - basis[t])) if mpx else None,
                                  parsed=parsed))

            # What the position would be worth had it closed at the author's last mention of
            # it, rather than running to as_of. This is the number a "the author exited and
            # we missed it" ruling implies, so the sheet can price the ruling instead
            # of merely recording it.
            alt = None
            if after and after[-1]["pnl"] is not None:
                alt = dict(ts=after[-1]["ts"], px=after[-1]["px"],
                           pnl=after[-1]["pnl"])
            opened = mine[0] if mine else None
            rows.append(dict(
                ticker=t, frac=f, shares=shares[t], basis=basis[t],
                direction="short" if directions.get(t, 1) == -1 else "long",
                opened=opened,
                unrealised=unreal, realised=per_ticker[t] - unreal,
                px_end=(shares[t] and (directions.get(t, 1) * unreal + basis[t]) / shares[t]),
                trades=mine, last_ts=last_ts, mentions_after=after, alt_close=alt,
                records=counts[t], leveraged=t in A.LEVERAGED_ETFS,
                inverse=t in A.INVERSE_ETFS))

        payload = dict(generated=dt.datetime.now(dt.timezone.utc).isoformat()[:19],
                       since=args.since, unit=args.unit,
                       as_of=str(as_of) if as_of else None,
                       net=net, realised=realised, open_val=open_val,
                       costs=costs, avg_cap=avg_cap, positions=rows)
        with io.open(args.dump_open, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, indent=1))

    print("\n=== result%s ===" % ((", %s onward" % args.since) if args.since else ""))
    print("  unit (full position)      $%s" % format(args.unit, ",.0f"))
    print("  commission                $%.3f/share, $%.2f floor" % (args.per_share, args.min_commission))
    print("  half-spread charged       %.1f bps per action" % args.spread_bps)
    print("")
    print("  implied trades priced     %d  (skipped %d)" % (len(log), skipped))
    print("  realised P&L              %s" % money(realised))
    print("  open positions at end     %d, unrealised %s%s"
          % (len(still), money(open_val),
             ("  [%d marked stale: %s]" % (len(stale), " ".join(stale))) if stale else ""))
    if as_of is not None:
        print("  all marked as of          %s" % as_of)
    print("  transaction costs         %s" % money(-costs))
    print("  ------------------------------------------")
    print("  NET                       %s" % money(net))
    print("")
    print("  capital deployed: average $%s, peak $%s (%.1f units at once)"
          % (format(avg_cap, ",.0f"), format(peak_cap, ",.0f"), peak_cap / args.unit))
    print("  RETURN on average capital %+.2f%%" % (100.0 * net / avg_cap))
    if args.account:
        print("  RETURN on stated account  %+.2f%%   ($%s stated account size)"
              % (100.0 * net / args.account, format(args.account, ",.0f")))
        print("    average deployment %.0f%% of it, peak %.0f%%"
              % (100.0 * avg_cap / args.account, 100.0 * peak_cap / args.account))
    print("  RETURN on peak capital    %+.2f%%" % (100.0 * net / peak_cap))
    print("  realised only, avg cap    %+.2f%%" % (100.0 * (realised - costs) / avg_cap))

    # One number here would be dishonest, because it averages two things we know very
    # differently well. A realised leg is a round trip: a buy was stated, a sell was
    # stated, and the market priced both. An unrealised one is a position never seen
    # closed, marked to the end of the window - and some of those can turn out to be
    # positions never taken at all, which no later message could close.
    #
    # So the honest output is a bracket, not a point. The two ends are the two extreme
    # readings of the same replay: one counts every never-closed position as real, the
    # other is what remains if EVERY never-closed position is our error.
    print("")
    print("  the result is bracketed, because %d positions were never seen closed:"
          % len(still))
    print("    every one of them real, marked to %s   %+.2f%%"
          % (as_of or "the end", 100.0 * net / avg_cap))
    print("    every one of them a position the author never"
          " took   %+.2f%%" % (100.0 * (realised - costs) / avg_cap))
    share = abs(open_val) / abs(net) if net else 0.0
    print("    they carry %s of the %s net." % (money(open_val), money(net)))
    if share >= 0.25:
        print("    Which end is right is not a detail - it is most of the answer.")
    else:
        # When this block carries most of the net, the sentence below is the honest
        # headline. When it is a rounding error, saying so would overstate an
        # uncertainty that has already closed - so the wording follows the share.
        print("    That is %.0f%% of it, so the bracket is no longer where the"
              % (100 * share))
        print("    uncertainty lives.")
    # Both ends divide by the SAME average capital, and that denominator was measured
    # with those positions deployed. The lower end therefore DECOMPOSES this replay; it
    # does not predict a cleaner one, which would also have had less capital at work.
    print("    (both ends share a denominator measured WITH those positions deployed,")
    print("     so the lower end decomposes this replay rather than predicting one)")
    print("")
    top = sorted(per_ticker.items(), key=lambda kv: -kv[1])
    print("  best:  %s" % ", ".join("%s %s" % (k, money(v)) for k, v in top[:6]))
    print("  worst: %s" % ", ".join("%s %s" % (k, money(v)) for k, v in top[-6:]))

    print("SUMMARY net=%.0f realised=%.0f unreal=%.0f costs=%.0f avgcap=%.0f "
          "ret=%.4f retreal=%.4f trades=%d open=%d"
          % (net, realised, open_val, costs, avg_cap, net / avg_cap,
             (realised - costs) / avg_cap, len(log), len(still)))

    out = os.path.join(STOCKS, "data", "parsed", "trades.csv")
    if log:
        # Created here, not assumed: on a fresh unpack this folder does not exist yet.
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with io.open(out, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(log[0].keys()))
            w.writeheader()
            w.writerows(log)
        print("\n  wrote %s" % os.path.relpath(out, PROJECT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
