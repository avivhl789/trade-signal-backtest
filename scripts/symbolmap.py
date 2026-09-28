# -*- coding: utf-8 -*-
"""Canonical symbol -> broker CSV name.

Some cTrader accounts expose index CFDs with a '.cash' suffix; metals, crypto and
single stocks may be bare. Keeping it in one place means the engine never guesses a
filename.

The table below is a STARTER SET, not the list this study ran on - fill in whatever
`SpecExporter` prints for your own account. `HistoryExporter` writes one
`<csv name>_M1.csv` per symbol, and the key here is the name the parser resolves a
message to; the value is that filename without the suffix.
"""
CSV_FOR = {
    # metals and crypto: bare
    "XAUUSD": "XAUUSD",
    "XAGUSD": "XAGUSD",
    "BTCUSD": "BTCUSD",

    # index CFDs: '.cash'
    "US100":  "US100.cash",
    "GER40":  "GER40.cash",

    # single stocks: bare
    "TSLA":   "TSLA",

    # ...add the rest of your instruments here. Anything absent is simply skipped:
    # a symbol with no CSV never enters the book, which is why the engine cannot
    # silently price an instrument you did not export.
}
