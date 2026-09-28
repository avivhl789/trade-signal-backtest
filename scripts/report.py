# -*- coding: utf-8 -*-
"""Reproducible headline report: join-month returns, management sensitivity, risk days.

Every figure in the write-up should be produced by this script.
Usage: python scripts/report.py
"""
import os, sys, datetime as dt, statistics, collections
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import book_engine as be, mtm, costs

DELAYS = (1, 2, 3, 4, 5)


def compounded_from(log, start, end=None):
    """Legacy R-compounding diagnostic; not a fresh-account cohort simulation."""
    eq = be.INITIAL
    legs = [l for l in log if l["ts"] >= start and (end is None or l["ts"] < end)]
    for l in sorted(legs, key=lambda x: x["exit_ts"]):
        eq += l["r"] * (eq * l["risk"] / 100.0)
    return 100 * (eq / be.INITIAL - 1)


def s2_logs(acts, n=40, seed=11, take_rate=0.8):
    rng = np.random.default_rng(seed)
    out = []
    ids = [a["id"] for a in acts]
    for _ in range(n):
        d, t = {}, {}
        for mid in ids:
            if mid not in d:
                d[mid] = int(rng.choice(DELAYS))
                t[mid] = bool(rng.random() < take_rate)
        out.append(be.run(acts, take=[t[m] for m in ids], delays=[d[m] for m in ids]))
    return out


def join_month_table(log=None, s2=None, first=None, last=None, acts=None, samples=40):
    acts = be.load_actions() if acts is None else acts
    # The grid follows the data unless the caller pins it: from the month of the first
    # action up to (not including) the month of the last.
    t0, t1 = min(a["ts"] for a in acts), max(a["ts"] for a in acts)
    first = first or (t0.year, t0.month)
    last = last or (t1.year, t1.month)
    spec = costs._specs.load()
    def net(legs):
        return 100 * sum(l["pnl"] - costs.financing_leg(l, spec) for l in legs) / be.INITIAL
    print("\n=== fresh-account net return by join month ===")
    print("  %-9s %9s %11s" % ("join", "S0", "S2 median"))
    m = dt.datetime(*first, 1)
    while m < dt.datetime(*last, 1):
        selected = [a for a in acts if a["ts"] >= m]
        a0 = net(be.run(selected))
        a2 = statistics.median(net(L) for L in s2_logs(selected, n=samples))
        print("  %-9s %+8.1f%% %+10.1f%%" % (m.strftime("%Y-%m"), a0, a2))
        m = (m.replace(day=28) + dt.timedelta(days=8)).replace(day=1)


def management_sensitivity(acts, seeds=200, seed=99):
    """Take every entry; miss management messages at rate X."""
    print("\n=== take every entry, miss management at rate X ===")
    print("  %-10s %12s %12s %11s" % ("miss", "return med", "p5", "maxDD med"))
    ids = [a["id"] for a in acts]
    is_entry = {}
    for a in acts:
        # Mixed posts contain both an entry and management. The message must remain
        # mandatory in an "every entry" experiment regardless of which action row is
        # encountered last.
        is_entry[a["id"]] = is_entry.get(a["id"], False) or a["kind"] == "entry"
    for miss in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0):
        rng = np.random.default_rng(seed)
        rets, dds = [], []
        n = 1 if miss in (0.0, 1.0) else seeds
        for _ in range(n):
            keep = {}
            for mid in ids:
                if mid not in keep:
                    keep[mid] = True if is_entry[mid] else (rng.random() >= miss)
            lg = be.run(acts, take=[keep[m] for m in ids])
            eq = be.INITIAL; peak = eq; mdd = 0.0
            for l in sorted(lg, key=lambda x: x["exit_ts"]):
                eq += l["pnl"]; peak = max(peak, eq); mdd = max(mdd, (peak - eq) / peak)
            rets.append(100 * (eq / be.INITIAL - 1)); dds.append(100 * mdd)
        rets.sort(); dds.sort()
        p = lambda a, q: a[int(q * (len(a) - 1))]
        print("  %-10s %+11.2f%% %+11.2f%% %10.2f%%" %
              ("%d%%" % (100 * miss), p(rets, .5), p(rets, .05), p(dds, .5)))


def worst_days(log, k=5):
    """Largest intraday equity drops against the configured $5,000 daily limit."""
    grid, balance, floating, equity, _ = mtm.equity_series(
        log, include_financing=True, adverse=True)
    days = mtm.local_days(grid)
    rows = []
    for d in np.unique(days):
        m = days == d
        first = int(np.flatnonzero(m)[0])
        base = balance[first - 1] if first else be.INITIAL
        rows.append((float(base - equity[m].min()), int(d)))
    rows.sort(reverse=True)
    print("\n=== worst intraday equity drops (Prague days) ===")
    for drop, d in rows[:k]:
        print("  %s  -$%-9s (%.2f%% of initial)" %
              (dt.date.fromordinal(d + 719163), format(drop, ",.0f"), 100 * drop / be.INITIAL))
    print("  evaluation daily limit $5,000 -> %s"
          % ("BREACHED" if rows[0][0] > 5000 else "not breached"))


if __name__ == "__main__":
    acts = be.load_actions()
    log = be.run(acts)
    b = be.LAST_BOOK
    print("=== book ===")
    print("  actions %d | legs %d | positions %d" %
          (len(acts), len(log), len({l["entry_id"] for l in log})))
    print("  pending: filled %d expired %d cancelled %d void %d" %
          (b.pending_filled, b.pending_expired, b.pending_cancelled, b.pending_void))
    print("  size-ups %d (void %d) | restores %d (void %d) | "
          "stop gap-fills %d | rejected stop-moves %d" %
          (b.size_ups, b.size_ups_void, b.restores, b.restores_void,
           b.stop_crossed_updates, b.rejected_stops))
    print("  queued actions cancelled %d | unresolved conditions %d | "
          "ambiguous sizing actions %d" %
          (b.scheduled_cancelled, b.conditional_unresolved,
           b.ambiguous_unresolved))
    print("  exits:", dict(collections.Counter(l["reason"] for l in log)))
    if b.unpriced:
        # A parsed action with no price file is not a small detail: the trade is
        # missing from every figure below, so name the instruments rather than
        # letting the book look complete.
        miss = collections.Counter(u["symbol"] for u in b.unpriced)
        print("  !! %d action(s) DROPPED - no price file for: %s"
              % (len(b.unpriced), ", ".join("%s x%d" % kv for kv in sorted(miss.items()))))
        print("     export those instruments with tools/cbot/HistoryExporter.cs, "
              "or they stay missing from every number above.")
    fin = costs.financing_cost(log)
    gross = sum(l["pnl"] for l in log)

    # THE HEADLINE IS AFTER FINANCING.
    #
    # It is easy to get this wrong: to print the raw sum of leg P&L as the headline and
    # relegate financing to a footnote underneath. A follower comparing that against a
    # broker statement would find the statement worse, because swap is charged on
    # NOTIONAL - which can be tens of times the amount risked - and a swing book can
    # carry gross exposure of a large fraction of the account.
    #
    # The generic-rate table below can understate it badly: a flat 5%/yr estimate can be
    # a small fraction of what real per-side CFD swap rates cost. CFD swap carries a
    # broker markup over any clean funding rate, and on crypto it is punitive (-30%/yr
    # on BOTH sides of bitcoin).
    spec_fin = costs.financing_from_specs(log)
    print("")
    print("=== headline ===")
    print("  gross (sum of leg P&L)        %+10.0f   %+7.2f%%" % (gross, gross / 1000.0))
    if spec_fin is not None:
        net = gross - spec_fin["total"]
        print("  broker swap on notional       %+10.0f" % (-spec_fin["total"]))
        print("  NET, after financing          %+10.0f   %+7.2f%%   <-- the number"
              % (net, net / 1000.0))
        worst = sorted(spec_fin["per_symbol"].items(), key=lambda kv: -kv[1])[:5]
        print("  heaviest carry: %s"
              % ", ".join("%s %.0f" % (s, -v) for s, v in worst))
    else:
        print("  broker specs unavailable - financing NOT applied; treat the gross")
        print("  figure as an upper bound, not a result.")

    print("")
    print("=== financing on overnight exposure ===")
    span_days = max(1.0, (max(l["exit_ts"] for l in log)
                          - min(l["ts"] for l in log)).total_seconds() / 86400.0)
    print("  notional-days $%s  (avg gross exposure %.0f%% of account)"
          % (format(fin["notional_days"], ",.0f"),
             100 * fin["notional_days"] / span_days / be.INITIAL))
    print("  %-10s %12s %14s" % ("rate/yr", "cost", "S0 after"))
    for rate in (0.0, 0.03, 0.05, 0.07, 0.10):
        c = costs.financing_cost(log, long_rate=rate)["total"]
        print("  %-10s %12s %13.2f%%" % ("%.0f%%" % (100*rate), format(c, ",.0f"),
                                         100 * (be.INITIAL + gross - c) / be.INITIAL - 100))

    worst_days(log)
    join_month_table(acts=acts)
    management_sensitivity(acts)
