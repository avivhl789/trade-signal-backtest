# -*- coding: utf-8 -*-
"""Build DEMO.html from the synthetic fixture by actually running the engine.

Nothing on the page is typed by hand. Every value comes from parsing the fixture
messages and replaying them against the fixture prices, so a rule change that alters
behaviour alters the page. If this file ever starts hard-coding numbers, it has stopped
being evidence and become a brochure.

Run:  python demo/build_demo.py
"""
import csv
import datetime as dt
import hashlib
import html
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
os.chdir(ROOT)

import book_engine as be          # noqa: E402
import message_input              # noqa: E402
import parse_signals              # noqa: E402

OUT = os.path.join(ROOT, "DEMO.html")
RAW = os.path.join(ROOT, "data", "raw", "demo")

# The demo reads its own fixture stream whatever the engine is configured to read.
# It used to rely on the fixture carrying the same placeholder stream id the package
# ships with, which meant that configuring SOURCE_STREAMS for your own source - step 2
# of GETTING_STARTED - left this page empty, with no error to explain it. The one
# command that proves the package works must not depend on the package being unconfigured.
DEMO_STREAM = "demo"
be.INPUT_DIR = parse_signals.INPUT_DIR = RAW
be.SOURCE_STREAMS = parse_signals.SOURCE_STREAMS = frozenset((DEMO_STREAM,))

# What each fixture message is there to demonstrate. Labels only - no values.
DEMONSTRATES = {
    "1001": "כניסת שוק",
    "1002": "כניסת שוק, מכשיר שני",
    "1003": "כניסה ממתינה שמתמלאת מאוחר יותר",
    "1004": "מכשיר בלי קובץ מחירים",
    "1005": "העברת סטופ לנקודת הכניסה",
    "1006": "כניסה ממתינה שלא מתמלאת",
    "1007": "ביטול פקודה ממתינה",
    "1008": "מימוש חלקי",
    "1009": "הערת שוק — לא הוראה",
    "1010": "הוראה לכל הספר, עם החרגה",
    "1011": "סגירה אחרונה",
}

# Engine effect names -> how to describe them. These are only ever shown when the
# engine actually recorded the effect; the parsed instruction type is NOT evidence
# that anything happened.
EFFECT_TEXT = {
    "stop_to_breakeven": "הסטופ הועבר לנקודת הכניסה",
    "stop_moved": "הסטופ עודכן",
    "pending_cancelled": "בוטלה פקודה ממתינה",
    "scheduled_cancelled": "בוטלה פעולה שעוד לא הגיע זמן ביצועה",
    "target_cancelled": "היעד בוטל — הפוזיציה ממשיכה עם הסטופ בלבד",
    "size_up": "נוספה חשיפה לפוזיציה קיימת",
    "restored": "הוחזרה כמות שמומשה קודם",
    "pending_resized": "עודכן הסיכון של פקודה ממתינה",
}


def load_messages():
    return [{"id": m["id"], "ts": m["timestamp"][:19].replace("T", " "),
             "text": m["content"]}
            for m in message_input.load(RAW, (DEMO_STREAM,))]


def load_parsed(name):
    out = {}
    p = os.path.join(ROOT, "data", "parsed", name)
    if not os.path.exists(p):
        return out
    for r in csv.DictReader(io.open(p, encoding="utf-8-sig")):
        out.setdefault(r["id"], []).append(r)
    return out


def num(x):
    """Trim a parsed level to something readable without inventing precision."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return html.escape(str(x))
    return ("%.4f" % f).rstrip("0").rstrip(".")


def fingerprint():
    """Hash every input that can change what this page says.

    Messages, prices, broker specs, the symbol map, the engine modules and this
    renderer. Leaving any of them out means two different pages can carry the same
    fingerprint, which is worse than having none.
    """
    h = hashlib.sha256()
    targets = []
    for base in (RAW, os.path.join(ROOT, "data", "prices", "ctrader")):
        if os.path.isdir(base):
            targets += [os.path.join(base, f) for f in sorted(os.listdir(base))]
    targets += [os.path.join(ROOT, "scripts", f) for f in
                ("book_engine.py", "instructions.py", "lexicon.py", "parse_signals.py",
                 "message_input.py", "symbolmap.py", "specs.py", "costs.py",
                 "futures_prices.py")]
    targets.append(os.path.abspath(__file__))
    spec = os.path.join(ROOT, "data", "prices", "ctrader", "_specs.csv")
    if os.path.isfile(spec):
        targets.append(spec)
    for t in targets:
        if os.path.isfile(t):
            h.update(os.path.basename(t).encode("utf-8"))
            h.update(io.open(t, "rb").read())
    return h.hexdigest()[:16]


def main():
    msgs = load_messages()

    # Run the engine FIRST. load_actions() is what invokes the parser, and on a fresh
    # unpack data/parsed/ does not exist yet - reading the CSVs before this point
    # silently yields nothing and every card loses its middle band.
    acts = be.load_actions()
    log = be.run(acts)
    book = be.LAST_BOOK

    entries, events = load_parsed("entries.csv"), load_parsed("events.csv")
    if not entries and not events:
        raise SystemExit("no parsed output found - the parse step did not produce "
                         "data/parsed/entries.csv or events.csv")

    acts_by_id = {}
    for a in acts:
        acts_by_id.setdefault(str(a.get("id")), []).append(a)
    unpriced = {str(u["id"]) for u in book.unpriced}

    opened = {}
    for l in log:
        opened.setdefault(str(l["entry_id"]), []).append(l)

    # The engine stamps each leg with the message that caused it. A leg with no cause
    # fired on its own - a stop, a target, a timeout - and must not be attached to
    # whichever message happens to sit before it.
    closed_by = {}
    for l in log:
        cid = l.get("cause_id")
        if cid:
            closed_by.setdefault(str(cid), []).append(l)
    effects = getattr(book, "effects", {})

    effects_by_msg = {str(k): v for k, v in effects.items()}

    cards = []
    for m in msgs:
        mid = m["id"]
        src = "".join("<div class=line>%s</div>" % html.escape(x) if x.strip() else
                      "<div class=line>&nbsp;</div>" for x in m["text"].split("\n"))

        parsed = []
        for e in entries.get(mid, []):
            f = ['<b>כניסה</b>', '<span class=tag>%s</span>' % html.escape(e["direction"]),
                 '<span class=tag>%s</span>' % html.escape(e["symbol"]),
                 'סיכון <span class=n>%s%%</span>' % num(e["risk_pct"])]
            f.append('סוג <span class=tag>%s</span>' % html.escape(e["entry_type"]))
            if e.get("trigger"):
                f.append('טריגר <span class=n>%s</span>' % num(e["trigger"]))
            if e.get("stop"):
                f.append('סטופ <span class=n>%s</span>' % num(e["stop"]))
            if e.get("tp"):
                f.append('יעד <span class=n>%s</span>' % num(e["tp"]))
            parsed.append(" · ".join(f))
        for v in events.get(mid, []):
            f = ['<b>ניהול</b>', '<span class=tag>%s</span>' % html.escape(v["kinds"])]
            if v.get("symbol"):
                f.append('מכשיר בהודעה <span class=tag>%s</span>' % html.escape(v["symbol"]))
            if v.get("frac"):
                f.append('שבר <span class=n>%s</span>' % num(v["frac"]))
            if v.get("to_breakeven") == "True":
                f.append('<span class=tag>לנקודת הכניסה</span>')
            parsed.append(" · ".join(f))
        if not parsed:
            parsed.append('<span class=muted>המנוע לא זיהה כאן הוראה. ההודעה לא נכנסה '
                          'לזרם הפעולות.</span>')

        scope = []
        for a in acts_by_id.get(mid, []):
            s = "כל הספר" if a.get("book_wide") else (a.get("symbol") or "—")
            cls = "warn" if a.get("book_wide") else "tag"
            scope.append('<span class=tag>%s</span> → <span class=%s>%s</span>'
                         % (html.escape(a["kind"]), cls, html.escape(s)))

        effects, kind = [], "none"
        if mid in unpriced:
            kind = "drop"
            effects.append('<b>נופל.</b> אין קובץ מחירים למכשיר הזה, ולכן העסקה הזאת '
                           'לא נכנסה לספר ולא מופיעה באף מספר בדף.')
        for l in opened.get(mid, []):
            kind = "exec"
            effects.append(
                'נפתחה <span class=tag>%s</span> במחיר <span class=n>%s</span> בשעה '
                '<span class=n>%s</span> · <span class=n>%d%%</span> ממנה נסגרו ב-'
                '<span class=n>%s</span> (<span class=tag>%s</span>) · '
                'R <span class="n %s">%+.3f</span>'
                % (html.escape(l["symbol"]), num(l["entry_px"]),
                   l["ts"].strftime("%H:%M"), round(100 * l["frac"]),
                   l["exit_ts"].strftime("%H:%M"),
                   html.escape(l["reason"]) + ("" if l.get("cause_id")
                                               else " — ללא הודעה"),
                   "pos" if l["r"] >= 0 else "neg", l["r"]))
        for l in closed_by.get(mid, []):
            kind = "exec"
            effects.append(
                'נסגרו <span class=n>%d%%</span> מהפוזיציה של הודעה '
                '<span class=n>%s</span> (<span class=tag>%s</span>) · '
                '<span class=tag>%s</span> · R <span class="n %s">%+.3f</span>'
                % (round(100 * l["frac"]), html.escape(str(l["entry_id"])),
                   html.escape(l["symbol"]), html.escape(l["reason"]),
                   "pos" if l["r"] >= 0 else "neg", l["r"]))
        if not effects:
            recorded = effects_by_msg.get(mid) or {}
            for name, count in sorted(recorded.items()):
                kind = "state"
                effects.append("%s%s. לא נסגרה פוזיציה — זו פעולת ניהול, לא יציאה."
                               % (html.escape(EFFECT_TEXT.get(name, name)),
                                  (" (<span class=n>%d</span>)" % count) if count > 1 else ""))
            if recorded:
                pass
            elif events.get(mid):
                kind = "state"
                effects.append("המנוע זיהה כאן הוראה, אבל היא לא שינתה דבר בספר: "
                               "לא נסגרה פוזיציה ולא נרשמה פעולת ניהול.")
            elif entries.get(mid):
                kind = "state"
                effects.append('נרשמה פקודה ממתינה. היא לא התמלאה בטווח הנתונים, '
                               'ולכן לא נפתחה פוזיציה.')
            else:
                effects.append('<span class=muted>אין השפעה על הספר.</span>')

        cards.append(
            '<article class="card k-%s">'
            '<header><span class=time>%s</span>'
            '<span class=mid>הודעה %s</span>'
            '<span class=demo>%s</span></header>'
            '<div class=band><div class=lab>מה נכתב</div><div class="val src" dir=rtl>%s</div></div>'
            '<div class=band><div class=lab>מה המנוע הבין</div><div class=val>%s</div></div>'
            '%s'
            '<div class=band><div class=lab>מה קרה בספר</div><div class=val>%s</div></div>'
            '</article>'
            % (kind, m["ts"][11:16], html.escape(mid),
               html.escape(DEMONSTRATES.get(mid, "")), src,
               "".join("<div class=row>%s</div>" % x for x in parsed),
               ('<div class=band><div class=lab>על מה זה חל</div><div class=val>%s</div></div>'
                % "".join("<div class=row>%s</div>" % x for x in scope)) if scope else "",
               "".join("<div class=row>%s</div>" % x for x in effects)))

    stats = [("הודעות בקובץ", len(msgs)),
             ("כניסות שזוהו", sum(len(v) for v in entries.values())),
             ("הוראות ניהול", sum(len(v) for v in events.values())),
             ("פעולות בזרם", len(acts)),
             ("רגליים בספר", len(log)),
             ("נפלו בלי מחירים", len(book.unpriced))]

    # str.replace, not %-formatting: the stylesheet is full of literal percent signs.
    doc = TEMPLATE
    for key, val in (
        ("__STATS__", "".join('<div class=stat><span class=n>%d</span><span>%s</span></div>'
                              % (v, html.escape(k)) for k, v in stats)),
        ("__CARDS__", "".join(cards)),
        ("__BUILT__", dt.datetime.now().strftime("%Y-%m-%d %H:%M")),
        ("__FP__", fingerprint()),
    ):
        doc = doc.replace(key, val)
    io.open(OUT, "w", encoding="utf-8", newline="\n").write(doc)
    print("wrote %s (%.1f KB)" % (os.path.relpath(OUT, ROOT), len(doc) / 1024.0))
    for k, v in stats:
        print("  %-22s %d" % (k, v))


TEMPLATE = u"""<!doctype html>
<html lang=he dir=rtl><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>הדגמה — מנוע שחזור לערוצי מסחר</title>
<style>
:root{
  --bg:#f7f7f5; --card:#fff; --ink:#1b1f24; --dim:#5d6672; --line:#e2e2dd;
  --src:#f2f1ec; --accent:#2f5d8a; --exec:#2d6a4f; --warn:#8a5a00; --drop:#9b2c2c;
  --pos:#2d6a4f; --neg:#9b2c2c;
}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
  --bg:#14171a; --card:#1c2024; --ink:#e7e9ea; --dim:#9aa3ad; --line:#2b3136;
  --src:#22272c; --accent:#7fb0dd; --exec:#6fce9f; --warn:#e0b25c; --drop:#e98b8b;
  --pos:#6fce9f; --neg:#e98b8b;
}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.65 "Segoe UI",system-ui,sans-serif;padding:32px 20px 64px}
.wrap{max-width:860px;margin:0 auto}
h1{font-size:25px;margin:0 0 6px;letter-spacing:-.2px}
.sub{color:var(--dim);margin:0 0 22px;max-width:62ch}
.note{background:var(--card);border:1px solid var(--line);border-radius:10px;
  padding:14px 16px;margin:0 0 22px;color:var(--dim);font-size:14px}
.note b{color:var(--ink)}
.stats{display:flex;flex-wrap:wrap;gap:10px;margin:0 0 26px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:9px;
  padding:9px 14px;display:flex;gap:8px;align-items:baseline;font-size:13px;color:var(--dim)}
.stat .n{font-size:18px;font-weight:600;color:var(--ink)}
.card{background:var(--card);border:1px solid var(--line);border-radius:11px;
  margin:0 0 14px;overflow:hidden}
.card header{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;
  padding:11px 16px;border-bottom:1px solid var(--line)}
.time{font-weight:700;font-variant-numeric:tabular-nums}
.mid{color:var(--dim);font-size:13px}
.demo{margin-inline-start:auto;font-size:12px;color:var(--dim);
  border:1px solid var(--line);border-radius:20px;padding:2px 10px}
.band{display:flex;gap:14px;padding:11px 16px;border-top:1px solid var(--line)}
.band:first-of-type{border-top:0}
.lab{flex:0 0 108px;color:var(--dim);font-size:12.5px;padding-top:2px}
.val{flex:1;min-width:0}
.row{padding:1px 0}
.src{background:var(--src);border-radius:8px;padding:9px 12px;white-space:pre-wrap}
.line{font-variant-numeric:tabular-nums}
.tag{background:color-mix(in srgb,var(--accent) 14%,transparent);color:var(--accent);
  border-radius:5px;padding:1px 7px;font-size:13px;white-space:nowrap}
.warn{background:color-mix(in srgb,var(--warn) 18%,transparent);color:var(--warn);
  border-radius:5px;padding:1px 7px;font-size:13px;font-weight:600}
.n{font-variant-numeric:tabular-nums;direction:ltr;display:inline-block;unicode-bidi:embed}
.pos{color:var(--pos);font-weight:600}.neg{color:var(--neg);font-weight:600}
.muted{color:var(--dim)}
.k-exec .lab~.val .row{color:var(--ink)}
.k-drop{border-color:color-mix(in srgb,var(--drop) 45%,var(--line))}
.k-drop .band:last-child .val{color:var(--drop)}
.k-state .band:last-child .val{color:var(--warn)}
footer{margin-top:30px;padding-top:16px;border-top:1px solid var(--line);
  color:var(--dim);font-size:13px}
code{background:var(--src);border-radius:5px;padding:1px 6px;direction:ltr;
  display:inline-block;font-size:12.5px}
@media(max-width:640px){.band{flex-direction:column;gap:5px}.lab{flex:none}}
</style></head><body><div class=wrap>

<h1>מה המנוע עושה עם הודעה</h1>
<p class=sub>אחת עשרה הודעות סינתטיות, יום מסחר אחד, שלושה מכשירים. לכל הודעה מוצג
מה נכתב, מה המנוע הבין ממנה, על מה זה חל, ומה קרה בפועל בספר הפוזיציות.</p>

<div class=note>
<b>הכול בדף הזה נוצר בהרצה.</b> ההודעות והמחירים סינתטיים ונמצאים בחבילה; המספרים
הם הפלט של אותו מנוע שאתם מריצים בעצמכם, לא טקסט שנכתב ביד. שינוי בכללים משנה את
הדף.<br>
<b>שימו לב לשלוש שורות במיוחד:</b> ההודעה ב-10:30 (מכשיר בלי מחירים — נופלת),
ההודעה ב-11:45 (הערת שוק — המנוע לא נוגע בה), וההודעה ב-12:00 (הפרסר רושם מכשיר
אחד, ההיקף בפועל הוא כל הספר חוץ ממנו).
</div>

<div class=stats>__STATS__</div>

__CARDS__

<footer>
נוצר ב-__BUILT__ · טביעת אצבע של הקלט והמנוע: <code>__FP__</code><br>
לשחזור: <code>python demo/build_demo.py</code><br>
המחירים בהדגמה סינתטיים, ומפרטי הברוקר אינם זמינים — ולכן המנוע אינו מחיל עלויות
מימון, ומדווח על כך בעצמו. אל תקראו את המספרים כאן כתוצאה; הם קיימים כדי להראות
את השרשרת.
</footer>
</div></body></html>
"""

if __name__ == "__main__":
    main()
