"""
ORB + Fibonacci Lab: Inspector tests the opening-range breakout + Fibonacci retracement strategy.

Continuation: ORB → breakout → outburst → Fib (ORB edge-to-outburst extreme) → limit entry on the retracement
Reversal:     the continuation Fib fails (5-min CLOSE past 78.6%) → price breaks the ORB midpoint →
              opposite outburst → new Fib (swing extreme → new outburst extreme) → limit entry on the retracement

Traded on the shares with 2¢/share costs, measured in R. Everything out by 3:55 PM ET.
Run:  python orb_fib_lab.py --months 6
"""

import argparse
import datetime as dt
import html
import itertools
import json
import os
import sys
import time

import vault_options as vo
from strategy_lab import fetch, by_day, stats, max_dd, SYMBOLS, SLIP, FLAT_HM

UTC = dt.timezone.utc
ET = vo.LOCAL_TZ


def resolve(day, k, d, entry, stop, target):
    """Walk from the fill bar k. Stop checked first (also on the fill bar: conservative). Returns R after costs."""
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    cost = SLIP / risk
    for i in range(k, len(day)):
        b = day[i]
        if (b["l"] <= stop) if d == 1 else (b["h"] >= stop):
            return -1.0 - cost
        if i > k and ((b["h"] >= target) if d == 1 else (b["l"] <= target)):
            return (target - entry) * d / risk - cost
        if (b["e"].hour, b["e"].minute) >= FLAT_HM or i == len(day) - 1:
            return (b["c"] - entry) * d / risk - cost
    return None


def fib_trade(day, start, d, anchor, rng, p, until):
    """From bar `start`, with a move starting at `anchor` in direction d: track the outburst extreme, and when the
    outburst is big enough and price retraces to the Fib level, enter. Returns (trade or None, failed_at, extreme)."""
    ext = anchor
    qualified = False
    for k in range(start, len(day)):
        b = day[k]
        e = b["e"]
        if (e.hour, e.minute) >= until:
            return None, None, ext
        new_ext = max(ext, b["h"]) if d == 1 else min(ext, b["l"])
        leg = (new_ext - anchor) * d
        if not qualified and leg >= (1 + p["outburst"]) * rng:      # leg includes the ORB itself: must run X× range PAST it
            qualified = True
        if qualified:
            lvl = ext - d * p["fib"] * (ext - anchor) * d          # entry level from the extreme reached BEFORE this bar
            touched = (b["l"] <= lvl) if d == 1 else (b["h"] >= lvl)
            if touched and (ext - anchor) * d >= (1 + p["outburst"]) * rng:
                leg0 = (ext - anchor) * d
                stop = (ext - d * 0.786 * leg0) if p["stop"] == "786" else (anchor - d * 0.0005 * anchor)
                target = ext if p["target"] == "extreme" else lvl + d * 1.5 * abs(lvl - stop)
                fail_lvl = ext - d * 0.786 * leg0
                r = resolve(day, k, d, lvl, stop, target)
                # did the Fib fail later? first 5-min close past 78.6%
                failed = next((i for i in range(k, len(day)) if (day[i]["c"] < fail_lvl if d == 1 else day[i]["c"] > fail_lvl)), None)
                return ({"r": r, "d": d, "e": e, "k": k}, failed, ext) if r is not None else (None, failed, ext)
        ext = new_ext
    return None, None, ext


def day_trades(day, p):
    orb_n = p["orb"] // 5
    if len(day) < orb_n + 6 or day[0]["e"].hour != 9 or day[0]["e"].minute != 30:
        return []
    box = day[:orb_n]
    hi, lo = max(b["h"] for b in box), min(b["l"] for b in box)
    mid, rng = (hi + lo) / 2, hi - lo
    if rng <= 0:
        return []
    out = []
    # breakout: first 5-min close outside the range
    brk = next((k for k in range(orb_n, len(day)) if day[k]["c"] > hi or day[k]["c"] < lo), None)
    if brk is None:
        return []
    d = 1 if day[brk]["c"] > hi else -1
    anchor = lo if d == 1 else hi                                    # Fib from ORB low (bull) / ORB high (bear)
    t, failed, ext = fib_trade(day, brk, d, anchor, rng, p, p["until"])
    if t and p["setups"] in ("both", "continuation"):
        out.append(dict(t, setup="continuation"))
    if p["setups"] in ("both", "reversal"):
        # reversal: Fib failed (close past 78.6%) AND price breaks the ORB midpoint the other way
        start = failed if failed is not None else (t["k"] if t else None)
        if start is None and t is None:
            # no continuation entry happened; still look for a failure of the move after the breakout
            leg = (ext - anchor) * d
            if leg >= (1 + p["outburst"]) * rng:
                fail_lvl = ext - d * 0.786 * leg
                start = next((i for i in range(brk, len(day)) if (day[i]["c"] < fail_lvl if d == 1 else day[i]["c"] > fail_lvl)), None)
        if start is not None:
            shift = next((i for i in range(start, len(day)) if (day[i]["c"] < mid if d == 1 else day[i]["c"] > mid)), None)
            if shift is not None:
                swing = max(b["h"] for b in day[brk:shift + 1]) if d == 1 else min(b["l"] for b in day[brk:shift + 1])
                t2, _, _ = fib_trade(day, shift, -d, swing, rng, p, p["until"])
                if t2:
                    out.append(dict(t2, setup="reversal"))
    return out


GRID = {
    "orb": [15, 30],
    "outburst": [0.25, 0.5, 1.0],
    "fib": [0.5, 0.618],
    "stop": ["786", "anchor"],
    "target": ["extreme", "1.5R"],
    "until": [(12, 0), (15, 0)],
}


def label(p):
    return (f"{p['orb']}-min ORB · outburst ≥{p['outburst']:g}× range · enter at {p['fib'] * 100:.1f}% · stop "
            f"{'beyond 78.6%' if p['stop'] == '786' else 'beyond the Fib start'} · target "
            f"{'outburst high/low' if p['target'] == 'extreme' else '1.5R'} · entries until {p['until'][0] - 12 if p['until'][0] > 12 else p['until'][0]} "
            f"{'PM' if p['until'][0] >= 12 else 'AM'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=float, default=6)
    a = ap.parse_args()
    key, secret = os.environ.get("ALPACA_KEY_ID", "").strip(), os.environ.get("ALPACA_SECRET_KEY", "").strip()
    if not key or not secret:
        sys.exit("Add ALPACA_KEY_ID and ALPACA_SECRET_KEY.")
    api = vo.Alpaca(key, secret)
    t0 = time.time()
    start = dt.datetime.now(UTC) - dt.timedelta(days=a.months * 30.4 + 3)
    data, log = {}, []
    for sym in SYMBOLS:
        try:
            data[sym] = by_day(fetch(api, sym, start))
            log.append(f"{sym}: {len(data[sym])} days")
        except vo.ApiError as e:
            log.append(f"{sym}: skipped ({e})")
    first = min(d[0][0]["ts"] for d in data.values())
    last = max(d[-1][-1]["ts"] for d in data.values())
    split = first + (last - first) / 2                                  # first 3 months tune, last 3 months check
    split_label = dt.datetime.fromtimestamp(split, ET).strftime("%b %d")
    rows = []
    keys = list(GRID)
    for combo in itertools.product(*GRID.values()):
        for setups in ("both", "continuation", "reversal"):
            p = dict(zip(keys, combo), setups=setups)
            ts = []
            for sym, days in data.items():
                for day in days:
                    ts += [dict(t, sym=sym) for t in day_trades(day, p)]
            if len(ts) < 20:
                continue
            ts.sort(key=lambda t: t["e"])
            rs = [t["r"] for t in ts]
            tr = [t["r"] for t in ts if t["e"].timestamp() < split]
            te = [t["r"] for t in ts if t["e"].timestamp() >= split]
            mon = {}
            for t in ts:
                mon[t["e"].strftime("%Y-%m")] = mon.get(t["e"].strftime("%Y-%m"), 0) + t["r"]
            rows.append({"label": label(p) + f" · {setups}", "setups": setups, "p": {k: str(v) for k, v in p.items()},
                         "all": stats(rs), "train": stats(tr), "test": stats(te),
                         "etf": stats([t["r"] for t in ts if t["sym"] in ("SPY", "QQQ", "IWM")]),
                         "cont": stats([t["r"] for t in ts if t["setup"] == "continuation"]),
                         "rev": stats([t["r"] for t in ts if t["setup"] == "reversal"]),
                         "months_up": sum(1 for v in mon.values() if v > 0), "months": len(mon), "max_dd": round(max_dd(rs), 1)})
    log.append(f"Tested {len(rows)} versions in {time.time() - t0:.0f}s")
    both = [r for r in rows if r["setups"] == "both"]
    best = max(both, key=lambda r: r["train"]["avg"] if r["train"]["n"] >= 30 else -9)
    pos_all = sum(1 for r in both if r["all"]["avg"] > 0)
    held = sum(1 for r in both if r["train"]["avg"] > 0.05 and (r["test"]["avg"] or -1) > 0.05)
    top_test = sorted(both, key=lambda r: -(r["test"]["avg"] or -9))[:3]
    msg = "\n".join([
        f"🕵️ ORB + Fibonacci Lab · {a.months:g} months · 9 stocks/ETFs · 2¢/share costs", "",
        f"Tried {len(both)} versions of the full strategy (continuation + reversal).",
        f"• {pos_all} of {len(both)} made money over the whole period",
        f"• {held} of {len(both)} made money in BOTH the first half (before {split_label}) and the second half", "",
        f"Best on the first half: {best['label']}",
        f"   first half {best['train']['avg']:+.2f}R ({best['train']['n']} trades) → second half {(best['test']['avg'] or 0):+.2f}R ({best['test']['n']} trades)",
        f"   win rate {best['all']['win']:.0%} · continuation {(best['cont']['avg'] or 0):+.2f}R ({best['cont']['n']}) · reversal {(best['rev']['avg'] or 0):+.2f}R ({best['rev']['n']})",
        f"   SPY/QQQ/IWM only {(best['etf']['avg'] or 0):+.2f}R · {best['months_up']}/{best['months']} months up", "",
        "Your backtest: 323 trades, 59% wins, +0.44R a trade."])
    print(msg)
    json.dump({"when": t0, "months": a.months, "split": split_label, "rows": rows}, open("orb_fib_lab.json", "w"), indent=1, default=str)
    trs = "".join(f"<tr><td>{html.escape(r['label'])}</td><td class=n>{r['all']['n']}</td><td class=n>{r['all']['win']:.0%}</td>"
                  f"<td class='n {'pos' if r['all']['avg'] > 0 else 'neg'}'>{r['all']['avg']:+.2f}R</td><td class=n>{r['train']['avg'] or 0:+.2f}R</td>"
                  f"<td class=n>{r['test']['avg'] or 0:+.2f}R</td><td class=n>{r['months_up']}/{r['months']}</td></tr>"
                  for r in sorted(rows, key=lambda r: -r["all"]["avg"])[:120])
    open("orb_fib_lab.html", "w").write(f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ORB Fib Lab</title><style>body{{background:#0f1220;color:#e8ecf8;font:13px/1.4 system-ui;padding:16px}}table{{border-collapse:collapse;width:100%}}
td,th{{padding:5px;border-bottom:1px solid #2a3150;text-align:left}}td.n{{text-align:right}}.pos{{color:#53f0a6}}.neg{{color:#ff6b7c}}</style></head>
<body><h1>ORB + Fibonacci Lab</h1><pre>{html.escape(msg)}</pre><table><tr><th>Version</th><th>Trades</th><th>Win</th><th>Avg</th><th>1st half</th>
<th>2nd half</th><th>Months up</th></tr>{trs}</table></body></html>""")
    tg = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    try:
        chat = json.load(open(vo.STATE_FILE)).get("chat_id")
    except Exception:
        chat = None
    if tg and chat:
        try:
            vo.http("POST", f"https://api.telegram.org/bot{tg}/sendMessage", body={"chat_id": chat, "text": msg}, tries=2)
        except Exception as e:
            print(f"Telegram failed: {e}")


if __name__ == "__main__":
    main()
