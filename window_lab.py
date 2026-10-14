"""3-month windows for the 0DTE SPY put spread on REAL option prices: what each account size did in every 3-month stretch."""
import datetime as dt, json, os, statistics as stt
import numpy as np
import premium_lab as pl, vault_options as vo

V = dict(fam="put", sym="SPY", dte=0, delta=0.30, width=2, tp=None, stop=None, filt="trend", x21=False)
api = vo.Alpaca(os.environ["ALPACA_KEY_ID"].strip(), os.environ["ALPACA_SECRET_KEY"].strip())
daily, intra, vol, log = pl.load_data(api)
mk = pl.Market(daily, intra, vol)
real = pl.Real(api)
cal = pl.calibrate(mk, real, log)
paths = pl.zero_paths(mk, "SPY", 0.30, 2, "put", cal[("SPY", "0dte")])
rt, skipped = pl.real_replay(V, paths, mk, real)
rt.sort(key=lambda t: t["d"])
out = {"trades": len(rt), "skipped": skipped, "avg_risk": round(stt.mean(t["risk"] for t in rt), 1),
       "avg_usd": round(stt.mean(t["usd"] for t in rt), 2), "per_month": round(len(rt) / ((rt[-1]["d"] - rt[0]["d"]).days / 30.4), 1),
       "worst_trade": round(min(t["usd"] for t in rt), 1), "worst_day_list": sorted(round(t["usd"], 1) for t in rt)[:8]}
months = sorted({(t["d"].year, t["d"].month) for t in rt})
res = {}
for cash in (500, 1000, 2000, 3000, 5000):
    for rf in (0.05, 0.10):
        wins = []
        for i in range(len(months) - 2):
            a, b = months[i], months[i + 2]
            ts = [t for t in rt if a <= (t["d"].year, t["d"].month) <= b]
            ac = pl.account(ts, cash, risk_frac=rf, cap=0.5)
            if ac:
                wins.append(round(ac["end"] - cash, 0))
        if wins:
            res[f"{cash}@{int(rf*100)}"] = {"median": stt.median(wins), "avg": round(stt.mean(wins)), "best": max(wins),
                                             "worst": min(wins), "up": sum(1 for w in wins if w > 0), "n": len(wins)}
out["windows"] = res
print(json.dumps(out, indent=1))
json.dump(out, open("window_lab.json", "w"), indent=1)
