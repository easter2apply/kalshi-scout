"""
GLD Study: the Inspector learns gold (GLD) before Vault Options switches to it.

1) Gold facts     how GLD moves: by hour, weekday, gaps, Fed/CPI/jobs days, versus the dollar (UUP), rates (TLT) and stocks (SPY)
2) News           what each kind of headline does to GLD in the next hour (direction and size)
3) Trends         do GLD trends keep going? (50-day / 20-day trend, last 5 days)
4) Every setup    every setup today's Vault Options would have taken on GLD, priced on REAL GLD option bars, tagged
                  with ~25 conditions (time, news, dollar, rates, stocks, gold fear gauge, trend, gap...) -> lessons + playbook
5) New entries    gap fade, and Easter's 200 EMA + supply/demand strategy, priced on real GLD options
Account size for the test: $1,000 (10% risk per trade, -40% option stop), because GLD options cost more than IWM's.
First 2/3 of the period = "train", last 1/3 = "unseen".
"""
import bisect
import datetime as dt
import json
import os
import statistics as stt
import time
import urllib.request

import vault_options as vo
import options_inspector as oi
import iwm_options_lab as L
import iwm_study as S
import entry_lab as E

UTC, ET = dt.timezone.utc, vo.LOCAL_TZ
SYM = "GLD"
MONTHS = 24
ACCOUNT = 1000.0
L.SYM = SYM                                     # option symbols (OCC) are built from this
oi.BUDGET = ACCOUNT * vo.RISK_PCT / vo.OPT_STOP # $250 per trade
oi.RISK_USD = ACCOUNT * vo.RISK_PCT             # 1R = $100
oi.strike_step = lambda price, etf: 1.0         # GLD has $1 strikes near the money
KEY = "her_s40"
S.FOMC = S.FOMC
NEWS = {
    "Fed / interest rates": ("fed ", "fomc", "powell", "rate cut", "rate hike", "interest rate", "fed's", "federal reserve", "treasury yield", "yields"),
    "Inflation (CPI/PCE)": ("cpi", "inflation", "pce", "consumer price"),
    "Jobs data": ("payroll", "jobs report", "unemployment", "jobless", "nonfarm"),
    "US dollar": ("dollar", "greenback", "dxy", "currency"),
    "War / geopolitics": ("war", "missile", "strike on", "attack", "iran", "israel", "russia", "ukraine", "china", "taiwan", "sanction", "conflict", "military"),
    "Tariffs / trade": ("tariff", "trade war", "trade deal"),
    "Gold & central banks": ("gold", "bullion", "central bank buying", "safe haven", "safe-haven", "precious metal"),
    "Stock sell-off / fear": ("sell-off", "selloff", "plunge", "tumble", "crash", "recession", "turmoil"),
    "Government shutdown / debt": ("shutdown", "debt ceiling", "deficit", "downgrade"),
}


# ───────────────────────────── data ─────────────────────────────

def cboe(name):
    try:
        req = urllib.request.Request(f"https://cdn.cboe.com/api/global/us_indices/daily_prices/{name}_History.csv",
                                     headers={"User-Agent": "Mozilla/5.0"})
        rows = urllib.request.urlopen(req, timeout=60).read().decode().splitlines()[1:]
        out = {}
        for r in rows:
            p = r.split(",")
            m, d, y = p[0].split("/")
            out[dt.date(int(y), int(m), int(d))] = float(p[-1])
        return out
    except Exception as e:
        print(name, "unavailable", e)
        return {}


def news(api, start, end, syms):
    out, token = [], None
    for _ in range(1200):
        p = {"symbols": syms, "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "limit": 50, "sort": "asc"}
        if token:
            p["page_token"] = token
        try:
            j = api.d("/v1beta1/news", p)
        except vo.ApiError as e:
            if "429" in str(e):
                time.sleep(10); continue
            print("news stopped:", e); break
        for n in j.get("news") or []:
            t = dt.datetime.fromisoformat(n["created_at"].replace("Z", "+00:00"))
            out.append((t.timestamp(), (n.get("headline") or "").lower()))
        token = j.get("next_page_token")
        if not token:
            break
    out.sort()
    return out


def groups_of(h):
    return [g for g, kws in NEWS.items() if any(k in h for k in kws)]


avg, r2 = S.avg, S.r2


def corr(a, b):
    try:
        return r2(stt.correlation(a, b))
    except Exception:
        return None


# ───────────────────────────── 1-3) facts, news, trends ─────────────────────────────

def facts(D, gld_days, events):
    g, spy, tlt, uup = D["GLD"], D["SPY"], D["TLT"], D["UUP"]
    days = sorted(d for d in g if d in spy)
    ret = {}
    for a, b in zip(days, days[1:]):
        ret[b] = {k: (D[k][b]["c"] / D[k][a]["c"] - 1) if (a in D[k] and b in D[k]) else None for k in ("GLD", "SPY", "TLT", "UUP")}
    def pair(k):
        xs = [(v["GLD"], v[k]) for v in ret.values() if v[k] is not None]
        return corr([x for x, _ in xs], [y for _, y in xs])
    rng = [(g[d]["h"] - g[d]["l"]) / g[d]["o"] for d in days]
    hours = {}
    for d, bars in gld_days.items():
        for b in bars:
            hours.setdefault(dt.datetime.fromtimestamp(b["ts"], ET).hour, []).append(abs(b["c"] / b["o"] - 1))
    wk = {}
    for d, v in ret.items():
        wk.setdefault(d.strftime("%a"), []).append(v["GLD"])
    gaps = fills = 0
    follow = []
    for a, b in zip(days, days[1:]):
        if b not in gld_days:
            continue
        gp = g[b]["o"] / g[a]["c"] - 1
        if abs(gp) >= 0.004:
            gaps += 1
            fills += any(x["l"] <= g[a]["c"] <= x["h"] for x in gld_days[b])
            follow.append((gp, g[b]["c"] / g[b]["o"] - 1))
    ev = {}
    for name, ds in events.items():
        on = [abs(ret[d]["GLD"]) for d in ret if d in ds]
        off = [abs(ret[d]["GLD"]) for d in ret if d not in ds]
        ev[name] = {"days": len(on), "avg_move_pct": r2(avg(on) * 100 if on else None), "normal_pct": r2(avg(off) * 100),
                    "x_normal": r2(avg(on) / avg(off) if on else None)}
    # dollar / rates / stocks days
    def cond(k, lo, hi):
        xs = [v["GLD"] for v in ret.values() if v[k] is not None and lo <= v[k] < hi]
        return {"days": len(xs), "gld_avg_pct": r2(avg(xs) * 100 if xs else None, 3),
                "gld_up": r2(sum(1 for x in xs if x > 0) / len(xs) if xs else None)}
    rel = {"Dollar up big (UUP +0.4%+)": cond("UUP", 0.004, 9), "Dollar down big (UUP -0.4%+)": cond("UUP", -9, -0.004),
           "Rates falling (TLT +0.8%+)": cond("TLT", 0.008, 9), "Rates rising (TLT -0.8%+)": cond("TLT", -9, -0.008),
           "Stocks drop 1%+ (SPY)": cond("SPY", -9, -0.01), "Stocks rally 1%+ (SPY)": cond("SPY", 0.01, 9)}
    # FOMC 2 PM -> close
    fa, na = [], []
    for d, bars in gld_days.items():
        p2 = S.price_at(bars, dt.datetime(d.year, d.month, d.day, 14, 0, tzinfo=ET).timestamp())
        if p2:
            (fa if d.isoformat() in S.FOMC else na).append(abs(bars[-1]["c"] / p2 - 1))
    return {"days": len(days), "corr_spy": pair("SPY"), "corr_tlt": pair("TLT"), "corr_dollar_uup": pair("UUP"),
            "avg_daily_range_pct": r2(avg(rng) * 100),
            "hour_move_pct": {h: r2(avg(v) * 400, 3) for h, v in sorted(hours.items())},
            "weekday": {k: {"avg_pct": r2(avg(v) * 100, 3), "up": r2(sum(1 for x in v if x > 0) / len(v))} for k, v in wk.items()},
            "gaps": gaps, "gap_fill_rate": r2(fills / max(gaps, 1)),
            "gap_keeps_going": r2(sum(1 for a, b in follow if a * b > 0) / max(len(follow), 1)),
            "events": ev, "gld_on": rel,
            "fomc_2pm_to_close_pct": r2(avg(fa) * 100 if fa else None), "normal_2pm_to_close_pct": r2(avg(na) * 100)}


def news_reactions(nw, gld_days):
    """For each kind of headline during market hours: GLD's move over the next hour (signed and size)."""
    typical = []
    for d, bars in gld_days.items():
        for i in range(len(bars) - 4):
            typical.append(abs(bars[i + 4]["c"] / bars[i]["c"] - 1))
    base = avg(typical)
    seen, out = set(), {}
    for ts, h in nw:
        e = dt.datetime.fromtimestamp(ts, ET)
        if e.date() not in gld_days or not ((9, 30) <= (e.hour, e.minute) < (15, 0)):
            continue
        bars = gld_days[e.date()]
        p0, p1 = S.price_at(bars, ts), S.price_at(bars, ts + 3600)
        if not p0 or not p1:
            continue
        for gname in groups_of(h):
            k = (gname, e.date(), e.hour)
            if k in seen:
                continue
            seen.add(k)
            out.setdefault(gname, []).append(p1 / p0 - 1)
    res = {}
    for gname, xs in out.items():
        res[gname] = {"headline_hours": len(xs), "next_hour_size_pct": r2(avg([abs(x) for x in xs]) * 100, 3),
                      "x_normal_hour": r2(avg([abs(x) for x in xs]) / base if base else None),
                      "next_hour_avg_pct": r2(avg(xs) * 100, 3), "up_share": r2(sum(1 for x in xs if x > 0) / len(xs))}
    return {"normal_hour_pct": r2(base * 100, 3), "groups": res}


def trends(g):
    days = sorted(g)
    c = [g[d]["c"] for d in days]
    out = {"above_50d": [], "below_50d": [], "20d_over_50d": [], "20d_under_50d": [], "up_5d": [], "down_5d": []}
    daily = [c[i] / c[i - 1] - 1 for i in range(1, len(c))]
    for i in range(50, len(c) - 5):
        s50, s20 = sum(c[i - 49:i + 1]) / 50, sum(c[i - 19:i + 1]) / 20
        fwd = c[i + 5] / c[i] - 1
        out["above_50d" if c[i] > s50 else "below_50d"].append(fwd)
        out["20d_over_50d" if s20 > s50 else "20d_under_50d"].append(fwd)
        out["up_5d" if c[i] > c[i - 5] else "down_5d"].append(fwd)
    res = {k: {"days": len(v), "next_5d_avg_pct": r2(avg(v) * 100 if v else None), "next_5d_up": r2(sum(1 for x in v if x > 0) / len(v) if v else None)}
           for k, v in out.items()}
    res["daily_autocorr"] = corr(daily[:-1], daily[1:])
    res["up_days_share"] = r2(sum(1 for x in daily if x > 0) / len(daily))
    res["total_move_pct"] = r2((c[-1] / c[0] - 1) * 100)
    return res


# ───────────────────────────── 4) tags for every setup ─────────────────────────────

def tag(s, ctx):
    e = dt.datetime.fromtimestamp(s["ts"], ET)
    d = e.date()
    pd_ = ctx["prev_day"].get(d)
    def move(sym):
        bars = ctx["days"][sym].get(d, [])
        px = S.price_at(bars, s["ts"])
        return (px / bars[0]["o"] - 1) if (px and bars) else 0
    gb = ctx["days"]["GLD"].get(d, [])
    prev = ctx["D"]["GLD"].get(pd_, {}).get("c")
    opn = gb[0]["o"] if gb else None
    gap = (opn / prev - 1) if (opn and prev) else 0
    mv = move("GLD")
    D = s["dir"]
    vix, gvz = ctx["vix"].get(pd_), ctx["gvz"].get(pd_)
    sma = ctx["sma50"].get(pd_)
    trend_up = (prev > sma) if (prev and sma) else None
    c5 = ctx["c5"].get(pd_)
    i0 = bisect.bisect_left(ctx["news_ts"], s["ts"] - 7200)
    i1 = bisect.bisect_right(ctx["news_ts"], s["ts"])
    gs = set()
    for _, h in ctx["news"][i0:i1]:
        gs.update(groups_of(h))
    t = {
        "Time of day": f"{e.hour}:00–{e.hour + 1}:00" if e.hour >= 10 else "9:30–10:00",
        "Weekday": e.strftime("%A"),
        "Setup": s["setup"].replace(" 15m", ""),
        "Direction": "calls (bullish)" if D == 1 else "puts (bearish)",
        "Council": "approved" if s["approved"] else "rejected",
        "Fed decision day": "yes" if d.isoformat() in S.FOMC else "no",
        "Jobs report day": "yes" if d in ctx["jobs_days"] else "no",
        "CPI day": "yes" if d in ctx["cpi_days"] else "no",
        "Gold fear gauge (GVZ)": ("unknown" if gvz is None else "calm (<15)" if gvz < 15 else "normal (15–20)" if gvz < 20
                                  else "jumpy (20–25)" if gvz < 25 else "wild (25+)"),
        "Stock fear gauge (VIX)": ("unknown" if vix is None else "calm (<15)" if vix < 15 else "normal (15–20)" if vix < 20 else "nervous (20+)"),
        "Trade vs GLD's 50-day trend": ("unknown" if trend_up is None else "with the trend" if (1 if trend_up else -1) == D else "against the trend"),
        "Trade vs last 5 days": ("unknown" if c5 is None else "with the last 5 days" if (1 if c5 > 0 else -1) == D else "against the last 5 days"),
        "Opening gap": "gap up > 0.4%" if gap > 0.004 else "gap down > 0.4%" if gap < -0.004 else "small gap",
        "Trade vs the gap": "with the gap" if gap * D > 0.002 else "against the gap" if gap * D < -0.002 else "no real gap",
        "Move since the open (in trade direction)": ("already ran > 0.6%" if mv * D > 0.006 else "against us > 0.6%" if mv * D < -0.006 else "quiet so far"),
        "Dollar today (UUP)": ("dollar helping" if -move("UUP") * D > 0.001 else "dollar fighting" if -move("UUP") * D < -0.001 else "dollar flat"),
        "Rates today (TLT)": ("rates helping" if move("TLT") * D > 0.002 else "rates fighting" if move("TLT") * D < -0.002 else "rates flat"),
        "Stocks today (SPY)": "stocks up" if move("SPY") > 0.003 else "stocks down" if move("SPY") < -0.003 else "stocks flat",
        "Option price": "under $1" if s.get("ask", 0) < 1 else "$1–2" if s["ask"] < 2 else "over $2",
    }
    for g in NEWS:
        t[f"News in last 2h: {g}"] = "yes" if g in gs else "no"
    return t


# ───────────────────────────── 5) Easter's 200 EMA + supply/demand ─────────────────────────────

def ema(xs, n):
    k, out, e = 2 / (n + 1), [], None
    for x in xs:
        e = x if e is None else x * k + e * (1 - k)
        out.append(e)
    return out


def atr_list(b, n=14):
    out, a = [], None
    for i, x in enumerate(b):
        tr = x["h"] - x["l"] if i == 0 else max(x["h"] - x["l"], abs(x["h"] - b[i - 1]["c"]), abs(x["l"] - b[i - 1]["c"]))
        a = tr if a is None else (a * (n - 1) + tr) / n
        out.append(a)
    return out


def find_zones(bars, atr, imp=1.5, cls=1.0, base_max=1.0):
    zones = []
    for i in range(20, len(bars) - 1):
        a = atr[i - 1]
        for blen in (1, 2, 3):
            base = bars[i - blen:i]
            if any(b["h"] - b["l"] > base_max * a for b in base):
                continue
            hi, lo = max(b["h"] for b in base), min(b["l"] for b in base)
            hit = None
            for k in (1, 2):
                mv = bars[i:i + k]
                if len(mv) < k:
                    continue
                if max(b["h"] for b in mv) - hi > imp * a and mv[-1]["c"] > hi + cls * a:
                    hit = {"kind": "demand", "hi": hi, "lo": lo, "known": i + k, "dead": False}; break
                if lo - min(b["l"] for b in mv) > imp * a and mv[-1]["c"] < lo - cls * a:
                    hit = {"kind": "supply", "hi": hi, "lo": lo, "known": i + k, "dead": False}; break
            if hit:
                zones.append(hit); break
    return zones


def pivots(bars, k=3):
    hi, lo = [], []
    for i in range(k, len(bars) - k):
        if all(bars[i]["h"] >= bars[j]["h"] for j in range(i - k, i + k + 1)):
            hi.append((i + k, bars[i]["h"]))
        if all(bars[i]["l"] <= bars[j]["l"] for j in range(i - k, i + k + 1)):
            lo.append((i + k, bars[i]["l"]))
    return hi, lo


def strong(b, d, a):
    body, rng = (b["c"] - b["o"]) * d, b["h"] - b["l"]
    return body > 0 and rng > 0 and body >= 0.6 * rng and body >= 0.25 * a


def ema200_signals(bars, zone=True, slope_min=1.0, start_ts=0):
    """Her rules on GLD 15-min bars. Returns signals with the stock-level result too (stop / swing target)."""
    atr = atr_list(bars)
    e200 = ema([b["c"] for b in bars], 200)
    zones = find_zones(bars, atr) if zone else []
    sh, sl = pivots(bars)
    out, zi, live, busy = [], 0, [], 0
    for i in range(260, len(bars) - 3):
        while zi < len(zones) and zones[zi]["known"] <= i:
            live.append(zones[zi]); zi += 1
        c = bars[i]["c"]
        for z in live:
            if (z["kind"] == "demand" and c < z["lo"]) or (z["kind"] == "supply" and c > z["hi"]):
                z["dead"] = True
        live = [z for z in live if not z["dead"]][-150:]
        if i < busy or bars[i]["ts"] < start_ts:
            continue
        a, e = atr[i], e200[i]
        slope = (e - e200[i - 50]) / a if a else 0
        if abs(slope) < slope_min:
            continue
        d = 1 if slope > 0 else -1
        b = bars[i]
        if not all((bars[j]["c"] - e200[j]) * d > 0 for j in range(i - 3, i)):
            continue
        if not ((b["l"] <= e) if d == 1 else (b["h"] >= e)):
            continue
        if zone:
            want = "demand" if d == 1 else "supply"
            if not any(z["kind"] == want and z["lo"] - 0.5 * a <= e <= z["hi"] + 0.5 * a for z in live):
                continue
        c1 = None
        for s_ in (i, i + 1):
            if strong(bars[s_], d, a) and strong(bars[s_ + 1], d, a) and (bars[s_ + 1]["c"] - e200[s_ + 1]) * d > 0:
                c1 = s_; break
        if c1 is None:
            continue
        c2 = c1 + 1
        entry = bars[c2]["c"]
        stop = bars[c1]["l"] if d == 1 else bars[c1]["h"]
        risk = abs(entry - stop)
        cands = ([p for k, p in sh if k <= c2 and p > entry and k >= c2 - 300] if d == 1
                 else [p for k, p in sl if k <= c2 and p < entry and k >= c2 - 300])
        if not cands or risk <= 0:
            continue
        tp = cands[-1]
        r = None
        for j in range(c2 + 1, min(len(bars), c2 + 26 * 5)):
            x = bars[j]
            if (x["l"] <= stop) if d == 1 else (x["h"] >= stop):
                r = -1.0; break
            if (x["h"] >= tp) if d == 1 else (x["l"] <= tp):
                r = abs(tp - entry) / risk; break
        if r is None:
            j = min(len(bars), c2 + 26 * 5) - 1
            r = d * (bars[j]["c"] - entry) / risk
        out.append({"ts": bars[c2]["ts"] + 900, "dir": d, "px": entry, "stock_r": round(r - 0.02, 3), "rr": round(abs(tp - entry) / risk, 2)})
        busy = c2 + 1
    return out


# ───────────────────────────── main ─────────────────────────────

def main():
    t0 = time.time()
    api = vo.Alpaca(os.environ["ALPACA_KEY_ID"].strip(), os.environ["ALPACA_SECRET_KEY"].strip())
    now = dt.datetime.now(UTC)
    start = now - dt.timedelta(days=MONTHS * 30.4)
    log = []
    oi.ALL_EXITS = {"now": {"style": "her_trail", "stop": 0.40}}
    oi.walk = L.model_walk
    m15 = oi.fetch_bars(api, SYM, "15Min", start - dt.timedelta(days=40))
    h1 = oi.fetch_bars(api, SYM, "1Hour", start - dt.timedelta(days=75))
    intraday = {k: S.by_day(oi.fetch_bars(api, k, "15Min", start - dt.timedelta(days=5))) for k in ("SPY", "TLT", "UUP")}
    intraday["GLD"] = S.by_day(m15)
    D = {k: S.daily_bars(api, k, start - dt.timedelta(days=120)) for k in ("GLD", "SPY", "TLT", "UUP")}
    vix, gvz = S.vix_history(), cboe("GVZ")
    nw = news(api, start - dt.timedelta(days=2), now, "GLD,IAU,GDX,SLV,SPY")
    log.append(f"Data: {len(m15)} GLD 15-min bars, {len(D['GLD'])} days, {len(nw)} headlines, VIX {len(vix)}, GVZ {len(gvz)} days")
    print(log[-1], flush=True)

    jobs_days, cpi_days = set(), set()
    for ts, h in nw:
        e = dt.datetime.fromtimestamp(ts, ET)
        if not (8 <= e.hour < 10):
            continue
        if ("nonfarm" in h or "payrolls" in h or "jobs report" in h) and e.weekday() == 4:
            jobs_days.add(e.date())
        if ("cpi" in h or "consumer price index" in h) and any(w in h for w in ("rose", "rises", "y/y", "m/m", "vs", "est")):
            cpi_days.add(e.date())
    gd = D["GLD"]
    ddays = sorted(gd)
    prev_day = {b: a for a, b in zip(ddays, ddays[1:])}
    sma50, c5 = {}, {}
    cl = []
    for i, d in enumerate(ddays):
        cl.append(gd[d]["c"])
        if len(cl) >= 50:
            sma50[d] = sum(cl[-50:]) / 50
        if i >= 5:
            c5[d] = cl[-1] / cl[-6] - 1
    gld_days = {d: b for d, b in intraday["GLD"].items() if d >= start.date()}
    F = facts(D, gld_days, {"Fed decision days": {dt.date.fromisoformat(x) for x in S.FOMC}, "Jobs report days": jobs_days,
                            "CPI days": cpi_days})
    F["jobs_days_found"], F["cpi_days_found"] = len(jobs_days), len(cpi_days)
    NR = news_reactions(nw, gld_days)
    TR = trends({d: v for d, v in gd.items() if d >= start.date() - dt.timedelta(days=80)})
    print(json.dumps({"facts": F, "news": NR, "trends": TR}, default=str)[:3000], flush=True)

    # 4) today's bot on GLD, real option prices
    ctx = {"days": intraday, "D": D, "prev_day": prev_day, "vix": vix, "gvz": gvz, "sma50": sma50, "c5": c5,
           "news": nw, "news_ts": [x[0] for x in nw], "jobs_days": jobs_days, "cpi_days": cpi_days}
    setups = oi.replay(SYM, m15, h1, start.timestamp(), log)
    close_at = {b["ts"] + 900: b for b in m15}
    rp = L.RealPrices(api)
    rows, costs, no_fit = [], [], 0
    for s in setups:
        b = close_at.get(s["ts"])
        if not b:
            continue
        at = dt.datetime.fromtimestamp(s["ts"], UTC)
        keep_b = oi.BUDGET
        oi.BUDGET = 1e9
        big, _ = oi.pick(SYM, s["dir"], b["c"], s["iv"], at)       # what the nearest-the-money contract costs
        oi.BUDGET = keep_b
        if big:
            costs.append(big["ask"] * 100)
        if not s.get("contract"):
            no_fit += 1
            continue
        ct, _ = oi.pick(SYM, s["dir"], b["c"], s["iv"], at)
        if not ct:
            continue
        ro = E.real_from_open(rp, ct, s["ts"])
        if not ro or KEY not in ro:
            continue
        s["ask"] = ct["ask"]
        rows.append({"ts": s["ts"], "t": s["t"], "dir": s["dir"], "setup": s["setup"], "approved": s["approved"],
                     "votes": s["votes"], "real": ro, "tags": tag(s, ctx)})
        if len(rows) % 150 == 0:
            print(f"{len(rows)} setups priced ({rp.calls} calls, {(time.time() - t0) / 60:.1f} min)", flush=True)
    log.append(f"{len(rows)} GLD bot setups priced on real option bars; {no_fit} had no contract within the ${oi.BUDGET:.0f} budget")
    costs.sort()
    cost_info = {"median_contract_usd": round(costs[len(costs) // 2]) if costs else None,
                 "p25": round(costs[len(costs) // 4]) if costs else None, "p75": round(costs[3 * len(costs) // 4]) if costs else None,
                 "min_account_for_median": round(costs[len(costs) // 2] / (vo.RISK_PCT / vo.OPT_STOP)) if costs else None}
    rows.sort(key=lambda r: r["ts"])
    split = (rows[0]["ts"] + (rows[-1]["ts"] - rows[0]["ts"]) * 2 / 3) if rows else start.timestamp() + (now - start).total_seconds() * 2 / 3
    split_label = dt.datetime.fromtimestamp(split, ET).strftime("%b %d, %Y")
    res = lambda taken, key=KEY: S.summary(taken, key, split)

    ideas = {}
    approved = [r for r in rows if r["approved"]]
    if rows:
        ideas["Today's bot on GLD (council approves, your exits)"] = res(E.sequential(approved, KEY))
        ideas["Today's bot, -30% stop"] = res(E.sequential(approved, "her_s30"), "her_s30")
        ideas["Today's bot, only WITH GLD's 50-day trend"] = res(E.sequential(
            approved, KEY, keep=lambda r, c: r["tags"]["Trade vs GLD's 50-day trend"] == "with the trend"))
        ideas["Today's bot, no chasing (same rules as IWM)"] = res(E.sequential(
            approved, KEY, keep=lambda r, c: r["real"]["run_up"] < 0.5, no_reentry=True))
        ideas["Every setup (even ones the council rejected)"] = res(E.sequential(rows, KEY))
    LS = S.lessons(rows, split, KEY) if len(rows) > 60 else None
    rules, filt = S.playbook(rows, split, KEY) if len(rows) > 150 else ([], lambda r: True)
    pb = res(E.sequential([r for r in rows if filt(r)], KEY)) if rules else None

    # 5) new entries
    def priced(sigs):
        out = []
        for g in sigs:
            ct, _ = oi.pick(SYM, g["dir"], g["px"], max(oi.IV_FLOOR, g.get("iv", 0.18)), dt.datetime.fromtimestamp(g["ts"], UTC))
            if not ct:
                continue
            ro = E.real_from_open(rp, ct, g["ts"])
            if ro and KEY in ro:
                out.append({"ts": g["ts"], "dir": g["dir"], "real": ro, "stock_r": g.get("stock_r")})
        return out

    gaps = {}
    for min_gap in (0.003, 0.005, 0.008):
        for at in ((9, 45), (10, 0), (10, 30)):
            for confirm in (False, True):
                gs = E.gap_fade_setups(m15, gd, prev_day, min_gap, at, confirm)
                gs = [g for g in gs if g["ts"] >= start.timestamp()]
                grows = priced(gs)
                if len(grows) >= 15:
                    gaps[f"Gap fade: gap {min_gap:.1%}+, enter {at[0]}:{at[1]:02d}" + (", once it turns" if confirm else "")] = res(E.sequential(grows, KEY))
    ema_res = {}
    for name, zone, slope in (("Her 200 EMA + zone, clear slope", True, 1.0), ("Her 200 EMA + zone, gentle slope", True, 0.5),
                              ("200 EMA tap + 2 candles, no zone", False, 1.0)):
        sigs = ema200_signals(m15, zone, slope, start.timestamp())
        for g in sigs:
            j = bisect.bisect_right([b["ts"] + 900 for b in m15], g["ts"]) - 1
            g["iv"] = max(oi.IV_FLOOR, (oi.realized_vol(m15, j) or 0.15) * oi.IV_MULT)
        stock = [g["stock_r"] for g in sigs]
        pr = priced(sigs)
        ema_res[name] = {"signals": len(sigs), "stock_avg_r": r2(avg(stock), 3) if stock else None,
                         "stock_win": r2(sum(1 for x in stock if x > 0) / len(stock)) if stock else None,
                         "stock_total_r": r2(sum(stock), 1) if stock else None,
                         "options": res(E.sequential(pr, KEY)) if pr else None,
                         "options_s30": res(E.sequential(pr, "her_s30"), "her_s30") if pr else None}
        print(name, ema_res[name], flush=True)

    out = {"when": t0, "split": split_label, "account": ACCOUNT, "budget": oi.BUDGET, "facts": F, "news": NR, "trends": TR,
           "contract_cost": cost_info, "ideas": ideas, "lessons": LS, "playbook": {"rules": rules, "result": pb},
           "gap_fade": gaps, "ema200": ema_res, "log": log, "calls": rp.calls}
    log.append(f"Finished in {(time.time() - t0) / 60:.1f} min, {rp.calls} option price calls")
    json.dump(out, open("gld_study.json", "w"), indent=1, default=str)
    for k, v in list(ideas.items()) + list(gaps.items()):
        print(f"{k}: n={v['n']} win={v['win']} avg={v['avg_r']} first={v['first']} unseen={v['unseen']} (n={v['n_unseen']}) ${v['usd']}")
    print(json.dumps({"cost": cost_info, "playbook": out["playbook"]}, default=str))
    print("\n".join(log))


if __name__ == "__main__":
    main()
