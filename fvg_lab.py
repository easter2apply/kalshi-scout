"""FVG Lab: fair value gap strategies on Bitcoin, Nasdaq (QQQ) and gold (GLD).

Bullish FVG: candle 3's low is above candle 1's high (gap between them), and candle 2 is a big move (body >= 1 ATR... see gap filter).
Bearish FVG: candle 3's high is below candle 1's low.
Gap filter: gap size >= 0.3 ATR(14) (small gaps are noise).

Strategies
  retrace  - after a bullish FVG, place a buy limit at the gap's top (or middle). Stop = candle 1's low (the swing that
             launched the move) - 0.1 ATR. Target = 2R (variants 1R / 3R). Order lives 20 candles; cancelled if price closes
             below the gap first. Mirror for bearish.
  ifvg     - inverse FVG: a bearish FVG gets closed through (a candle closes above its top) -> buy at that close.
             Stop = lowest low of the last 5 candles - 0.1 ATR. Target 2R. Mirror for sells.
Filters: trend = price above/below the 200 EMA on the same chart; NY = signals 9:30-11:30 ET only.
One trade at a time. Stocks: no new trades after 3:30 PM, flat at the close. Stop checked before target inside a candle.
Costs: crypto 0.04% of price per round trip; QQQ/GLD 2 cents per share.
"""
import datetime as dt
import json
import os

import vault_options as vo
from rsi1_lab import fetch, agg, ema, atr_list, summ


def run(b, crypto, v):
    atr = atr_list(b)
    e200 = ema([x["c"] for x in b], 200)
    rr = v.get("rr", 2.0)
    trades, pos, orders = [], None, []
    for i in range(3, len(b)):
        x = b[i]
        new_day = not crypto and x["day"] != b[i - 1]["day"]
        if new_day:
            orders = []
            if pos:
                pos["exit"] = b[i - 1]["c"]; trades.append(pos); pos = None
        # manage open trade
        if pos:
            d = pos["d"]
            if (x["l"] <= pos["stop"]) if d == 1 else (x["h"] >= pos["stop"]):
                pos["exit"] = pos["stop"]; trades.append(pos); pos = None
            elif (x["h"] >= pos["tp"]) if d == 1 else (x["l"] <= pos["tp"]):
                pos["exit"] = pos["tp"]; trades.append(pos); pos = None
        # pending limit orders (retrace)
        if not pos and orders:
            keep = []
            for o in orders:
                d = o["d"]
                if i - o["born"] > 20:
                    continue
                if (x["h"] >= o["tp"]) if d == 1 else (x["l"] <= o["tp"]):
                    continue                                    # ran to target without us: missed, cancel
                touched = (x["l"] <= o["entry"]) if d == 1 else (x["h"] >= o["entry"])
                if touched and not pos:
                    fill = min(x["o"], o["entry"]) if d == 1 else max(x["o"], o["entry"])
                    R = abs(fill - o["stop"])
                    if R <= 0:
                        continue
                    pos = {"d": d, "entry": fill, "stop": o["stop"], "tp": fill + d * rr * R, "R": R, "ts": x["ts"]}
                    if (x["l"] <= pos["stop"]) if d == 1 else (x["h"] >= pos["stop"]):   # stopped in the same candle
                        pos["exit"] = pos["stop"]; trades.append(pos); pos = None
                    continue
                if (x["c"] < o["gap_lo"]) if d == 1 else (x["c"] > o["gap_hi"]):
                    continue                                    # gap failed before fill
                keep.append(o)
            orders = keep if not pos else []
        a = atr[i]
        if a is None or e200[i] is None:
            continue
        c1, c3 = b[i - 2], x
        if not crypto and (x["m"] >= 930 or c1["day"] != x["day"]):
            continue
        if v.get("ny") and not (crypto and 570 <= x["m"] < 690 or (not crypto and x["m"] < 690)):
            continue
        bull = c3["l"] > c1["h"] and c3["l"] - c1["h"] >= 0.3 * a
        bear = c3["h"] < c1["l"] and c1["l"] - c3["h"] >= 0.3 * a
        if v["kind"] == "retrace" and not pos:
            for d, ok in ((1, bull), (-1, bear)):
                if not ok:
                    continue
                if v.get("trend") and (x["c"] - e200[i]) * d <= 0:
                    continue
                gap_lo, gap_hi = (c1["h"], c3["l"]) if d == 1 else (c3["h"], c1["l"])
                entry = (gap_lo + gap_hi) / 2 if v.get("mid") else (gap_hi if d == 1 else gap_lo)
                stop = (c1["l"] - 0.1 * a) if d == 1 else (c1["h"] + 0.1 * a)
                R = abs(entry - stop)
                orders.append({"d": d, "entry": entry, "stop": stop, "tp": entry + d * rr * R, "gap_lo": gap_lo, "gap_hi": gap_hi, "born": i})
        if v["kind"] == "ifvg":
            # remember recent FVGs and trade the close-through
            fv = v.setdefault("_fv", {})
            lst = fv.setdefault(id(b), [])
            if bull:
                lst.append({"d": 1, "lo": c1["h"], "hi": c3["l"], "i": i})
            if bear:
                lst.append({"d": -1, "lo": c3["h"], "hi": c1["l"], "i": i})
            lst[:] = [g for g in lst if i - g["i"] <= 30]
            if pos:
                continue
            for g in list(lst):
                d = 0
                if g["d"] == -1 and x["c"] > g["hi"] and g["i"] < i:
                    d = 1
                elif g["d"] == 1 and x["c"] < g["lo"] and g["i"] < i:
                    d = -1
                if not d:
                    continue
                lst.remove(g)
                if v.get("trend") and (x["c"] - e200[i]) * d <= 0:
                    continue
                lo5 = min(y["l"] for y in b[i - 4:i + 1]); hi5 = max(y["h"] for y in b[i - 4:i + 1])
                stop = (lo5 - 0.1 * a) if d == 1 else (hi5 + 0.1 * a)
                R = abs(x["c"] - stop)
                if R <= 0:
                    continue
                pos = {"d": d, "entry": x["c"], "stop": stop, "tp": x["c"] + d * rr * R, "R": R, "ts": x["ts"]}
                break
    out = []
    for p in trades:
        r = p["d"] * (p["exit"] - p["entry"]) / p["R"]
        cost = (0.0004 * p["entry"] if crypto else 0.02) / p["R"]
        out.append({"ts": p["ts"], "r": r - cost, "hit": 1 if r > 0 else 0, "cost": cost})
    return out


VERSIONS = {
    "Retrace to gap top · 2R": {"kind": "retrace"},
    "Retrace to gap middle · 2R": {"kind": "retrace", "mid": True},
    "Retrace · 2R · with 200 EMA trend": {"kind": "retrace", "trend": True},
    "Retrace · 2R · trend · 9:30-11:30 only": {"kind": "retrace", "trend": True, "ny": True},
    "Retrace · 1R": {"kind": "retrace", "rr": 1.0},
    "Retrace · 3R": {"kind": "retrace", "rr": 3.0},
    "Inverse FVG · 2R": {"kind": "ifvg"},
    "Inverse FVG · 2R · trend": {"kind": "ifvg", "trend": True},
}


def main():
    api = vo.Alpaca(os.environ["ALPACA_KEY_ID"].strip(), os.environ["ALPACA_SECRET_KEY"].strip())
    now = dt.datetime.now(dt.timezone.utc)
    rep = {"how": __doc__, "results": {}}
    days = 4 * 365
    start = now - dt.timedelta(days=days)
    m1 = fetch(api, "GLD", start, False)
    print("GLD", len(m1), flush=True)
    for tf in (15, 30):
        b = agg(m1, tf, False)
        for vn in ("Retrace to gap top · 2R", "Retrace · 2R · with 200 EMA trend", "Retrace · 2R · trend · 9:30-11:30 only", "Retrace · 1R", "Retrace · 3R"):
            v = dict(VERSIONS[vn])
            tr = run(b, False, v)
            key = f"GLD 4yr · {tf}m · {vn}"
            s = summ(tr, start.timestamp() + days * 86400 * 2 / 3, days / 7)
            yrs = {}
            for x in tr:
                y = dt.datetime.fromtimestamp(x["ts"], dt.timezone.utc).year
                yrs.setdefault(y, []).append(x["r"])
            s["by_year"] = {y: [len(v), round(sum(v) / len(v), 3)] for y, v in sorted(yrs.items())}
            rep["results"][key] = s
            print(key, s, flush=True)
            json.dump(rep, open("fvg_lab.json", "w"), indent=1)


if __name__ == "__main__":
    main()
