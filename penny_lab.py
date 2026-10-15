"""
Penny Crypto Lab: which picking rules would have worked on Coinbase coins priced at $1.50 or less?

Data: Coinbase Exchange public daily candles for every USD pair, including delisted ones (so dead coins count).
Each Monday the lab builds the universe: coins at or under $1.50 with 60+ days of history and real trading
($250k+ a day on Coinbase), stablecoins removed. Then each rule picks coins and holds them for the week.
Costs: 0.8% each way (Coinbase fees + spread). Rules are judged on the first 2/3 and the last 1/3 separately.
"""
import datetime as dt
import json
import math
import statistics as stt
import sys
import time
import urllib.request

API = "https://api.exchange.coinbase.com"
YEARS = 3.2
MAX_PRICE = 1.50
MIN_DOLLAR_VOL = 250_000
COST = 0.008
STABLE = {"USDT", "USDC", "DAI", "PYUSD", "EURC", "GUSD", "PAX", "BUSD", "TUSD", "UST", "USDS", "GYEN", "RLUSD", "USD1", "FDUSD"}


def get(path, params=None, tries=6):
    url = API + path + ("?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else "")
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "penny-lab"})
            return json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
        except Exception as e:
            if i == tries - 1:
                raise
            time.sleep(1.5 * (i + 1))


def candles(pid, start, end):
    out, cur = {}, start
    while cur < end:
        nxt = min(cur + dt.timedelta(days=299), end)
        try:
            rows = get(f"/products/{pid}/candles", {"granularity": 86400, "start": cur.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                                    "end": nxt.strftime("%Y-%m-%dT%H:%M:%SZ")})
        except Exception:
            rows = []
        for t, lo, hi, op, cl, vol in rows if isinstance(rows, list) else []:
            out[dt.datetime.utcfromtimestamp(t).date()] = (op, hi, lo, cl, vol)
        cur = nxt
        time.sleep(0.12)
    return out


def main():
    t0 = time.time()
    end = dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - dt.timedelta(days=int(YEARS * 365))
    prods = [p for p in get("/products") if p.get("quote_currency") == "USD" and p.get("base_currency") not in STABLE]
    print(f"{len(prods)} USD products ({sum(1 for p in prods if p.get('status') != 'online')} not online)", flush=True)
    data = {}
    for i, p in enumerate(prods):
        c = candles(p["id"], start, end)
        if len(c) >= 90:
            data[p["base_currency"]] = c
        if i % 50 == 0:
            print(f"{i}/{len(prods)} fetched ({time.time() - t0:.0f}s)", flush=True)
    btc = data.get("BTC", {})
    days = sorted(btc)
    print(f"{len(data)} coins with history; {len(days)} BTC days", flush=True)

    def close(c, d):
        return data[c][d][3] if d in data[c] else None

    def ret(c, d, n):
        a, b = close(c, d - dt.timedelta(days=n)), close(c, d)
        return (b / a - 1) if a and b else None

    def avg_close(c, d, n):
        xs = [data[c][d - dt.timedelta(days=k)][3] for k in range(n) if d - dt.timedelta(days=k) in data[c]]
        return sum(xs) / len(xs) if len(xs) >= n * 0.8 else None

    def dvol(c, d, n):
        xs = [data[c][d - dt.timedelta(days=k)][3] * data[c][d - dt.timedelta(days=k)][4] for k in range(n)
              if d - dt.timedelta(days=k) in data[c]]
        return sum(xs) / max(len(xs), 1)

    mondays = [d for d in days if d.weekday() == 0 and d >= days[0] + dt.timedelta(days=120)]
    weeks = list(zip(mondays, mondays[1:]))

    ucache = {}

    def universe(d):
        if d in ucache:
            return ucache[d]
        out = ucache.setdefault(d, [])
        for c in data:
            if c == "BTC":
                continue
            px = close(c, d)
            first = min(data[c])
            if not px or px > MAX_PRICE or (d - first).days < 60:
                continue
            if dvol(c, d, 30) < MIN_DOLLAR_VOL:
                continue
            out.append(c)
        return out

    def btc_up(d, n=100):
        m = avg_close("BTC", d, n)
        return m is not None and close("BTC", d) > m

    rules = {
        "Equal-weight every penny coin": lambda u, d: u,
        "Top 5 by 30-day gain": lambda u, d: sorted([c for c in u if ret(c, d, 30) is not None], key=lambda c: -ret(c, d, 30))[:5],
        "Top 5 by 90-day gain": lambda u, d: sorted([c for c in u if ret(c, d, 90) is not None], key=lambda c: -ret(c, d, 90))[:5],
        "Top 5 by 30-day gain, only when BTC is in an uptrend": lambda u, d: (sorted([c for c in u if ret(c, d, 30) is not None], key=lambda c: -ret(c, d, 30))[:5] if btc_up(d) else []),
        "Top 5 by 90-day gain, only when BTC is in an uptrend": lambda u, d: (sorted([c for c in u if ret(c, d, 90) is not None], key=lambda c: -ret(c, d, 90))[:5] if btc_up(d) else []),
        "Top 5 trend + rising volume, BTC uptrend": lambda u, d: (sorted(
            [c for c in u if ret(c, d, 30) is not None and avg_close(c, d, 50) and close(c, d) > avg_close(c, d, 50)
             and dvol(c, d, 14) > 1.5 * dvol(c, d, 90)], key=lambda c: -ret(c, d, 30))[:5] if btc_up(d) else []),
        "Every penny coin above its 50-day average, BTC uptrend": lambda u, d: ([c for c in u if avg_close(c, d, 50) and close(c, d) > avg_close(c, d, 50)] if btc_up(d) else []),
        "Biggest 5 losers of the last 30 days (buy the dip)": lambda u, d: sorted([c for c in u if ret(c, d, 30) is not None], key=lambda c: ret(c, d, 30))[:5],
        "Top 5 by 7-day gain (chasing pumps)": lambda u, d: sorted([c for c in u if ret(c, d, 7) is not None], key=lambda c: -ret(c, d, 7))[:5],
        "Bitcoin, buy and hold": None,
        "Bitcoin, only when above its 100-day average": "btc_trend",
    }
    split = weeks[int(len(weeks) * 2 / 3)][0]
    out = {}
    for name, rule in rules.items():
        eq, peak, dd, held, wk = 1.0, 1.0, 0.0, set(), []
        picks_log = []
        for d0, d1 in weeks:
            if rule is None:
                picks = ["BTC"]
            elif rule == "btc_trend":
                picks = ["BTC"] if btc_up(d0) else []
            else:
                picks = rule(universe(d0), d0)
            rs = []
            for c in picks:
                a, b = close(c, d0), close(c, d1)
                if a and b:
                    rs.append(b / a - 1)
                elif a:
                    later = [data[c][x][3] for x in sorted(data[c]) if d0 < x <= d1]
                    rs.append((later[-1] / a - 1) if later else -0.9)        # stopped trading: assume it mostly died
            turnover = len(set(picks) ^ held) / max(len(picks) + len(held), 1) * 2 if (picks or held) else 0
            r = (sum(rs) / len(rs) if rs else 0.0) - COST * turnover
            held = set(picks)
            eq *= 1 + r
            peak = max(peak, eq)
            dd = min(dd, eq / peak - 1)
            wk.append((d0, r))
            if len(picks_log) < 400:
                picks_log.append([d0.isoformat(), picks[:5]])
        yrs = {}
        for d0, r in wk:
            yrs.setdefault(d0.year, 1.0)
            yrs[d0.year] *= 1 + r
        first = [r for d0, r in wk if d0 < split]
        last = [r for d0, r in wk if d0 >= split]
        comp = lambda xs: math.prod(1 + x for x in xs) - 1
        out[name] = {"total": round((eq - 1) * 100, 1), "max_drop": round(dd * 100, 1),
                     "weeks_up": round(sum(1 for _, r in wk if r > 0) / len(wk) * 100), "first_part": round(comp(first) * 100, 1),
                     "last_part": round(comp(last) * 100, 1), "by_year": {y: round((v - 1) * 100, 1) for y, v in yrs.items()},
                     "start_500": round(500 * eq), "recent_picks": picks_log[-6:]}
        print(name, out[name]["total"], out[name]["max_drop"], out[name]["first_part"], out[name]["last_part"], flush=True)
    uni_sizes = [len(universe(d)) for d, _ in weeks[::4]]
    res = {"when": t0, "weeks": len(weeks), "from": weeks[0][0].isoformat(), "to": weeks[-1][1].isoformat(),
           "split": split.isoformat(), "coins": len(data), "universe_avg": round(sum(uni_sizes) / len(uni_sizes)),
           "universe_now": universe(weeks[-1][1]) if weeks else [], "rules": out}
    json.dump(res, open("penny_lab.json", "w"), indent=1, default=str)
    print(json.dumps({k: v for k, v in res.items() if k != "rules"}, default=str))


if __name__ == "__main__":
    main()
