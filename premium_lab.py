"""
Premium Lab: Inspector tests SELLING options premium (and buying only when options are cheap) on SPY and QQQ.

  A. Put credit spreads      sell a put, buy a cheaper put below it (max loss = width - credit)
  B. Iron condors            a put credit spread + a call credit spread
  C. Buy when cheap          buy a 45-day straddle only when implied volatility is in its bottom range

How prices are made
  • 2016 → today: option prices are rebuilt with Black-Scholes using the real implied-volatility index of each day
    (VIX / VIX9D for SPY, VXN for QQQ, from CBOE) plus a volatility "skew" (OTM puts cost more than OTM calls).
  • The skew and level are CALIBRATED against real Alpaca option prices (Feb 2024 → today), so the model is
    forced to match what those options really cost.
  • The best versions are then replayed on REAL option prices (Feb 2024 → today) as the final check.

Pick → check: versions are picked on 2016-2022 only, then judged on 2023 → today (unseen).
Costs: every leg loses a few cents to the bid/ask spread on the way in and on the way out.

Run:  python premium_lab.py
"""

import argparse
import bisect
import datetime as dt
import html
import itertools
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request

import numpy as np
from scipy.special import ndtr, ndtri

import vault_options as vo

UTC = dt.timezone.utc
ET = vo.LOCAL_TZ
SYMS = ["SPY", "QQQ"]
START = dt.date(2016, 1, 4)
SPLIT = dt.date(2023, 1, 1)               # pick on 2016-2022, judge on 2023 → today
REAL_FROM = dt.date(2024, 2, 5)           # Alpaca option history starts Feb 2024
DAILY_EXP_FROM = dt.date(2022, 11, 14)    # before this, SPY/QQQ 0DTE only existed Mon/Wed/Fri
SLIP = {("SPY", "0dte"): 0.02, ("SPY", "swing"): 0.04, ("QQQ", "0dte"): 0.03, ("QQQ", "swing"): 0.05}  # $/share/leg/side
RATES = {2016: .004, 2017: .01, 2018: .019, 2019: .021, 2020: .004, 2021: .0005, 2022: .02, 2023: .051, 2024: .052,
         2025: .043, 2026: .039}
CBOE = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{}_History.csv"

DTES = [0, 7, 30, 45]
DELTAS = [0.10, 0.16, 0.20, 0.30]
WIDTHS = {"0dte": [1, 2], "swing": [2, 5]}
TPS = [0.5, 0.75, None]
STOPS = [1.0, 2.0, None]                  # stop when the spread costs (1+k)× the credit → lose k× the credit
FILTERS = ["none", "vix15", "ivr30", "trend"]
STRADDLE_TP = [0.25, 0.5, 1.0]
STRADDLE_STOP = [0.4, 0.6, None]
STRADDLE_FILTER = ["always", "cheap30", "cheap20"]


def rate(d):
    return RATES.get(d.year, 0.04)


# ───────────────────────────── data ─────────────────────────────

def get(api, path, params, tries=8):
    for i in range(tries):
        try:
            return api.d(path, params)
        except vo.ApiError as e:
            if ("429" in str(e) or "50" in str(e)[:3]) and i < tries - 1:
                time.sleep(3 + 3 * i)
                continue
            raise


def stock_bars(api, sym, timeframe, start, end):
    out, token = [], None
    for feed in ("sip", "iex"):
        try:
            out, token = [], None
            while True:
                p = {"symbols": sym, "timeframe": timeframe, "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": 10000, "feed": feed, "adjustment": "raw",
                     "sort": "asc"}
                if token:
                    p["page_token"] = token
                j = get(api, "/v2/stocks/bars", p)
                out += (j.get("bars") or {}).get(sym, [])
                token = j.get("next_page_token")
                if not token:
                    return out, feed
        except vo.ApiError as e:
            print(f"{sym} {timeframe} on {feed}: {e}", flush=True)
    return out, "none"


def cboe(name):
    req = urllib.request.Request(CBOE.format(name), headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req, timeout=60).read().decode()
    out = {}
    for line in raw.splitlines()[1:]:
        p = line.strip().split(",")
        if len(p) < 5:
            continue
        try:
            m, d, y = p[0].split("/")
            out[dt.date(int(y), int(m), int(d))] = (float(p[1]), float(p[4]))
        except ValueError:
            continue
    return out


def load_data(api):
    end = dt.datetime.now(UTC) - dt.timedelta(hours=2)
    start = dt.datetime(START.year, START.month, START.day, tzinfo=UTC) - dt.timedelta(days=400)
    daily, intra, log = {}, {}, []
    for s in SYMS:
        rows, feed = stock_bars(api, s, "1Day", start, end)
        daily[s] = {}
        for b in rows:
            d = vo.et(b["t"]).date()
            daily[s][d] = {"o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]}
        rows, feed5 = stock_bars(api, s, "5Min", dt.datetime(START.year, START.month, START.day, tzinfo=UTC), end)
        intra[s] = {}
        for b in rows:
            e = vo.et(b["t"])
            mm = e.hour * 60 + e.minute
            if 570 <= mm < 960:
                intra[s].setdefault(e.date(), []).append((mm, b["o"], b["h"], b["l"], b["c"]))
        log.append(f"{s}: {len(daily[s])} days ({feed}), 5-min bars on {len(intra[s])} days ({feed5})")
        print(log[-1], flush=True)
    vol = {}
    for n in ("VIX", "VIX9D", "VXN"):
        try:
            vol[n] = cboe(n)
            log.append(f"{n}: {len(vol[n])} days from CBOE")
        except Exception as e:
            vol[n] = {}
            log.append(f"{n}: not available ({e})")
        print(log[-1], flush=True)
    return daily, intra, vol, log


# ───────────────────────────── pricing ─────────────────────────────

def iv_of(S, K, T, base, m, a):
    atm = base * m
    sd = np.log(S / K) / (atm * np.sqrt(np.maximum(T, 1e-8)))
    return atm * np.clip(1 + a * sd, 0.6, 2.5)


def bs(S, K, T, sig, r, put):
    S, K = np.asarray(S, float), np.asarray(K, float)
    T = np.asarray(T, float)
    live = T > 1e-7
    Tq = np.where(live, T, 1.0)
    sq = sig * np.sqrt(Tq)
    d1 = (np.log(S / K) + (r + 0.5 * sig * sig) * Tq) / sq
    d2 = d1 - sq
    call = S * ndtr(d1) - K * np.exp(-r * Tq) * ndtr(d2)
    val = (call - S + K * np.exp(-r * Tq)) if put else call
    intrinsic = np.maximum(K - S, 0) if put else np.maximum(S - K, 0)
    return np.where(live, np.maximum(val, intrinsic * 0.999), intrinsic)


def leg_price(S, K, T, base, r, put, cal):
    m, a = cal
    return bs(S, K, T, iv_of(S, K, T, base, m, a), r, put)


def strike_for(S, T, base, r, delta, put, cal):
    sig = base * cal[0]
    d1 = float(ndtri(1 - delta)) if put else float(ndtri(delta))
    K = S
    for _ in range(5):
        K = S * math.exp(-(d1 * sig * math.sqrt(T) - (r + sig * sig / 2) * T))
        sig = float(iv_of(S, K, T, base, *cal))
    return float(round(K))


def spread_value(S, T, base, r, legs, cal):
    """Cost to buy the position back (positive for a credit position). legs: (K, put?, +1 short / -1 long)."""
    v = 0.0
    for K, put, q in legs:
        v = v + q * leg_price(S, K, T, base, r, put, cal)
    return v


def occ(sym, exp, put, K):
    return f"{sym}{exp:%y%m%d}{'P' if put else 'C'}{int(round(K * 1000)):08d}"


# ───────────────────────────── market calendar / vol ─────────────────────────────

class Market:
    def __init__(self, daily, intra, vol):
        self.daily, self.intra = daily, intra
        self.days = sorted(set(daily["SPY"]) & set(daily["QQQ"]))
        self.idx = {d: i for i, d in enumerate(self.days)}
        self.vol = {}
        for n, ser in vol.items():
            self.vol[n] = ser
        self.cache = {}
        self.sma = {s: self._sma(s, 50) for s in SYMS}
        self.rank = {s: self._rank(s) for s in SYMS}

    def _ff(self, name, d, field):
        ser = self.vol.get(name) or {}
        if d in ser:
            return ser[d][field]
        keys = self.cache.setdefault(name, sorted(ser))
        i = bisect.bisect_right(keys, d) - 1
        return ser[keys[i]][field] if i >= 0 else None

    def base(self, sym, d, short, field=1):
        """Implied vol (decimal) for a symbol on a day. short=True → 9-day vol (VIX9D)."""
        vix = self._ff("VIX", d, field) or 18.0
        b = vix
        if sym == "QQQ":
            vxn = self._ff("VXN", d, field)
            b = vxn if vxn else vix * 1.25
        if short:
            v9 = self._ff("VIX9D", d, field)
            if v9:
                b = b * v9 / vix
        return b / 100.0

    def _sma(self, s, n):
        out, closes = {}, []
        for d in sorted(self.daily[s]):
            closes.append(self.daily[s][d]["c"])
            if len(closes) >= n:
                out[d] = sum(closes[-n:]) / n
        return out

    def _rank(self, s):
        out, hist = {}, []
        for d in self.days:
            v = self.base(s, d, False)
            hist.append(v)
            w = hist[-252:]
            if len(w) >= 120:
                lo, hi = min(w), max(w)
                out[d] = (v - lo) / (hi - lo) if hi > lo else 0.5
        return out

    def expiry(self, i, dte):
        """Index of the expiration day: first Friday on/after day+dte-3 (Thursday if that Friday is a holiday)."""
        d = self.days[i]
        t = d + dt.timedelta(days=max(dte - 3, 1))
        t += dt.timedelta(days=(4 - t.weekday()) % 7)
        if t > self.days[-1]:
            return None, t
        j = bisect.bisect_right(self.days, t) - 1
        return j, t

    def passes(self, flt, sym, d):
        if flt == "none":
            return True
        if flt == "vix15":
            return self.base(sym, d, False) * 100 > (15 if sym == "SPY" else 19)
        if flt == "ivr30":
            return self.rank[sym].get(d, 0) >= 0.30
        if flt == "trend":
            sma = self.sma[sym].get(d)
            return sma is not None and self.daily[sym][d]["c"] > sma
        if flt.startswith("cheap"):
            return self.rank[sym].get(d, 1) <= int(flt[5:]) / 100
        return True


# ───────────────────────────── trade paths ─────────────────────────────

def make_legs(S, T, base, r, delta, width, fam, cal):
    legs = []
    if fam in ("put", "condor"):
        kp = strike_for(S, T, base, r, delta, True, cal)
        legs += [(kp, True, 1), (kp - width, True, -1)]
    if fam == "condor":
        kc = strike_for(S, T, base, r, delta, False, cal)
        if kc <= (legs[0][0] if legs else 0):
            kc = legs[0][0] + width
        legs += [(kc, False, 1), (kc + width, False, -1)]
    if fam == "straddle":
        k = float(round(S))
        legs = [(k, True, -1), (k, False, -1)]          # long both (value is negative of a credit position)
    return legs


def first_hit(mask):
    i = int(np.argmax(mask))
    return i if mask[i] else None


def swing_paths(mk, sym, dte, delta, width, fam, cal):
    """One trade per week (first trading day of each week). Daily closes."""
    out, last_week = [], None
    for i, d in enumerate(mk.days):
        if d < START or d.isocalendar()[:2] == last_week:
            continue
        last_week = d.isocalendar()[:2]
        j, exp = mk.expiry(i, dte)
        if j is None or j <= i:
            continue
        S0 = mk.daily[sym][d]["c"]
        short = dte <= 10
        days = mk.days[i:j + 1]
        S = np.array([mk.daily[sym][x]["c"] for x in days])
        T = np.array([max((exp - x).days, 0) / 365 for x in days])
        T[-1] = 0.0
        base = np.array([mk.base(sym, x, short) for x in days])
        r = rate(d)
        legs = make_legs(S0, T[0], base[0], r, delta, width, fam, cal)
        v = spread_value(S, T, base, r, legs, cal)
        if fam == "straddle":
            v = -v
        out.append({"d": d, "x": days, "exp": exp, "legs": legs, "v": v, "w": v, "dl": np.array([(exp - x).days for x in days]),
                    "fam": fam, "sym": sym, "S0": S0})
    return out


def zero_paths(mk, sym, delta, width, fam, cal, entry_mm=595):
    """One trade a day, opened 10:00 ET, held to 4:00 PM close unless a rule fires. 5-min bars; stops use bar high/low."""
    out = []
    for d in mk.days:
        if d < START or d not in mk.intra[sym]:
            continue
        if d < DAILY_EXP_FROM and d.weekday() in (1, 3):
            continue
        bars = [b for b in mk.intra[sym][d] if b[0] >= entry_mm]
        if len(bars) < 30 or bars[0][0] != entry_mm or bars[-1][0] < 950:
            continue
        mm = np.array([b[0] + 5 for b in bars], float)            # bar close time
        T = (960 - mm) / 390 / 252
        T[-1] = 0.0
        base = mk.base(sym, d, True, field=0)                     # today's 9-day vol at the open
        r = rate(d)
        S0 = bars[0][4]
        legs = make_legs(S0, T[0], base, r, delta, width, fam, cal)
        c = np.array([b[4] for b in bars])
        hi = np.array([b[2] for b in bars])
        lo = np.array([b[3] for b in bars])
        v = spread_value(c, T, base, r, legs, cal)
        Tw = np.maximum(T, 1e-7)
        w = np.maximum(spread_value(hi, Tw, base, r, legs, cal), spread_value(lo, Tw, base, r, legs, cal))
        w[0] = v[0]
        w[-1] = v[-1]
        out.append({"d": d, "x": [d] * len(bars), "exp": d, "legs": legs, "v": v, "w": w, "dl": np.zeros(len(bars)),
                    "fam": fam, "sym": sym, "S0": S0, "mm": mm})
    return out


def settle(p, slip, tp, stop, x21, closes_only=False):
    """Apply the exit rules to one path. Returns (pnl per share, max loss per share, exit index) or None."""
    v, w = p["v"], p["w"]
    L = len(p["legs"])
    v0 = float(v[0])
    if p["fam"] == "straddle":
        debit = v0 + L * slip
        if debit <= 0.05:
            return None
        hits = []
        if tp:
            k = first_hit(v[1:] >= v0 * (1 + tp))
            hits.append((k + 1, v0 * (1 + tp)) if k is not None else None)
        if stop:
            k = first_hit(v[1:] <= v0 * (1 - stop))
            hits.append((k + 1, None) if k is not None else None)
        k = first_hit(p["dl"][1:] <= 21)
        hits.append((k + 1, None) if k is not None else None)
        hits = [h for h in hits if h]
        t, px = min(hits, key=lambda h: h[0]) if hits else (len(v) - 1, None)
        val = float(v[t]) if px is None else px
        got = max(val - L * slip, 0)
        return got - debit, debit, t
    credit = v0 - L * slip
    width = max(abs(p["legs"][0][0] - p["legs"][1][0]), abs(p["legs"][-2][0] - p["legs"][-1][0]))
    risk = width - credit
    if credit <= 0.03 or risk <= 0:
        return None
    hits = []
    if tp:
        k = first_hit(v[1:-1] <= v0 * (1 - tp)) if len(v) > 2 else None
        if k is not None:
            hits.append((k + 1, v0 * (1 - tp)))
    if stop:
        trig = v0 * (1 + stop)
        k = first_hit((v if closes_only else w)[1:-1] >= trig) if len(v) > 2 else None
        if k is not None:
            t = k + 1
            hits.append((t, max(trig, float(v[t])) if (closes_only or "mm" not in p) else trig))
    if x21:
        k = first_hit(p["dl"][1:-1] <= 21) if len(v) > 2 else None
        if k is not None:
            hits.append((k + 1, None))
    if hits:
        t, px = min(hits, key=lambda h: h[0])
        val = float(v[t]) if px is None else px
        pay = val + L * slip
    else:
        t = len(v) - 1
        val = float(v[t])
        pay = val + (L * slip if val > 0.005 else 0)
    pay = min(pay, width + L * slip)
    return credit - pay, risk, t


# ───────────────────────────── stats ─────────────────────────────

def stats(rs):
    if not rs:
        return {"n": 0, "win": 0, "avg": None, "tot": 0}
    return {"n": len(rs), "win": round(sum(1 for r in rs if r > 0) / len(rs), 3), "avg": round(sum(rs) / len(rs), 4),
            "tot": round(sum(rs), 2)}


def max_dd(rs):
    peak = cur = dd = 0.0
    for r in rs:
        cur += r
        peak = max(peak, cur)
        dd = min(dd, cur - peak)
    return dd


def label(v):
    if v["fam"] == "straddle":
        return (f"{v['sym']} buy 45-day straddle · when {v['filt']} · take +{v['tp']:.0%} · "
                f"stop {('-' + format(v['stop'], '.0%')) if v['stop'] else 'none'} · out at 21 days")
    name = "put spread" if v["fam"] == "put" else "iron condor"
    dte = "0DTE" if v["dte"] == 0 else f"{v['dte']}-day"
    return (f"{v['sym']} {dte} {name} · {int(v['delta'] * 100)}Δ · ${v['width']} wide · "
            f"take {('+' + format(v['tp'], '.0%')) if v['tp'] else 'hold'} · stop {(format(v['stop'], 'g') + 'x credit') if v['stop'] else 'none'}"
            f"{' · out at 21d' if v.get('x21') else ''} · {v['filt']}")


def run_version(v, paths, mk):
    slip = SLIP[(v["sym"], "0dte" if v["dte"] == 0 else "swing")]
    ts = []
    for p in paths:
        if not mk.passes(v["filt"], v["sym"], p["d"]):
            continue
        res = settle(p, slip, v["tp"], v["stop"], v.get("x21"))
        if res is None:
            continue
        pnl, risk, t = res
        ts.append({"d": p["d"], "out": p["x"][t], "r": pnl / risk, "usd": pnl * 100, "risk": risk * 100})
    return ts


def summarize(v, ts):
    rs = [t["r"] for t in ts]
    yr = {}
    for t in ts:
        yr.setdefault(t["d"].year, []).append(t["r"])
    return {"label": label(v), "v": v, "all": stats(rs), "train": stats([t["r"] for t in ts if t["d"] < SPLIT]),
            "test": stats([t["r"] for t in ts if t["d"] >= SPLIT]), "dd": round(max_dd(rs), 1),
            "years": {y: round(sum(x), 1) for y, x in sorted(yr.items())},
            "usd": round(sum(t["usd"] for t in ts) / max(len(ts), 1), 1),
            "worst": round(min(rs), 2) if rs else None}


# ───────────────────────────── account simulation ─────────────────────────────

def account(ts, start_cash, risk_frac=0.10, cap=0.5, since=None):
    ts = [t for t in ts if since is None or t["d"] >= since]
    if not ts:
        return None
    eq, open_, curve, skipped = start_cash, [], [], 0
    events = sorted(ts, key=lambda t: t["d"])
    i = 0
    all_days = sorted({t["d"] for t in events} | {t["out"] for t in events})
    peak, dd = start_cash, 0.0
    month = {}
    for day in all_days:
        still = []
        for o in open_:
            if o["out"] <= day:
                eq += o["usd"] * o["n"]
                month[o["out"].strftime("%Y-%m")] = month.get(o["out"].strftime("%Y-%m"), 0) + o["usd"] * o["n"]
            else:
                still.append(o)
        open_ = still
        while i < len(events) and events[i]["d"] == day:
            t = events[i]
            i += 1
            n = int(risk_frac * eq // t["risk"]) if t["risk"] > 0 else 0
            if n == 0 and t["risk"] <= 0.25 * eq:
                n = 1
            used = sum(o["risk"] * o["n"] for o in open_)
            while n > 0 and used + n * t["risk"] > cap * eq:
                n -= 1
            if n <= 0:
                skipped += 1
                continue
            if t["out"] <= day:                 # opened and closed the same day (0DTE)
                eq += t["usd"] * n
                month[day.strftime("%Y-%m")] = month.get(day.strftime("%Y-%m"), 0) + t["usd"] * n
            else:
                open_.append(dict(t, n=n))
        peak = max(peak, eq)
        dd = min(dd, (eq - peak) / peak)
        curve.append((day.isoformat(), round(eq, 2)))
    for o in open_:
        eq += o["usd"] * o["n"]
    yrs = max((events[-1]["d"] - events[0]["d"]).days / 365.25, 0.25)
    ups = sum(1 for x in month.values() if x > 0)
    return {"start": start_cash, "end": round(eq, 2), "yearly": round(((max(eq, 1) / start_cash) ** (1 / yrs) - 1) * 100, 1),
            "dd": round(dd * 100, 1), "months_up": ups, "months": len(month), "skipped": skipped,
            "avg_month": round((eq - start_cash) / max(len(month), 1), 2), "curve": curve[::5]}


# ───────────────────────────── real option prices ─────────────────────────────

class Real:
    def __init__(self, api):
        self.api, self.calls, self.cache = api, 0, {}

    def bars(self, contracts, timeframe, start, end):
        key = (tuple(sorted(contracts)), timeframe, start, end)
        if key in self.cache:
            return self.cache[key]
        out, token = {c: [] for c in contracts}, None
        while True:
            p = {"symbols": ",".join(contracts), "timeframe": timeframe, "start": start, "end": end, "limit": 10000}
            if token:
                p["page_token"] = token
            self.calls += 1
            if self.calls % 150 == 0:
                time.sleep(20)                       # stay under the free-plan rate limit
            try:
                j = get(self.api, "/v1beta1/options/bars", p)
            except vo.ApiError as e:
                print(f"options bars: {e}", flush=True)
                break
            for c, rows in (j.get("bars") or {}).items():
                out.setdefault(c, []).extend(rows)
            token = j.get("next_page_token")
            if not token:
                break
        self.cache[key] = out
        return out


def leg_contracts(p):
    return [occ(p["sym"], p["exp"], put, K) for K, put, q in p["legs"]]


def calibrate(mk, real, log):
    """Fit volatility level (m) and skew (a) so modeled prices match real Alpaca option prices."""
    cal = {}
    for kind in ("swing", "0dte"):
        for sym in SYMS:
            obs = []            # (S, K, T, base, r, put, real price)
            days = [d for d in mk.days if d >= REAL_FROM]
            if kind == "swing":
                weeks, picks = set(), []
                for d in days:
                    if d.isocalendar()[:2] not in weeks:
                        weeks.add(d.isocalendar()[:2])
                        picks.append(d)
                for d in picks[::2]:
                    i = mk.idx[d]
                    S, r = mk.daily[sym][d]["c"], rate(d)
                    contracts = []
                    for dte in (7, 45):
                        j, exp = mk.expiry(i, dte)
                        T = max((exp - d).days, 1) / 365
                        base = mk.base(sym, d, dte <= 10)
                        for delta in (0.10, 0.20, 0.35):
                            for put in (True, False):
                                K = strike_for(S, T, base, r, delta, put, (1.0, 0.1))
                                contracts.append((occ(sym, exp, put, K), S, K, T, base, r, put))
                    got = real.bars([c[0] for c in contracts], "1Day", d.isoformat(), (d + dt.timedelta(days=1)).isoformat())
                    for c in contracts:
                        rows = [b for b in got.get(c[0]) or [] if vo.et(b["t"]).date() == d]
                        if rows and rows[-1]["c"] >= 0.10 and rows[-1].get("v", 0) >= 20:
                            obs.append(c[1:] + (rows[-1]["c"],))
            else:
                for d in days[::3]:
                    if d not in mk.intra[sym]:
                        continue
                    bar = next((b for b in mk.intra[sym][d] if b[0] == 595), None)
                    if not bar:
                        continue
                    S, r, T = bar[4], rate(d), (960 - 600) / 390 / 252
                    base = mk.base(sym, d, True, field=0)
                    contracts = []
                    for delta in (0.05, 0.10, 0.20, 0.35):
                        for put in (True, False):
                            K = strike_for(S, T, base, r, delta, put, (1.0, 0.1))
                            contracts.append((occ(sym, d, put, K), S, K, T, base, r, put))
                    start = dt.datetime(d.year, d.month, d.day, 9, 55, tzinfo=ET).astimezone(UTC)
                    got = real.bars([c[0] for c in contracts], "5Min", start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                    (start + dt.timedelta(minutes=4)).strftime("%Y-%m-%dT%H:%M:%SZ"))
                    for c in contracts:
                        rows = got.get(c[0]) or []
                        if rows and rows[0]["c"] >= 0.05:
                            obs.append(c[1:] + (rows[0]["c"],))
            if len(obs) < 30:
                cal[(sym, kind)] = (0.9, 0.10)
                log.append(f"Calibration {sym} {kind}: only {len(obs)} real prices → default (0.90, 0.10)")
                continue
            arr = np.array([o[:4] + (o[4],) for o in obs], float)
            puts = np.array([o[5] for o in obs], bool)
            realp = np.array([o[6] for o in obs], float)
            best = None
            for m in np.arange(0.55, 1.46, 0.025):
                for a in np.arange(-0.05, 0.401, 0.025):
                    S, K, T, base, r = arr.T
                    model = np.where(puts, leg_price(S, K, T, base, r, True, (m, a)), leg_price(S, K, T, base, r, False, (m, a)))
                    err = np.median(np.abs(np.log(np.maximum(model, 0.005) / realp)))
                    if best is None or err < best[0]:
                        best = (err, round(float(m), 3), round(float(a), 3))
            m, a = best[1], best[2]
            S, K, T, base, r = arr.T
            model = np.where(puts, leg_price(S, K, T, base, r, True, (m, a)), leg_price(S, K, T, base, r, False, (m, a)))
            ratio = np.median(model / realp)
            cal[(sym, kind)] = (m, a)
            log.append(f"Calibration {sym} {kind}: {len(obs)} real prices · level {m} · skew {a} · "
                       f"typical miss {best[0] * 100:.0f}% · model/real median {ratio:.2f}")
            print(log[-1], flush=True)
    return cal


def real_replay(v, paths, mk, real):
    """Same trades, but every price comes from real Alpaca option bars (Feb 2024 →)."""
    slip = SLIP[(v["sym"], "0dte" if v["dte"] == 0 else "swing")]
    out, skipped = [], 0
    for p in paths:
        if p["d"] < REAL_FROM or not mk.passes(v["filt"], v["sym"], p["d"]):
            continue
        cs = leg_contracts(p)
        if v["dte"] == 0:
            d = p["d"]
            s = dt.datetime(d.year, d.month, d.day, 9, 55, tzinfo=ET).astimezone(UTC)
            e = dt.datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET).astimezone(UTC)
            got = real.bars(cs, "5Min", s.strftime("%Y-%m-%dT%H:%M:%SZ"), e.strftime("%Y-%m-%dT%H:%M:%SZ"))
            series = {}
            for c in cs:
                series[c] = {vo.et(b["t"]).hour * 60 + vo.et(b["t"]).minute + 5: b["c"] for b in got.get(c) or []}
            keys = list(p["mm"])
        else:
            got = real.bars(cs, "1Day", p["d"].isoformat(), p["x"][-1].isoformat())
            series = {c: {vo.et(b["t"]).date(): b["c"] for b in got.get(c) or []} for c in cs}
            keys = list(p["x"])
        if any(keys[0] not in series[c] for c in cs):
            skipped += 1
            continue
        last = {c: series[c][keys[0]] for c in cs}
        vals = []
        for n, k in enumerate(keys):
            for c in cs:
                if k in series[c]:
                    last[c] = series[c][k]
            if n == len(keys) - 1:                          # expiration: intrinsic value from the stock close
                S = mk.daily[p["sym"]][p["exp"]]["c"] if v["dte"] else float(mk.intra[p["sym"]][p["d"]][-1][4])
                val = sum(q * (max(K - S, 0) if put else max(S - K, 0)) for K, put, q in p["legs"])
            else:
                val = sum(q * last[c] for c, (K, put, q) in zip(cs, p["legs"]))
            vals.append(-val if p["fam"] == "straddle" else val)
        rp = dict(p, v=np.array(vals), w=np.array(vals))
        rp.pop("mm", None)
        res = settle(rp, slip, v["tp"], v["stop"], v.get("x21"), closes_only=True)
        mres = settle(p, slip, v["tp"], v["stop"], v.get("x21"))
        if res is None or mres is None:
            skipped += 1
            continue
        out.append({"d": p["d"], "out": p["x"][res[2]], "r": res[0] / res[1], "usd": res[0] * 100, "risk": res[1] * 100,
                    "model_r": mres[0] / mres[1], "credit_real": float(vals[0]), "credit_model": float(p["v"][0])})
    return out, skipped


# ───────────────────────────── main ─────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fake", action="store_true", help="offline smoke test with made-up prices")
    a = ap.parse_args()
    t0 = time.time()
    if a.fake:
        import premium_fake
        daily, intra, vol, log = premium_fake.data()
        api = None
    else:
        key, secret = os.environ.get("ALPACA_KEY_ID", "").strip(), os.environ.get("ALPACA_SECRET_KEY", "").strip()
        if not key or not secret:
            sys.exit("Add ALPACA_KEY_ID and ALPACA_SECRET_KEY.")
        api = vo.Alpaca(key, secret)
        daily, intra, vol, log = load_data(api)
    mk = Market(daily, intra, vol)
    real = Real(api) if api else None
    cal = calibrate(mk, real, log) if real else {(s, k): (0.9, 0.1) for s in SYMS for k in ("swing", "0dte")}

    # 1) every price path once
    paths = {}
    for sym in SYMS:
        for dte in DTES:
            kind = "0dte" if dte == 0 else "swing"
            for fam in ("put", "condor"):
                for delta in DELTAS:
                    for width in WIDTHS[kind]:
                        k = (sym, dte, fam, delta, width)
                        paths[k] = (zero_paths(mk, sym, delta, width, fam, cal[(sym, kind)]) if dte == 0 else
                                    swing_paths(mk, sym, dte, delta, width, fam, cal[(sym, kind)]))
        paths[(sym, 45, "straddle", 0.5, 0)] = swing_paths(mk, sym, 45, 0.5, 0, "straddle", cal[(sym, "swing")])
        print(f"{sym}: paths built ({time.time() - t0:.0f}s)", flush=True)

    # 2) every version
    rows, trades = [], {}
    for (sym, dte, fam, delta, width), ps in paths.items():
        if fam == "straddle":
            combos = [dict(fam=fam, sym=sym, dte=dte, delta=delta, width=width, tp=tp, stop=st, filt=f, x21=True)
                      for tp, st, f in itertools.product(STRADDLE_TP, STRADDLE_STOP, STRADDLE_FILTER)]
        else:
            x21s = [False, True] if dte >= 30 else [False]
            combos = [dict(fam=fam, sym=sym, dte=dte, delta=delta, width=width, tp=tp, stop=st, filt=f, x21=x)
                      for tp, st, f, x in itertools.product(TPS, STOPS, FILTERS, x21s)]
        for v in combos:
            ts = run_version(v, ps, mk)
            if len(ts) < 30:
                continue
            row = summarize(v, ts)
            row["id"] = len(rows)
            rows.append(row)
            trades[row["id"]] = ts
    log.append(f"Tested {len(rows)} versions in {(time.time() - t0) / 60:.1f} min")
    print(log[-1], flush=True)

    # 3) pick on 2016-2022 only, judge on 2023 →
    def bucket(r):
        v = r["v"]
        return ("straddle" if v["fam"] == "straddle" else f"{v['fam']}-{'0dte' if v['dte'] == 0 else 'swing'}")
    buckets = {}
    for r in rows:
        buckets.setdefault(bucket(r), []).append(r)
    picks = {}
    for b, rs in buckets.items():
        ok = [r for r in rs if r["train"]["n"] >= 40]
        if ok:
            picks[b] = max(ok, key=lambda r: r["train"]["avg"])
    held = [r for r in rows if (r["train"]["avg"] or -1) > 0.02 and (r["test"]["avg"] or -1) > 0.02 and r["test"]["n"] >= 30]
    pos_test = [r for r in rows if (r["test"]["avg"] or -1) > 0 and r["test"]["n"] >= 30]
    n_test = sum(1 for r in rows if r["test"]["n"] >= 30)

    # 4) real option prices for each pick (Feb 2024 →) + the most robust held version
    robust, seen = [], set()
    for r in sorted(held, key=lambda r: -min(r["train"]["avg"], r["test"]["avg"])):
        sig = (r["all"]["n"], r["all"]["tot"])
        if sig not in seen and r not in picks.values():
            seen.add(sig)
            robust.append(r)
        if len(robust) == 2:
            break
    checks = list(picks.items()) + [(f"robust-{i + 1}", r) for i, r in enumerate(robust) if r not in picks.values()]
    for name, r in checks:
        v = r["v"]
        r["acct_1k"] = account(trades[r["id"]], 1000, since=SPLIT)
        r["acct_5k"] = account(trades[r["id"]], 5000, since=SPLIT)
        r["acct_5k_safe"] = account(trades[r["id"]], 5000, risk_frac=0.05, cap=0.25, since=SPLIT)
        r["acct_5k_all"] = account(trades[r["id"]], 5000)
        if real:
            rt, skipped = real_replay(v, paths[(v["sym"], v["dte"], v["fam"], v["delta"], v["width"])], mk, real)
            r["real"] = {"stats": stats([t["r"] for t in rt]), "model_same_trades": stats([t["model_r"] for t in rt]),
                         "skipped": skipped, "acct_5k": account(rt, 5000),
                         "credit_ratio": round(float(np.median([t["credit_real"] / t["credit_model"] for t in rt
                                                                 if t["credit_model"] > 0.02])), 2) if rt else None}
            print(f"{name}: real replay {r['real']['stats']} (model {r['real']['model_same_trades']}), "
                  f"{real.calls} option calls so far", flush=True)

    # 5) report
    def line(r):
        s = f"{r['label']}\n   2016-22 {r['train']['avg'] or 0:+.3f}R ({r['train']['n']}) → 2023-now {r['test']['avg'] or 0:+.3f}R ({r['test']['n']}) · win {r['all']['win']:.0%}"
        for k, name in (("acct_5k", "$5,000 @10% risk"), ("acct_5k_safe", "$5,000 @5% risk"), ("acct_1k", "$1,000 @10% risk")):
            ac = r.get(k)
            if not ac:
                continue
            if not ac["months"]:
                s += f"\n   {name}: can't afford these trades"
            else:
                s += (f"\n   {name} since 2023 → ${ac['end']:,.0f} ({ac['yearly']:+.1f}%/yr, worst drop {ac['dd']:.0f}%, "
                      f"{ac['months_up']}/{ac['months']} months up)")
        if r.get("real") and r["real"]["stats"]["n"]:
            rs = r["real"]
            s += (f"\n   REAL prices Feb 2024-now: {rs['stats']['avg']:+.3f}R on {rs['stats']['n']} trades "
                  f"(model said {rs['model_same_trades']['avg']:+.3f}R) · real credit ≈ {rs['credit_ratio']}× model")
            if rs.get("acct_5k"):
                s += f" · $5,000 → ${rs['acct_5k']['end']:,.0f}"
        return s

    names = {"put-swing": "Put credit spreads (weekly to 45-day)", "condor-swing": "Iron condors (weekly to 45-day)",
             "put-0dte": "0DTE put spreads", "condor-0dte": "0DTE iron condors", "straddle": "Buy only when cheap"}
    parts = [f"🕵️ Premium Lab · SPY + QQQ · 2016 → today · {len(rows)} versions", "",
             f"• {len(pos_test)} of {n_test} versions made money on the unseen years (2023 → now)",
             f"• {len(held)} made money in BOTH 2016-2022 and 2023-now (> +0.02R a trade)", ""]
    for b in ("put-swing", "condor-swing", "put-0dte", "condor-0dte", "straddle"):
        if b in picks:
            parts += [f"{names[b]} — best on 2016-22:", "   " + line(picks[b]), ""]
    for i, r in enumerate(robust):
        parts += [f"Most robust #{i + 1} (good in both periods):", "   " + line(r), ""]
    msg = "\n".join(parts).rstrip()
    print(msg)
    print("\n".join(log))

    keep = {r["id"] for _, r in checks}
    json.dump({"when": t0, "cal": {f"{k[0]}-{k[1]}": v for k, v in cal.items()}, "log": log, "summary": msg,
               "rows": [{k: x for k, x in r.items() if k != "curve"} for r in rows],
               "checks": {n: r["id"] for n, r in checks}},
              open("premium_lab.json", "w"), default=str)
    top = sorted(rows, key=lambda r: -min(r["train"]["avg"] or -9, r["test"]["avg"] or -9))[:150]
    trs = "".join(
        f"<tr{' class=pick' if r['id'] in keep else ''}><td>{html.escape(r['label'])}</td><td class=n>{r['all']['n']}</td>"
        f"<td class=n>{r['all']['win']:.0%}</td><td class='n {'pos' if (r['train']['avg'] or 0) > 0 else 'neg'}'>{r['train']['avg'] or 0:+.3f}</td>"
        f"<td class='n {'pos' if (r['test']['avg'] or 0) > 0 else 'neg'}'>{r['test']['avg'] or 0:+.3f}</td><td class=n>{r['dd']}</td>"
        f"<td>{' '.join(f'{y % 100}:{x:+.0f}' for y, x in r['years'].items())}</td></tr>" for r in top)
    open("premium_lab.html", "w").write(f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Premium Lab</title><style>body{{background:#0f1220;color:#e8ecf8;font:13px/1.4 system-ui;padding:16px}}table{{border-collapse:collapse;width:100%}}
td,th{{padding:5px;border-bottom:1px solid #2a3150;text-align:left;vertical-align:top}}td.n{{text-align:right}}.pos{{color:#53f0a6}}.neg{{color:#ff6b7c}}
tr.pick td{{background:#1b2440}}pre{{white-space:pre-wrap}}</style></head>
<body><h1>Premium Lab</h1><pre>{html.escape(msg)}</pre><pre>{html.escape(chr(10).join(log))}</pre>
<h2>Top 150 (ranked by the weaker of the two periods)</h2><table><tr><th>Version</th><th>Trades</th><th>Win</th><th>2016-22 R</th>
<th>2023-now R</th><th>Worst slide R</th><th>Year by year (R)</th></tr>{trs}</table></body></html>""")
    tg = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    try:
        chat = json.load(open(vo.STATE_FILE)).get("chat_id")
    except Exception:
        chat = None
    if tg and chat and not a.fake:
        try:
            vo.http("POST", f"https://api.telegram.org/bot{tg}/sendMessage", body={"chat_id": chat, "text": msg[:4000]}, tries=2)
        except Exception as e:
            print(f"Telegram failed: {e}")


if __name__ == "__main__":
    main()
