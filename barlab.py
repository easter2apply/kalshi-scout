"""Bar Lab: does Easter's CRT work better on volume or range candles than on time candles?

Builds three kinds of candles from the same 2 years of 1-minute SIP data (9:30-4:00 ET):
  time   - normal 5-minute candles
  volume - a new candle every N shares (N picked so there are as many candles as 5-min candles)
  range  - a new candle once the candle's high-low reaches R dollars (R = the median 5-min candle range)
Candles reset each morning. A candle only exists once it's finished, so no peeking ahead.

CRT (same live rules as the futures test, entry on C2's close):
  C2 trades past C1's high (or low) and closes back inside C1 without taking the other side.
  Enter at C2's close. Stop = C2's wick. Half off at C1's 50%, stop to entry, rest at C1's far side.
  Walked on 1-minute bars, flat by 3:55 PM. Cost: 1 cent per share each way.
"""
import datetime as dt
import json
import os
import statistics as stt

import vault_options as vo

ET = vo.LOCAL_TZ
SYMS = ["SPY", "QQQ", "IWM", "GLD", "DIA"]
COST = 0.02


def fetch(api, sym, start):
    out, token = [], None
    for _ in range(600):
        p = {"symbols": sym, "timeframe": "1Min", "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": 10000,
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
                         "l": float(b["l"]), "c": float(b["c"]), "v": float(b.get("v", 0))})
    return bars


def build(m1, kind, size):
    """Candles as {day, o,h,l,c, i0, i1} where i1 is the 1-min bar whose close finishes the candle."""
    out, cur = [], None
    for i, b in enumerate(m1):
        if cur is not None and cur["day"] != b["day"]:
            out.append(cur); cur = None                      # each day starts fresh
        if cur is None:
            cur = {"day": b["day"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": 0.0, "i0": i, "i1": i, "m0": b["m"]}
        else:
            cur["h"] = max(cur["h"], b["h"]); cur["l"] = min(cur["l"], b["l"]); cur["c"] = b["c"]; cur["i1"] = i
        cur["v"] += b["v"]
        done = ((kind == "time" and (b["m"] - 570) % 5 == 4) or
                (kind == "volume" and cur["v"] >= size) or
                (kind == "range" and cur["h"] - cur["l"] >= size))
        if done:
            out.append(cur); cur = None
    if cur:
        out.append(cur)
    return out


def walk(m1, i, d, entry, stop, tp1, tp2):
    risk = abs(entry - stop)
    day = m1[i]["day"]
    half, banked, sl, tg = False, 0.0, stop, tp1
    j = i
    while j < len(m1) and m1[j]["day"] == day:
        b = m1[j]
        if (b["h"] >= sl) if d == -1 else (b["l"] <= sl):
            return banked + (0.5 if half else 1) * d * (sl - entry) / risk
        if (b["l"] <= tg) if d == -1 else (b["h"] >= tg):
            if not half:
                half, banked, sl, tg = True, 0.5 * abs(tp1 - entry) / risk, entry, tp2
                if not ((b["l"] <= tg) if d == -1 else (b["h"] >= tg)):
                    j += 1; continue
            return banked + 0.5 * abs(tg - entry) / risk
        if b["m"] >= 955:
            return banked + (0.5 if half else 1) * d * (b["c"] - entry) / risk
        j += 1
    return banked + (0.5 if half else 1) * d * (m1[j - 1]["c"] - entry) / risk


def crt(m1, C, min_risk):
    out, busy_day, busy_i = [], None, -1
    for k in range(1, len(C)):
        c1, c2 = C[k - 1], C[k]
        if c1["day"] != c2["day"] or c2["i1"] <= busy_i:
            continue
        up = c2["h"] > c1["h"] and not c2["l"] < c1["l"] and c1["l"] < c2["c"] < c1["h"]
        dn = c2["l"] < c1["l"] and not c2["h"] > c1["h"] and c1["l"] < c2["c"] < c1["h"]
        if not (up or dn):
            continue
        d = -1 if up else 1
        entry, stop = c2["c"], (c2["h"] if d == -1 else c2["l"])
        mid, far = (c1["h"] + c1["l"]) / 2, (c1["l"] if d == -1 else c1["h"])
        risk = abs(entry - stop)
        i = c2["i1"] + 1
        if risk < min_risk or i >= len(m1) or m1[i]["day"] != c2["day"] or m1[i]["m"] >= 955:
            continue
        if (mid - entry) * d <= 0:
            continue
        r = walk(m1, i, d, entry, stop, mid, far) - COST / risk
        out.append({"ts": m1[i]["ts"], "r": r, "d": d})
        # one trade at a time: skip candles that finish before this trade would be closed (approx: next 30 min)
        busy_i = i + 30
    return out


def summ(tr, split, weeks):
    if not tr:
        return {"n": 0}
    rs = [x["r"] for x in tr]
    sd = stt.pstdev(rs) if len(rs) > 1 else 0
    f = lambda xs: round(stt.mean([x["r"] for x in xs]), 3) if xs else None
    return {"n": len(rs), "per_week": round(len(rs) / weeks, 1), "win": round(sum(r > 0 for r in rs) / len(rs), 3),
            "avg_r": round(stt.mean(rs), 3), "total_r": round(sum(rs), 1),
            "t": round(stt.mean(rs) / (sd / len(rs) ** .5), 2) if sd else 0,
            "train": f([x for x in tr if x["ts"] < split]), "unseen": f([x for x in tr if x["ts"] >= split])}


def main():
    api = vo.Alpaca(os.environ["ALPACA_KEY_ID"].strip(), os.environ["ALPACA_SECRET_KEY"].strip())
    start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=730)
    split = start.timestamp() + 730 * 86400 * 2 / 3
    rep = {"how": __doc__, "by_symbol": {}, "totals": {}}
    pooled = {}
    for s in SYMS:
        m1 = fetch(api, s, start)
        t5 = build(m1, "time", None)
        vol_size = sum(b["v"] for b in m1) / max(1, len(t5))
        rng_size = stt.median(c["h"] - c["l"] for c in t5)
        sets = {"time": t5, "volume": build(m1, "volume", vol_size), "range": build(m1, "range", rng_size)}
        min_risk = 0.05 * rng_size                       # ignore tiny stops that only the cost decides
        rep["by_symbol"][s] = {"bars_1m": len(m1), "volume_per_candle": round(vol_size), "range_per_candle": round(rng_size, 3)}
        for kind, C in sets.items():
            for gname, gate in (("all day", None), ("first 2 hours", 690)):
                tr = crt(m1, [c for c in C if gate is None or c["m0"] < gate], min_risk)
                key = f"{kind} · {gname}"
                rep["by_symbol"][s][key] = summ(tr, split, 104)
                rep["by_symbol"][s][key]["candles"] = len(C)
                pooled.setdefault(key, []).extend(tr)
        print(s, json.dumps({k: (v.get("avg_r"), v.get("n")) for k, v in rep["by_symbol"][s].items() if isinstance(v, dict)}), flush=True)
        json.dump(rep, open("barlab.json", "w"), indent=1)
    for k, tr in pooled.items():
        rep["totals"][k] = summ(tr, split, 104)
        a = rep["totals"][k]
        print(f"{k:28} n={a['n']:6} /wk={a['per_week']:6} win={a['win']:.0%} avg={a['avg_r']:+.3f} t={a['t']} train={a['train']} unseen={a['unseen']}", flush=True)
    json.dump(rep, open("barlab.json", "w"), indent=1)


if __name__ == "__main__":
    main()
