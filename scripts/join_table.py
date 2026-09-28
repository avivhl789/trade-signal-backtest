# -*- coding: utf-8 -*-
"""What a channel returned, by the date you started following it.

A whole-record figure describes someone who followed from the first message to the last.
Nobody does that. What a subscriber actually gets depends on when they joined, and this
prints that spread instead of one number.

TWO CONVENTIONS THAT ARE EASY TO MIX UP, so both are stated here.

  * `report.py`'s join grid compounds R-multiples and is GROSS.
  * Everything below is NET of per-leg financing and simple on the account base.
    Financing is allocated to the leg that incurred it, so a cohort is charged only the
    carry it actually paid.

Quoting a row from one beside a row from the other invites a comparison that does not
hold. Keep them apart.

S0 is the ideal follower: every message, acted on instantly. S2 is realistic: a random
1-5 bar delay and 80% of messages acted on, median of 40 draws, with the 10th and 90th
percentiles printed because the spread between them is the honest width of the answer.

Usage: python scripts/join_table.py [--json PATH]
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import statistics
import numpy as np
import mtm
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import book_engine as be   # noqa: E402
import report as R         # noqa: E402
from costs import _specs   # noqa: E402

# CONFIGURE ME: the date you started following the channel.
JOIN = dt.datetime(2025, 1, 1)
SPEC = _specs.load()

# CONFIGURE ME. Trading return alone does not answer "was it worth it"; these are the
# costs it has to clear first. Left at 0 the subscription is simply not charged, and the
# break-even lines below are meaningless - put in what you actually paid.
SUB_ILS = 0.0            # one year of channel access, in shekels
EVALUATION_USD = 600.0   # cost of the configured $100k evaluation preset
ILS_PER_USD = 3.7        # stated, not looked up - see --rate and the sensitivity output

# The configured evaluation limits are STATIC: measured against the initial balance,
# not a trailing peak.
# That distinction decides whether an account survives, and it is the reason the worst
# peak-to-trough figure is not the number that matters.
EVALUATION_MAX_LOSS = 10.0
EVALUATION_MAX_DAILY = 5.0

# THE PART THAT CHANGES THE ANSWER.
#
# This preset models a two-stage evaluation: a +10% phase, a +5% phase, and then a
# funded account whose eligible profit is split 80/20 in the trader's favour.
#
# Profit made inside the two evaluation phases is NEVER paid out. It only unlocks the next
# phase, and each phase restarts at the initial balance. That reset is decisive here: a
# cohort that climbs 10% and then 5% arrives at its funded account with NO buffer, having
# spent every point of gain on qualifying. Modelling this as a single account - which an
# earlier version of this report did - credits the trader with profit they could never
# withdraw and lends them a cushion they do not have.
EVALUATION_TARGETS = (10.0, 5.0)
PAYOUT_SPLIT = 0.80


def financing(leg):
    """Swap actually paid by this leg, on calendar days held."""
    sp = SPEC.get(leg["symbol"])
    if sp is None or not leg.get("exit_ts") or not leg.get("ts"):
        return 0.0
    days = (leg["exit_ts"] - leg["ts"]).total_seconds() / 86400.0
    if days <= 0:
        return 0.0
    per_unit, pct_notional = _specs.swap_daily(sp, leg.get("dir", 1))
    units = leg["units"] * leg["frac"]
    cost = units * per_unit * days
    if pct_notional:
        cost += units * leg["entry_px"] * pct_notional * days
    return cost


def net_from(log, start):
    """Net percentage of a ledger subset; use fresh_log/cohort for new accounts."""
    legs = [l for l in log if l["ts"] >= start]
    return 100.0 * sum(l["pnl"] - financing(l) for l in legs) / be.INITIAL


def risk_from(log, start):
    """Conservative OHLC equity loss and Prague-day loss, including financing."""
    legs = [l for l in log if l["ts"] >= start]
    out = mtm.equity_series(legs, include_financing=True, adverse=True)
    if out is None:
        return 0.0, 0.0
    grid, balance, _, equity, _ = out
    days = mtm.local_days(grid)
    starts = np.r_[0, np.flatnonzero(np.diff(days)) + 1]
    bases = np.r_[be.INITIAL, balance[starts[1:] - 1]]
    daily_base = np.repeat(bases, np.diff(np.r_[starts, len(grid)]))
    return (100.0 * min(0.0, float(equity.min()) - be.INITIAL) / be.INITIAL,
            100.0 * min(0.0, float((equity - daily_base).min())) / be.INITIAL)


def drawdown_from(log, start):
    """Close-marked net-equity peak-to-trough, in percentage points of initial."""
    out = mtm.equity_series([l for l in log if l["ts"] >= start], include_financing=True)
    if out is None:
        return 0.0
    equity = out[3]
    peak = np.maximum.accumulate(np.r_[be.INITIAL, equity])[1:]
    return 100.0 * float((equity - peak).min()) / be.INITIAL


def fresh_log(acts, start, **kwargs):
    """A new account sees only messages available from its joining time onward."""
    return be.run([a for a in acts if a["ts"] >= start], **kwargs)


def evaluation_phases(log, start, targets=EVALUATION_TARGETS, acts=None):
    """Evaluate sequential flat-book phase transitions with net OHLC equity.

    Each phase restarts at INITIAL. With source actions, sizing is replayed after
    every reset. Without actions this is explicitly a fixed-ledger diagnostic.
    OHLC adverse marks are a conservative bound, not synchronized intraminute ticks.
    """
    remaining = [l for l in log if l["ts"] >= start]
    passed, funded_from = [], None
    peak = ending = 0.0
    for phase in range(len(targets) + 1):
        if not remaining:
            break
        target = targets[phase] if phase < len(targets) else float("inf")
        result = mtm.evaluation_evaluate(
            remaining, profit_pct=target,
            max_loss_pct=EVALUATION_MAX_LOSS,
            daily_loss_pct=EVALUATION_MAX_DAILY,
            include_financing=True)
        hit, breach = result["target_time"], result["breach_time"]
        if breach is not None and (hit is None or breach <= hit):
            return dict(outcome="burned in phase %d" % (phase + 1), passed=passed,
                        funded_from=funded_from, peak=peak, ending=ending,
                        breach_time=str(breach), basis="conservative OHLC equity")
        if phase == len(targets):
            series = mtm.equity_series(remaining, include_financing=True)
            peak = max(0.0, float(series[1].max()) - be.INITIAL)
            ending = float(series[1][-1]) - be.INITIAL
            break
        if hit is None:
            break
        passed.append([phase + 1, str(hit)])
        if phase + 1 == len(targets):
            funded_from = str(hit)
        # Passing requires a flat book. Pending/new messages are restarted in the
        # next account; no open position is silently transferred across phases.
        remaining = (be.run([a for a in acts if a["ts"] > hit]) if acts is not None
                     else [l for l in remaining if l["ts"] > hit])
    return dict(outcome="funded" if len(passed) == len(targets) else "never funded",
                passed=passed, funded_from=funded_from, peak=peak, ending=ending,
                basis="conservative OHLC equity",
                sizing="fresh phase replay" if acts is not None else "fixed ledger")


def months_between(start, end):
    return (end.year - start.year) * 12 + (end.month - start.month) + 1


def cohort(log, s2, start, label, acts=None, samples=40):
    if acts is not None:
        selected = [a for a in acts if a["ts"] >= start]
        log = be.run(selected)
        s2 = R.s2_logs(selected, n=samples)
    s2_vals = sorted(net_from(L, start) for L in s2)
    legs = [l for l in log if l["ts"] >= start]
    worst_static, worst_day = risk_from(log, start)
    phases = evaluation_phases(log, start, acts=acts)
    return {
        "evaluation": phases,
        "basis": "fresh account" if acts is not None else "ledger subset",
        "payout_peak": PAYOUT_SPLIT * max(0.0, phases["peak"]),
        "payout_held": PAYOUT_SPLIT * max(0.0, phases["ending"]),
        "worst_from_start": worst_static,
        "worst_day": worst_day,
        "evaluation_breach": (worst_static <= -EVALUATION_MAX_LOSS
                              or worst_day <= -EVALUATION_MAX_DAILY),
        "join": label,
        "s0": net_from(log, start),
        "s2_median": statistics.median(s2_vals),
        "s2_p10": s2_vals[max(0, int(0.10 * len(s2_vals)) - 1)],
        "s2_p90": s2_vals[min(len(s2_vals) - 1, int(0.90 * len(s2_vals)))],
        "drawdown": drawdown_from(log, start),
        "legs": len(legs),
        "months": months_between(start, max(l["exit_ts"] for l in log)) if legs else 0,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None)
    ap.add_argument("--samples", type=int, default=40)
    ap.add_argument("--sub-ils", type=float, default=SUB_ILS)
    ap.add_argument("--evaluation-usd", type=float, default=EVALUATION_USD,
                    help="cost of the configured evaluation account")
    ap.add_argument("--rate", type=float, default=ILS_PER_USD,
                    help="ILS per USD. Stated, not looked up; sensitivity is printed.")
    args = ap.parse_args()

    acts = be.load_actions()
    log = be.run(acts)
    book = be.LAST_BOOK
    s2 = []  # each joining account gets its own replay below

    # An action with no price file is missing from every number below. Saying so here is
    # not a detail: the table looks complete either way.
    if getattr(book, "unpriced", None):
        missing = collections.Counter(u["symbol"] for u in book.unpriced)
        print("!! %d action(s) DROPPED - no price file for: %s"
              % (len(book.unpriced),
                 ", ".join("%s x%d" % kv for kv in sorted(missing.items()))))
        print("   every figure below is computed without them. Export those instruments "
              "with tools/cbot/HistoryExporter.cs.\n")

    first = min(l["ts"] for l in log)
    last = max(l["exit_ts"] for l in log)

    rows = []
    month = dt.datetime(first.year, first.month, 1)
    while month <= last:
        if any(l["ts"] >= month for l in log):
            rows.append(cohort(log, s2, month, month.strftime("%Y-%m"), acts, args.samples))
        month = (month.replace(day=28) + dt.timedelta(days=8)).replace(day=1)

    joined = cohort(log, s2, JOIN, JOIN.strftime("%Y-%m-%d"), acts, args.samples)
    s2 = R.s2_logs([a for a in acts if a["ts"] >= JOIN], n=args.samples)

    print("=== futures: return by join date ===")
    if SPEC:
        print("Net of per-leg financing, simple %% of the $%s account."
              % format(be.INITIAL, ",.0f"))
    else:
        # financing() returns 0 for every leg without broker rows. Printing "net of
        # financing" then overstates a CFD book, which pays swap on notional.
        print("GROSS of financing, simple %% of the $%s account."
              % format(be.INITIAL, ",.0f"))
        print("No data/prices/ctrader/_specs.csv, so swap is NOT charged and every "
              "figure below is an upper bound. Run tools/cbot/SpecExporter.cs.")
    print("Whole record (join %s): %+.2f%%   %d legs"
          % (first.strftime("%Y-%m-%d"), net_from(log, first), len(log)))
    print()
    print("  %-10s %9s %10s %18s %10s %7s %7s"
          % ("join", "S0", "S2 med", "S2 p10-p90", "worst DD", "legs", "months"))
    for row in rows:
        print("  %-10s %+8.2f%% %+9.2f%% %8.2f%% %+7.2f%% %9.2f%% %7d %7d"
              % (row["join"], row["s0"], row["s2_median"], row["s2_p10"],
                 row["s2_p90"], row["drawdown"], row["legs"], row["months"]))
    print()
    print("  %-10s %+8.2f%% %+9.2f%% %8.2f%% %+7.2f%% %9.2f%% %7d %7d   <-- you"
          % (joined["join"], joined["s0"], joined["s2_median"], joined["s2_p10"],
             joined["s2_p90"], joined["drawdown"], joined["legs"], joined["months"]))

    # ---- what it cost, and what had to be cleared before any of it was profit -------
    breached = [r for r in rows if r["evaluation_breach"]]
    print()
    print("=== evaluation survival (static limits: %.0f%% max loss, %.0f%% max daily) ==="
          % (EVALUATION_MAX_LOSS, EVALUATION_MAX_DAILY))
    print("  %-10s %18s %12s   %s"
          % ("join", "worst from start", "worst day", "survives?"))
    for row in rows + [joined]:
        print("  %-10s %17.2f%% %11.2f%%   %s"
              % (row["join"], row["worst_from_start"], row["worst_day"],
                 "ACCOUNT BURNED" if row["evaluation_breach"] else "yes"))
    print("  join months that burn the account: %s"
          % (" ".join(r["join"] for r in breached) or "none"))

    print()
    print("=== net of what you paid ===")
    sub_usd = args.sub_ils / args.rate
    cost = sub_usd + args.evaluation_usd
    print("  subscription  %s ILS  = $%s at %.2f ILS/USD"
          % (format(args.sub_ils, ",.0f"), format(sub_usd, ",.0f"), args.rate))
    print("  evaluation    $%s" % format(args.evaluation_usd, ",.0f"))
    print("  total         $%s  = %.2f%% of the account that must be earned first"
          % (format(cost, ",.0f"), 100.0 * cost / be.INITIAL))
    print()
    draws = sorted(net_from(L, JOIN) for L in s2)
    cleared = sum(1 for d in draws if d / 100.0 * be.INITIAL > cost)
    for label, gross in (("S0 ideal", joined["s0"]),
                         ("S2 p10", joined["s2_p10"]),
                         ("S2 median", joined["s2_median"]),
                         ("S2 p90", joined["s2_p90"])):
        dollars = gross / 100.0 * be.INITIAL
        print("  %-10s trading %+7.2f%% = $%9s   after fees $%9s  (%+.2f%%)"
              % (label, gross, format(dollars, ",.0f"),
                 format(dollars - cost, ",.0f"), 100.0 * (dollars - cost) / be.INITIAL))
    print()
    print("  realistic draws that earned back the fees: %d of %d (%.0f%%)"
          % (cleared, len(draws), 100.0 * cleared / len(draws)))
    print("  sensitivity to the exchange rate:")
    for rate in (3.4, 3.6, 3.8, 4.0):
        c = args.sub_ils / rate + args.evaluation_usd
        n = sum(1 for d in draws if d / 100.0 * be.INITIAL > c)
        print("    %.2f ILS/USD  break-even %.2f%%  %d of %d draws clear it"
              % (rate, 100.0 * c / be.INITIAL, n, len(draws)))

    negative = [r for r in rows if r["s0"] < 0]
    print()
    print("join months with a negative S0: %d of %d%s"
          % (len(negative), len(rows),
             ("  (%s)" % " ".join(r["join"] for r in negative)) if negative else ""))
    print("data ends %s" % last)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            draws = sorted(net_from(L, JOIN) for L in s2)
            cost = args.sub_ils / args.rate + args.evaluation_usd
            json.dump({"whole_record": net_from(log, first), "legs": len(log),
                       "your_join": joined, "cohorts": rows, "data_ends": str(last),
                       "costs": {
                           "sub_ils": args.sub_ils,
                           "evaluation_usd": args.evaluation_usd,
                           "rate": args.rate, "total_usd": cost,
                           "breakeven_pct": 100.0 * cost / be.INITIAL,
                           "draws_clearing": sum(
                               1 for d in draws if d / 100.0 * be.INITIAL > cost),
                           "draws": len(draws),
                           "owner_after_fees_usd":
                               joined["s2_median"] / 100.0 * be.INITIAL - cost,
                       },
                       "burned": [r["join"] for r in rows
                                  if r["evaluation_breach"]]},
                      fh, indent=2)
        print("wrote %s" % args.json)


if __name__ == "__main__":
    main()
