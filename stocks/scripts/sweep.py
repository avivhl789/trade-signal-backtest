# -*- coding: utf-8 -*-
"""How much of the result is the data, and how much is our assumptions?

Runs the study across plausible values for every choice we had to make, and reports the
range. The point is not to find a better number. It is to find out whether the number
is worth improving: if varying defensible assumptions moves the answer by thirty points,
then accuracy work on the parser is secondary to the assumptions themselves.

Each scenario shells out to run_study.py so the real code path is exercised rather than
a reimplementation of it.
"""
import os
import re
import os
import subprocess

# CONFIGURE ME: the date your own window starts ("YYYY-MM-DD"), or "" for all of it.
SINCE = os.environ.get("STUDY_SINCE", "")
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STOCKS = os.path.dirname(HERE)
PROJECT = os.path.dirname(STOCKS)
STUDY = os.path.join(HERE, "run_study.py")

SUMMARY = re.compile(
    r"SUMMARY net=(-?\d+) realised=(-?\d+) unreal=(-?\d+) costs=(-?\d+) "
    r"avgcap=(-?\d+) ret=(-?[\d.]+) retreal=(-?[\d.]+) trades=(\d+) open=(\d+)")


def run(label, extra):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    out = subprocess.run([sys.executable, STUDY, "--since", SINCE, "--quiet"]
                         + extra, capture_output=True, text=True, env=env,
                         cwd=PROJECT, encoding="utf-8", errors="replace")
    m = SUMMARY.search(out.stdout or "")
    if not m:
        print("  %-38s FAILED  %s" % (label, (out.stderr or "")[-90:].replace("\n", " ")))
        return None
    net, real, unreal, costs, avgcap, ret, retreal, trades, opn = m.groups()
    row = dict(label=label, net=float(net), realised=float(real),
               unreal=float(unreal), costs=float(costs), avgcap=float(avgcap),
               ret=float(ret) * 100, retreal=float(retreal) * 100,
               trades=int(trades), open=int(opn))
    print("  %-38s net %+8.0f  ret %+7.2f%%  realised-only %+6.2f%%  trades %3d  open %2d"
          % (label, row["net"], row["ret"], row["retreal"], row["trades"], row["open"]))
    return row


SCENARIOS = [
    ("BASELINE", []),

    # --- the notional, which only matters because commission is absolute ---
    ("full position $2,000", ["--unit", "2000"]),
    ("full position $25,000", ["--unit", "25000"]),
    ("full position $100,000", ["--unit", "100000"]),

    # --- the vague-quantity defaults: "a part", "a bit" ---
    ("vague add 0.15 / cut 0.33", ["--default-add", "0.15", "--default-cut", "0.33"]),
    ("vague add 0.50 / cut 0.67", ["--default-add", "0.50", "--default-cut", "0.67"]),
    ("bare entry 0.25", ["--default-entry", "0.25"]),
    ("bare entry 1.00", ["--default-entry", "1.0"]),

    # --- the two structural policies ---
    ("no corroboration (keep singles)", ["--min-records", "1"]),
    ("corroboration >= 3 records", ["--min-records", "3"]),
    ("ignore declarations entirely", ["--no-declarations"]),

    # --- costs ---
    ("spread 25 bps/action", ["--spread-bps", "25"]),
    ("spread 100 bps/action", ["--spread-bps", "100"]),
    ("commission $0.01/share", ["--per-share", "0.01"]),
    ("commission free", ["--per-share", "0", "--min-commission", "0"]),
]


def main():
    print("SENSITIVITY SWEEP - stocks channel, %s onward\n" % (SINCE or "whole record"))
    rows = [r for r in (run(lab, ex) for lab, ex in SCENARIOS) if r]
    if not rows:
        return 1
    base = rows[0]
    rets = [r["ret"] for r in rows]
    reals = [r["retreal"] for r in rows]

    print("\n" + "=" * 78)
    print("RANGE across %d scenarios" % len(rows))
    print("  total return      %+.2f%%  to  %+.2f%%   (spread %.1f points)"
          % (min(rets), max(rets), max(rets) - min(rets)))
    print("  realised only     %+.2f%%  to  %+.2f%%   (spread %.1f points)"
          % (min(reals), max(reals), max(reals) - min(reals)))
    print("  baseline          %+.2f%% total, %+.2f%% realised"
          % (base["ret"], base["retreal"]))

    print("\n  scenarios that move the total return most:")
    for r in sorted(rows[1:], key=lambda x: -abs(x["ret"] - base["ret"]))[:5]:
        print("    %-38s %+7.2f pp" % (r["label"], r["ret"] - base["ret"]))

    print("\n  sign of the answer:")
    pos = sum(1 for r in rows if r["ret"] > 0)
    print("    total return positive in %d of %d scenarios" % (pos, len(rows)))
    posr = sum(1 for r in rows if r["retreal"] > 0)
    print("    realised-only positive in %d of %d scenarios" % (posr, len(rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
