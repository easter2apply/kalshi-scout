"""
Inspector for Vault Options — replays months of real stock prices through the options council.

What it does
  1. downloads 15-minute and 1-hour stock bars from Alpaca (same keys as the bot)
  2. walks forward candle by candle, finding the same setups the live bot would see
  3. asks the same council functions to vote (The Reporter abstains: there's no free news history)
  4. prices the option the bot would have bought with Black-Scholes, using each stock's own recent
     volatility, plus a bid/ask spread, and walks it to the exit with the bot's real rules
  5. grades: trades taken, win rate, R per trade, dollars on $250, each officer, each setup and stock,
     training months vs unseen months, and four exit plans side by side

Important: the option prices are a MODEL, not real historical quotes (those aren't free).
Real options usually cost more than this model when a big move is expected, so treat results as optimistic.

Run:  python options_inspector.py --months 6
Needs ALPACA_KEY_ID and ALPACA_SECRET_KEY (TELEGRAM_BOT_TOKEN optional for the summary).
"""

import argparse
import datetime as dt
import html
import json
import math
import os
import sys
import time

import vault_options as vo

UTC = dt.timezone.utc
ET = vo.LOCAL_TZ
WINDOW_15 = 360              # the live bot looks at ~14 days of 15-minute bars
WINDOW_H1 = 300
RATE = 0.04                  # interest rate for option pricing
IV_FLOOR = 0.15
IV_MULT = 1.15               # options usually price a bit more movement than recently happened
MIN_N = 30                   # don't call an officer helpful or harmful on fewer setups than this
EDGE = 0.10                  # an officer "helps" if his YES setups beat his NO setups by 0.10R
BUDGET = vo.START_BALANCE * vo.RISK_PCT / vo.OPT_STOP

PLANS = {
    "now": {"label": "Current rules (hold weeklies + take profit early)", "hold": True, "partial": True},
    "day_plain": {"label": "Old rules (sell everything by 3:40, fixed +60% target)", "hold": False, "partial": False},
    "hold_plain": {"label": "Hold weeklies, fixed +60% target", "hold": True, "partial": False},
    "day_partial": {"label": "Sell by 3:40 daily, take profit early", "hold": False, "partial": True},
}


# Extra exit plans the lab tries (all sell by 3:40 PM the same day; weeklies held overnight did worst)
LAB_EXITS = {
    "s30_t60_p": {"label": "stop −30%, target +60%, take profit early", "hold": False, "partial": True, "stop": 0.30},
    "s50_t60_p": {"label": "stop −50%, target +60%, take profit early", "hold": False, "partial": True, "stop": 0.50},
    "s40_t100": {"label": "stop −40%, target +100%", "hold": False, "partial": False, "target": 1.00},
    "s40_t40": {"label": "stop −40%, target +40%", "hold": False, "partial": False, "target": 0.40},
    "s30_t45": {"label": "stop −30%, target +45%", "hold": False, "partial": False, "stop": 0.30, "target": 0.45},
    "s25_t50": {"label": "stop −25%, target +50%", "hold": False, "partial": False, "stop": 0.25, "target": 0.50},
}
ALL_EXITS = {**PLANS, **LAB_EXITS}
RISK_USD = vo.START_BALANCE * vo.RISK_PCT          # $25: results below are in units of this

# ───────────────────────────── data ──────────────────────────────────────────

def fetch_bars(api, sym, timeframe, start):
    """All regular-hours bars for one symbol since `start` (UTC datetime)."""
    out, token = [], None
    step = {"15Min": 900, "1Hour": 3600}[timeframe]
    for _ in range(200):
        p = {"symbols": sym, "timeframe": timeframe, "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": 10000,
             "feed": vo.STOCK_FEED, "adjustment": "split", "sort": "asc"}
        if token:
            p["page_token"] = token
        res = api.d("/v2/stocks/bars", p)
        out.extend((res.get("bars") or {}).get(sym, []))
        token = res.get("next_page_token")
        if not token:
            break
    now = time.time()
    bars = []
    for b in out:
        t0 = dt.datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
        if t0.timestamp() + step > now:
            continue
        e = t0.astimezone(ET)
        if not ((9, 30) <= (e.hour, e.minute) < (16, 0)):
            continue
        bars.append({"t": t0.strftime("%Y-%m-%dT%H:%M:%S"), "ts": int(t0.timestamp()), "o": float(b["o"]), "h": float(b["h"]),
                     "l": float(b["l"]), "c": float(b["c"]), "v": float(b.get("v", 0))})
    return bars


# ───────────────────────────── option model ──────────────────────────────────

def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(S, K, T, iv, kind):
    """(price, delta) of a call or put."""
    if T <= 1e-6:
        intrinsic = max(0.0, S - K) if kind == "call" else max(0.0, K - S)
        return intrinsic, (1.0 if (kind == "call" and S > K) else -1.0 if (kind == "put" and S < K) else 0.0)
    sq = iv * math.sqrt(T)
    d1 = (math.log(S / K) + (RATE + iv * iv / 2) * T) / sq
    d2 = d1 - sq
    if kind == "call":
        return S * ncdf(d1) - K * math.exp(-RATE * T) * ncdf(d2), ncdf(d1)
    return K * math.exp(-RATE * T) * ncdf(-d2) - S * ncdf(-d1), ncdf(d1) - 1


def half_spread(mid, etf):
    return max(0.01, mid * (0.02 if etf else 0.035))


def years_left(exp_date, at_utc):
    close = dt.datetime(exp_date.year, exp_date.month, exp_date.day, 16, 0, tzinfo=ET)
    return max(0.0, (close - at_utc.astimezone(ET)).total_seconds()) / (365 * 86400)


def strike_step(price, etf):
    if etf or price >= 25:
        return 1.0 if (etf or price < 250) else 5.0
    return 0.5


def expiry_for(sym, e):
    """Same window as the live bot: 0DTE for SPY/QQQ/IWM before 1:30 PM, otherwise the nearest weekly 2–10 days out."""
    if sym in vo.ZERO_DTE and (e.hour, e.minute) < vo.ZERO_DTE_UNTIL:
        return e.date(), True
    d = e.date() + dt.timedelta(days=vo.WEEKLY_DAYS[0])
    while d.weekday() >= 5 or (sym not in vo.ETFS and d.weekday() != 4):
        d += dt.timedelta(days=1)                       # stocks: Friday weeklies; ETFs: any weekday
    return d, False


def pick(sym, d, S, iv, at_utc):
    """The contract the live bot would pick: highest delta inside the range that fits the budget."""
    e = at_utc.astimezone(ET)
    exp, zero = expiry_for(sym, e)
    T = years_left(exp, at_utc)
    kind = "call" if d == 1 else "put"
    etf = sym in vo.ETFS
    step = strike_step(S, etf)
    atm = round(S / step) * step
    best, cheapest = None, None
    for k in range(-3, 40):
        K = atm + d * k * step                          # walk out of the money
        if K <= 0:
            break
        mid, delta = bs(S, K, T, iv, kind)
        if abs(delta) < vo.DELTA_RANGE[0]:
            break
        if abs(delta) > vo.DELTA_RANGE[1] or mid <= 0:
            continue
        hs = half_spread(mid, etf)
        if 2 * hs / mid > vo.MAX_SPREAD_PCT:
            continue
        ask = round(mid + hs, 2)
        cost = ask * 100
        cheapest = cost if cheapest is None else min(cheapest, cost)
        if cost <= BUDGET:
            best = {"K": K, "exp": exp, "zero": zero, "kind": kind, "ask": ask, "delta": abs(delta), "etf": etf}
            break                                       # first one that fits is the closest to the money
    if not best:
        return None, (f"cheapest option ${cheapest:,.0f} over the ${BUDGET:,.0f} budget" if cheapest else "no contract in range")
    best["qty"] = max(1, min(vo.MAX_CONTRACTS, int(BUDGET // (best["ask"] * 100))))
    return best, None


def bid_at(c, S, at_utc, iv):
    mid, _ = bs(S, c["K"], years_left(c["exp"], at_utc), iv, c["kind"])
    return max(0.0, mid - half_spread(mid, c["etf"])) if mid > 0 else 0.0


def last_sell_day(exp):
    d = exp - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def walk(c, m15, j, iv, plan):
    """Follow one option trade bar by bar with the bot's exit rules. Returns (pl $, R, how, exit index)."""
    entry, qty0 = c["ask"], c["qty"]
    qty, banked = qty0, 0.0
    stop_pct, tgt_pct = plan.get("stop", vo.OPT_STOP), plan.get("target", vo.OPT_TARGET)
    sl, tp = entry * (1 - stop_pct), entry * (1 + tgt_pct)
    partial = breakeven = trail = False
    peak = entry
    risk = entry * stop_pct * 100 * qty0
    d = 1 if c["kind"] == "call" else -1
    if c["zero"]:
        sell_day, sell_hm = c["exp"], (15, 30)
    elif plan["hold"]:
        sell_day, sell_hm = last_sell_day(c["exp"]), vo.FLATTEN_AT
    else:
        sell_day, sell_hm = dt.datetime.fromtimestamp(m15[j]["ts"], ET).date(), vo.FLATTEN_AT

    def done(px, how, k):
        pl = (px - entry) * 100 * qty + banked
        return round(pl, 2), round(pl / risk, 3), how, k

    for k in range(j + 1, len(m15)):
        b = m15[k]
        t_end = dt.datetime.fromtimestamp(b["ts"] + 900, UTC)
        e = t_end.astimezone(ET)
        if e.date() > sell_day:                              # data gap past the sell day
            return done(bid_at(c, m15[k - 1]["c"], t_end, iv), "end of day", k)
        worst = bid_at(c, b["l"] if d == 1 else b["h"], t_end, iv)
        best = bid_at(c, b["h"] if d == 1 else b["l"], t_end, iv)
        opn = bid_at(c, b["o"], t_end, iv)
        if trail:
            sl = max(sl, peak * (1 - vo.TRAIL_GIVEBACK))
        if worst <= sl:                                      # stop first when both happen in one candle
            how = "trailing stop" if trail and sl > entry else "breakeven" if breakeven else "stop"
            return done(min(sl, opn) if opn < sl else sl, how, k)
        if best >= tp:
            return done(tp, "target", k)
        if plan["partial"] and not partial and best >= entry * (1 + vo.PARTIAL_AT):
            partial = trail = breakeven = True
            px = entry * (1 + vo.PARTIAL_AT)
            tp = entry * (1 + vo.RUNNER_TARGET)
            if qty >= 2:
                n = qty // 2
                banked += (px - entry) * 100 * n
                qty -= n
                sl = max(sl, entry)
            else:
                sl = max(sl, entry * (1 + vo.LOCK_AT))
        elif not breakeven and best >= entry * (1 + vo.BREAKEVEN_AT):
            breakeven = True
            sl = max(sl, entry)
        peak = max(peak, best)
        if e.date() == sell_day and (e.hour, e.minute) >= sell_hm:
            return done(bid_at(c, b["c"], t_end, iv), "end of day", k)
    return None                                              # still open when the data ends


# ───────────────────────────── replay ────────────────────────────────────────

def realized_vol(m15, j):
    """Annualized volatility from the last ~20 trading days of 15-minute closes."""
    xs = [m15[i]["c"] for i in range(max(1, j - 26 * 20), j + 1)]
    rets = [math.log(xs[i] / xs[i - 1]) for i in range(1, len(xs)) if xs[i - 1] > 0]
    if len(rets) < 50:
        return None
    mu = sum(rets) / len(rets)
    var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var * 26 * 252)


def replay(sym, m15, h1, test_from, log):
    setups = []
    i1 = 0
    lb_done = set()
    t0 = time.time()
    for j, c in enumerate(m15):
        close_ts = c["ts"] + 900
        while i1 < len(h1) and h1[i1]["ts"] + 3600 <= close_ts:
            i1 += 1
        if close_ts < test_from or j < 200 or i1 < 210:
            continue
        now = dt.datetime.fromtimestamp(close_ts, UTC)
        if not vo.in_session(sym, now):
            continue
        mw = m15[max(0, j - WINDOW_15 + 1):j + 1]
        a = vo.analyze(mw, h1[max(0, i1 - WINDOW_H1):i1])
        a.update(htf_label="1-hour trend", spread_max=vo.MAX_SPREAD_ATR_15M)
        decisions = []
        if a["setup"] and a["atr"]:
            a["setup"] += " 15m"
            decisions.append(("M15", a))
        day = now.astimezone(ET).date()
        if day not in lb_done:
            orb = vo.opening_range(sym, mw, a, now)
            if orb:
                lb_done.add(day)
                decisions.append(("ORB", orb))
        if not decisions:
            continue
        rv = realized_vol(m15, j)
        if not rv:
            continue
        iv = max(IV_FLOOR, rv * IV_MULT)
        for tf, s in decisions:
            d = s["trend"]
            ct, why_not = pick(sym, d, c["c"], iv, now)
            ok_risk = ct is not None
            votes = (vo.council_orb(s, ok_risk, why_not or "", now, sym) if tf == "ORB"
                     else vo.council(s, 0, ok_risk, why_not or "", now, sym))
            votes["The Reporter"] = (None, "no news history to replay")
            plain = {n: o for n, (o, _) in votes.items()}
            rec = {"sym": sym, "t": c["t"], "ts": close_ts, "setup": s["setup"], "dir": d, "votes": plain,
                   "approved": vo.approved(votes) and ok_risk, "contract": bool(ct), "hour": now.astimezone(ET).hour,
                   "iv": round(iv, 3)}
            if ct:
                rec.update(zero=ct["zero"], ask=ct["ask"], qty=ct["qty"], delta=round(ct["delta"], 2))
                rec["x"] = {}
                for key, plan in ALL_EXITS.items():
                    res = walk(ct, m15, j, iv, plan)
                    if res:
                        rec["x"][key] = {"pl": res[0], "r": res[1], "how": res[2], "exit_ts": m15[res[3]]["ts"] + 900}
                if "now" not in rec["x"]:
                    continue                                 # still open when the data ends
            setups.append(rec)
    log.append(f"{sym}: {len(setups)} setups, {sum(1 for s in setups if s['approved'])} approved ({time.time() - t0:.0f}s)")
    return setups


def take_trades(setups, key, keep=None, council=True):
    """Apply the bot's limits in time order: one trade per stock, 2 at once, daily loss limit."""
    open_until, taken, day_loss = {}, [], {}
    for s in sorted(setups, key=lambda s: s["ts"]):
        if (council and not s["approved"]) or key not in s.get("x", {}) or (keep and not keep(s)):
            continue
        open_until = {k: v for k, v in open_until.items() if v > s["ts"]}
        if s["sym"] in open_until or len(open_until) >= vo.MAX_OPEN_TRADES:
            continue
        day = dt.datetime.fromtimestamp(s["ts"], ET).date()
        if day_loss.get(day, 0) <= -vo.DAILY_LOSS_LIMIT * vo.START_BALANCE:
            continue
        x = s["x"][key]
        open_until[s["sym"]] = x["exit_ts"]
        taken.append({**s, "pl": x["pl"], "r": x["r"], "how": x["how"]})
        day_loss[day] = day_loss.get(day, 0) + min(0, x["pl"])
    return taken


# ───────────────────────────── grading ───────────────────────────────────────

def stats(rs):
    n = len(rs)
    if not n:
        return {"n": 0, "win": None, "avg": None, "total": 0.0}
    return {"n": n, "win": sum(1 for r in rs if r > 0) / n, "avg": sum(rs) / n, "total": sum(rs)}


def max_dd(pls):
    peak = cur = dd = 0.0
    for p in pls:
        cur += p
        peak = max(peak, cur)
        dd = max(dd, peak - cur)
    return dd


def grade(setups, months):
    g = {"months": months, "budget": BUDGET}
    priced = [s for s in setups if s["contract"]]
    g["setups"] = len(setups)
    g["no_contract"] = len(setups) - len(priced)
    g["approved"] = sum(1 for s in setups if s["approved"])
    plans = {}
    for key, plan in PLANS.items():
        tk = take_trades(setups, key)
        pls = [t["pl"] for t in tk]
        plans[key] = {"label": plan["label"], "r": stats([t["r"] for t in tk]), "dollars": round(sum(pls), 2),
                      "max_dd": round(max_dd(pls), 2), "per_month": len(tk) / max(months, 1e-9)}
    g["plans"] = plans
    taken = take_trades(setups, "now")
    g["taken"] = [{k: t[k] for k in ("sym", "t", "setup", "dir", "pl", "r", "how", "zero", "ask", "qty")} for t in taken[-40:]]
    # train vs unseen (first 2/3 vs last 1/3 of the period)
    if taken:
        t0, t1 = min(s["ts"] for s in setups), max(s["ts"] for s in setups)
        split = t0 + (t1 - t0) * 2 / 3
        g["split_label"] = dt.datetime.fromtimestamp(split, ET).strftime("%b %d")
        g["first"] = stats([t["r"] for t in taken if t["ts"] < split])
        g["last"] = stats([t["r"] for t in taken if t["ts"] >= split])
    # officers: setups where he said YES vs NO (plan "now", every priced setup, as if taken)
    off = {}
    for n in vo.VOTERS:
        yes = [s["x"]["now"]["r"] for s in priced if s["votes"].get(n) is True]
        no = [s["x"]["now"]["r"] for s in priced if s["votes"].get(n) is False]
        sy, sn = stats(yes), stats(no)
        diff = (sy["avg"] - sn["avg"]) if sy["n"] and sn["n"] else None
        if n == "The Reporter":
            verdict = "can't replay"
        elif diff is None or min(sy["n"], sn["n"]) < MIN_N:
            verdict = "not enough data"
        else:
            verdict = "helps" if diff >= EDGE else "hurts" if diff <= -EDGE else "no real effect"
        off[n] = {"yes": sy, "no": sn, "diff": diff, "verdict": verdict}
    g["officers"] = off
    g["take_all"] = stats([s["x"]["now"]["r"] for s in priced])
    g["council"] = stats([s["x"]["now"]["r"] for s in priced if s["approved"]])
    g["rejected"] = stats([s["x"]["now"]["r"] for s in priced if not s["approved"]])
    groups = {}
    for key in ("setup", "sym"):
        gg = {}
        for t in taken:
            gg.setdefault(t[key], []).append(t["r"])
        groups[key] = {k: stats(v) for k, v in gg.items()}
    zz = {}
    for t in taken:
        zz.setdefault("same-day (0DTE)" if t["zero"] else "weeklies", []).append(t["r"])
    groups["kind"] = {k: stats(v) for k, v in zz.items()}
    hw = {}
    for t in taken:
        hw.setdefault(t["how"], []).append(t["r"])
    groups["exit"] = {k: stats(v) for k, v in hw.items()}
    g["groups"] = groups
    mon = {}
    for t in taken:
        mon.setdefault(t["t"][:7], []).append(t["pl"])
    g["monthly"] = {k: round(sum(v), 2) for k, v in sorted(mon.items())}
    no_ct = {}
    for s in setups:
        if not s["contract"]:
            no_ct[s["sym"]] = no_ct.get(s["sym"], 0) + 1
    g["no_contract_by_sym"] = no_ct
    g["advice"] = advice(g)
    return g


def grade_with_lab(setups, months, log):
    g = grade(setups, months)
    g["lab"] = lab(setups, log)
    return g


# ───────────────────────────── the lab ───────────────────────────────────────

LAB_SYMS = {"whole watchlist": None, "SPY/QQQ/IWM only": set(vo.ETFS), "watchlist minus SOFI": set(vo.PAIRS) - {"SOFI"}}
LAB_SETUPS = {"all setups": None, "no 15-min crossovers": {"pullback 15m", "opening breakout"},
              "opening breakout only": {"opening breakout"}, "pullbacks only": {"pullback 15m"}}
LAB_KIND = {"0DTE or weekly": None, "0DTE only": True, "weeklies only": False}
LAB_HOURS = {"all day": None, "10 AM–noon": (10, 12), "10 AM–2 PM": (10, 14), "noon–3 PM": (12, 15)}
LAB_COUNCIL = {"council approves": True, "every setup": False}


def _rs(tk):
    return [t["pl"] / RISK_USD for t in tk]


def lab(setups, log):
    priced = sorted([s for s in setups if s.get("x")], key=lambda s: s["ts"])
    if len(priced) < 300:
        return None
    t0, t1 = priced[0]["ts"], priced[-1]["ts"]
    split = t0 + (t1 - t0) * 2 / 3
    rows = []
    for sy_l, syms in LAB_SYMS.items():
        for se_l, sets in LAB_SETUPS.items():
            for k_l, zero in LAB_KIND.items():
                for h_l, hrs in LAB_HOURS.items():
                    def keep(s, syms=syms, sets=sets, zero=zero, hrs=hrs):
                        return ((syms is None or s["sym"] in syms) and (sets is None or s["setup"] in sets)
                                and (zero is None or s.get("zero") == zero) and (hrs is None or hrs[0] <= s["hour"] < hrs[1]))
                    pool = [s for s in priced if keep(s)]
                    if len(pool) < 60:
                        continue
                    for c_l, council in LAB_COUNCIL.items():
                        for ex in ALL_EXITS:
                            tk = take_trades(pool, ex, council=council)
                            tr = [t for t in tk if t["ts"] < split]
                            te = [t for t in tk if t["ts"] >= split]
                            if len(tr) < 60 or len(te) < 25:
                                continue
                            rows.append({"rule": {"symbols": sy_l, "setups": se_l, "contracts": k_l, "hours": h_l, "council": c_l,
                                                  "exit": ALL_EXITS[ex]["label"]}, "key": (sy_l, se_l, k_l, h_l, c_l, ex),
                                         "train": stats(_rs(tr)), "test": stats(_rs(te)), "all": stats(_rs(tk))})
    log.append(f"Lab: tried {len(rows)} versions")
    if not rows:
        return None
    rows.sort(key=lambda r: -r["train"]["avg"])
    top = rows[:15]
    held = [r for r in top if r["test"]["avg"] > 0.05 and r["train"]["avg"] > 0.05]
    out = {"tried": len(rows), "split": dt.datetime.fromtimestamp(split, ET).strftime("%b %d, %Y"),
           "top": [{k: v for k, v in r.items() if k != "key"} for r in top], "held": len(held),
           "positive_test": sum(1 for r in rows if r["test"]["avg"] > 0), "best_test_any": max(r["test"]["avg"] for r in rows)}
    # the proposal: same-day only, no crossovers, SPY/QQQ/IWM only, council, take profit early
    prop = next((r for r in rows if r["key"] == ("SPY/QQQ/IWM only", "no 15-min crossovers", "0DTE or weekly", "all day",
                                                 "council approves", "day_partial")), None)
    out["proposal"] = {k: v for k, v in prop.items() if k != "key"} if prop else None
    # stress test the best training rule: monthly consistency, drawdown, and worse fills
    champ = top[0]
    sy_l, se_l, k_l, h_l, c_l, ex = champ["key"]
    syms, sets, zero, hrs = LAB_SYMS[sy_l], LAB_SETUPS[se_l], LAB_KIND[k_l], LAB_HOURS[h_l]
    tk = take_trades([s for s in priced if (syms is None or s["sym"] in syms) and (sets is None or s["setup"] in sets)
                      and (zero is None or s.get("zero") == zero) and (hrs is None or hrs[0] <= s["hour"] < hrs[1])],
                     ex, council=LAB_COUNCIL[c_l])
    mon = {}
    for t in tk:
        mon.setdefault(t["t"][:7], 0.0)
        mon[t["t"][:7]] += t["pl"]
    worse = [(t["pl"] - t.get("qty", 1) * 100 * 0.04) / RISK_USD for t in tk]     # 2¢ worse on the way in and out
    out["champ"] = {"rule": champ["rule"], "months_up": sum(1 for v in mon.values() if v > 0), "months": len(mon),
                    "max_dd": round(max_dd([t["pl"] for t in tk]), 2), "dollars": round(sum(t["pl"] for t in tk), 2),
                    "worse_fills": stats(worse), "train": champ["train"], "test": champ["test"]}
    return out


def lab_text(L):
    if not L:
        return []
    c = L["champ"]
    rule = lambda r: " · ".join(r[k] for k in ("symbols", "setups", "contracts", "hours", "council", "exit"))
    lines = ["", f"🧪 Options lab: tried {L['tried']} versions (tuned before {L['split']}, checked after)",
             f"Best on the tuning months: {rule(c['rule'])}",
             f"   tuning {c['train']['avg']:+.2f}R ({c['train']['n']} trades) → unseen {c['test']['avg']:+.2f}R ({c['test']['n']} trades)",
             f"   {c['months_up']} of {c['months']} months up · worst drawdown ${c['max_dd']:,.0f} · with 2¢ worse fills {c['worse_fills']['avg']:+.2f}R",
             f"{L['held']} of the top 15 stayed above +0.05R on the unseen months; {L['positive_test']} of {L['tried']} were positive there at all."]
    p = L.get("proposal")
    if p:
        lines.append(f"Proposed changes (same-day sells, no crossovers, SPY/QQQ/IWM): tuning {p['train']['avg']:+.2f}R → unseen {p['test']['avg']:+.2f}R "
                     f"({p['all']['n']} trades)")
    return lines


def advice(g):
    out = []
    p = g["plans"]["now"]
    r = p["r"]
    if r["n"] < 20:
        out.append(f"Only {r['n']} trades in the replay. Too few to judge; run more months.")
    elif (r["avg"] or 0) > 0.1 and g.get("last", {}).get("avg") is not None and g["last"]["avg"] > 0:
        out.append(f"The options council made money in the replay ({r['avg']:+.2f}R a trade over {r['n']} trades) "
                   f"and stayed positive on the unseen months. Keep paper trading it and compare.")
    elif (r["avg"] or 0) > 0:
        out.append(f"Slightly positive ({r['avg']:+.2f}R a trade over {r['n']}), but not convincingly. Keep it on paper.")
    else:
        out.append(f"The options council lost money in the replay ({r['avg']:+.2f}R a trade over {r['n']} trades). "
                   "Don't put real money on it as-is.")
    best = max(g["plans"].items(), key=lambda kv: (kv[1]["r"]["avg"] if kv[1]["r"]["avg"] is not None else -9))
    if best[0] != "now" and best[1]["r"]["avg"] is not None and r["avg"] is not None and best[1]["r"]["avg"] - r["avg"] >= 0.05:
        out.append(f"\"{best[1]['label']}\" did better than the current rules ({best[1]['r']['avg']:+.2f}R vs {r['avg']:+.2f}R).")
    c, rj = g["council"], g["rejected"]
    if c["avg"] is not None and rj["avg"] is not None:
        out.append(f"The council {'is' if c['avg'] > rj['avg'] else 'is NOT'} picking better setups than it skips "
                   f"({c['avg']:+.2f}R approved vs {rj['avg']:+.2f}R rejected).")
    for n, o in g["officers"].items():
        if o["verdict"] == "hurts":
            out.append(f"{n} hurts: his YES setups did {abs(o['diff']):.2f}R worse than his NO setups.")
    if g["setups"] and g["no_contract"] / g["setups"] > 0.3:
        worst = sorted(g["no_contract_by_sym"].items(), key=lambda kv: -kv[1])[:3]
        out.append(f"{g['no_contract'] / g['setups']:.0%} of setups had no option inside the ${g['budget']:.0f} budget "
                   f"(mostly {', '.join(k for k, _ in worst)}).")
    return out


# ───────────────────────────── report ────────────────────────────────────────

def fr(x):
    return "—" if x is None else f"{x:+.2f}R"


def pc(x):
    return "—" if x is None else f"{x:.0%}"


def money(x):
    return f"{'+' if x >= 0 else '−'}${abs(x):,.2f}"


def render(g, log, started):
    p = g["plans"]["now"]
    r = p["r"]
    cls = "good" if (r["avg"] or 0) > 0.1 and r["n"] >= 30 else "bad" if (r["avg"] or 0) < 0 else "wait"
    plan_rows = "".join(
        f"<tr{' class=base' if k == 'now' else ''}><td>{html.escape(v['label'])}</td><td class=n>{v['r']['n']}</td>"
        f"<td class=n>{pc(v['r']['win'])}</td><td class='n {'pos' if (v['r']['avg'] or 0) > 0 else 'neg'}'>{fr(v['r']['avg'])}</td>"
        f"<td class=n>{money(v['dollars'])}</td><td class=n>${v['max_dd']:,.0f}</td></tr>" for k, v in g["plans"].items())
    badge = {"helps": "good", "hurts": "bad"}
    off_rows = "".join(
        f"<tr><td class=nm>{n}<small>{html.escape(vo.ROLES.get(n, ''))}</small></td>"
        f"<td><span class='pill {badge.get(o['verdict'], 'wait')}'>{o['verdict']}</span></td>"
        f"<td class=n>{fr(o['yes']['avg'])}<small>{o['yes']['n']}</small></td><td class=n>{fr(o['no']['avg'])}<small>{o['no']['n']}</small></td>"
        f"<td class=n><b>{fr(o['diff'])}</b></td></tr>" for n, o in g["officers"].items())

    def grp(title, rows):
        body = "".join(f"<tr><td>{html.escape(k)}</td><td class=n>{s['n']}</td><td class=n>{pc(s['win'])}</td>"
                       f"<td class='n {'pos' if (s['avg'] or 0) > 0 else 'neg'}'>{fr(s['avg'])}</td></tr>"
                       for k, s in sorted(rows.items(), key=lambda kv: -(kv[1]['avg'] if kv[1]['avg'] is not None else -9)))
        return f"<h3>{title}</h3><div class=tbl><table><tr><th></th><th>Trades</th><th>Win</th><th>Avg</th></tr>{body}</table></div>"

    mx = max([abs(v) for v in g["monthly"].values()] + [1])
    bars = "".join(f"<div class=mb><span>{k[2:]}</span><i class='{'pos' if v >= 0 else 'neg'}' style='--w:{abs(v) / mx * 100:.0f}%'></i>"
                   f"<b>{money(v)}</b></div>" for k, v in g["monthly"].items())
    split = ""
    if g.get("first"):
        split = (f"<p>Training months (before {g['split_label']}): <b>{fr(g['first']['avg'])}</b> over {g['first']['n']} trades · "
                 f"Unseen months (after): <b>{fr(g['last']['avg'])}</b> over {g['last']['n']} trades</p>")
    tips = "".join(f"<li>{html.escape(x)}</li>" for x in g["advice"])
    trades = "".join(f"<tr><td>{t['t'][5:16].replace('T', ' ')}</td><td>{t['sym']} {'call' if t['dir'] == 1 else 'put'}"
                     f"<small>{html.escape(t['setup'])}{' · 0DTE' if t['zero'] else ''}</small></td><td class=n>${t['ask']:.2f} × {t['qty']}</td>"
                     f"<td class='n {'pos' if t['pl'] > 0 else 'neg'}'>{money(t['pl'])}<small>{html.escape(t['how'])}</small></td></tr>"
                     for t in reversed(g["taken"][-25:]))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Options Inspector</title><style>
:root{{--bg:#0f1220;--panel:#171b2e;--fg:#e8ecf8;--muted:#9aa3c0;--line:#2a3150;--good:#53f0a6;--bad:#ff6b7c;--wait:#f4c84a;--acc:#38e1ff}}
@media (prefers-color-scheme: light){{:root:not([data-theme="dark"]){{--bg:#f4f5fa;--panel:#fff;--fg:#151a2c;--muted:#5b6380;--line:#dde1ee;--good:#138a55;--bad:#c23345;--wait:#9a7100;--acc:#0b7fa0}}}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif;padding:20px 16px 40px}}
.wrap{{max-width:900px;margin:0 auto;display:grid;gap:18px}} header{{display:flex;gap:14px;align-items:center}}
.badge{{width:52px;height:52px;border-radius:14px;background:linear-gradient(135deg,#3b1f5f,#ff7ac8);display:grid;place-items:center;font-size:28px;flex:none}}
h1{{margin:0;font-size:24px}} header p{{margin:2px 0 0;color:var(--muted);font-size:13px}}
section{{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px}}
h2{{margin:0 0 10px;font-size:13px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}} h3{{margin:14px 0 6px;font-size:14px}}
.verdict{{font-size:18px;font-weight:700;margin:0}} .verdict.good{{color:var(--good)}} .verdict.bad{{color:var(--bad)}} .verdict.wait{{color:var(--wait)}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-top:12px}}
.kpi{{border:1px solid var(--line);border-radius:10px;padding:10px 12px}} .kpi span{{display:block;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}}
.kpi b{{font-size:22px;font-variant-numeric:tabular-nums}} .kpi small{{display:block;color:var(--muted);font-size:12px}}
.tbl{{overflow-x:auto}} table{{width:100%;border-collapse:collapse;font-size:14px}} th,td{{padding:8px 6px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}
th{{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}} td.n,th:not(:first-child){{text-align:right;font-variant-numeric:tabular-nums}}
td small{{display:block;color:var(--muted);font-size:11.5px}} td.nm{{font-weight:700}} tr.base td{{background:color-mix(in srgb,var(--acc) 8%,transparent)}}
.pill{{font-size:11.5px;font-weight:700;padding:3px 8px;border-radius:99px;border:1px solid currentColor;white-space:nowrap}}
.pill.good{{color:var(--good)}} .pill.bad{{color:var(--bad)}} .pill.wait{{color:var(--wait)}} .pos{{color:var(--good)}} .neg{{color:var(--bad)}}
ul.tips{{margin:0;padding-left:20px;display:grid;gap:8px}} .fine{{color:var(--muted);font-size:12.5px}}
.mb{{display:grid;grid-template-columns:44px 1fr 90px;gap:8px;align-items:center;font-size:13px;margin:4px 0}}
.mb i{{display:block;height:12px;border-radius:4px;width:var(--w);min-width:2px}} .mb i.pos{{background:var(--good)}} .mb i.neg{{background:var(--bad)}}
.mb b{{text-align:right;font-variant-numeric:tabular-nums}} .mb span{{color:var(--muted)}}
@media (max-width:560px){{th,td{{padding:7px 4px;font-size:13px}}}}
</style></head><body><div class="wrap">
<header><div class="badge" aria-hidden="true">🕵️</div><div><h1>Inspector: Vault Options</h1>
<p>Replayed {g['months']:g} months of real stock prices through the options council · {dt.datetime.fromtimestamp(started, ET):%a %b %d, %I:%M %p} ET</p></div></header>
<section><h2>Verdict</h2><p class="verdict {cls}">{html.escape(g['advice'][0])}</p>
<div class="kpis">
 <div class="kpi"><span>Trades</span><b>{r['n']}</b><small>about {p['per_month']:.0f} a month</small></div>
 <div class="kpi"><span>Win rate</span><b>{pc(r['win'])}</b><small>with partial profits</small></div>
 <div class="kpi"><span>Avg per trade</span><b class="{'pos' if (r['avg'] or 0) > 0 else 'neg'}">{fr(r['avg'])}</b><small>1R = the planned max loss</small></div>
 <div class="kpi"><span>On $250</span><b class="{'pos' if p['dollars'] >= 0 else 'neg'}">{money(p['dollars'])}</b><small>no compounding</small></div>
 <div class="kpi"><span>Worst drawdown</span><b>${p['max_dd']:,.0f}</b><small>from a high</small></div>
</div>{split}</section>
<section><h2>What to do</h2><ul class="tips">{tips}</ul></section>
<section><h2>Exit plans, same entries</h2><div class="tbl"><table><tr><th>Plan</th><th>Trades</th><th>Win</th><th>Avg</th><th>$ on $250</th><th>Worst dip</th></tr>{plan_rows}</table></div></section>
<section><h2>The officers</h2><p class="fine">How setups did when each officer said YES vs NO (every setup, as if taken). The Reporter can't be replayed: there's no free news history.</p>
<div class="tbl"><table><tr><th>Officer</th><th>Verdict</th><th>YES avg</th><th>NO avg</th><th>Difference</th></tr>{off_rows}</table></div>
<p class="fine">Council approved {fr(g['council']['avg'])} ({g['council']['n']}) vs rejected {fr(g['rejected']['avg'])} ({g['rejected']['n']}) · taking every setup {fr(g['take_all']['avg'])}</p></section>
<section><h2>Breakdown</h2>{grp('By setup', g['groups']['setup'])}{grp('By stock', g['groups']['sym'])}{grp('Same-day vs weeklies', g['groups']['kind'])}{grp('How trades ended', g['groups']['exit'])}</section>
<section><h2>Month by month</h2>{bars or '<p class=fine>No trades.</p>'}</section>
<section><h2>Latest replayed trades</h2><div class="tbl"><table><tr><th>When</th><th>Trade</th><th>Paid</th><th>Result</th></tr>{trades}</table></div></section>
<section><h2>How this works (and its limits)</h2><p class="fine">Real 15-minute and 1-hour stock bars from Alpaca ({vo.STOCK_FEED.upper()} feed) go through the bot's own setup finder and council.
Option prices are modeled with Black-Scholes using each stock's last 20 days of movement ×{IV_MULT}, plus a {2 * 2:.0f}–{2 * 3.5:.0f}% bid/ask spread, then walked to the exit with the bot's real rules
(stop −{vo.OPT_STOP:.0%}, target +{vo.OPT_TARGET:.0%}, early profit at +{vo.PARTIAL_AT:.0%}, trailing stop, 3:40 PM / day-before-expiry exits, 2 trades at once, ${BUDGET:.0f} per-trade budget).
Real options get pricier before earnings and big events, and real fills can be worse, so treat these numbers as a best case. {g['setups']} setups seen, {g['no_contract']} had no affordable contract.</p>
<details><summary class="fine">Run log</summary><ul class="fine">{''.join(f'<li>{html.escape(l)}</li>' for l in log)}</ul></details></section>
</div></body></html>"""


def telegram_summary(g):
    p = g["plans"]["now"]
    r = p["r"]
    lines = [f"🕵️ Inspector: Vault Options, {g['months']:g} months replayed", "",
             f"Trades: {r['n']} · win rate {pc(r['win'])} · {fr(r['avg'])} per trade",
             f"On $250: {money(p['dollars'])} · worst dip ${p['max_dd']:,.0f}"]
    if g.get("first"):
        lines.append(f"Training months {fr(g['first']['avg'])} → unseen months {fr(g['last']['avg'])}")
    lines += ["", "Exit plans (same entries):"]
    lines += [f"• {v['label']}: {fr(v['r']['avg'])}, {money(v['dollars'])}" for v in g["plans"].values()]
    icon = {"helps": "✅", "hurts": "❌", "no real effect": "➖", "not enough data": "❔", "can't replay": "📰"}
    lines += ["", "Officers:"] + [f"{icon[o['verdict']]} {n}: {o['verdict']} ({fr(o['diff'])})" for n, o in g["officers"].items()]
    lines += ["", "What to do:"] + [f"• {x}" for x in g["advice"][:4]]
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        o_, r_ = repo.split("/", 1)
        lines += ["", f"Full report (after the next dashboard update): https://{o_}.github.io/{r_}/inspector.html"]
    lines.append("Option prices are modeled, not real quotes, so this is a best case.")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Backtest Vault Options on Alpaca history.")
    ap.add_argument("--months", type=float, default=6)
    ap.add_argument("--symbols", default=",".join(vo.PAIRS))
    args = ap.parse_args()
    key, secret = os.environ.get("ALPACA_KEY_ID", "").strip(), os.environ.get("ALPACA_SECRET_KEY", "").strip()
    if not key or not secret:
        sys.exit("Add ALPACA_KEY_ID and ALPACA_SECRET_KEY.")
    api = vo.Alpaca(key, secret)
    started = time.time()
    now = dt.datetime.now(UTC)
    test_from = (now - dt.timedelta(days=args.months * 30.4)).timestamp()
    log, setups = [], []
    for sym in [s.strip().upper() for s in args.symbols.split(",") if s.strip()]:
        try:
            m15 = fetch_bars(api, sym, "15Min", now - dt.timedelta(days=args.months * 30.4 + 40))
            h1 = fetch_bars(api, sym, "1Hour", now - dt.timedelta(days=args.months * 30.4 + 75))
        except vo.ApiError as e:
            log.append(f"{sym}: skipped ({e})")
            continue
        log.append(f"{sym}: {len(m15)} 15-min bars ({m15[0]['t'][:10] if m15 else '?'} → {m15[-1]['t'][:10] if m15 else '?'}), {len(h1)} hourly")
        setups += replay(sym, m15, h1, test_from, log)
        print(log[-1], flush=True)
    if not setups:
        sys.exit("No setups found. Check the log above.")
    g = grade_with_lab(setups, args.months, log)
    log.append(f"Finished in {(time.time() - started) / 60:.1f} min")
    with open("options_inspector.html", "w", encoding="utf-8") as f:
        f.write(render(g, log, started))
    with open("options_inspector.json", "w", encoding="utf-8") as f:
        json.dump({"when": started, "grade": g}, f, indent=1, default=str)
    msg = telegram_summary(g) + "\n".join(lab_text(g.get("lab")))
    print(msg)
    tg = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not chat:
        try:
            chat = json.load(open(vo.STATE_FILE)).get("chat_id")
        except Exception:
            chat = None
    if tg and chat:
        try:
            vo.http("POST", f"https://api.telegram.org/bot{tg}/sendMessage",
                    body={"chat_id": chat, "text": msg, "disable_web_page_preview": True}, tries=2)
        except Exception as e:
            print(f"Telegram failed: {e}")


if __name__ == "__main__":
    main()
