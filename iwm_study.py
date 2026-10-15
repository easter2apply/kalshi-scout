"""
IWM Study: the Inspector learns IWM inside and out and turns it into a playbook for the Vault Options council.

1) IWM facts      how IWM moves: by hour, weekday, gaps, Fed/jobs/CPI days, versus SPY and bonds (TLT = interest rates)
2) Every setup    every IWM setup the bot would have seen in 24 months, priced on the REAL option contract,
                  tagged with ~20 conditions (time, setup, direction, news, VIX, trend, gap, SPY, rates...)
3) Lessons        a condition only counts if it helped (or hurt) in BOTH the first 2/3 and the last 1/3 of the period
4) Playbook       up to 4 "skip" rules chosen on the first 2/3 only, then judged on the unseen last 1/3 (real prices,
                  one trade at a time like the live bot)
"""
import bisect
import datetime as dt
import json
import math
import os
import statistics as stt
import time
import urllib.request

import vault_options as vo
import options_inspector as oi
import iwm_options_lab as L

UTC, ET = dt.timezone.utc, vo.LOCAL_TZ
SYM = "IWM"
MONTHS = 24
RULES = {"her_s30": {"style": "her_trail", "stop": 0.30}, "her_s40": {"style": "her_trail", "stop": 0.40},
         "her_s50": {"style": "her_trail", "stop": 0.50}, "old_s40": {"style": "old", "stop": 0.40},
         "all30_s40": {"style": "all30", "stop": 0.40}}
MAIN = "her_s40"
FOMC = {"2024-11-07", "2024-12-18", "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18", "2025-07-30", "2025-09-17",
        "2025-10-29", "2025-12-10", "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16"}
RECON = {"2025-06-27", "2026-06-26"}            # Russell reconstitution (huge IWM volume at the close)
NEWS_GROUPS = {
    "Fed / interest rates": ("fed ", "fomc", "powell", "rate cut", "rate hike", "interest rate", "fed's", "federal reserve"),
    "Inflation (CPI/PCE)": ("cpi", "inflation", "pce", "consumer price"),
    "Jobs data": ("payroll", "jobs report", "unemployment", "jobless", "nonfarm"),
    "Tariffs / trade": ("tariff", "trade war", "trade deal"),
    "Small caps / Russell": ("small cap", "small-cap", "russell 2000", "russell", "iwm"),
    "Bank stress / credit": ("regional bank", "bank stock", "credit", "default"),
}


# ───────────────────────────── data ─────────────────────────────

def daily_bars(api, sym, start):
    out, token = [], None
    while True:
        p = {"symbols": sym, "timeframe": "1Day", "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": 10000,
             "feed": vo.STOCK_FEED, "adjustment": "split", "sort": "asc"}
        if token:
            p["page_token"] = token
        j = api.d("/v2/stocks/bars", p)
        out += (j.get("bars") or {}).get(sym, [])
        token = j.get("next_page_token")
        if not token:
            break
    return {vo.et(b["t"]).date(): b for b in out}


def vix_history():
    try:
        req = urllib.request.Request("https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
                                     headers={"User-Agent": "Mozilla/5.0"})
        rows = urllib.request.urlopen(req, timeout=60).read().decode().splitlines()[1:]
        out = {}
        for r in rows:
            p = r.split(",")
            m, d, y = p[0].split("/")
            out[dt.date(int(y), int(m), int(d))] = float(p[4])
        return out
    except Exception as e:
        print("VIX unavailable", e)
        return {}


def news(api, start, end):
    out, token = [], None
    for _ in range(800):
        p = {"symbols": "IWM,SPY,QQQ", "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "limit": 50, "sort": "asc"}
        if token:
            p["page_token"] = token
        try:
            j = api.d("/v1beta1/news", p)
        except vo.ApiError as e:
            if "429" in str(e):
                time.sleep(10)
                continue
            print("news stopped:", e)
            break
        for n in j.get("news") or []:
            t = dt.datetime.fromisoformat(n["created_at"].replace("Z", "+00:00"))
            out.append((t.timestamp(), (n.get("headline") or "").lower()))
        token = j.get("next_page_token")
        if not token:
            break
    out.sort()
    return out


def group_of(h):
    return [g for g, kws in NEWS_GROUPS.items() if any(k in h for k in kws)]


# ───────────────────────────── helpers ─────────────────────────────

def avg(xs):
    return sum(xs) / len(xs) if xs else None


def r2(x, n=2):
    return None if x is None else round(x, n)


def by_day(m15):
    days = {}
    for b in m15:
        days.setdefault(dt.datetime.fromtimestamp(b["ts"], ET).date(), []).append(b)
    return days


def price_at(day_bars, ts):
    """close of the last 15-min bar that finished at or before ts"""
    px = None
    for b in day_bars:
        if b["ts"] + 900 <= ts:
            px = b["c"]
    return px


# ───────────────────────────── 1) IWM facts ─────────────────────────────

def facts(iwm_d, spy_d, tlt_d, iwm_days, events):
    days = sorted(d for d in iwm_d if d in spy_d)
    ret = {}
    for a, b in zip(days, days[1:]):
        ret[b] = (iwm_d[b]["c"] / iwm_d[a]["c"] - 1, spy_d[b]["c"] / spy_d[a]["c"] - 1,
                  (tlt_d[b]["c"] / tlt_d[a]["c"] - 1) if a in tlt_d and b in tlt_d else None)
    xs = [v for v in ret.values()]
    iw = [v[0] for v in xs]
    sp = [v[1] for v in xs]
    beta = (sum((a - avg(iw)) * (b - avg(sp)) for a, b in zip(iw, sp)) / sum((b - avg(sp)) ** 2 for b in sp))
    tl = [(v[0], v[2]) for v in xs if v[2] is not None]
    corr_tlt = stt.correlation([a for a, _ in tl], [b for _, b in tl])
    corr_spy = stt.correlation(iw, sp)
    rng = [(iwm_d[d]["h"] - iwm_d[d]["l"]) / iwm_d[d]["o"] for d in days]
    # hour-by-hour typical move
    hours = {}
    for d, bars in iwm_days.items():
        for b in bars:
            h = dt.datetime.fromtimestamp(b["ts"], ET).hour
            hours.setdefault(h, []).append(abs(b["c"] / b["o"] - 1))
    hour_move = {h: r2(avg(v) * 100 * 4, 3) for h, v in sorted(hours.items())}      # ≈ % per hour
    # weekday
    wk = {}
    for d, v in ret.items():
        wk.setdefault(d.strftime("%a"), []).append(v[0])
    weekday = {k: {"avg_pct": r2(avg(v) * 100, 3), "up": r2(sum(1 for x in v if x > 0) / len(v), 2)} for k, v in wk.items()}
    # gaps: how often IWM gets back to yesterday's close the same day
    gaps, fills = 0, 0
    big_gap_follow = []
    for a, b in zip(days, days[1:]):
        if b not in iwm_days:
            continue
        g = iwm_d[b]["o"] / iwm_d[a]["c"] - 1
        if abs(g) >= 0.004:
            gaps += 1
            prev = iwm_d[a]["c"]
            hit = any((x["l"] <= prev <= x["h"]) for x in iwm_days[b])
            fills += hit
            big_gap_follow.append((g, iwm_d[b]["c"] / iwm_d[b]["o"] - 1))
    gap_cont = sum(1 for g, r in big_gap_follow if g * r > 0) / max(len(big_gap_follow), 1)
    # event days
    ev = {}
    for name, ds in events.items():
        on = [abs(ret[d][0]) for d in ret if d in ds]
        off = [abs(ret[d][0]) for d in ret if d not in ds]
        ev[name] = {"days": len(on), "avg_abs_move_pct": r2(avg(on) * 100 if on else None, 2),
                    "normal_day_pct": r2(avg(off) * 100, 2), "x_normal": r2(avg(on) / avg(off) if on else None, 2)}
    # FOMC: the 2 PM move
    fomc_after = []
    for d in iwm_days:
        if d.isoformat() in FOMC:
            bars = iwm_days[d]
            p2 = price_at(bars, dt.datetime(d.year, d.month, d.day, 14, 0, tzinfo=ET).timestamp())
            p4 = bars[-1]["c"]
            if p2:
                fomc_after.append(abs(p4 / p2 - 1))
    normal_after = []
    for d, bars in iwm_days.items():
        if d.isoformat() not in FOMC:
            p2 = price_at(bars, dt.datetime(d.year, d.month, d.day, 14, 0, tzinfo=ET).timestamp())
            if p2:
                normal_after.append(abs(bars[-1]["c"] / p2 - 1))
    # rates: IWM vs SPY on big bond days
    big_tlt_up = [v[0] - v[1] for v in xs if v[2] is not None and v[2] >= 0.008]
    big_tlt_dn = [v[0] - v[1] for v in xs if v[2] is not None and v[2] <= -0.008]
    return {"days": len(days), "beta_to_spy": r2(beta), "corr_spy": r2(corr_spy), "corr_tlt": r2(corr_tlt),
            "avg_daily_range_pct": r2(avg(rng) * 100), "hour_move_pct": hour_move, "weekday": weekday,
            "gaps": gaps, "gap_fill_rate": r2(fills / max(gaps, 1)), "gap_keeps_going": r2(gap_cont),
            "events": ev, "fomc_2pm_to_close_pct": r2(avg(fomc_after) * 100 if fomc_after else None),
            "normal_2pm_to_close_pct": r2(avg(normal_after) * 100),
            "iwm_vs_spy_when_rates_fall_pct": r2(avg(big_tlt_up) * 100 if big_tlt_up else None),
            "iwm_vs_spy_when_rates_rise_pct": r2(avg(big_tlt_dn) * 100 if big_tlt_dn else None),
            "big_rate_days": [len(big_tlt_up), len(big_tlt_dn)]}


# ───────────────────────────── 2) every setup on real prices ─────────────────────────────

def real_outcomes(rp, ct, signal_ts):
    """Real result of one setup under every exit rule (one price fetch)."""
    sell_day, sell_hm = L.sell_time(ct, None)
    start = dt.datetime.fromtimestamp(signal_ts, UTC)
    end = dt.datetime(sell_day.year, sell_day.month, sell_day.day, 16, 0, tzinfo=ET).astimezone(UTC)
    bars = rp.bars(L.occ(ct), start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ"))
    if not bars or (bars[0][0] - start).total_seconds() > 1800:
        return None
    entry = round(bars[0][1] * 1.02 + 0.01, 2)
    qty = max(1, min(vo.MAX_CONTRACTS, int(oi.BUDGET // (entry * 100))))
    out = {"entry": entry, "qty": qty}
    for key, rule in RULES.items():
        def steps():
            for i, (t, o, h, l, cl) in enumerate(bars):
                e = (t + dt.timedelta(minutes=15)).astimezone(ET)
                last = i == len(bars) - 1 or (e.date() == sell_day and (e.hour, e.minute) >= sell_hm)
                yield i, o * 0.98, l * 0.98, h * 0.98, cl * 0.98, last
                if last:
                    return
        res = L.walker(entry, qty, steps(), rule)
        if res:
            pl, how, k = res
            out[key] = {"pl": round(pl, 2), "r": round(pl / oi.RISK_USD, 3), "how": how,
                        "exit_ts": (bars[k][0] + dt.timedelta(minutes=15)).timestamp()}
    return out


def tag(s, ctx):
    """All the conditions at the moment of the setup."""
    e = dt.datetime.fromtimestamp(s["ts"], ET)
    d = e.date()
    iwm_day = ctx["iwm_days"].get(d, [])
    spy_day = ctx["spy_days"].get(d, [])
    tlt_day = ctx["tlt_days"].get(d, [])
    prev = ctx["prev_close"].get(d)
    px = price_at(iwm_day, s["ts"]) or 0
    opn = iwm_day[0]["o"] if iwm_day else None
    gap = (opn / prev - 1) if (opn and prev) else 0
    move = (px / opn - 1) if (opn and px) else 0
    spx = price_at(spy_day, s["ts"])
    spy_move = (spx / spy_day[0]["o"] - 1) if (spx and spy_day) else 0
    tpx = price_at(tlt_day, s["ts"])
    tlt_move = (tpx / tlt_day[0]["o"] - 1) if (tpx and tlt_day) else 0
    vix = ctx["vix"].get(ctx["prev_day"].get(d))                          # yesterday's close: known at the time
    sma = ctx["sma50"].get(ctx["prev_day"].get(d))
    ydc = ctx["iwm_d"].get(ctx["prev_day"].get(d), {}).get("c")
    trend_up = (ydc > sma) if (ydc and sma) else None
    i0 = bisect.bisect_left(ctx["news_ts"], s["ts"] - 7200)
    i1 = bisect.bisect_right(ctx["news_ts"], s["ts"])
    groups = set()
    for _, h in ctx["news"][i0:i1]:
        groups.update(group_of(h))
    d_s = d.isoformat()
    third_fri = d.weekday() == 4 and 15 <= d.day <= 21
    D = s["dir"]
    t = {
        "Time of day": f"{e.hour}:00–{e.hour + 1}:00" if e.hour >= 10 else "9:30–10:00",
        "Weekday": e.strftime("%A"),
        "Setup": s["setup"].replace(" 15m", ""),
        "Direction": "calls (bullish)" if D == 1 else "puts (bearish)",
        "Expiration": "0DTE (same day)" if s.get("zero") else "weekly",
        "Council": "approved" if s["approved"] else "rejected",
        "Fed decision day": "yes" if d_s in FOMC else "no",
        "Jobs report day": "yes" if d in ctx["jobs_days"] else "no",
        "CPI day": "yes" if d in ctx["cpi_days"] else "no",
        "Monthly options expiration (3rd Friday)": "yes" if third_fri else "no",
        "Russell reconstitution day": "yes" if d_s in RECON else "no",
        "VIX (fear gauge)": ("unknown" if vix is None else "calm (<15)" if vix < 15 else "normal (15–20)" if vix < 20
                             else "nervous (20–25)" if vix < 25 else "scared (25+)"),
        "IWM vs its 50-day average": "unknown" if trend_up is None else ("above (uptrend)" if trend_up else "below (downtrend)"),
        "Opening gap": ("gap up > 0.5%" if gap > 0.005 else "gap down > 0.5%" if gap < -0.005 else "small gap"),
        "Trade vs the gap": ("with the gap" if gap * D > 0.002 else "against the gap" if gap * D < -0.002 else "no real gap"),
        "Move since the open (in trade direction)": ("already ran > 0.7%" if move * D > 0.007 else "against us > 0.7%" if move * D < -0.007
                                                     else "quiet so far"),
        "SPY agrees (moving same way today)": "yes" if spy_move * D > 0.002 else "no" if spy_move * D < -0.002 else "flat",
        "IWM stronger than SPY today (in trade direction)": "yes" if (move - spy_move) * D > 0.002 else "no" if (move - spy_move) * D < -0.002 else "same",
        "Bonds/rates today": ("rates falling (TLT up)" if tlt_move > 0.003 else "rates rising (TLT down)" if tlt_move < -0.003 else "rates flat"),
        "Option price": "under $0.30" if s.get("ask", 0) < 0.30 else "$0.30–0.60" if s["ask"] < 0.60 else "over $0.60",
    }
    for g in NEWS_GROUPS:
        t[f"News in last 2h: {g}"] = "yes" if g in groups else "no"
    return t


# ───────────────────────────── 3) lessons + 4) playbook ─────────────────────────────

def lessons(rows, split, key):
    base = [r for r in rows if key in r["real"]]
    overall = avg([r["real"][key]["r"] for r in base])
    tr0 = avg([r["real"][key]["r"] for r in base if r["ts"] < split])
    te0 = avg([r["real"][key]["r"] for r in base if r["ts"] >= split])
    out = []
    for feat in base[0]["tags"]:
        vals = {}
        for r in base:
            vals.setdefault(r["tags"][feat], []).append(r)
        for v, rs in vals.items():
            tr = [r["real"][key]["r"] for r in rs if r["ts"] < split]
            te = [r["real"][key]["r"] for r in rs if r["ts"] >= split]
            if len(rs) < 40 or len(tr) < 15 or len(te) < 10:
                continue
            a, b = avg(tr), avg(te)
            inb = [r["real"][key]["r"] for r in rs]
            rest = [r["real"][key]["r"] for r in base if r["tags"][feat] != v]
            tstat = 0.0
            if len(inb) > 2 and len(rest) > 2:
                se = math.sqrt(stt.variance(inb) / len(inb) + stt.variance(rest) / len(rest))
                tstat = (avg(inb) - avg(rest)) / se if se else 0.0
            verdict = ("helps" if a > tr0 + 0.08 and b > te0 + 0.08 and tstat >= 2 else
                       "hurts" if a < tr0 - 0.08 and b < te0 - 0.08 and tstat <= -2 else "")
            pls = [r["real"][key]["pl"] for r in rs]
            out.append({"feature": feat, "value": v, "n": len(rs), "avg_r": r2(avg([r["real"][key]["r"] for r in rs]), 3),
                        "first": r2(a, 3), "unseen": r2(b, 3), "win": r2(sum(1 for p in pls if p > 0) / len(pls)),
                        "usd": round(sum(pls)), "t": r2(tstat), "verdict": verdict})
    return {"overall": r2(overall, 3), "first": r2(tr0, 3), "unseen": r2(te0, 3), "n": len(base), "rows": out}


def one_at_a_time(rs, key):
    """Like the live bot: IWM only, one trade open at a time, in time order."""
    taken, busy_until = [], 0
    for r in sorted(rs, key=lambda r: r["ts"]):
        if key not in r["real"] or r["ts"] < busy_until:
            continue
        taken.append(r)
        busy_until = r["real"][key]["exit_ts"]
    return taken


def summary(rs, key, split):
    pls = [r["real"][key]["pl"] for r in rs]
    tr = [r["real"][key]["r"] for r in rs if r["ts"] < split]
    te = [r["real"][key]["r"] for r in rs if r["ts"] >= split]
    mon = {}
    for r in rs:
        m = dt.datetime.fromtimestamp(r["ts"], ET).strftime("%Y-%m")
        mon[m] = mon.get(m, 0) + r["real"][key]["pl"]
    return {"n": len(rs), "win": r2(sum(1 for p in pls if p > 0) / max(len(pls), 1)), "avg_r": r2(avg([r["real"][key]["r"] for r in rs]), 3),
            "first": r2(avg(tr), 3), "unseen": r2(avg(te), 3), "n_unseen": len(te), "usd": round(sum(pls)),
            "usd_unseen": round(sum(r["real"][key]["pl"] for r in rs if r["ts"] >= split)),
            "months_up": sum(1 for v in mon.values() if v > 0), "months": len(mon), "worst_drop": round(oi.max_dd(pls))}


def playbook(rows, split, key, max_rules=4):
    """Greedy: on the first 2/3 only, add the skip-rule that most improves the average result (one trade at a time)."""
    train = [r for r in rows if r["ts"] < split and key in r["real"]]
    rules, keep = [], train
    candidates = sorted({(f, r["tags"][f]) for r in train for f in r["tags"]})
    for _ in range(max_rules):
        cur = avg([r["real"][key]["r"] for r in one_at_a_time(keep, key)])
        best = None
        for f, v in candidates:
            if (f, v) in rules:
                continue
            k2 = [r for r in keep if r["tags"][f] != v]
            taken = one_at_a_time(k2, key)
            if len(taken) < 80:
                continue
            a = avg([r["real"][key]["r"] for r in taken])
            if best is None or a > best[0]:
                best = (a, f, v)
        if not best or best[0] < cur + 0.03:
            break
        rules.append((best[1], best[2]))
        keep = [r for r in keep if r["tags"][best[1]] != best[2]]
    filt = lambda r: all(r["tags"][f] != v for f, v in rules)
    return rules, filt


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
    spy15 = oi.fetch_bars(api, "SPY", "15Min", start - dt.timedelta(days=5))
    tlt15 = oi.fetch_bars(api, "TLT", "15Min", start - dt.timedelta(days=5))
    iwm_d = daily_bars(api, SYM, start - dt.timedelta(days=120))
    spy_d = daily_bars(api, "SPY", start - dt.timedelta(days=10))
    tlt_d = daily_bars(api, "TLT", start - dt.timedelta(days=10))
    vix = vix_history()
    nw = news(api, start - dt.timedelta(days=2), now)
    log.append(f"Data: {len(m15)} IWM 15-min bars, {len(iwm_d)} days, {len(nw)} headlines, VIX {len(vix)} days")
    print(log[-1], flush=True)

    # event days found from the headlines themselves (8:00–9:00 AM releases)
    jobs_days, cpi_days = set(), set()
    for ts, h in nw:
        e = dt.datetime.fromtimestamp(ts, ET)
        if not (8 <= e.hour < 10):
            continue
        if ("nonfarm" in h or "payrolls" in h or "jobs report" in h) and e.weekday() == 4:
            jobs_days.add(e.date())
        if ("cpi" in h or "consumer price index" in h) and ("rose" in h or "rises" in h or "y/y" in h or "m/m" in h or "vs" in h or "est" in h):
            cpi_days.add(e.date())
    ddays = sorted(iwm_d)
    prev_day = {b: a for a, b in zip(ddays, ddays[1:])}
    closes, sma50 = [], {}
    for d in ddays:
        closes.append(iwm_d[d]["c"])
        if len(closes) >= 50:
            sma50[d] = sum(closes[-50:]) / 50
    ctx = {"iwm_days": by_day(m15), "spy_days": by_day(spy15), "tlt_days": by_day(tlt15), "iwm_d": iwm_d,
           "prev_close": {d: iwm_d[prev_day[d]]["c"] for d in prev_day}, "prev_day": prev_day, "vix": vix, "sma50": sma50,
           "news": nw, "news_ts": [x[0] for x in nw], "jobs_days": jobs_days, "cpi_days": cpi_days}

    F = facts(iwm_d, spy_d, tlt_d, {d: b for d, b in ctx["iwm_days"].items() if d >= start.date()},
              {"Fed decision days": {dt.date.fromisoformat(x) for x in FOMC}, "Jobs report days": jobs_days, "CPI days": cpi_days,
               "Monthly options expiration": {d for d in ddays if d.weekday() == 4 and 15 <= d.day <= 21}})
    F["jobs_days_found"], F["cpi_days_found"] = len(jobs_days), len(cpi_days)
    print(json.dumps(F)[:1500], flush=True)

    setups = oi.replay(SYM, m15, h1, start.timestamp(), log)
    close_at = {b["ts"] + 900: b for b in m15}
    rp = L.RealPrices(api)
    rows, missing = [], 0
    for s in setups:
        if not s.get("contract"):
            continue
        b = close_at.get(s["ts"])
        ct, _ = oi.pick(SYM, s["dir"], b["c"], s["iv"], dt.datetime.fromtimestamp(s["ts"], UTC)) if b else (None, None)
        if not ct:
            continue
        ro = real_outcomes(rp, ct, s["ts"])
        if not ro or MAIN not in ro:
            missing += 1
            continue
        rows.append({"ts": s["ts"], "t": s["t"], "real": ro, "tags": tag(s, ctx), "setup": s["setup"]})
        if len(rows) % 200 == 0:
            print(f"{len(rows)} setups priced ({rp.calls} calls, {(time.time() - t0) / 60:.1f} min)", flush=True)
    log.append(f"{len(rows)} IWM setups priced on real option bars ({missing} had no real prices)")
    rows.sort(key=lambda r: r["ts"])
    split = rows[0]["ts"] + (rows[-1]["ts"] - rows[0]["ts"]) * 2 / 3
    split_label = dt.datetime.fromtimestamp(split, ET).strftime("%b %d, %Y")

    exits = {k: summary(one_at_a_time([r for r in rows if r["tags"]["Council"] == "approved"], k), k, split) for k in RULES}
    L_main = lessons(rows, split, MAIN)
    rules, filt = playbook(rows, split, MAIN)
    base_live = one_at_a_time([r for r in rows if r["tags"]["Council"] == "approved"], MAIN)
    pb = one_at_a_time([r for r in rows if filt(r)], MAIN)
    out = {"when": t0, "split": split_label, "facts": F, "exits": exits, "lessons": L_main,
           "playbook": {"rules": rules, "result": summary(pb, MAIN, split), "baseline_live_bot": summary(base_live, MAIN, split),
                        "result_by_stop": {k: summary(one_at_a_time([r for r in rows if filt(r)], k), k, split) for k in RULES}},
           "log": log, "calls": rp.calls}
    log.append(f"Finished in {(time.time() - t0) / 60:.1f} min, {rp.calls} option price calls")
    json.dump(out, open("iwm_study.json", "w"), indent=1, default=str)
    print(json.dumps({k: out[k] for k in ("exits", "playbook")}, indent=1, default=str))
    print("\n".join(log))


if __name__ == "__main__":
    main()
