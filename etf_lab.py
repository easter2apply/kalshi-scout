"""Dinner Table ETF: equal-weight $1,000 in the public companies at the Sept 29, 2026 White House AI luncheon."""
import datetime as dt, json, os
import vault_options as vo

TICKERS = {"NVDA": "Nvidia", "MSFT": "Microsoft", "AMZN": "Amazon", "META": "Meta", "GOOGL": "Alphabet (Google)", "TSLA": "Tesla",
           "AMD": "AMD", "AVGO": "Broadcom", "MU": "Micron", "PLTR": "Palantir", "NOW": "ServiceNow", "PANW": "Palo Alto Networks"}
BENCH = {"SPY": "S&P 500 (SPY)", "QQQ": "Nasdaq 100 (QQQ)"}
EVENT = dt.date(2026, 9, 29)
api = vo.Alpaca(os.environ["ALPACA_KEY_ID"].strip(), os.environ["ALPACA_SECRET_KEY"].strip())


def closes(sym, start):
    out, token = {}, None
    while True:
        p = {"symbols": sym, "timeframe": "1Day", "start": start.isoformat() + "T00:00:00Z", "limit": 10000,
             "feed": "sip", "adjustment": "all", "sort": "asc"}
        if token:
            p["page_token"] = token
        try:
            j = api.d("/v2/stocks/bars", p)
        except vo.ApiError:
            p["feed"] = "iex"; j = api.d("/v2/stocks/bars", p)
        for b in (j.get("bars") or {}).get(sym, []):
            out[vo.et(b["t"]).date()] = b["c"]
        token = j.get("next_page_token")
        if not token:
            return out


def run(start_day, label):
    rows, data = [], {}
    for s in list(TICKERS) + list(BENCH):
        c = closes(s, start_day - dt.timedelta(days=7))
        d0 = max(d for d in c if d <= start_day)
        dN = max(c)
        data[s] = (c[d0], c[dN], d0, dN, c)
    per = 1000 / len(TICKERS)
    total = sum(per * data[s][1] / data[s][0] for s in TICKERS)
    # daily path of the basket (buy and hold, equal dollars at the start)
    days = sorted(set.intersection(*(set(d for d in data[s][4] if d >= data[s][2]) for s in TICKERS)))
    path = [(d.isoformat(), round(sum(per * data[s][4][d] / data[s][0] for s in TICKERS), 2)) for d in days]
    peak, dd = 0, 0
    for _, v in path:
        peak = max(peak, v); dd = min(dd, v / peak - 1)
    res = {"label": label, "start": data["NVDA"][2].isoformat(), "end": data["NVDA"][3].isoformat(), "value": round(total, 2),
           "max_drop": round(dd * 100, 1), "path": path,
           "stocks": sorted([{"sym": s, "name": TICKERS[s], "buy": data[s][0], "now": data[s][1],
                              "pct": round((data[s][1] / data[s][0] - 1) * 100, 1), "dollars": round(per * data[s][1] / data[s][0], 2)}
                             for s in TICKERS], key=lambda x: -x["pct"]),
           "bench": {BENCH[s]: round(1000 * data[s][1] / data[s][0], 2) for s in BENCH}}
    print(label, res["start"], "->", res["end"], res["value"], res["bench"], res["max_drop"])
    for x in res["stocks"]:
        print(f"  {x['sym']:5} {x['pct']:+6.1f}%  ${x['dollars']:.2f}")
    return res


out = {"since_lunch": run(EVENT, "Bought at the close on the day of the luncheon"),
       "one_year": run(EVENT - dt.timedelta(days=365), "Same basket bought one year earlier (hindsight)")}
json.dump(out, open("etf_lab.json", "w"), indent=1, default=str)
