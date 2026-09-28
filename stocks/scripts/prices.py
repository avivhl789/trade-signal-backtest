# -*- coding: utf-8 -*-
"""Hourly bars per ticker, cached to stocks/data/prices/<TICKER>.csv.

yfinance serves hourly data 730 days back, which covers a window of up to two years.
auto_adjust=True applies split and dividend adjustment, which matters for any universe
that includes reverse splits.

A missing ticker is recorded as missing rather than silently skipped. Delisted names and
renamed tickers are exactly where a study like this quietly loses its losers.
"""
import io
import os
import sys
import time
import warnings

warnings.filterwarnings("ignore")

HERE = os.path.dirname(os.path.abspath(__file__))
STOCKS = os.path.dirname(HERE)
CACHE = os.path.join(STOCKS, "data", "prices")

START = os.environ.get("STUDY_START", "")   # CONFIGURE ME: "YYYY-MM-DD", a month
                                            # before your window, for context
INTERVAL = "1h"


def path_for(ticker):
    return os.path.join(CACHE, "%s.csv" % ticker.replace("/", "_"))


def fetch(ticker, force=False):
    """Download and cache one ticker. Returns row count, or 0 if unavailable."""
    import yfinance as yf
    p = path_for(ticker)
    if os.path.exists(p) and not force:
        with io.open(p, encoding="utf-8") as f:
            return max(sum(1 for _ in f) - 1, 0)
    if not os.path.isdir(CACHE):
        os.makedirs(CACHE)
    try:
        df = yf.Ticker(ticker).history(start=START, interval=INTERVAL, auto_adjust=True)
    except Exception as e:
        print("   %-6s FETCH ERROR %s" % (ticker, str(e)[:70]))
        return 0
    if df is None or not len(df):
        return 0
    df = df[["Open", "High", "Low", "Close", "Volume"]]
    df.index.name = "ts"
    df.to_csv(p, encoding="utf-8")
    return len(df)


def load(ticker):
    """(timestamps, closes) as parallel lists, or (None, None) if not cached.

    TIMESTAMPS ARE RETURNED AS NAIVE **UTC**, to match the documented message input.

    This was a look-ahead bug. yfinance labels bars in exchange-local time with an
    offset ("YYYY-MM-DD 09:30:00-04:00"). Dropping the tzinfo left naive New York time,
    which was then compared against naive UTC message times. A message at 14:52 UTC -
    09:52 in New York, just after the open - was matched to the 15:30 New York bar,
    5.5 hours later, and a message before a market holiday could be priced days forward.

    Every fill was therefore taken at a price the author could not have traded at, and
    the error ran in the look-ahead direction. Converting to UTC here fixes it at the
    single point where both clocks meet.

    Closes only: entries and exits both price at the close of the bar containing the
    message, which is the most defensible single choice when the message names no price.
    """
    import csv
    import datetime as dt
    p = path_for(ticker)
    if not os.path.exists(p):
        return None, None
    ts, px = [], []
    with io.open(p, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            raw = row.get("ts") or row.get("Datetime") or row.get("Date")
            if not raw:
                continue
            try:
                t = dt.datetime.fromisoformat(raw)
            except ValueError:
                continue
            c = row.get("Close")
            if not c:
                continue
            if t.tzinfo is not None:
                t = t.astimezone(dt.timezone.utc).replace(tzinfo=None)
            else:
                # No offset in the file means we cannot know which clock it is on.
                # Refuse rather than guess: guessing is what caused the bug above.
                raise ValueError(
                    "%s has timestamps with no UTC offset; refusing to guess a "
                    "timezone. Re-fetch this ticker." % ticker)
            ts.append(t)
            px.append(float(c))
    return (ts, px) if ts else (None, None)


def main(tickers):
    got, missing = {}, []
    for i, t in enumerate(sorted(set(tickers)), 1):
        n = fetch(t)
        if n:
            got[t] = n
        else:
            missing.append(t)
        if i % 10 == 0:
            print("   ... %d/%d" % (i, len(set(tickers))))
        time.sleep(0.15)          # be polite to the endpoint
    print("\ncached %d tickers" % len(got))
    if missing:
        print("UNAVAILABLE (%d): %s" % (len(missing), " ".join(missing)))
        print("  These are not skippable - a delisted or renamed ticker is usually a")
        print("  loser, and dropping them silently biases the study upward.")
    return got, missing


if __name__ == "__main__":
    main(sys.argv[1:])
