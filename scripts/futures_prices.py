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

_cache = {}

def get(symbol):
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
