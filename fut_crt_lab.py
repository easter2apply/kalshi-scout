"""Futures CRT Lab: the CRT 3-candle model on futures stand-ins (QQQ=NQ, SPY=ES, IWM=RTY, DIA=YM, GLD=GC),
2 years of real 5-minute candles (regular hours 9:30-4:00 ET). Shorts shown; longs are the mirror.

Candle 1 (CRT candle): high, low, 50%.
Candle 2 (manipulation): trades above C1's high and CLOSES BACK INSIDE C1 (valid CRT). Closing above = invalid.
Entries:
  close   - enter at C2's close
  tbs     - Turtle Body Soup on 5m during candle 3: a 5m candle takes the prior 5m candle's high, then closes red
            below the prior candle's body. Setup dies if price breaks C2's high first.
  push50  - Easter's version: during C2, after the sweep, price pushes to C1's 50%, then a 5m CRT or 5m break of
            structure confirms (enter at that 5m close).
Stop: above the manipulation high. Targets (classic): half at C1's 50%, stop to entry, rest at C1's low.
  push50 uses Easter's targets: half at C1's body (its open), rest at C1's low.
Day trades: anything open at 3:55 PM ET is closed. Cost: 2 cents per share round trip.
"""
import bisect
import datetime as dt
import json
import os
import statistics as stt
import time

import vault_options as vo

ET = vo.LOCAL_TZ
SYMS = {"QQQ": "NQ", "SPY": "ES", "IWM": "RTY", "DIA": "YM", "GLD": "GC"}
COST = 0.02


def fetch(api, sym, start):
    out, token = [], None
    for _ in range(400):
        p = {"symbols": sym, "timeframe": "5Min", "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": 10000,
             "feed": "sip", "adjustment": "split", "sort": "asc"}
        if token:
            p["page_token"] = token
        j = api.d("/v2/stocks/bars", p)
        out += (j.get("bars") or {}).get(sym, [])
        token = j.get("next_page_token")
        if not token:
            break
    bars = []
    for b in out:
        t = dt.datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
        e = t.astimezone(ET)
        m = e.hour * 60 + e.minute
        if 570 <= m < 960:
            bars.append({"ts": t.timestamp(), "day": e.date().isoformat(), "m": m, "o": float(b["o"]), "h": float(b["h"]),
                         "l": float(b["l"]), "c": float(b["c"])})
    return bars


def agg(m5, minutes):
    """Group 5m bars into `minutes` bars aligned to 9:30 ET."""
    out, cur = [], None
    for i, b in enumerate(m5):
        key = (b["day"], (b["m"] - 570) // minutes)
        if cur is None or cur["key"] != key:
            if cur:
                out.append(cur)
            cur = {"key": key, "day": b["day"], "ts": b["ts"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "i0": i, "i1": i}
        else:
            cur["h"] = max(cur["h"], b["h"]); cur["l"] = min(cur["l"], b["l"]); cur["c"] = b["c"]; cur["i1"] = i
    if cur:
        out.append(cur)
    return out


def walk(m5, i, d, entry, stop, tp1, tp2):
    """Manage from 5m bar i: half at TP1 (stop to entry), rest at TP2; close at 3:55 PM. Returns (R, exit index)."""
    risk = abs(entry - stop)
    day = m5[i]["day"] if i < len(m5) else None
    half, banked, sl, tg = False, 0.0, stop, tp1
    j = i
    while j < len(m5) and m5[j]["day"] == day:
        b = m5[j]
        if (b["h"] >= sl) if d == -1 else (b["l"] <= sl):
            return banked + (0.5 if half else 1) * d * (sl - entry) / risk, j
        if (b["l"] <= tg) if d == -1 else (b["h"] >= tg):
            if not half and tp2 != tp1:
                half, banked, sl, tg = True, 0.5 * abs(tp1 - entry) / risk, entry, tp2
                if not ((b["l"] <= tg) if d == -1 else (b["h"] >= tg)):
                    j += 1; continue
            return banked + (0.5 if half else 1) * abs(tg - entry) / risk, j
        if b["m"] >= 955:
            return banked + (0.5 if half else 1) * d * (b["c"] - entry) / risk, j
        j += 1
    j = min(j, len(m5)) - 1
    return banked + (0.5 if half else 1) * d * (m5[j]["c"] - entry) / risk, j


def setups(sym, m5, tf, v):
    T = agg(m5, tf)
    out, busy = [], -1
    for k in range(1, len(T) - 1):
        c1, c2 = T[k - 1], T[k]
        if c1["day"] != c2["day"] or c2["i0"] <= busy:
            continue
        if c2["h"] > c1["h"] and c1["l"] < c2["c"] < c1["h"] and not (c2["l"] < c1["l"]):
            d = -1
        elif c2["l"] < c1["l"] and c1["l"] < c2["c"] < c1["h"] and not (c2["h"] > c1["h"]):
            d = 1
        else:
            continue
        if v.get("color") and ((c1["c"] <= c1["o"]) if d == -1 else (c1["c"] >= c1["o"])):
            continue                                   # Easter's filter: green C1 for shorts, red C1 for longs
        mid = (c1["h"] + c1["l"]) / 2
        far = c1["l"] if d == -1 else c1["h"]
        entry = stop = i_in = None
        tp1, tp2 = mid, far
        if v["entry"] == "close":
            entry, stop, i_in = c2["c"], (c2["h"] if d == -1 else c2["l"]), c2["i1"] + 1
        elif v["entry"] == "tbs":
            c3_end = T[k + 1]["i1"] if T[k + 1]["day"] == c2["day"] else c2["i1"]
            for j in range(c2["i1"] + 1, c3_end + 1):
                x, p = m5[j], m5[j - 1]
                if (x["h"] > c2["h"]) if d == -1 else (x["l"] < c2["l"]):
                    break                              # manipulation extreme broken: dead
                tbs = ((x["h"] > p["h"] and x["c"] < min(p["o"], p["c"]) and x["c"] < x["o"]) if d == -1 else
                       (x["l"] < p["l"] and x["c"] > max(p["o"], p["c"]) and x["c"] > x["o"]))
                if tbs:
                    entry, stop, i_in = x["c"], (c2["h"] if d == -1 else c2["l"]), j + 1
                    break
        else:                                          # push50 (Easter's version), inside C2
            swept = touched = None
            for j in range(c2["i0"], c2["i1"] + 1):
                x = m5[j]
                if swept is None and ((x["h"] > c1["h"]) if d == -1 else (x["l"] < c1["l"])):
                    swept = j
                if swept is not None and ((x["l"] <= mid) if d == -1 else (x["h"] >= mid)):
                    touched = j; break
            if touched is not None:
                for j in range(max(touched, c2["i0"] + 1), c2["i1"] + 1):
                    x, p = m5[j], m5[j - 1]
                    crt5 = (x["h"] > p["h"] and x["c"] < p["h"] and x["c"] < x["o"]) if d == -1 else (x["l"] < p["l"] and x["c"] > p["l"] and x["c"] > x["o"])
                    bos = j >= 3 and ((x["c"] < min(m5[q]["l"] for q in range(j - 3, j))) if d == -1 else (x["c"] > max(m5[q]["h"] for q in range(j - 3, j))))
                    if crt5 or bos:
                        ext = max(m5[q]["h"] for q in range(c2["i0"], j + 1)) if d == -1 else min(m5[q]["l"] for q in range(c2["i0"], j + 1))
                        entry, stop, i_in = x["c"], ext, j + 1
                        tp1 = c1["o"]
                        break
        if entry is None or i_in >= len(m5) or m5[i_in]["day"] != c2["day"]:
            continue
        risk = abs(entry - stop)
        if risk <= 0 or (tp1 - entry) * d <= 0 or (tp2 - entry) * d <= 0:
            continue
        if v.get("tp") == "tp2":
            tp1 = tp2
        elif v.get("tp") == "tp1":
            tp2 = tp1
        r, jx = walk(m5, i_in, d, entry, stop, tp1, tp2)
        out.append({"sym": sym, "ts": m5[i_in]["ts"], "d": d, "r": r - COST / risk, "risk": risk, "rr2": abs(far - entry) / risk})
        busy = jx
    return out


VERSIONS = {
    "15m CRT · enter on C2 close": {"tf": 15, "entry": "close"},
    "15m CRT · TBS entry on 5m": {"tf": 15, "entry": "tbs"},
    "15m CRT · Easter's 50% push + 5m confirm": {"tf": 15, "entry": "push50"},
    "15m CRT · Easter's version + color filter": {"tf": 15, "entry": "push50", "color": True},
    "15m CRT · TBS · all out at 50%": {"tf": 15, "entry": "tbs", "tp": "tp1"},
    "15m CRT · TBS · all out at CRT low": {"tf": 15, "entry": "tbs", "tp": "tp2"},
    "1H CRT · enter on C2 close": {"tf": 60, "entry": "close"},
    "1H CRT · TBS entry on 5m": {"tf": 60, "entry": "tbs"},
    "1H CRT · Easter's 50% push + 5m confirm": {"tf": 60, "entry": "push50"},
    "1H CRT · TBS · all out at CRT low": {"tf": 60, "entry": "tbs", "tp": "tp2"},
    "30m CRT · TBS entry on 5m": {"tf": 30, "entry": "tbs"},
}


def summ(tr, split, weeks):
    if not tr:
        return {"n": 0}
    rs = [x["r"] for x in tr]
    sd = stt.pstdev(rs) if len(rs) > 1 else 0
    streak = worst = 0
    for r in rs:
        streak = streak + 1 if r < 0 else 0; worst = max(worst, streak)
    f = lambda xs: round(stt.mean([x["r"] for x in xs]), 3) if xs else None
    return {"n": len(rs), "per_week": round(len(rs) / weeks, 1), "win": round(sum(r > 0 for r in rs) / len(rs), 3),
            "avg_r": round(stt.mean(rs), 3), "total_r": round(sum(rs), 1), "t": round(stt.mean(rs) / (sd / len(rs) ** .5), 2) if sd else 0,
            "worst_streak": worst, "train": f([x for x in tr if x["ts"] < split]), "unseen": f([x for x in tr if x["ts"] >= split]),
            "longs": f([x for x in tr if x["d"] == 1]), "shorts": f([x for x in tr if x["d"] == -1])}


def main():
    api = vo.Alpaca(os.environ["ALPACA_KEY_ID"].strip(), os.environ["ALPACA_SECRET_KEY"].strip())
    start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=730)
    split = start.timestamp() + 730 * 86400 * 2 / 3
    data = {}
    for s in SYMS:
        data[s] = fetch(api, s, start)
        print(s, len(data[s]), flush=True)
    rep = {"versions": {}}
    for name, v in VERSIONS.items():
        allt, per = [], {}
        for s, m5 in data.items():
            tr = setups(s, m5, v["tf"], v)
            per[SYMS[s] + " (" + s + ")"] = summ(tr, split, 104)
            allt += tr
        rep["versions"][name] = {"all": summ(allt, split, 104), "by_market": per}
        a = rep["versions"][name]["all"]
        print(f"{name:44} n={a.get('n',0):5} /wk={a.get('per_week',0):5} win={a.get('win',0):.0%} avg={a.get('avg_r',0):+.3f} t={a.get('t',0)} "
              f"streak={a.get('worst_streak',0)} train={a.get('train')} unseen={a.get('unseen')} | " +
              " ".join(f"{k.split()[0]}:{(x.get('avg_r') or 0):+.2f}" for k, x in per.items()), flush=True)
        json.dump(rep, open("fut_crt_lab.json", "w"), indent=1)


if __name__ == "__main__":
    main()
