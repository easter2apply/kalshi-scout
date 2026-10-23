"""ORB5 Lab: Easter's 5-minute opening range breakout (IWM Day Signal rules) on 2 years of IWM 5-min bars.
Results in R on IWM itself (1R = entry to stop). Same engine the live bot uses (vault_options.orb5)."""
import datetime as dt, json, os, statistics as stt, time
import vault_options_orb5 as vo

def fetch(api, sym, start):
    out, token = [], None
    for _ in range(400):
        p = {"symbols": sym, "timeframe": "5Min", "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": 10000,
             "feed": vo.STOCK_FEED, "adjustment": "raw", "sort": "asc"}
        if token: p["page_token"] = token
        j = api.d("/v2/stocks/bars", p)
        out += (j.get("bars") or {}).get(sym, [])
        token = j.get("next_page_token")
        if not token: break
    return out

def main():
    api = vo.Alpaca(os.environ["ALPACA_KEY_ID"].strip(), os.environ["ALPACA_SECRET_KEY"].strip())
    start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=730)
    res = {}
    for feed in ("iex", "sip"):
        vo.STOCK_FEED = feed
        try:
            raw = fetch(api, "IWM", start)
        except Exception as e:
            res[feed] = {"error": str(e)[:200]}; continue
        days = {}
        for b in raw:
            e = vo.et(b["t"].replace("Z", "")[:19])
            m = e.hour * 60 + e.minute
            if 570 <= m < 960:
                days.setdefault(e.date().isoformat(), []).append({"mins": m, "label": e.strftime("%H:%M"), "t": b["t"][:19],
                    "o": float(b["o"]), "h": float(b["h"]), "l": float(b["l"]), "c": float(b["c"])})
        trades = []
        phases = {}
        for d, bars in sorted(days.items()):
            if len(bars) < 60: continue
            r = vo.orb5(bars, True)
            phases[r["phase"]] = phases.get(r["phase"], 0) + 1
            t = r["trade"]
            if t and t["result"] is not None:
                trades.append({"day": d, "dir": t["dir"], "r": round(t["result"], 3), "risk_pct": round(t["one_r"] / t["entry"] * 100, 3),
                               "be": t["be"], "entry": t["entry_label"]})
        rs = [x["r"] for x in trades]
        n = len(rs); cut = int(n * 2 / 3)
        f = lambda xs: {"n": len(xs), "win": round(sum(1 for x in xs if x > 0) / len(xs), 3) if xs else None,
                        "scratch": round(sum(1 for x in xs if abs(x) < 0.05) / len(xs), 3) if xs else None,
                        "avg_r": round(stt.mean(xs), 3) if xs else None, "total_r": round(sum(xs), 1),
                        "t": round(stt.mean(xs) / (stt.pstdev(xs) / len(xs) ** .5), 2) if len(xs) > 2 and stt.pstdev(xs) else None}
        mon = {}
        for x in trades: mon[x["day"][:7]] = mon.get(x["day"][:7], 0) + x["r"]
        dd, pk, cum = 0, 0, 0
        for x in rs: cum += x; pk = max(pk, cum); dd = min(dd, cum - pk)
        res[feed] = {"days": len(days), "phases": phases, "all": f(rs), "first_2_3": f(rs[:cut]), "last_1_3": f(rs[cut:]),
                     "calls": f([x["r"] for x in trades if x["dir"] > 0]), "puts": f([x["r"] for x in trades if x["dir"] < 0]),
                     "median_risk_pct": stt.median([x["risk_pct"] for x in trades]) if trades else None,
                     "months_up": sum(1 for v in mon.values() if v > 0), "months": len(mon), "worst_drawdown_r": round(dd, 1),
                     "by_month": {k: round(v, 2) for k, v in sorted(mon.items())}, "last_trades": trades[-15:]}
        print(feed, json.dumps({k: v for k, v in res[feed].items() if k not in ("by_month", "last_trades")}), flush=True)
    json.dump(res, open("orb5_lab.json", "w"), indent=1)

if __name__ == "__main__":
    main()
