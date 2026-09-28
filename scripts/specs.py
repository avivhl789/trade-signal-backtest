# -*- coding: utf-8 -*-
"""Broker contract specifications, read from the live cTrader account.

`tools/cbot/SpecExporter.cs` writes data/prices/ctrader/_specs.csv straight from the
operator's account, which is authoritative in a way a marketing page is not: commission and
swap are account-specific. This module turns that into the per-side price cost the
engine uses, and the real swap rates for financing.

Until the file exists, the engine keeps its documented provisional values.
"""
import csv, io, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC_PATH = os.path.join(ROOT, "data", "prices", "ctrader", "_specs.csv")

# csv symbol name -> our canonical name
# Starter set, same as symbolmap.CSV_FOR - extend both together. An instrument enters
# the book only when it has BOTH a contract row here and an M1 file, so a half-finished
# export can never be priced with guessed numbers.
CANON = {"US100.cash": "US100", "GER40.cash": "GER40",
         "XAUUSD": "XAUUSD", "XAGUSD": "XAGUSD",
         "BTCUSD": "BTCUSD", "TSLA": "TSLA"}


def load():
    if not os.path.exists(SPEC_PATH):
        return {}
    out = {}
    with io.open(SPEC_PATH, encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            name = CANON.get(row["symbol"], row["symbol"])
            def num(k, default=0.0):
                try:
                    return float(row.get(k) or default)
                except ValueError:
                    return default
            out[name] = {
                "spread": num("spread_now"),
                "commission": num("commission"),
                "commission_type": (row.get("commission_type") or "").strip(),
                "swap_long": num("swap_long"),
                "swap_short": num("swap_short"),
                "swap_calc": (row.get("swap_calc") or "").strip(),
                "tick_size": num("tick_size"),
                "tick_value": num("tick_value"),
                "lot_size": num("lot_size", 1.0),
                "pip_size": num("pip_size"),
            }
    return out


def commission_pct(spec):
    """Commission as a percentage of notional, per side.

    Some cTrader accounts report crypto at 0.0325, representing
    "0.0325% per side on notional" and confirming the unit is percent.

    cTrader quotes commission four ways. Two scale with notional and belong here; the
    per-lot pair is a fixed amount and is converted by commission_per_unit() instead.
    Returning 0.0 for everything else was not neutral - it silently made a real cost
    disappear for whole instrument classes.
    """
    kind = spec["commission_type"]
    if kind == "PercentageOfTradingVolume":
        return spec["commission"]
    if kind == "UsdPerMillionUsd":
        # USD per $1,000,000 of notional -> percent of notional
        return spec["commission"] / 10000.0
    return 0.0


def commission_per_unit(spec):
    """Per-side commission quoted per LOT, as a price offset per unit traded.

    Assumes the per-lot amount is in the instrument's quote currency, which is what
    "QuoteCurrencyPerOneLot" states and what a USD-quoted instrument makes true of
    "UsdPerOneLot" as well. On a non-USD instrument quoted in USD per lot this is an
    approximation, and it is the conservative direction: the cost is charged, not
    dropped.
    """
    if spec["commission_type"] in ("UsdPerOneLot", "QuoteCurrencyPerOneLot"):
        return spec["commission"] / (spec["lot_size"] or 1.0)
    return 0.0


def half_spread(spec):
    return (spec["spread"] or 0.0) / 2.0


def swap_daily(spec, side):
    """Daily financing for one unit held.

    Returns (price_units_per_unit, pct_of_notional). Exactly one is non-zero,
    because cTrader quotes swap either in pips or as an annual percentage.
    A positive number is a COST; a negative one is a credit.
    """
    raw = spec["swap_long"] if side == 1 else spec["swap_short"]
    if spec["swap_calc"] == "Percentage":
        return 0.0, -raw / 100.0 / 365.0
    return -raw * (spec["pip_size"] or 1.0), 0.0


def build_cost_tables(defaults):
    """(per-side price offset, per-side commission %) from broker truth.

    Any instrument with both a spec row and a price file enters the book - the table is
    not limited to the handful that were hardcoded, or new broker data would be read and
    then thrown away.

    An instrument whose spec row is absent keeps its provisional estimate and *says so*.
    Falling back silently is how real broker costs get replaced by estimates again,
    unnoticed, when a partial SpecExporter run truncates the file.
    """
    import os as _os
    from symbolmap import CSV_FOR

    spec = load()
    cost = dict(defaults)
    comm = {k: 0.0 for k in defaults}

    for sym, sp in spec.items():
        if sym not in CSV_FOR:
            continue
        if not _os.path.exists(_os.path.join(
                _os.path.dirname(SPEC_PATH), CSV_FOR[sym] + "_M1.csv")):
            continue                       # spec but no bars: nothing to simulate
        hs = half_spread(sp)
        per_unit = commission_per_unit(sp)
        if hs > 0:
            cost[sym] = hs + per_unit
        elif sym not in cost:
            continue                       # no spread and no fallback: cannot price it
        elif per_unit:
            cost[sym] = cost[sym] + per_unit
        comm[sym] = commission_pct(sp)

    missing = sorted(s for s in defaults if s not in spec)
    if missing:
        sys.stderr.write(
            "specs: WARNING - no broker row for %s; using provisional cost estimates. "
            "Re-run tools/cbot/SpecExporter.cs.\n" % ", ".join(missing))
    return cost, comm


def override_costs(defaults):
    return build_cost_tables(defaults)[0]
