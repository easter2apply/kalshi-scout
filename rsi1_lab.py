"""RSI(1) Lab: backtest of the Vault RSI(1) Signals indicator (same rules as the Pine script).

Oscillator: RSI(1) of close (Pine math: 100 if no down move, 0 if no up move), fast = EMA20 of it, slow = EMA30.
BUY when fast crosses back above 8. SELL when fast crosses back below 91. (Option: fast must also be past slow.)
Entry at the signal candle's close. Stop = lowest low (highest high) of the last 10 candles -/+ 0.25 ATR(14),
never closer than 1 ATR. A third off at TP1 (1R), TP2 (2R), TP3 (3R); stop to entry after TP1.
An opposite signal closes the trade at that close and flips. Stop is checked before targets inside a candle.
Costs: crypto 0.04% of price per round trip; QQQ/GLD 2 cents per share.
Data: Alpaca 1-minute bars (BTC/USD 24/7 last 6 months; QQQ and GLD 9:30-4:00 ET last 12 months), built up to 5m/15m.
"""
import datetime as dt
import json
import os
import statistics as stt

import vault_options as vo

ET = vo.LOCAL_TZ


def fetch(api, sym, start, crypto):
    out, token = [], None
    url = "/v1beta3/crypto/us/bars" if crypto else "/v2/stocks/bars"
    for _ in range(800):
        p = {"symbols": sym, "timeframe": "1Min", "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": 10000, "sort": "asc"}
        if not crypto:
            p.update(feed="sip", adjustment="split")
        if token:
            p["page_token"] = token
        j = api.d(url, p)
        out += (j.get("bars") or {}).get(sym, [])
        token = j.get("next_page_token")
        if not token:
            break
    bars = []
    for b in out:
        t = dt.datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
        e = t.astimezone(ET)
        m = e.hour * 60 + e.minute
        if crypto or 570 <= m < 960:
            bars.append({"ts": t.timestamp(), "day": e.date().isoformat(), "m": m, "o": float(b["o"]), "h": float(b["h"]),
                         "l": float(b["l"]), "c": float(b["c"])})
    return bars


def agg(m1, n, crypto):
    if n == 1:
        return m1
    out, cur = [], None
    for b in m1:
        key = (int(b["ts"] // (60 * n))) if crypto else (b["day"], (b["m"] - 570) // n)
        if cur is None or cur["key"] != key:
            if cur:
                out.append(cur)
            cur = dict(b, key=key)
        else:
            cur["h"] = max(cur["h"], b["h"]); cur["l"] = min(cur["l"], b["l"]); cur["c"] = b["c"]
    if cur:
        out.append(cur)
    return out


def ema(a, n):
    k, e, out = 2 / (n + 1), None, []
    for i, x in enumerate(a):
        e = x if e is None else x * k + e * (1 - k)
        out.append(e if i >= n - 1 else None)
    return out


def atr_list(b, n=14):
    out, a = [None] * len(b), None
    for i in range(1, len(b)):
        tr = max(b[i]["h"] - b[i]["l"], abs(b[i]["h"] - b[i - 1]["c"]), abs(b[i]["l"] - b[i - 1]["c"]))
        a = tr if a is None else (a * (n - 1) + tr) / n
        out[i] = a if i >= n else None
    return out


def run(b, crypto, v):
    c = [x["c"] for x in b]
    r = [100.0 if i and c[i] >= c[i - 1] else 0.0 for i in range(len(c))]   # Pine rsi(1): 100 unless the close fell
    r[0] = 50.0
    fast, slow, atr = ema(r, v.get("fast", 20)), ema(r, v.get("slow", 30)), atr_list(b)
    buyL, sellL = v.get("buy", 8), v.get("sell", 91)
    tps = v.get("tps", (1, 2, 3))
    trades, pos = [], None
    for i in range(31, len(b)):
        x = b[i]
        # manage open trade
        if pos:
            d = pos["d"]
            if not crypto and x["day"] != pos["day"]:            # stocks: flat at the close
                pos["exit"] = b[i - 1]["c"]; trades.append(pos); pos = None
            else:
                if (x["l"] <= pos["stop"]) if d == 1 else (x["h"] >= pos["stop"]):
                    pos["exit"] = pos["stop"]; trades.append(pos); pos = None
                else:
                    while pos["hit"] < len(tps) and ((x["h"] >= pos["t"][pos["hit"]]) if d == 1 else (x["l"] <= pos["t"][pos["hit"]])):
                        pos["hit"] += 1
                        if pos["hit"] == 1 and v.get("be", True):
                            pos["stop"] = pos["entry"]
                    if pos["hit"] == len(tps):
                        pos["exit"] = None; trades.append(pos); pos = None
        f0, f1, s1 = fast[i - 1], fast[i], slow[i]
        if f0 is None or s1 is None or atr[i] is None:
            continue
        buy = f0 <= buyL < f1 and (not v.get("x") or f1 > s1)
        sell = f0 >= sellL > f1 and (not v.get("x") or f1 < s1)
        d = 1 if buy else -1 if sell else 0
        if not d or (pos and pos["d"] == d):
            continue
        if not crypto and x["m"] >= 950:
            continue
        if pos:                                                   # flip
            pos["exit"] = x["c"]; trades.append(pos); pos = None
        lo = min(y["l"] for y in b[i - 9:i + 1]); hi = max(y["h"] for y in b[i - 9:i + 1])
        stop = (lo - 0.25 * atr[i]) if d == 1 else (hi + 0.25 * atr[i])
        if abs(x["c"] - stop) < atr[i]:
            stop = x["c"] - d * atr[i]
        R = abs(x["c"] - stop)
        pos = {"d": d, "entry": x["c"], "stop": stop, "R": R, "t": [x["c"] + d * k * R for k in tps], "hit": 0, "ts": x["ts"], "day": x["day"]}
    out = []
    for p in trades:
        n = len(tps)
        got = sum(tps[k] for k in range(p["hit"])) / n
        rest = 0.0 if p["exit"] is None else (n - p["hit"]) / n * p["d"] * (p["exit"] - p["entry"]) / p["R"]
        cost = (0.0004 * p["entry"] if crypto else 0.02) / p["R"]
        out.append({"ts": p["ts"], "r": got + rest - cost, "hit": p["hit"], "cost": cost})
    return out


def summ(tr, split, weeks):
    if not tr:
        return {"n": 0}
    rs = [x["r"] for x in tr]
    sd = stt.pstdev(rs) if len(rs) > 1 else 0
    f = lambda xs: round(stt.mean([x["r"] for x in xs]), 3) if xs else None
    streak = worst = 0
    for x in rs:
        streak = streak + 1 if x < 0 else 0; worst = max(worst, streak)
    return {"n": len(rs), "per_day": round(len(rs) / (weeks * 5), 1), "win": round(sum(x > 0 for x in rs) / len(rs), 3),
            "tp1": round(sum(x["hit"] >= 1 for x in tr) / len(tr), 3), "tp3": round(sum(x["hit"] >= 3 for x in tr) / len(tr), 3),
            "avg_r": round(stt.mean(rs), 3), "before_cost": round(stt.mean(x["r"] + x["cost"] for x in tr), 3),
            "total_r": round(sum(rs), 1), "t": round(stt.mean(rs) / (sd / len(rs) ** .5), 2) if sd else 0, "worst_streak": worst,
            "train": f([x for x in tr if x["ts"] < split]), "unseen": f([x for x in tr if x["ts"] >= split])}


VERSIONS = {
    "As built (8/91, EMA 20/30)": {},
    "+ fast must cross slow": {"x": True},
    "All out at TP1 (1R)": {"tps": (1,)},
    "No breakeven move": {"be": False},
    "Tighter zones 5/95": {"buy": 5, "sell": 95},
}


def main():
    api = vo.Alpaca(os.environ["ALPACA_KEY_ID"].strip(), os.environ["ALPACA_SECRET_KEY"].strip())
    now = dt.datetime.now(dt.timezone.utc)
    rep = {"how": __doc__, "results": {}}
    for name, sym, crypto, days in (("Bitcoin", "BTC/USD", True, 182), ("Nasdaq (QQQ)", "QQQ", False, 365), ("Gold (GLD)", "GLD", False, 365)):
        start = now - dt.timedelta(days=days)
        split = start.timestamp() + days * 86400 * 2 / 3
        weeks = days / 7
        m1 = fetch(api, sym, start, crypto)
        print(name, len(m1), "1-min bars", flush=True)
        for tf in (1, 5, 15):
            b = agg(m1, tf, crypto)
            for vn, v in VERSIONS.items():
                if tf != 1 and vn not in ("As built (8/91, EMA 20/30)", "All out at TP1 (1R)"):
                    continue
                key = f"{name} · {tf}m · {vn}"
                s = summ(run(b, crypto, v), split, weeks)
                rep["results"][key] = s
                print(f"{key:58} n={s.get('n')} /day={s.get('per_day')} win={s.get('win')} tp1={s.get('tp1')} avg={s.get('avg_r')} "
                      f"pre-cost={s.get('before_cost')} t={s.get('t')} train={s.get('train')} unseen={s.get('unseen')}", flush=True)
                json.dump(rep, open("rsi1_lab.json", "w"), indent=1)


if __name__ == "__main__":
    main()
