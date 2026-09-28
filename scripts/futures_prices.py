# -*- coding: utf-8 -*-
"""M1 price series loaded once into numpy arrays, with fast time->index lookup."""
import csv, io, os, sys, datetime as dt
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from symbolmap import CSV_FOR

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRICE_DIR = os.path.join(ROOT, "data", "prices", "ctrader")
EPOCH = dt.datetime(1970, 1, 1)

class Series:
    __slots__ = ("t", "o", "h", "l", "c", "symbol")
    def __init__(self, symbol, t, o, h, l, c):
        self.symbol, self.t, self.o, self.h, self.l, self.c = symbol, t, o, h, l, c

    def idx_at_or_after(self, when):
        """Index of the first bar starting at or after `when`, or -1."""
        k = int((when - EPOCH).total_seconds())
        i = int(np.searchsorted(self.t, k, side="left"))
        return i if i < len(self.t) else -1

    def time_at(self, i):
        return EPOCH + dt.timedelta(seconds=int(self.t[i]))

def _check_export_clock():
    """Warn if the price export is not on the same clock as the messages.

    Message timestamps are required to be UTC (message_input rejects anything else).
    Bar timestamps are whatever cTrader called server time when HistoryExporter ran,
    and most brokers run that at UTC+2 or UTC+3. Mixing the two shifts every fill by
    whole hours - silently, because a wrong bar is still a valid bar.

    HistoryExporter writes the offset to _meta.txt for exactly this reason. It is NOT
    applied automatically: the file records one offset, taken at export time, while a
    server on daylight saving changes offset partway through the window. Correcting by
    a single constant would be right for part of the export and wrong for the rest,
    which is worse than saying so out loud.
    """
    path = os.path.join(PRICE_DIR, "_meta.txt")
    if not os.path.isfile(path):
        return
    vals = {}
    try:
        for line in io.open(path, encoding="utf-8"):
            k, _, v = line.strip().partition(",")
            vals[k] = v
        srv = dt.datetime.strptime(vals["server_time"], "%Y-%m-%d %H:%M:%S")
        utc = dt.datetime.strptime(vals["local_utc_now"], "%Y-%m-%d %H:%M:%S")
    except (OSError, KeyError, ValueError):
        return
    off_min = int(round((srv - utc).total_seconds() / 60.0))
    if abs(off_min) < 2:
        return
    sys.stderr.write(
        "futures_prices: WARNING - your price export is on broker server time, which "
        "_meta.txt records as UTC%+d:%02d, but message timestamps are UTC. Every fill "
        "is looked up %+d minutes away from the message that caused it. Re-export from "
        "an account whose server time is UTC, or shift the CSV timestamps yourself "
        "before running.\n" % (off_min // 60, abs(off_min) % 60, -off_min))


_cache = {}
_clock_checked = False

def get(symbol):
    global _clock_checked
    if not _clock_checked:
        _clock_checked = True
        _check_export_clock()
    if symbol in _cache:
        return _cache[symbol]
    fname = CSV_FOR.get(symbol)
    if not fname:
        return None
    path = os.path.join(PRICE_DIR, fname + "_M1.csv")
    if not os.path.exists(path):
        return None
    ts, o, h, l, c = [], [], [], [], []
    with io.open(path, encoding="utf-8") as fh:
        r = csv.reader(fh); next(r)
        for x in r:
            d = dt.datetime.strptime(x[0], "%Y-%m-%d %H:%M:%S")
            ts.append(int((d - EPOCH).total_seconds()))
            o.append(float(x[1])); h.append(float(x[2])); l.append(float(x[3])); c.append(float(x[4]))
    s = Series(symbol, np.asarray(ts, dtype=np.int64), np.asarray(o), np.asarray(h),
               np.asarray(l), np.asarray(c))
    _cache[symbol] = s
    return s
