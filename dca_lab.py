"""$1,000 on Jan 1, 2026 + $50 every two weeks, split equally across the 12 dinner-table stocks."""
import datetime as dt, json
import etf_lab as E      # reuses its Alpaca price fetch

T = list(E.TICKERS)
start = dt.date(2025, 12, 31)
px = {s: E.closes(s, start - dt.timedelta(days=5)) for s in T + ["SPY", "QQQ"]}
days = sorted(set.intersection(*(set(d for d in px[s] if d >= start) for s in T)))
buys = [(days[0], 1000.0)]
d = dt.date(2026, 1, 15)
while d <= days[-1]:
    buy_day = next(x for x in days if x >= d)
    buys.append((buy_day, 50.0))
    d += dt.timedelta(days=14)
shares = {s: 0.0 for s in T + ["SPY", "QQQ"]}
log = []
for day, amt in buys:
    for s in T:
        shares[s] += amt / len(T) / px[s][day]
    for b in ("SPY", "QQQ"):
        shares[b] += amt / px[b][day]
    log.append([day.isoformat(), amt])
last = days[-1]
put_in = sum(a for _, a in buys)
val = sum(shares[s] * px[s][last] for s in T)
low = min((sum(shares_t * 0 for shares_t in []) for _ in [0]), default=0)
# value path
path, sh = [], {s: 0.0 for s in T}
bi = 0
contrib = 0.0
for day in days:
    while bi < len(buys) and buys[bi][0] == day:
        for s in T:
            sh[s] += buys[bi][1] / len(T) / px[s][day]
        contrib += buys[bi][1]
        bi += 1
    path.append([day.isoformat(), round(sum(sh[s] * px[s][day] for s in T), 2), contrib])
worst = min(path, key=lambda p: p[1] - p[2])
out = {"put_in": put_in, "n_deposits": len(buys) - 1, "last_deposit": buys[-1][0].isoformat(), "value": round(val, 2),
       "spy": round(shares["SPY"] * px["SPY"][last], 2), "qqq": round(shares["QQQ"] * px["QQQ"][last], 2), "end": last.isoformat(),
       "worst_point": worst, "per_stock": {s: round(shares[s] * px[s][last], 2) for s in T}}
print(json.dumps(out, indent=1))
json.dump(out, open("dca_lab.json", "w"), indent=1)
