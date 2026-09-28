# -*- coding: utf-8 -*-
"""Financing (swap) on overnight CFD exposure.

The spread/commission model in `book_engine.COST` is a per-side price offset, so it
scales with *stop distance*. Financing does not: it is charged on *notional*, which can
be many times the risked amount when stops are tight relative to price. It therefore
has to be modelled separately or it is silently omitted.

Convention: on CFDs the long side generally pays benchmark + a broker markup, and the
short side receives benchmark - a markup (often negative in practice). Rates here are
annualised and applied per calendar day held, on the still-open fraction.
"""
import collections, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import specs as _specs

DEFAULT_LONG_RATE = 0.05     # annualised, paid by longs
DEFAULT_SHORT_RATE = 0.00    # annualised, received by shorts (conservative: assume none)


def financing_cost(log, long_rate=DEFAULT_LONG_RATE, short_rate=DEFAULT_SHORT_RATE):
    """Total financing paid across the book, plus a per-instrument breakdown."""
    total = 0.0
    per_symbol = collections.defaultdict(float)
    notional_days = 0.0
    for l in log:
        days = (max(0.0, (l["exit_ts"] - l["ts"]).total_seconds()) / 86400.0
                if l.get("ts") is not None and l.get("exit_ts") is not None
                else max(l.get("hold_min", 0), 0) / 1440.0)
        if days <= 0:
            continue
        notional = l["units"] * l["frac"] * l["entry_px"]
        notional_days += notional * days
        rate = long_rate if l.get("dir", 1) == 1 else -short_rate
        cost = notional * days / 365.0 * rate
        total += cost
        per_symbol[l["symbol"]] += cost
    return {"total": total, "per_symbol": dict(per_symbol),
            "notional_days": notional_days}


def exposure_summary(log):
    """Notional-to-risk leverage, which is what makes financing non-negligible."""
    first = {}
    for l in log:
        first.setdefault(l.get("pos_id") or (l["entry_id"], l["symbol"]), l)
    rows = collections.defaultdict(list)
    for l in first.values():
        rows[l["symbol"]].append(l["units"] * l["entry_px"])
    return {s: (len(v), sum(v) / len(v)) for s, v in rows.items()}


def financing_leg(leg, spec=None):
    """Calendar-time financing for exactly the exited fraction of one leg."""
    spec = _specs.load() if spec is None else spec
    sp = (spec or {}).get(leg["symbol"])
    if sp is None or leg.get("ts") is None or leg.get("exit_ts") is None:
        return 0.0
    days = max(0.0, (leg["exit_ts"] - leg["ts"]).total_seconds() / 86400.0)
    per_unit, pct_notional = _specs.swap_daily(sp, leg.get("dir", 1))
    units = leg["units"] * leg["frac"]
    return units * days * (per_unit + leg.get("entry_px", 0.0) * pct_notional)


def financing_from_specs(log):
    """Financing using the broker's real per-side swap rates.

    cTrader quotes swap either in pips (a fixed price amount per unit per day) or as
    an annual percentage of notional. Both forms appear in this account: indices and
    metals are quoted in pips, bitcoin as -30% on both sides.

    Swap accrues on CALENDAR days, including weekends. `hold_min` counts market
    minutes and so understates the elapsed time whenever a position spans a weekend,
    which is why the elapsed time is taken from the timestamps instead.

    The usual triple-Wednesday convention only redistributes the weekend charge onto
    one trading day; over a full week it still totals seven days, so charging calendar
    days directly is equivalent and avoids inventing a multiplier.
    """
    spec = _specs.load()
    if not spec:
        return None
    total = 0.0
    per_symbol = collections.defaultdict(float)
    per_source = collections.defaultdict(float)
    for l in log:
        sp = spec.get(l["symbol"])
        if sp is None:
            continue
        if l.get("exit_ts") is None or l.get("ts") is None:
            continue
        days = (l["exit_ts"] - l["ts"]).total_seconds() / 86400.0
        if days <= 0:
            continue
        cost = financing_leg(l, spec)
        total += cost
        per_symbol[l["symbol"]] += cost
        if l.get("entry_id") is not None:
            per_source[(l["entry_id"], l.get("entry_seq"))] += cost
    return {"total": total, "per_symbol": dict(per_symbol),
            "per_source": dict(per_source)}
