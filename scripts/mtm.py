# -*- coding: utf-8 -*-
"""Mark-to-market equity curve and a configurable two-stage evaluation.

The evaluation preset uses *equity* — realised balance plus floating P&L on open
positions — and resets the daily loss limit at midnight Prague time, not UTC.
Those parameters are modelling assumptions, not a representation of every provider.
"""
import os, sys, datetime as dt, collections
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import futures_prices as prices
import book_engine as be
import costs

try:
    from zoneinfo import ZoneInfo
    PRAGUE = ZoneInfo("Europe/Prague")
except Exception:                                    # pragma: no cover
    PRAGUE = None

EPOCH = dt.datetime(1970, 1, 1)
STEP = 60


def _grid(t0, t1):
    a = int((t0 - EPOCH).total_seconds()) // STEP * STEP
    b = int(np.ceil((t1 - EPOCH).total_seconds() / STEP)) * STEP + STEP
    return np.arange(a, b, STEP, dtype=np.int64)


def equity_series(log, initial=be.INITIAL, include_financing=False, adverse=False):
    """Minute equity; optional continuous calendar financing and adverse OHLC marks.

    Adverse marks are a conservative intraminute bound, not a synchronized tick path.
    Exit-bar extremes are excluded because they can occur after the position closes.
    """
    if not log:
        return None
    t0 = min(l["ts"] for l in log)
    t1 = max(l["exit_ts"] for l in log)
    grid = _grid(t0, t1)

    # forward-filled close price per instrument on the shared grid
    px = {}
    for sym in be.COST:
        s = prices.get(sym)
        if s is None:
            continue
        idx = np.searchsorted(s.t, grid, side="right") - 1
        np.clip(idx, 0, len(s.t) - 1, out=idx)
        px[sym] = (s.l[idx], s.h[idx]) if adverse else s.c[idx]

    realised = np.zeros(len(grid))
    floating = np.zeros(len(grid))
    open_cnt = np.zeros(len(grid), dtype=np.int32)

    def gi(when):
        return int(np.searchsorted(grid, int((when - EPOCH).total_seconds()), side="left"))

    # realised: each leg's P&L lands at its exit and stays
    for l in log:
        realised[min(gi(l["exit_ts"]), len(grid) - 1)] += l["pnl"]
    realised = np.cumsum(realised)
    if include_financing:
        spec = costs._specs.load()
        slope = np.zeros(len(grid))
        intercept = np.zeros(len(grid))
        origin = int(grid[0])
        for leg in log:
            start_sec = (leg["ts"] - EPOCH).total_seconds() - origin
            end_sec = (leg["exit_ts"] - EPOCH).total_seconds() - origin
            duration = (leg["exit_ts"] - leg["ts"]).total_seconds()
            if duration > 0:
                rate = costs.financing_leg(leg, spec) / duration
                a, b = gi(leg["ts"]), gi(leg["exit_ts"])
                slope[a] += rate
                intercept[a] -= rate * start_sec
                if b < len(grid):
                    slope[b] -= rate
                    intercept[b] += rate * end_sec
        carry = np.cumsum(slope) * (grid - origin) + np.cumsum(intercept)
        realised -= carry

    # floating: per position, the still-open fraction marked against current price
    by_pos = collections.defaultdict(list)
    for l in log:
        # A source message id is NOT a position id - one message can open exposure in
        # several instruments. Prefer an explicit
        # pos_id when the engine supplies one.
        by_pos[l.get("pos_id") or (l["entry_id"], l["symbol"])].append(l)
    for eid, legs in by_pos.items():
        legs = sorted(legs, key=lambda x: x["exit_ts"])
        sym = legs[0]["symbol"]
        if sym not in px:
            continue
        entry_px = legs[0].get("entry_px")
        units = legs[0]["units"]
        d = legs[0].get("dir", 1)
        if entry_px is None:
            continue
        start = gi(legs[0]["ts"])
        remaining = 1.0
        for l in legs:
            end = min(gi(l["exit_ts"]), len(grid))
            if end > start and remaining > 0:
                marks = px[sym][0 if d == 1 else 1] if adverse else px[sym]
                floating[start:end] += units * remaining * d * (marks[start:end] - entry_px)
                open_cnt[start:end] += 1
            remaining -= l["frac"]
            start = end
    return grid, initial + realised, floating, initial + realised + floating, open_cnt


def local_days(grid):
    """Prague day at each sample, including the actual DST transition hour."""
    if PRAGUE is None:
        raise RuntimeError("Europe/Prague timezone data unavailable; install tzdata")
    hours = grid // 3600
    offsets = {}
    for hour in np.unique(hours):
        when = (EPOCH + dt.timedelta(hours=int(hour))).replace(tzinfo=dt.timezone.utc)
        offsets[int(hour)] = int(when.astimezone(PRAGUE).utcoffset().total_seconds())
    return (grid + np.array([offsets[int(h)] for h in hours])) // 86400


def evaluation_evaluate(log, initial=be.INITIAL, profit_pct=10.0, max_loss_pct=10.0,
                        daily_loss_pct=5.0, include_financing=False):
    """Evaluate mark-to-market equity with a Prague daily reset.

    Returns the first breach (if any) and whether the target was reached with the
    account still alive. This preset requires flat positions at the profit target.
    """
    out = equity_series(log, initial, include_financing=include_financing, adverse=True)
    if out is None:
        return None
    grid, balance, floating, equity, open_cnt = out

    # Compute the thresholds additively and compare with a cent of tolerance: the
    # multiplicative form gives 110000.00000000001 for a $100k / 10% target, so an
    # account landing exactly on the target would be judged to have missed it.
    EPS = 0.005
    floor = initial - initial * max_loss_pct / 100.0
    target = initial + initial * profit_pct / 100.0

    days = local_days(grid)

    # The preset measures the daily limit against the previous close.
    day_start = {}
    first_breach = None
    breach_kind = None
    prev_day = None
    day_open_balance = balance[0]
    for i in range(len(grid)):
        d = days[i]
        if d != prev_day:
            day_open_balance = balance[i - 1] if i else initial
            prev_day = d
        if equity[i] <= floor + EPS:
            first_breach, breach_kind = grid[i], "max_loss"
            break
        if equity[i] <= day_open_balance - initial * daily_loss_pct / 100.0 + EPS:
            first_breach, breach_kind = grid[i], "daily_loss"
            break

    # Target must be reached with positions actually closed. Zero net floating P&L is
    # NOT the same as flat - offsetting longs and shorts can net to nothing.
    flat = open_cnt == 0
    reached = np.where((balance >= target - EPS) & flat)[0]
    hit_i = int(reached[0]) if len(reached) else None
    if first_breach is not None and hit_i is not None:
        bi = int(np.searchsorted(grid, first_breach))
        if hit_i >= bi:
            hit_i = None

    return {
        "passed": hit_i is not None,
        "breach": breach_kind,
        "breach_time": (EPOCH + dt.timedelta(seconds=int(first_breach))) if first_breach is not None else None,
        "target_time": (EPOCH + dt.timedelta(seconds=int(grid[hit_i]))) if hit_i is not None else None,
        "min_equity": float(equity.min()),
        "max_equity": float(equity.max()),
        "final_balance": float(balance[-1]),
        "mtm_max_dd": float(((np.maximum.accumulate(equity) - equity) /
                             np.maximum.accumulate(equity)).max() * 100),
        "realised_max_dd": float(((np.maximum.accumulate(balance) - balance) /
                                  np.maximum.accumulate(balance)).max() * 100),
    }
