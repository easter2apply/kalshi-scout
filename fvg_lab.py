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


def htf_ema(b, mins, n=200):
    """EMA(n) of hourly closes, value known at each bar = last fully closed hour (no peeking)."""
    out, hc, cur_key, last_close, e, k = [], [], None, None, None, 2 / (n + 1)
    cnt = 0
    for x in b:
        key = (x["day"], (x["m"] - 570) // mins)
        if cur_key is not None and key != cur_key:
            cnt += 1
            e = last_close if e is None else last_close * k + e * (1 - k)
        cur_key = key
        last_close = x["c"]
        out.append(e if cnt >= n else None)
    return out


def run(b, crypto, v):
    atr = atr_list(b)
    e200 = ema([x["c"] for x in b], 200) if not v.get("htf") else htf_ema(b, 60, v.get("htf_n", 200))
    rr = v.get("rr", 2.0)
    # prior-day high/low per bar
    pdh, pdl, dh, dl, cur, prev = [], [], None, None, None, (None, None)
    for x in b:
        if x["day"] != cur:
            prev = (dh, dl) if cur is not None else (None, None)
            cur, dh, dl = x["day"], x["h"], x["l"]
        pdh.append(prev[0]); pdl.append(prev[1])
        dh = max(dh, x["h"]); dl = min(dl, x["l"])
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
            elif v.get("be") and not pos.get("be") and ((x["h"] >= pos["entry"] + pos["R"] * v["be"]) if d == 1 else (x["l"] <= pos["entry"] - pos["R"] * v["be"])):
                pos["be"] = True; pos["stop"] = pos["entry"]
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
                    tgt = o["tp"] if v.get("pd_target") else fill + d * rr * R
                    pos = {"d": d, "entry": fill, "stop": o["stop"], "tp": tgt, "R": R, "ts": x["ts"], "be": False}
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
        w0, w1 = v.get("win", (570, 690))
        if v.get("ny") and not (w0 <= x["m"] < w1):
            continue
        g = v.get("gap", 0.3)
        bull = c3["l"] > c1["h"] and c3["l"] - c1["h"] >= g * a
        bear = c3["h"] < c1["l"] and c1["l"] - c3["h"] >= g * a
        c2 = b[i - 1]
        if v.get("body"):
            rg2 = c2["h"] - c2["l"]
            strong = rg2 > 0 and abs(c2["c"] - c2["o"]) / rg2 >= v["body"]
            bull = bull and strong; bear = bear and strong
        if v.get("sweep"):
            lb = b[max(0, i - 2 - v["sweep"]):i - 2]
            if lb and not crypto:
                lb = [y for y in lb if y["day"] == x["day"]] or lb
            if lb:
                swept_lo = min(c1["l"], c2["l"]) < min(y["l"] for y in lb)
                swept_hi = max(c1["h"], c2["h"]) > max(y["h"] for y in lb)
                bull = bull and swept_lo; bear = bear and swept_hi
        if v["kind"] == "retrace" and not pos:
            for d, ok in ((1, bull), (-1, bear)):
                if not ok:
                    continue
                if v.get("trend") and (x["c"] - e200[i]) * d <= 0:
                    continue
                gap_lo, gap_hi = (c1["h"], c3["l"]) if d == 1 else (c3["h"], c1["l"])
                entry = (gap_lo + gap_hi) / 2 if v.get("mid") else (gap_hi if d == 1 else gap_lo)
                stop = (c1["l"] - 0.1 * a) if d == 1 else (c1["h"] + 0.1 * a)
                if v.get("stop") == "gap":
                    stop = (gap_lo - 0.1 * a) if d == 1 else (gap_hi + 0.1 * a)
                if v.get("stop") == "swing":
                    lb2 = b[max(0, i - 10):i + 1]
                    stop = (min(y["l"] for y in lb2) - 0.1 * a) if d == 1 else (max(y["h"] for y in lb2) + 0.1 * a)
                R = abs(entry - stop)
                if R <= 0:
                    continue
                tgt = entry + d * rr * R
                if v.get("pd_target"):
                    lvl = pdh[i] if d == 1 else pdl[i]
                    if lvl is not None and (lvl - entry) * d >= 1.5 * R:
                        tgt = lvl
                orders.append({"d": d, "entry": entry, "stop": stop, "tp": tgt, "gap_lo": gap_lo, "gap_hi": gap_hi, "born": i})
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


BASE = {"kind": "retrace", "trend": True, "ny": True}
TESTS = {
    "0 Base (current indicator)": {},
    "1 Sweep first (last 20 candles)": {"sweep": 20},
    "1 Sweep first (last 40 candles)": {"sweep": 40},
    "2 Bigger gap 0.5 ATR": {"gap": 0.5},
    "2 Bigger gap 0.8 ATR": {"gap": 0.8},
    "2 Strong candle 2 (70% body)": {"body": 0.7},
    "3 1-hour 200 EMA trend": {"htf": True},
    "3 1-hour 50 EMA trend": {"htf": True, "htf_n": 50},
    "4 Breakeven at 1R": {"be": 1.0},
    "4 Tighter stop (gap edge)": {"stop": "gap"},
    "4 Wider stop (10-candle swing)": {"stop": "swing"},
    "4 Target prior day high/low": {"pd_target": True},
    "4 Target 3R": {"rr": 3.0},
    "5 Window 9:30-10:30": {"win": (570, 630)},
    "5 Window 9:30-12:00": {"win": (570, 720)},
    "6 Sweep 20 + 1h trend": {"sweep": 20, "htf": True},
    "6 Sweep 20 + breakeven": {"sweep": 20, "be": 1.0},
}
MARKETS = ["GLD", "SLV", "SPY", "QQQ", "IWM", "DIA"]


def main():
    api = vo.Alpaca(os.environ["ALPACA_KEY_ID"].strip(), os.environ["ALPACA_SECRET_KEY"].strip())
    now = dt.datetime.now(dt.timezone.utc)
    rep = {"how": __doc__, "gold": {}, "markets": {}}
    days = 4 * 365
    start = now - dt.timedelta(days=days)
    split = start.timestamp() + days * 86400 * 2 / 3
    data = {}
    for sym in MARKETS:
        data[sym] = agg(fetch(api, sym, start, False), 15, False)
        print(sym, len(data[sym]), flush=True)

    def yrs(tr):
        out = {}
        for x in tr:
            out.setdefault(dt.datetime.fromtimestamp(x["ts"], dt.timezone.utc).year, []).append(x["r"])
        return {y: [len(v), round(sum(v) / len(v), 3)] for y, v in sorted(out.items())}

    for name, extra in TESTS.items():
        v = dict(BASE, **extra)
        tr = run(data["GLD"], False, dict(v))
        s = summ(tr, split, days / 7); s["by_year"] = yrs(tr)
        rep["gold"][name] = s
        allm = []
        per = {}
        for sym in MARKETS:
            t2 = run(data[sym], False, dict(v))
            per[sym] = round(sum(x["r"] for x in t2) / len(t2), 3) if t2 else None
            allm += t2
        s2 = summ(allm, split, days / 7); s2["by_market"] = per; s2["by_year"] = yrs(allm)
        rep["markets"][name] = s2
        print(f"{name:34} GOLD n={s.get('n')} avg={s.get('avg_r')} t={s.get('t')} tr={s.get('train')} un={s.get('unseen')} yrs={s['by_year']}", flush=True)
        print(f"{'':34} ALL6 n={s2.get('n')} avg={s2.get('avg_r')} t={s2.get('t')} tr={s2.get('train')} un={s2.get('unseen')} per={per}", flush=True)
        json.dump(rep, open("fvg_lab.json", "w"), indent=1)


if __name__ == "__main__":
    main()
