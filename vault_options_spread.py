#!/usr/bin/env python3
"""
Vault Options — a day-trading options bot for Alpaca, with signals you can copy on Robinhood.

Every 5 minutes during market hours it:
  1. reads your phone commands from Telegram (/status, /pause, /resume, /closeall, /testtrade, /live)
  2. manages open options trades: target, stop, breakeven, 0DTE closed before the bell, weeklies held until the day before expiry
  3. on each new 15-minute candle, the council votes on stock setups (trend pullbacks, crossovers,
     and opening-range breakouts) for the watchlist
  4. when approved, it buys a CALL (bullish) or PUT (bearish): 0DTE on SPY/QQQ/IWM in the morning,
     2–10 day weeklies otherwise. It only ever BUYS options, so the most a trade can lose is what it paid.
  5. texts you the exact contract so you can copy it on Robinhood, and publishes the dashboard

Starts on Alpaca's PAPER account (fake money). Real money is a deliberate change: set ALPACA_ENV=live.

Needs (as GitHub secrets): ALPACA_KEY_ID, ALPACA_SECRET_KEY, TELEGRAM_BOT_TOKEN
Only uses the Python standard library.
"""

import argparse
import datetime as dt
import html
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

# ─────────────────────────── SETTINGS (edit these) ───────────────────────────
PAIRS = ["SPY", "DIA", "AMZN", "IWM"]   # opening range breakout watchlist: SPY, DIA, AMZN tested positive in both halves of 2 years
SIGNAL_ONLY = {"IWM"}       # on probation: alerts + scorekeeping only, no money spent (IWM tested -0.07R). Remove from this set to let it trade
ZERO_DTE = {"SPY", "QQQ", "IWM"}   # these may use same-day (0DTE) contracts in the morning
START_BALANCE = 1000.00      # the bot trades as if the account holds this much (raised from $250 so SPY, DIA and AMZN contracts fit)
RISK_PCT = 0.10              # a full stop-out costs about 10% of the bankroll
OPT_STOP = 0.40              # sell if the option loses 40% of what we paid
OPT_TARGET = 0.60            # sell if the option gains 60% (1.5 : 1 reward : risk)
BREAKEVEN_AT = 0.40          # once up 40%, the stop moves to the entry price
HER_EXITS = True             # Easter's exits: at +30% the stop locks +30%; 2+ contracts sell half at +50% and the rest
LOCK_PROFIT_AT = 0.30        #   targets +60%; 1 contract trails 25% below its best price (never below +30%)
HALF_AT = 0.50
HALF_TARGET = 0.60
NO_CHASE = 0.50              # skip if the option already costs 50%+ more than its first price today,
                             #   and never a second trade in the same direction on the same day
PARTIAL = False              # (old exits, off) take some profit early, let the rest run:
PARTIAL_AT = 0.30            #   at +30%: sell half (2+ contracts) and move the stop to entry,
LOCK_AT = 0.10               #   or with 1 contract, lock in +10% instead
RUNNER_TARGET = 1.20         #   the rest aims for +120%...
TRAIL_GIVEBACK = 0.25        #   ...with a trailing stop 25% below its best price
MAX_CONTRACTS = 3            # never more than 3 contracts on one trade
MAX_OPEN_TRADES = 2          # never more than 2 trades at once (one per stock)
MAX_SPREAD_PCT = 0.15        # skip contracts whose bid/ask spread is over 15% of the price
SPREADS = True              # when one contract is over budget, buy a call/put spread instead (buy one, sell a further one): about half the price
SPREAD_MAX_LEG_SPREAD = 0.35  # skip a spread if the two legs' bid/ask gaps add up to more than 35% of its price
DELTA_RANGE = (0.15, 0.55)   # how far from the money: 0.5 ≈ at the money, 0.15 ≈ cheapest. It takes the closest-to-the-money contract the budget allows, and only goes further out when it has to
WEEKLY_DAYS = (2, 10)        # weeklies: 2–10 days to expiration
ZERO_DTE_UNTIL = (13, 30)    # 0DTE entries only before 1:30 PM ET
ENTRY_WINDOW = ((9, 45), (15, 0))   # new trades 9:45 AM – 3:00 PM ET
FLATTEN_AT = (15, 40)        # 0DTE and expiring trades are sold by 3:40 PM ET
REPORTER = True              # 6th council member: earnings calendar, headlines, StockTwits crowd (options only)
HOLD_WEEKLIES = True         # weeklies can be held overnight; they're sold by 3:40 PM the trading day before they expire
DAILY_LOSS_LIMIT = 0.20      # stop opening trades for the day after a 20% drop (two full losses)
MAX_DRAWDOWN = 0.35          # pause the bot entirely after a 35% drop from the peak
STREAK_HALF = 5              # after 5 losses in a row, trade half size
STREAK_TIMEOUT = 7           # after 7 losses in a row, no new trades for 24 hours
STREAK_TIMEOUT_HOURS = 24
STREAK_RECOVER_WINS = 2      # back to full size after 2 wins
STOP_ATR = 1.0               # (paper scoring of rejected setups) stock stop = 1 × ATR
TARGET_ATR = 1.5             # (paper scoring of rejected setups) stock target = 1.5 × ATR
COUNCIL_MIN_YES = 3
RELAXED = True               # practice-friendly rules: more trades so the council learns faster
ADX_MIN = 18 if RELAXED else 22                  # The Quant wants a trend at least this strong
MAX_SPREAD_ATR = 1.0         # (stocks: spreads are tiny; the option spread is checked separately)
MAX_SPREAD_ATR_15M = 1.0
SHADOW_DAYS = 3              # rejected setups are scored for up to 3 days
COUNCIL_MIN_SHARE = 0.6 if RELAXED else 0.75   # weighted YES share (equal weights: 3 of 5); Risk always vetoes

# News filter: no new trades from 30 min before to 15 min after a big US release (CPI, jobs, Fed)
NEWS_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NEWS_REFRESH_HOURS = 6
NEWS_BEFORE_MIN = 30
NEWS_AFTER_MIN = 15
NEWS_WARN_MIN = 30

# Learning
LEARN_MIN_CALLS = 20         # a voter's weight starts changing after 20 scored calls
BENCH_MIN = 20               # a stock + setup combo is benched if its last 20 results average below 0R

# Reports (Eastern time)
REPORT_HOUR_ET = 16          # daily report after the 4 PM close
WEEKLY_DAY = 4               # weekly report on Friday (Mon=0)

STRATEGY = "orb5"            # Easter's 5-minute opening range breakout (from the IWM Day Signal page) is the only strategy
OLD_SETUPS = False           # the old 15-min pullback/crossover/breakout setups are off (they tested negative)
ORB5 = True                  # mark 9:30–10:00 high/low, then watch 5-min candles 10:00–12:00 for a confirmed breakout
ORB5_BODY = 0.60             # breakout candle: body at least 60% of its range, closing outside the box
ORB5_RR = 1.5                # target = 1.5 × the risk (entry to stop) on IWM itself
ORB5_BE_AT = 1.0             # at +1R on IWM the stop moves to entry
ORB5_END = (12, 0)           # no new breakouts after 12:00 ET
ORB5_EOD = (15, 50)          # an ORB trade still open at 3:50 PM ET is closed (it's a day trade)
ORB5_OPT_SAFETY = 0.70       # option safety net: sell if the option itself loses 70% (fast gaps)
ORB5_FRESH_MIN = 12          # only act on an entry signal from the last 12 minutes (no late entries)
ORB = False                  # (old 15-min opening breakout, replaced by ORB5)
ORB_WINDOW = ((10, 0), (11, 30))
FULL_SCAN_MIN = 5
DEPLOY_EVERY_MIN = 14
STATE_FILE = "options_state.json"
LOCAL_TZ = ZoneInfo("America/New_York")
ALPACA_ENV = os.environ.get("ALPACA_ENV", "paper").strip().lower() or "paper"
OANDA_ENV = ALPACA_ENV       # (shared dashboard code reads this name)
TRADE_API = "https://api.alpaca.markets" if ALPACA_ENV == "live" else "https://paper-api.alpaca.markets"
DATA_API = "https://data.alpaca.markets"
OPTION_FEED = os.environ.get("ALPACA_OPTION_FEED", "indicative")   # free plan: indicative; paid plan: opra
STOCK_FEED = os.environ.get("ALPACA_STOCK_FEED", "iex")             # free plan: iex; paid plan: sip
NAMES = {s: s for s in PAIRS}
VOTERS = ["The Quant", "Risk", "Momentum", "Time", "Doubt", "The Reporter"]
ROLES = {"The Quant": "trend strength (ADX)", "Risk": "contract price, spread and loss limits",
         "Momentum": "MACD is with the move", "Time": "not overextended, inside trading hours",
         "Doubt": "the 1-hour trend agrees",
         "The Reporter": "no earnings before expiry; headlines and chatter don't fight the trade"}
CLOCK = {}                   # filled from Alpaca's market clock each run (holidays, early closes)
# ─────────────────────────────────────────────────────────────────────────────


# ───────────────────────────── HTTP helpers ──────────────────────────────────

class ApiError(Exception):
    pass


def http(method, url, token=None, params=None, body=None, tries=3, headers=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    hdr = {"Accept": "application/json", "User-Agent": "vault-options/1.0"}
    if token:
        hdr["Authorization"] = f"Bearer {token}"
    hdr.update(headers or {})
    if data is not None:
        hdr["Content-Type"] = "application/json"
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, headers=hdr, method=method)
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:300]
            if e.code in (429, 502, 503) and attempt < tries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise ApiError(f"{e.code}: {msg}")
        except urllib.error.URLError as e:
            if attempt < tries - 1:
                time.sleep(1.5)
                continue
            raise ApiError(str(e))


def et(ts_iso_or_dt):
    d = ts_iso_or_dt if isinstance(ts_iso_or_dt, dt.datetime) else dt.datetime.fromisoformat(ts_iso_or_dt.replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(LOCAL_TZ)


class Alpaca:
    def __init__(self, key, secret):
        self.h = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}

    def t(self, method, path, params=None, body=None, tries=3):
        return http(method, f"{TRADE_API}{path}", params=params, body=body, tries=tries, headers=self.h)

    def d(self, path, params=None):
        return http("GET", f"{DATA_API}{path}", params=params, headers=self.h)

    def account(self):
        return self.t("GET", "/v2/account")

    def clock(self):
        return self.t("GET", "/v2/clock")

    def bars(self, symbols, timeframe, days):
        """Completed bars per symbol as {t, o, h, l, c} (t = bar start, UTC ISO)."""
        start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        out, token = {s: [] for s in symbols}, None
        step = {"5Min": 300, "15Min": 900, "1Hour": 3600}[timeframe]
        for _ in range(20):
            p = {"symbols": ",".join(symbols), "timeframe": timeframe, "start": start, "limit": 10000,
                 "feed": STOCK_FEED, "adjustment": "raw", "sort": "asc"}
            if token:
                p["page_token"] = token
            res = self.d("/v2/stocks/bars", p)
            for s, rows in (res.get("bars") or {}).items():
                out.setdefault(s, []).extend(rows)
            token = res.get("next_page_token")
            if not token:
                break
        now = time.time()
        clean = {}
        for s, rows in out.items():
            bars = []
            for b in rows:
                t0 = dt.datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
                if t0.timestamp() + step > now:
                    continue                                  # still forming
                e = et(t0)
                if not ((e.hour, e.minute) >= (9, 30) and (e.hour, e.minute) < (16, 0)):
                    continue                                  # regular hours only
                bars.append({"t": t0.strftime("%Y-%m-%dT%H:%M:%S"), "o": float(b["o"]), "h": float(b["h"]),
                             "l": float(b["l"]), "c": float(b["c"]), "v": float(b.get("v", 0))})
            clean[s] = bars
        return clean

    def chain(self, underlying, kind, exp_from, exp_to):
        """Option snapshots for one underlying: {contract: {bid, ask, delta, strike, exp, type}}."""
        out, token = {}, None
        for _ in range(10):
            p = {"feed": OPTION_FEED, "type": kind, "expiration_date_gte": exp_from, "expiration_date_lte": exp_to,
                 "limit": 1000}
            if token:
                p["page_token"] = token
            res = self.d(f"/v1beta1/options/snapshots/{underlying}", p)
            for sym, snap in (res.get("snapshots") or {}).items():
                q = snap.get("latestQuote") or {}
                g = snap.get("greeks") or {}
                info = parse_occ(sym)
                if not info:
                    continue
                out[sym] = {"bid": float(q.get("bp") or 0), "ask": float(q.get("ap") or 0),
                            "delta": g.get("delta"), **info}
            token = res.get("next_page_token")
            if not token:
                break
        return out

    def quotes(self, contracts):
        if not contracts:
            return {}
        res = self.d("/v1beta1/options/snapshots", {"symbols": ",".join(contracts), "feed": OPTION_FEED})
        out = {}
        for sym, snap in (res.get("snapshots") or {}).items():
            q = snap.get("latestQuote") or {}
            out[sym] = {"bid": float(q.get("bp") or 0), "ask": float(q.get("ap") or 0)}
        return out

    def positions(self):
        return {p["symbol"]: p for p in self.t("GET", "/v2/positions")}

    def buy(self, contract, qty, limit):
        return self.t("POST", "/v2/orders", body={"symbol": contract, "qty": str(qty), "side": "buy", "type": "limit",
                                                  "limit_price": f"{limit:.2f}", "time_in_force": "day"}, tries=1)

    def order(self, oid):
        return self.t("GET", f"/v2/orders/{oid}")

    def cancel(self, oid):
        return self.t("DELETE", f"/v2/orders/{oid}", tries=1)

    def close(self, contract, qty=None):
        return self.t("DELETE", f"/v2/positions/{contract}", params={"qty": str(qty)} if qty else None, tries=1)

    def buy_spread(self, long_sym, short_sym, qty, limit):
        """Open a debit spread in one order: buy long_sym, sell short_sym, paying at most `limit` per spread."""
        return self.t("POST", "/v2/orders", body={
            "order_class": "mleg", "qty": str(qty), "type": "limit", "limit_price": f"{limit:.2f}", "time_in_force": "day",
            "legs": [{"symbol": long_sym, "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_open"},
                     {"symbol": short_sym, "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"}]}, tries=1)

    def close_spread(self, long_sym, short_sym, qty):
        """Close a debit spread in one market order (sell the long leg, buy back the short leg)."""
        return self.t("POST", "/v2/orders", body={
            "order_class": "mleg", "qty": str(qty), "type": "market", "time_in_force": "day",
            "legs": [{"symbol": long_sym, "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_close"},
                     {"symbol": short_sym, "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_close"}]}, tries=1)


def parse_occ(sym):
    """'SPY261013C00585000' → {underlying, exp 'YYYY-MM-DD', type 'call'/'put', strike 585.0}."""
    try:
        i = len(sym) - 15
        und, y, m, d, cp, k = sym[:i], sym[i:i + 2], sym[i + 2:i + 4], sym[i + 4:i + 6], sym[i + 6], sym[i + 7:]
        return {"underlying": und, "exp": f"20{y}-{m}-{d}", "type": "call" if cp == "C" else "put", "strike": int(k) / 1000}
    except (ValueError, IndexError):
        return None


def contract_name(sym):
    o = parse_occ(sym) or {}
    if not o:
        return sym
    exp = dt.date.fromisoformat(o["exp"])
    k = f"{o['strike']:g}"
    return f"{o['underlying']} ${k} {'Call' if o['type'] == 'call' else 'Put'} · {exp:%b} {exp.day}"


def wait_fill(api, oid, secs=8):
    o = {}
    for _ in range(secs):
        time.sleep(1)
        try:
            o = api.order(oid)
        except ApiError:
            continue
        if o.get("status") in ("filled", "canceled", "expired", "rejected"):
            break
    return o


# ───────────────────────────── Indicators ────────────────────────────────────

def ema(xs, n):
    k, out, e = 2 / (n + 1), [], None
    for x in xs:
        e = x if e is None else x * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes, n=14):
    gains, losses, out = [], [], [None] * len(closes)
    ag = al = None
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        g, l = max(d, 0), max(-d, 0)
        if i <= n:
            gains.append(g); losses.append(l)
            if i == n:
                ag, al = sum(gains) / n, sum(losses) / n
        else:
            ag = (ag * (n - 1) + g) / n
            al = (al * (n - 1) + l) / n
        if ag is not None:
            out[i] = 100 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def atr_adx(c, n=14):
    """Wilder ATR and ADX lists for candle dicts."""
    trs, pdm, mdm = [], [], []
    for i in range(1, len(c)):
        h, l, pc = c[i]["h"], c[i]["l"], c[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        up, dn = h - c[i - 1]["h"], c[i - 1]["l"] - l
        pdm.append(up if up > dn and up > 0 else 0)
        mdm.append(dn if dn > up and dn > 0 else 0)

    def wilder(xs):
        out, s = [], None
        for i, x in enumerate(xs):
            if i < n - 1:
                out.append(None); continue
            s = sum(xs[:n]) if i == n - 1 else s - s / n + x
            out.append(s)
        return out

    tr_s, p_s, m_s = wilder(trs), wilder(pdm), wilder(mdm)
    atr = [None] + [(t / n if t is not None else None) for t in tr_s]
    dx = []
    for t, p, m in zip(tr_s, p_s, m_s):
        if t is None or t == 0:
            dx.append(None); continue
        pdi, mdi = 100 * p / t, 100 * m / t
        dx.append(0 if pdi + mdi == 0 else 100 * abs(pdi - mdi) / (pdi + mdi))
    adx, a, vals = [None], None, []
    for d in dx:
        if d is None:
            adx.append(None); continue
        vals.append(d)
        if len(vals) < n:
            adx.append(None)
        elif len(vals) == n:
            a = sum(vals) / n; adx.append(a)
        else:
            a = (a * (n - 1) + d) / n; adx.append(a)
    return atr, adx


def macd_hist(closes):
    fast, slow = ema(closes, 12), ema(closes, 26)
    line = [f - s for f, s in zip(fast, slow)]
    sig = ema(line, 9)
    return [l - s for l, s in zip(line, sig)]


# ───────────────────────────── Strategy + council ────────────────────────────

def analyze(h1, h4):
    """Indicator snapshot for the last completed 1-hour candle."""
    cl = [x["c"] for x in h1]
    e20, e50 = ema(cl, 20), ema(cl, 50)
    atr, adx = atr_adx(h1)
    r = rsi(cl)
    mh = macd_hist(cl)
    c4 = [x["c"] for x in h4]
    h4_50, h4_200 = ema(c4, 50), ema(c4, 200)
    i = len(h1) - 1
    trend = 1 if e20[i] > e50[i] else -1
    crossed = any((e20[j] > e50[j]) != (e20[j - 1] > e50[j - 1]) for j in range(i - 2, i + 1))
    # pullback: one of the last 2 candles touched EMA20 and the last one closed back on the trend side
    touched = any(h1[j]["l"] <= e20[j] <= h1[j]["h"] for j in (i - 1, i))
    closed_right = (h1[i]["c"] > e20[i]) if trend == 1 else (h1[i]["c"] < e20[i])
    return {
        "time": h1[i]["t"], "close": cl[i], "ema20": e20[i], "ema50": e50[i], "trend": trend,
        "setup": "crossover" if crossed else "pullback" if (touched and closed_right) else None,
        "atr": atr[i], "adx": adx[i], "rsi": r[i], "macd": mh[i], "macd_prev": mh[i - 1],
        "h4_trend": 1 if h4_50[-1] > h4_200[-1] else -1, "h4_close": c4[-1], "h4_ema200": h4_200[-1],
    }


def market_open_now(now_utc):
    """Uses Alpaca's clock when we have it (holidays, early closes), otherwise weekday 9:30–4:00 ET."""
    if CLOCK.get("checked") and abs(now_utc.timestamp() - CLOCK["checked"]) < 600:
        return bool(CLOCK.get("is_open"))
    e = et(now_utc)
    return e.weekday() < 5 and (9, 30) <= (e.hour, e.minute) < (16, 0)


def in_session(pair, now_utc):
    """True when this stock may open a new trade right now."""
    if not market_open_now(now_utc):
        return False
    e = et(now_utc)
    lo, hi = ENTRY_WINDOW
    return lo <= (e.hour, e.minute) < hi


def next_vote(now_utc):
    """Next 15-minute candle close inside the entry window (UTC datetime), and which stocks are live then."""
    t = now_utc.replace(minute=(now_utc.minute // 15) * 15, second=0, microsecond=0) + dt.timedelta(minutes=15)
    if not market_open_now(now_utc) and CLOCK.get("next_open"):
        nxt = dt.datetime.fromisoformat(CLOCK["next_open"].replace("Z", "+00:00")).astimezone(dt.timezone.utc)
        if nxt > t:
            t = nxt
    for _ in range(4 * 24 * 5):
        e = et(t)
        lo, hi = ENTRY_WINDOW
        if e.weekday() < 5 and lo <= (e.hour, e.minute) < hi and (e.minute % 15 == 0):
            return t, list(PAIRS)
        t += dt.timedelta(minutes=15)
        t = t.replace(minute=(t.minute // 15) * 15, second=0, microsecond=0)
    return t, []


def council(a, spread, risk_ok, risk_why, now_utc, pair=None):
    d = a["trend"]
    v = {}
    adx = a["adx"] or 0
    v["The Quant"] = (adx >= ADX_MIN, f"ADX {adx:.0f} (needs {ADX_MIN})")
    rs = a["atr"] and spread / a["atr"]
    if not risk_ok:
        v["Risk"] = (False, risk_why)
    else:
        ok = rs is not None and rs <= a.get("spread_max", MAX_SPREAD_ATR)
        v["Risk"] = (ok, f"spread is {rs:.0%} of ATR" if rs is not None else "no ATR yet")
    m, mp = a["macd"], a["macd_prev"]
    if RELAXED:     # "not against us": on our side of zero, or turning our way
        mom_ok = (m > 0 or m > mp) if d == 1 else (m < 0 or m < mp)
        v["Momentum"] = (mom_ok, f"MACD {'with us' if mom_ok else 'against us'} ({'up' if d == 1 else 'down'})")
    else:
        mom_ok = (m > 0 and m >= mp) if d == 1 else (m < 0 and m <= mp)
        v["Momentum"] = (mom_ok, f"MACD {'building' if mom_ok else 'not confirming'} {'up' if d == 1 else 'down'}")
    r = a["rsi"] or 50
    rsi_ok = ((40 <= r <= 75) if d == 1 else (25 <= r <= 60)) if RELAXED else ((45 <= r <= 70) if d == 1 else (30 <= r <= 55))
    open_now = in_session(pair, now_utc)
    v["Time"] = (rsi_ok and open_now,
                 f"RSI {r:.0f}" + ("" if rsi_ok else " (stretched)") + ("" if open_now else ", outside trading hours"))
    big = a["h4_trend"] == d and ((a["h4_close"] > a["h4_ema200"]) if d == 1 else (a["h4_close"] < a["h4_ema200"]))
    big = big and a.get("extra_trend", d) == d
    lbl = a.get("htf_label", "4-hour trend")
    v["Doubt"] = (big, f"{lbl} {('agree' if big else 'disagree') if 'and' in lbl else ('agrees' if big else 'disagrees')}")
    return v


# ───────────────────────────── The Reporter (news check, options only) ──────────
EARNINGS_URL = "https://api.nasdaq.com/api/calendar/earnings"
STOCKTWITS_URL = "https://api.stocktwits.com/api/2/streams/symbol/{s}.json"
EARNINGS_DAYS = 14           # look this far ahead on the earnings calendar (refreshed every 12 hours)
HEADLINE_HOURS = 6           # headlines from the last 6 hours count
FRESH_MIN = 45               # a big headline in the last 45 minutes can block a trade on its own
ETFS = {"SPY", "QQQ", "IWM", "DIA"}
BULL_WORDS = ("beat", "beats", "tops", "upgrade", "raises", "raised", "record", "surge", "soar", "jump", "rall", "approval",
              "approved", "buyback", "partnership", "outperform", "strong demand", "exceeds", "higher", "gains", "bullish")
BEAR_WORDS = ("miss", "downgrade", "cuts", "lowers", "plunge", "sink", "falls", "tumble", "slump", "lawsuit", "probe",
              "investigation", "recall", "halt", "bankrupt", "layoff", "offering", "dilut", "warns", "weak", "underperform",
              "delay", "lower", "drops", "bearish", "selloff", "sell-off")
BIG_WORDS = ("halt", "fda", "sec ", "doj", "merger", "acquire", "acquisition", "buyout", "bankrupt", "guidance",
             "earnings", "results", "offering", "tariff", "ceo", "lawsuit", "probe")


def load_earnings(st, log):
    """Upcoming earnings dates for the watchlist from Nasdaq's public calendar, cached for 12 hours."""
    E = st.get("earnings") or {}
    if time.time() - E.get("fetched", 0) < 12 * 3600:
        return E
    out, ok = {}, 0
    day = dt.datetime.now(LOCAL_TZ).date()
    want = {p for p in PAIRS if p not in ETFS}
    for k in range(EARNINGS_DAYS + 1):
        d = day + dt.timedelta(days=k)
        if d.weekday() >= 5:
            continue
        try:
            j = http("GET", EARNINGS_URL, params={"date": d.isoformat()}, tries=2,
                     headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15",
                              "Accept": "application/json, text/plain, */*", "Origin": "https://www.nasdaq.com",
                              "Referer": "https://www.nasdaq.com/"})
            ok += 1
        except ApiError as e:
            log.append(f"Reporter: earnings calendar unavailable for {d} ({str(e)[:60]})")
            continue
        for row in ((j.get("data") or {}).get("rows") or []):
            sym = (row.get("symbol") or "").upper()
            if sym in want:
                t = (row.get("time") or "").lower()
                when = "after" if "after" in t else "before" if "pre" in t else "unknown"
                out.setdefault(sym, []).append({"date": d.isoformat(), "when": when})
        time.sleep(0.3)
    if ok:
        E = {"fetched": time.time(), "dates": out, "days_checked": ok}
        st["earnings"] = E
    return E


def earnings_hit(st, pair, sell_by_et):
    """The first earnings release between now and when we'd have to sell, or None. (Already-released ones don't count.)"""
    now_et = dt.datetime.now(LOCAL_TZ)
    for e in (st.get("earnings") or {}).get("dates", {}).get(pair, []):
        d = dt.date.fromisoformat(e["date"])
        hour = 16 if e["when"] == "after" else 8                 # after the close, or before the open (unknown = assume before)
        release = dt.datetime(d.year, d.month, d.day, hour, 5 if hour == 16 else 0, tzinfo=LOCAL_TZ)
        if now_et < release <= sell_by_et:
            return e, release
    return None


def headlines(api, pair, log):
    """Recent Benzinga headlines for this symbol from Alpaca's news feed (same API keys)."""
    start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=HEADLINE_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        j = api.d("/v1beta1/news", {"symbols": pair, "start": start, "limit": 20, "sort": "desc"})
    except ApiError as e:
        log.append(f"Reporter: news feed unavailable ({str(e)[:60]})")
        return None
    out = []
    for n in j.get("news", []) or []:
        try:
            t = dt.datetime.fromisoformat(n.get("created_at", "").replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        text = f"{n.get('headline', '')} {n.get('summary', '')}".lower()
        score = sum(w in text for w in BULL_WORDS) - sum(w in text for w in BEAR_WORDS)
        out.append({"t": t, "headline": (n.get("headline") or "").strip(), "score": score,
                    "big": any(w in text for w in BIG_WORDS), "n_syms": len(n.get("symbols") or [])})
    return out


def crowd_mood(pair, log):
    """StockTwits: how many recent posts are tagged Bullish vs Bearish (public, no key). None if unavailable."""
    try:
        j = http("GET", STOCKTWITS_URL.format(s=pair), tries=1, headers={"User-Agent": "Mozilla/5.0"})
    except ApiError:
        return None
    bull = bear = 0
    for m in (j.get("messages") or [])[:30]:
        b = (((m.get("entities") or {}).get("sentiment")) or {}).get("basic")
        bull += b == "Bullish"
        bear += b == "Bearish"
    return {"bull": bull, "bear": bear}


def reporter_vote(st, api, pair, d, pick, now_utc, log):
    """(vote, why, hard_veto). Earnings before we'd sell is a hard NO; fresh headlines or a crowd leaning hard against us are a NO."""
    if not REPORTER:
        return None, "off", False
    e_now = dt.datetime.now(LOCAL_TZ)
    zero = bool(pick and pick.get("zero")) or (pick is None and pair in ETFS)
    if pick and not zero:
        exp = dt.date.fromisoformat(pick["exp"])
        last = exp - dt.timedelta(days=1)
        while last.weekday() >= 5:
            last -= dt.timedelta(days=1)
        sell_by = dt.datetime(last.year, last.month, last.day, 15, 40, tzinfo=LOCAL_TZ) if HOLD_WEEKLIES else \
            e_now.replace(hour=15, minute=40)
    elif pick is None and not zero:
        sell_by = e_now + dt.timedelta(days=WEEKLY_DAYS[1])          # no contract yet: assume a full-length weekly
    else:
        sell_by = e_now.replace(hour=15, minute=40)
    side = "calls" if d == 1 else "puts"
    if pair not in ETFS:
        hit = earnings_hit(st, pair, sell_by)
        if hit:
            e, rel = hit
            return (False, f"{pair} reports earnings {rel:%a %b} {rel.day} {'after the close' if e['when'] == 'after' else 'before the open'}, "
                           f"before we'd sell. Too much gap risk", True)
    news = headlines(api, pair, log)
    crowd = crowd_mood(pair, log)
    if news is None and crowd is None:
        return None, "couldn't reach the news feeds this time", False
    now_ts = time.time()
    against = [n for n in (news or []) if n["score"] * d < 0]
    fresh_big = [n for n in against if n["big"] and now_ts - n["t"] <= FRESH_MIN * 60 and n["n_syms"] <= 3]
    if fresh_big:
        n = fresh_big[0]
        return False, f"fresh headline against our {side}: \"{n['headline'][:70]}\"", False
    net = sum(n["score"] for n in (news or []) if n["n_syms"] <= 3)
    if net * d <= -3:
        return False, f"today's headlines lean the other way ({len(against)} against our {side})", False
    if crowd and crowd["bull"] + crowd["bear"] >= 8:
        lean = (crowd["bull"] - crowd["bear"]) / (crowd["bull"] + crowd["bear"])
        if lean * d <= -0.5:
            return False, (f"StockTwits crowd is {crowd['bear'] if d == 1 else crowd['bull']} to "
                           f"{crowd['bull'] if d == 1 else crowd['bear']} {'bearish' if d == 1 else 'bullish'}"), False
    bits = []
    if pair not in ETFS:
        bits.append("no earnings before we'd sell" if (st.get("earnings") or {}).get("days_checked") else "earnings calendar unavailable")
    if news:
        agree = [n for n in news if n["score"] * d > 0]
        bits.append(f"{len(news)} headline{'s' if len(news) > 1 else ''} today" + (f", {len(agree)} on our side" if agree else ", none against us"))
    elif news is not None:
        bits.append("no headlines today")
    if crowd and crowd["bull"] + crowd["bear"]:
        bits.append(f"StockTwits {crowd['bull']} bullish / {crowd['bear']} bearish")
    return True, "; ".join(bits) or "quiet news", False


def opening_range(pair, m15, h1a, now_utc):
    """A breakout of today's 9:30–10:00 ET range on a completed 15-min candle, or None."""
    if not ORB or not m15:
        return None
    today = et(now_utc).date()
    day_bars = [x for x in m15 if et(x["t"]).date() == today]
    box = [x for x in day_bars if (9, 30) <= (et(x["t"]).hour, et(x["t"]).minute) < (10, 0)]
    if len(box) < 2:
        return None
    last = m15[-1]
    le = et(last["t"])
    lo, hi = ORB_WINDOW
    if le.date() != today or not (lo <= (le.hour, le.minute) < hi):
        return None
    top, bot = max(x["h"] for x in box), min(x["l"] for x in box)
    size = top - bot
    buf = size * 0.1
    if last["c"] > top + buf:
        d = 1
    elif last["c"] < bot - buf:
        d = -1
    else:
        return None
    body = abs(last["c"] - last["o"]) / max(last["h"] - last["l"], 1e-9)
    return {"time": last["t"], "setup": "opening breakout", "trend": d, "close": last["c"], "atr": size,
            "sl_price": (top + bot) / 2, "box": (bot, top), "size": size, "body": body,
            "h1_trend": h1a["trend"], "h1_adx": h1a["adx"] or 0, "rsi": h1a.get("rsi")}


def orb5(bars, complete):
    """Easter's opening range breakout on one session's completed 5-min bars ({mins,label,o,h,l,c}, ascending).
    Same rules as the IWM Day Signal page: 9:30–10:00 box; 10:00–12:00 a strong candle (60%+ body) closes outside;
    the next candle must stay outside without touching the line and make a new extreme; entry at its close;
    stop past the breakout candles; target 1.5R; stop to breakeven at 1R; one trade a day."""
    r = {"phase": "wait", "status": "", "events": [], "orh": None, "orl": None, "trade": None, "box_bars": 0}
    box = [b for b in bars if 570 <= b["mins"] < 600]
    r["box_bars"] = len(box)
    if box:
        r["orh"], r["orl"] = max(b["h"] for b in box), min(b["l"] for b in box)
    if len(box) < 6:
        r["phase"] = "building"
        r["status"] = "Building the opening range until 10:00 ET" if box else "Waiting for the 9:30 ET open"
        return r
    orh, orl = r["orh"], r["orl"]
    end = ORB5_END[0] * 60 + ORB5_END[1]
    r["status"] = f"Range set. Watching for a strong 5-min close outside it until {ORB5_END[0] % 12 or 12}:{ORB5_END[1]:02d} ET"
    dirn, bH, bL, pos, traded, t, closed_note, pend = 0, 0.0, 0.0, 0, False, None, False, ""

    def ev(b, txt, kind):
        r["events"].append({"t": b["label"], "mins": b["mins"], "txt": txt, "kind": kind})

    for b in bars:
        if b["mins"] < 600:
            continue
        in_win = b["mins"] < end
        sig = 0
        if not traded and pos == 0 and in_win:
            if dirn == -1:
                if b["h"] < orl and b["l"] < bL:
                    sig = -1
                elif b["h"] >= orl:
                    dirn = 0; ev(b, "Breakdown failed: price came back to the range low", "fail")
                else:
                    bH = max(bH, b["h"])
            elif dirn == 1:
                if b["l"] > orh and b["h"] > bH:
                    sig = 1
                elif b["l"] <= orh:
                    dirn = 0; ev(b, "Breakout failed: price came back to the range high", "fail")
                else:
                    bL = min(bL, b["l"])
            if dirn == 0 and not sig:
                rg = b["h"] - b["l"]
                if b["c"] < orl and rg > 0 and b["c"] < b["o"] and (b["o"] - b["c"]) / rg >= ORB5_BODY:
                    dirn, bH, bL = -1, b["h"], b["l"]
                    pend = "Strong close below the range low. Waiting for the next candle to hold below and make a new low"
                    ev(b, pend, "pend")
                elif b["c"] > orh and rg > 0 and b["c"] > b["o"] and (b["c"] - b["o"]) / rg >= ORB5_BODY:
                    dirn, bH, bL = 1, b["h"], b["l"]
                    pend = "Strong close above the range high. Waiting for the next candle to hold above and make a new high"
                    ev(b, pend, "pend")
        if sig:
            pos, traded, dirn = sig, True, 0
            entry = b["c"]
            stop = bH if sig < 0 else bL
            one_r = abs(stop - entry)
            t = {"dir": sig, "entry": entry, "stop0": stop, "stop": stop, "one_r": one_r, "target": entry + sig * ORB5_RR * one_r,
                 "be": False, "result": None, "entry_mins": b["mins"], "entry_label": b["label"], "entry_t": b["t"], "exit_mins": None}
            ev(b, f"{'LONG' if sig > 0 else 'SHORT'} at {entry:.2f}, stop {stop:.2f}, target {t['target']:.2f}", "entry")
            continue
        if not traded and not in_win and not closed_note:
            closed_note, dirn = True, 0
            ev(b, f"No setup by {ORB5_END[0] % 12 or 12}:{ORB5_END[1]:02d} ET. Done for the day", "none")
        if pos != 0:
            sgn = pos
            if (b["h"] >= t["stop"]) if sgn < 0 else (b["l"] <= t["stop"]):
                pos = 0; t["result"] = 0.0 if t["be"] else -1.0; t["exit_mins"] = b["mins"]
                ev(b, "Stopped at breakeven: 0R" if t["be"] else "Stop hit: −1R", "be_exit" if t["be"] else "sl")
            else:
                if not t["be"] and ((b["l"] <= t["entry"] - ORB5_BE_AT * t["one_r"]) if sgn < 0 else (b["h"] >= t["entry"] + ORB5_BE_AT * t["one_r"])):
                    t["be"], t["stop"] = True, t["entry"]
                    ev(b, f"1R reached. Stop moves to breakeven at {t['entry']:.2f}", "be")
                if (b["l"] <= t["target"]) if sgn < 0 else (b["h"] >= t["target"]):
                    pos = 0; t["result"] = ORB5_RR; t["exit_mins"] = b["mins"]
                    ev(b, f"Target hit: +{ORB5_RR:g}R", "tp")
    last = bars[-1] if bars else None
    if pos != 0 and complete and last and last["mins"] >= 955:
        t["result"] = (last["c"] - t["entry"]) * t["dir"] / t["one_r"]; t["exit_mins"] = last["mins"]; pos = 0
        ev(last, f"Closed at 4:00 PM ET: {t['result']:+.1f}R", "eod")
    r["trade"] = t
    if t:
        if t["result"] is None:
            r["phase"] = "long" if t["dir"] > 0 else "short"
            r["status"] = f"{'LONG' if t['dir'] > 0 else 'SHORT'} trade open" + (", stop at breakeven" if t["be"] else "")
        else:
            r["phase"] = "done"; r["status"] = f"Trade finished: {t['result']:+.1f}R"
    elif dirn != 0:
        r["phase"] = "pending"; r["status"] = pend
    elif closed_note:
        r["phase"] = "none"; r["status"] = "No setup today"
    return r


def council_orb5(o, t, risk_ok, risk_why, news_hit, now_utc, pair):
    """The council's take on an ORB entry. The rules already decided the trade; Risk can still veto."""
    v = {}
    size = o["orh"] - o["orl"]
    pct = size / t["entry"] * 100
    ok_size = 0.15 <= pct <= 2.5
    v["The Quant"] = (ok_size, f"opening range is ${size:.2f} ({pct:.2f}% of the price)" + ("" if ok_size else ", too " + ("small" if pct < 0.15 else "wild")))
    v["Risk"] = (False, risk_why) if not risk_ok else (True, "contract and limits OK")
    v["Momentum"] = (True, f"strong {'green' if t['dir'] > 0 else 'red'} 5-min close outside the range, then the next candle held and made a new {'high' if t['dir'] > 0 else 'low'}")
    e = et(now_utc)
    v["Time"] = ((10, 0) <= (e.hour, e.minute) <= (12, 15), f"entry at {t['entry_label']} ET, inside the 10:00–12:00 window")
    v["Doubt"] = (news_hit is None, f"big news right now: {news_label(news_hit)}" if news_hit else f"stop ${abs(t['entry'] - t['stop0']):.2f} away, target 1.5× that")
    return v


def orb5_panel(st):
    """Today's opening ranges: a chart for each ticker with a signal or a breakout watch, one line for the rest."""
    oa = st.get("orb5") or {}
    if not oa.get("date") or not oa.get("pairs"):
        return ""
    e = html.escape

    def chart(o, t):
        cs = o.get("candles") or []
        if not cs or not o.get("orh"):
            return ""
        W, H, pad, LBL = 420, 190, 8, 104
        hi = max([c[2] for c in cs] + [o["orh"]] + ([t["target"], t["stop0"]] if t else []))
        lo = min([c[3] for c in cs] + [o["orl"]] + ([t["target"], t["stop0"]] if t else []))
        sp = (hi - lo) or 1
        y = lambda v: pad + (hi - v) / sp * (H - 2 * pad)
        n = len(cs); step = (W - LBL) / max(n, 1); bw = max(2, step * 0.6)
        parts = []
        for i, (lab, op, h, l, c) in enumerate(cs):
            x = 4 + step * (i + 0.5); col = "#45e39b" if c >= op else "#ff6378"
            parts.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{y(h):.1f}" y2="{y(l):.1f}" stroke="{col}"/>'
                         f'<rect x="{x - bw / 2:.1f}" y="{y(max(op, c)):.1f}" width="{bw:.1f}" height="{max(1, y(min(op, c)) - y(max(op, c))):.1f}" fill="{col}"/>')
        lines = [("Range high", o["orh"], "#8fb0d6"), ("Range low", o["orl"], "#8fb0d6")]
        if t:
            lines += [("Target", t["target"], "#45e39b"), ("Entry" + (" = stop" if t.get("be") else ""), t["entry"], "#c9cde4")]
            if not t.get("be"):
                lines.append(("Stop", t["stop"], "#ff6378"))
        last_y = -99
        for name, v, col in sorted(lines, key=lambda z: -z[1]):
            ly = y(v); ty = max(ly + 4, last_y + 13); last_y = ty
            parts.append(f'<line x1="0" x2="{W - LBL + 2}" y1="{ly:.1f}" y2="{ly:.1f}" stroke="{col}" stroke-dasharray="5 4"/>'
                         f'<text x="{W - LBL + 6}" y="{ty:.1f}" fill="{col}" font-size="10.5">{name} {v:.2f}</text>')
        return f'<svg viewBox="0 0 {W} {H + 6}" width="100%" role="img" aria-label="5-minute candles with the opening range">{"".join(parts)}</svg>'

    big, small = [], []
    for pair, o in sorted(oa["pairs"].items()):
        t = o.get("trade")
        if t or o.get("phase") == "pending":
            tr = ""
            if t:
                res = "" if t.get("result") is None else f" · result {t['result']:+.1f}R"
                tr = (f'<p><b>{"CALLS" if t["dir"] > 0 else "PUTS"}</b> from {t["entry"]:.2f} · stop {t["stop"]:.2f}'
                      f'{" (breakeven)" if t.get("be") else ""} · target {t["target"]:.2f}{res}</p>')
            rows = "".join(f'<li><span class="t">{e(x["t"])}</span> {e(x["txt"])}</li>' for x in o.get("events", []))
            big.append(f'<h3>{e(pair)} · range {o["orl"]:.2f}–{o["orh"]:.2f} · now {o.get("last") or 0:.2f}</h3>'
                       f'<p class="fine">{e(o.get("status", ""))}</p>{chart(o, t)}{tr}<ul class="fine">{rows}</ul>')
        else:
            rng = f'{o["orl"]:.2f}–{o["orh"]:.2f}' if o.get("orh") else "building"
            small.append(f'<tr><td>{e(pair)}</td><td>{rng}</td><td>{o.get("last") or 0:.2f}</td><td>{e(o.get("status", ""))}</td></tr>')
    tbl = (f'<div class="tbl"><table><tr><th>Ticker</th><th>Range</th><th>Now</th><th>Status</th></tr>{"".join(small)}</table></div>'
           if small else "")
    return f'<section><h2>Opening range breakout · {e(oa["date"])}</h2>{"".join(big)}{tbl}</section>'


def council_orb(b, risk_ok, risk_why, now_utc, pair, inf=None):
    d, v = b["trend"], {}
    pct = b["size"] / b["close"] * 100
    ok_size = 0.15 <= pct <= 2.5
    v["The Quant"] = (ok_size, f"opening range is {pct:.2f}% of the price" + ("" if ok_size else " (too " + ("small" if pct < 0.15 else "wild") + ")"))
    v["Risk"] = (False, risk_why) if not risk_ok else (True, "contract and limits OK")
    v["Momentum"] = (b["body"] >= 0.5, f"breakout candle {'closed strong' if b['body'] >= 0.5 else 'has a long wick'} ({b['body']:.0%} body)")
    v["Time"] = (in_session(pair, now_utc), "first hour of the session" if in_session(pair, now_utc) else "outside trading hours")
    against = b["h1_trend"] != d and b["h1_adx"] >= 25
    v["Doubt"] = (not against, "1-hour trend is strongly against it" if against else "1-hour trend isn't fighting it")
    return v


def tally(votes):
    return (sum(1 for ok, _ in votes.values() if ok is True), sum(1 for ok, _ in votes.values() if ok is False))


def voter_weights(st):
    """Voters who call it right more often get a louder vote (after LEARN_MIN_CALLS scored calls)."""
    w = {}
    for n in VOTERS:
        b = (st or {}).get("scoreboard", {}).get(n, {"right": 0, "total": 0})
        if b["total"] < LEARN_MIN_CALLS:
            w[n] = 1.0
            continue
        acc = (b["right"] + 2) / (b["total"] + 4)           # shrunk toward 50% so a lucky streak can't dominate
        w[n] = round(max(0.3, min(2.0, 1 + (acc - 0.5) * 4)), 2)
    return w


def approved(votes, weights=None):
    yes, _ = tally(votes)
    if yes < 3 or votes["Risk"][0] is not True:            # Risk is always a veto
        return False
    w = weights or {}
    ys = sum(w.get(n, 1.0) for n, (ok, _) in votes.items() if ok is True)
    ns = sum(w.get(n, 1.0) for n, (ok, _) in votes.items() if ok is False)
    return ys / (ys + ns) >= COUNCIL_MIN_SHARE - 1e-9


def record_result(st, pair, setup, r_mult, approved_trade=False):
    """Track each pair + setup combo in R; bench combos that keep losing, un-bench them when they recover."""
    L = st.setdefault("learn", {})
    oc = L.setdefault("outcomes", [])
    oc.append({"t": time.time(), "approved": approved_trade, "r": round(r_mult, 3), "pair": pair, "setup": setup})
    del oc[:-400]
    combo = f"{pair}:{setup}"
    xs = L.setdefault("combos", {}).setdefault(combo, [])
    xs.append(round(r_mult, 3))
    del xs[:-30]
    recent = xs[-BENCH_MIN:]
    if len(recent) < BENCH_MIN:
        return None
    avg = sum(recent) / len(recent)
    benched = L.setdefault("benched", {})
    note = None
    if avg < 0 and combo not in benched:
        benched[combo] = avg
        note = (f"🧠 Learned: {NAMES[pair]} {setup} setups averaged {avg:+.2f}R over the last {BENCH_MIN}. "
                f"Benching them. I'll keep scoring them on paper and bring them back if they recover.")
    elif avg >= 0.1 and combo in benched:
        benched.pop(combo)
        note = f"🧠 Learned: {NAMES[pair]} {setup} setups recovered ({avg:+.2f}R over the last {BENCH_MIN}). Back in the rotation."
    if note:
        L.setdefault("log", []).append({"t": time.time(), "text": note})
        L["log"] = L["log"][-40:]
        feed_add(st, "event", "Learning", who="Doubt", text=note.replace("🧠 Learned: ", ""))
    return note


def load_news(st, log):
    cal = st.get("news") or {}
    if time.time() - cal.get("fetched", 0) > NEWS_REFRESH_HOURS * 3600:
        try:
            evs = []
            for e in http("GET", NEWS_URL):
                if (e.get("impact") or "").lower() != "high":
                    continue
                try:
                    t = dt.datetime.fromisoformat(e["date"]).timestamp()
                except (KeyError, ValueError):
                    continue
                evs.append({"title": e.get("title", ""), "cur": e.get("country", ""), "t": t})
            st["news"] = {"fetched": time.time(), "events": evs}
        except Exception as ex:
            log.append(f"News calendar: unavailable this run ({ex})")
    return st.get("news", {}).get("events", [])


def news_label(e):
    return f"{e['cur']} {e['title']} ({dt.datetime.fromtimestamp(e['t'], LOCAL_TZ):%a %I:%M %p} ET)"


def pair_currencies(pair):
    return {"USD"}                              # every stock reacts to big US releases


def day_stats(st, nav=None):
    d = dt.datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    days = st.setdefault("days", {})
    for k in sorted(days)[:-14]:
        days.pop(k, None)
    rec = days.setdefault(d, {"start_nav": nav, "opened": 0, "setups": 0, "approved": 0, "rejected": 0, "news": 0, "benched": 0})
    if rec.get("start_nav") is None and nav is not None:
        rec["start_nav"] = nav
    return rec


# ───────────────────────────── Choosing the contract ─────────────────────────

def fmt_price(x, prec=2):
    return f"{x:.{prec}f}"


def tick_up(p):
    step = 0.05 if p >= 3 else 0.01
    return math.ceil(p / step - 1e-9) * step


def her_exits(api, st, meta, bid, tg, log):
    """Easter's exit rules. The stop (−OPT_STOP) and the end-of-day sale are handled by the main loop."""
    name, entry = contract_name(meta["sym"]), meta["entry"]
    if not meta.get("locked") and bid >= entry * (1 + LOCK_PROFIT_AT):
        meta["locked"] = True
        meta["sl"] = max(meta["sl"], round(entry * (1 + LOCK_PROFIT_AT), 2))
        if meta.get("qty0", meta["qty"]) < 2:
            meta["trail"], meta["peak"] = True, bid
        st["_changed"] = True
        feed_add(st, "event", f"{meta['pair']} profit locked", who="Risk", ok=True,
                 text=f"{name} is up {LOCK_PROFIT_AT:.0%}. Stop raised to ${meta['sl']:.2f}, so this trade can't lose now.")
        if tg:
            tg.say(f"🔐 {name} is up {LOCK_PROFIT_AT:.0%}. Stop raised to ${meta['sl']:.2f} (locks +{LOCK_PROFIT_AT:.0%})"
                   + (". Half comes off at +50%." if meta["qty"] >= 2 else " and now follows the price up (25% below its best).")
                   + f"\n\n👉 Robinhood: if you copied it, move your stop to ${meta['sl']:.2f}.")
    if meta.get("locked") and meta["qty"] >= 2 and not meta.get("half") and bid >= entry * (1 + HALF_AT):
        n = meta["qty"] // 2
        try:
            res = api.close(meta["sym"], n)
            o = wait_fill(api, res["id"]) if res.get("id") else {}
            px = float(o.get("filled_avg_price") or 0) or bid
        except ApiError as e:
            log.append(f"{meta['sym']}: couldn't sell half ({e})")
            return
        banked = round((px - entry) * 100 * n, 2)
        meta.update(half=True, qty=meta["qty"] - n, tp=round(entry * (1 + HALF_TARGET), 2), tp_on=True)
        meta["banked"] = round(meta.get("banked", 0) + banked, 2)
        meta["sold_early"] = meta.get("sold_early", 0) + n
        st["record"]["pl"] = round(st["record"]["pl"] + banked, 2)
        st["_changed"] = True
        feed_add(st, "event", f"{meta['pair']} took profit", who="Risk", ok=True,
                 text=f"Sold {n} of {name} at ${px:.2f} ({money(banked)}). The rest aims for ${meta['tp']:.2f}.")
        if tg:
            tg.say(f"💰 TOOK PROFIT on {name}: sold {n} at ${px:.2f} ({money(banked)}). The other {meta['qty']} aims for "
                   f"${meta['tp']:.2f} (+{HALF_TARGET:.0%}); stop stays at ${meta['sl']:.2f}.\n\n"
                   f"👉 Robinhood: if you copied it, sell {n} now and set a limit sell at ${meta['tp']:.2f} on the rest.")


def first_price_today(api, sym, now_utc):
    """The option's first traded price today (None if Alpaca has no bars for it yet)."""
    e = et(now_utc)
    start = dt.datetime(e.year, e.month, e.day, 9, 30, tzinfo=LOCAL_TZ).astimezone(dt.timezone.utc)
    try:
        j = api.d("/v1beta1/options/bars", {"symbols": sym, "timeframe": "1Min", "limit": 5,
                                            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ")})
        bars = (j.get("bars") or {}).get(sym) or []
        return float(bars[0]["o"]) if bars else None
    except (ApiError, KeyError, ValueError):
        return None


def gap_fade_holds():
    """Contracts the Gap Fade bot holds (same Alpaca account): never buy those, so neither bot sells the other's."""
    try:
        t = json.load(open("gap_state.json", encoding="utf-8")).get("trade")
        return {t["sym"]} if t else set()
    except (OSError, ValueError):
        return set()


def pick_contract(api, pair, d, price, budget, now_utc):
    """Best call (d=1) or put (d=-1) that fits the budget. Returns (contract dict, None) or (None, reason)."""
    e = et(now_utc)
    today = e.date()
    zero = pair in ZERO_DTE and (e.hour, e.minute) < ZERO_DTE_UNTIL
    if zero:
        lo_d, hi_d = today, today
    else:
        lo_d, hi_d = today + dt.timedelta(days=WEEKLY_DAYS[0]), today + dt.timedelta(days=WEEKLY_DAYS[1])
    kind = "call" if d == 1 else "put"
    try:
        chain = api.chain(pair, kind, lo_d.isoformat(), hi_d.isoformat())
    except ApiError as ex:
        return None, f"no option quotes ({ex})"
    if not chain:
        return None, "no contracts in the expiration window"
    fits, cheapest = [], None
    for sym, c in chain.items():
        bid, ask = c["bid"], c["ask"]
        if bid <= 0 or ask <= 0 or ask < bid:
            continue
        mid = (bid + ask) / 2
        spread = (ask - bid) / mid
        delta = abs(c["delta"]) if c.get("delta") is not None else None
        dist = (c["strike"] - price) / price * d          # > 0 means out of the money
        if delta is not None:
            if not (DELTA_RANGE[0] <= delta <= DELTA_RANGE[1]):
                continue
        elif not (-0.01 <= dist <= 0.04):
            continue
        if spread > MAX_SPREAD_PCT:
            continue
        cost = ask * 100
        if cheapest is None or cost < cheapest:
            cheapest = cost
        if cost > budget:
            continue
        fits.append((sym, c, delta if delta is not None else 0.5 - dist * 5, spread))
    if not fits:
        if cheapest is not None:
            return None, f"cheapest good {kind} costs ${cheapest:,.0f}, over the ${budget:,.0f} budget"
        return None, f"no {kind} with a tight enough spread"
    fits.sort(key=lambda x: (-x[2], x[1]["exp"], x[3]))          # closest to the money that fits, nearest expiry
    sym, c, delta, spread = fits[0]
    qty = max(1, min(MAX_CONTRACTS, int(budget // (c["ask"] * 100))))
    return {"sym": sym, "bid": c["bid"], "ask": c["ask"], "delta": c.get("delta"), "spread": spread, "qty": qty,
            "exp": c["exp"], "strike": c["strike"], "type": c["type"], "zero": zero}, None


def pick_spread(api, pair, d, price, target, budget, now_utc):
    """Debit spread for when a single contract is too pricey: buy the closest-to-the-money contract and sell the one
    nearest the ORB target (same expiration). Returns (spread dict, None) or (None, reason)."""
    e = et(now_utc)
    today = e.date()
    zero = pair in ZERO_DTE and (e.hour, e.minute) < ZERO_DTE_UNTIL
    lo_d, hi_d = (today, today) if zero else (today + dt.timedelta(days=WEEKLY_DAYS[0]), today + dt.timedelta(days=WEEKLY_DAYS[1]))
    kind = "call" if d == 1 else "put"
    try:
        chain = api.chain(pair, kind, lo_d.isoformat(), hi_d.isoformat())
    except ApiError as ex:
        return None, f"no option quotes ({ex})"
    by_exp = {}
    for sym, c in chain.items():
        if c["bid"] > 0 and c["ask"] >= c["bid"]:
            by_exp.setdefault(c["exp"], []).append((sym, c))
    best, cheapest = None, None
    for exp, legs in sorted(by_exp.items()):
        for lsym, lc in legs:
            ldelta = abs(lc["delta"]) if lc.get("delta") is not None else None
            dist = (lc["strike"] - price) / price * d
            if (ldelta is not None and not (0.35 <= ldelta <= 0.65)) or (ldelta is None and not (-0.01 <= dist <= 0.01)):
                continue
            shorts = [(ssym, sc) for ssym, sc in legs if (sc["strike"] - lc["strike"]) * d > 0]
            if not shorts:
                continue
            ssym, sc = min(shorts, key=lambda x: (abs(x[1]["strike"] - target), (x[1]["strike"] - lc["strike"]) * d))
            debit = lc["ask"] - sc["bid"]
            if debit <= 0.02:
                continue
            gaps = (lc["ask"] - lc["bid"]) + (sc["ask"] - sc["bid"])
            if gaps / debit > SPREAD_MAX_LEG_SPREAD:
                continue
            cost = debit * 100
            cheapest = cost if cheapest is None else min(cheapest, cost)
            if cost > budget:
                continue
            key = (ldelta if ldelta is not None else 0.5, -exp_rank(exp, today))
            if best is None or key > best[0]:
                best = (key, lsym, lc, ssym, sc, debit, gaps / debit)
    if not best:
        if cheapest is not None:
            return None, f"cheapest {kind} spread costs ${cheapest:,.0f}, over the ${budget:,.0f} budget"
        return None, f"no {kind} spread with tight enough prices"
    _, lsym, lc, ssym, sc, debit, gap = best
    qty = max(1, min(MAX_CONTRACTS, int(budget // (debit * 100))))
    return {"sym": lsym, "short": ssym, "bid": lc["bid"] - sc["ask"], "ask": round(debit, 2), "delta": lc.get("delta"),
            "spread": gap, "qty": qty, "exp": lc["exp"], "strike": lc["strike"], "short_strike": sc["strike"],
            "type": lc["type"], "zero": zero, "width": abs(sc["strike"] - lc["strike"])}, None


def exp_rank(exp, today):
    return (dt.date.fromisoformat(exp) - today).days


def pick_name(pick):
    """'SPY Oct 29 $770 call' or 'AMZN Oct 31 $250/$255 call spread'."""
    if pick.get("short"):
        e = dt.date.fromisoformat(pick["exp"])
        return f"{pick['sym'][:len(pick['sym']) - 15]} {e:%b} {e.day} ${pick['strike']:g}/${pick['short_strike']:g} {pick['type']} spread"
    return contract_name(pick["sym"])


# ───────────────────────────── State ─────────────────────────────────────────

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    for k, v in (("trades", {}), ("closed", []), ("shadow", []), ("last_bar", {}), ("scoreboard", {}),
                 ("council_record", {"right": 0, "total": 0}), ("record", {"wins": 0, "losses": 0, "pl": 0.0}),
                 ("pairs", {}), ("tg_offset", 0)):
        st.setdefault(k, v)
    return st


def save_state(st):
    st["closed"] = st["closed"][-60:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1, sort_keys=True)


def score(st, votes, won, was_trade):
    for name, ok in votes.items():
        if ok is None:
            continue
        b = st["scoreboard"].setdefault(name, {"right": 0, "total": 0})
        b["total"] += 1
        if ok == won:
            b["right"] += 1
    cr = st["council_record"]
    cr["total"] += 1
    right = (was_trade and won) or (not was_trade and not won)
    if right:
        cr["right"] += 1
    st.setdefault("history", []).append({"t": time.time(), "trade": was_trade, "won": won, "right": right})
    st["history"] = st["history"][-400:]


# ───────────────────────────── Telegram ──────────────────────────────────────

BOT_TITLE = 'Vault Options'
BOT_COMMAND_GROUPS = [
    ('📊 Check in', [
        ('status', "Bankroll, open options and today's results"),
        ('help', 'This list of commands'),
    ]),
    ('🎛 Control', [
        ('pause', 'Stop opening new trades'),
        ('resume', 'Start trading again'),
        ('closeall', 'Sell every open option and pause'),
        ('testtrade', 'Buy and sell one cheap SPY option to test'),
    ]),
    ('🏛 Council', [
        ('live', 'Turn the live council debate on or off'),
        ('happy', "Tell the council you're happy (cheers!)"),
        ('unhappy', "Tell the council you're not happy"),
    ]),
]
def help_text():
    lines = [f"📟 {BOT_TITLE} commands", ""]
    for group, cmds in BOT_COMMAND_GROUPS:
        lines.append(group)
        lines += [f"/{c} – {d}" for c, d in cmds]
        lines.append("")
    lines.append("Tip: tap the Menu button next to the message box to pick a command.")
    return "\n".join(lines)


def sync_menu(tg, st):
    """Keep Telegram's Menu button in sync with the commands this bot understands."""
    cmds = [{"command": c, "description": d[:60]} for _, group in BOT_COMMAND_GROUPS for c, d in group]
    sig = json.dumps(cmds, sort_keys=True)
    if st.get("menu_sig") == sig:
        return
    try:
        tg.api("setMyCommands", commands=cmds)
        st["menu_sig"] = sig
    except Exception:
        pass


class Telegram:
    def __init__(self, token, st):
        self.token, self.st, self.out = token, st, []

    def api(self, method, **params):
        return http("POST", f"https://api.telegram.org/bot{self.token}/{method}", body=params, tries=2)

    def chat(self):
        return os.environ.get("TELEGRAM_CHAT_ID", "").strip() or self.st.get("chat_id")

    def commands(self):
        """Return command words sent from your chat since the last run."""
        cmds = []
        try:
            res = self.api("getUpdates", offset=self.st.get("tg_offset", 0), timeout=0).get("result", [])
        except ApiError:
            return cmds
        for u in res:
            self.st["tg_offset"] = u["update_id"] + 1
            msg = u.get("message") or {}
            cid = str(msg.get("chat", {}).get("id", ""))
            if not cid:
                continue
            if not self.chat():
                self.st["chat_id"] = cid
            if cid != str(self.chat()):
                continue                       # ignore anyone else who finds the bot
            text = (msg.get("text") or "").strip().lower().split("@")[0]
            if text.startswith("/"):
                cmds.append(text)
        return cmds

    def say(self, text):
        self.out.append(text)

    def flush(self, log):
        cid = self.chat()
        if not cid or not self.out:
            if self.out and not cid:
                log.append("Telegram: no chat yet. Send your bot any message.")
            return
        sent = 0
        for text in self.out:
            try:
                self.api("sendMessage", chat_id=cid, text=text, disable_web_page_preview=True)
                sent += 1
                time.sleep(0.4)
            except ApiError as e:
                log.append(f"Telegram: send failed ({e})")
                break
        log.append(f"Telegram: sent {sent} message(s)")


def money(x):
    return f"{'+' if x >= 0 else '−'}${abs(x):,.2f}"


# ───────────────────────────── One run ───────────────────────────────────────

def plan(st, now=None):
    """'full' = scan for new trades, 'manage' = only look after open trades, 'skip' = nothing to do."""
    now = now or time.time()
    if now - st.get("last_full_run", 0) >= FULL_SCAN_MIN * 60 - 90:
        return "full"
    return "manage" if st.get("trades") else "skip"


def next_weekday(d):
    d += dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d


def must_sell_today(meta, today_s):
    """0DTE (or anything when holding is off) goes by the end of today; weeklies go the trading day before they expire."""
    exp = meta.get("exp")
    if not HOLD_WEEKLIES or not exp or exp <= today_s:
        return True
    return next_weekday(dt.date.fromisoformat(today_s)) >= dt.date.fromisoformat(exp)


def sell_by_text(exp, zero):
    if zero or not HOLD_WEEKLIES:
        return "Out by 3:40 PM today no matter what."
    last = dt.date.fromisoformat(exp) - dt.timedelta(days=1)
    while last.weekday() >= 5:
        last -= dt.timedelta(days=1)
    return f"Can be held overnight. Sold by 3:40 PM {last:%a %b} {last.day} at the latest (the day before it expires)."


def take_partial(api, st, meta, bid, now, tg, log):
    """At +PARTIAL_AT: bank half (or lock a small profit on 1 contract) and let the rest run on a trailing stop."""
    name = contract_name(meta["sym"])
    meta["partial"] = True
    meta["trail"] = True
    meta["breakeven"] = True
    meta["peak"] = bid
    meta["tp"] = round(meta["entry"] * (1 + RUNNER_TARGET), 2)
    if meta["qty"] >= 2:
        n = meta["qty"] // 2
        try:
            res = api.close(meta["sym"], n)
            o = wait_fill(api, res["id"]) if res.get("id") else {}
            px = float(o.get("filled_avg_price") or 0) or bid
        except ApiError as e:
            log.append(f"{meta['sym']}: couldn't sell half ({e})")
            meta["partial"] = False; meta["trail"] = False
            return
        banked = round((px - meta["entry"]) * 100 * n, 2)
        meta["qty"] -= n
        meta["banked"] = round(meta.get("banked", 0) + banked, 2)
        meta["sold_early"] = meta.get("sold_early", 0) + n
        st["record"]["pl"] = round(st["record"]["pl"] + banked, 2)       # booked now so the bankroll shows it
        meta["sl"] = meta["entry"]
        st["_changed"] = True
        feed_add(st, "event", f"{meta['pair']} took profit", who="Risk", ok=True,
                 text=f"Sold {n} of {name} at ${px:.2f} ({money(banked)}). The rest rides with the stop at entry.")
        log.append(f"{meta['pair']}: sold {n} of {meta['sym']} at {px:.2f} (+{PARTIAL_AT:.0%} partial)")
        if tg:
            tg.say(f"💰 TOOK PROFIT on {name}: sold {n} of {n + meta['qty']} at ${px:.2f} ({money(banked)})\n"
                   f"The other {meta['qty']} keeps running: stop moved to ${meta['entry']:.2f} (can't lose now), "
                   f"aiming for ${meta['tp']:.2f}, with a stop that follows the price up.\n\n"
                   f"👉 Robinhood: if you copied it, sell {n} of your {n + meta['qty']} contracts now (limit near ${px:.2f}) "
                   f"and set a stop at ${meta['entry']:.2f} on the rest.")
    else:
        meta["sl"] = round(meta["entry"] * (1 + LOCK_AT), 2)
        st["_changed"] = True
        feed_add(st, "event", f"{meta['pair']} profit locked", who="Risk", ok=True,
                 text=f"{name} is up {PARTIAL_AT:.0%}. Stop raised to ${meta['sl']:.2f} (+{LOCK_AT:.0%}) and it now trails the price.")
        if tg:
            tg.say(f"🔐 {name} is up {PARTIAL_AT:.0%}. Locked in a profit: stop raised to ${meta['sl']:.2f} (+{LOCK_AT:.0%}). "
                   f"Letting it run toward ${meta['tp']:.2f} with a stop that follows the price up.\n\n"
                   f"👉 Robinhood: if you copied it, set a stop at ${meta['sl']:.2f}.")


def close_trade(api, st, tid, meta, why, price_hint, now, tg, log):
    """Sell the contract at market and book the result."""
    try:
        if meta.get("short"):
            try:
                res = api.close_spread(meta["sym"], meta["short"], meta["qty"])
                o = wait_fill(api, res["id"]) if res.get("id") else {}
                exit_px = abs(float(o.get("filled_avg_price") or 0)) or price_hint
            except ApiError as e1:                         # fall back to closing each leg on its own
                log.append(f"{meta['sym']}: spread close order failed ({e1}), closing legs one by one")
                r1 = api.close(meta["sym"]); o1 = wait_fill(api, r1["id"]) if r1.get("id") else {}
                r2 = api.close(meta["short"]); o2 = wait_fill(api, r2["id"]) if r2.get("id") else {}
                p1, p2 = float(o1.get("filled_avg_price") or 0), float(o2.get("filled_avg_price") or 0)
                exit_px = round(p1 - p2, 2) if p1 else price_hint
        else:
            res = api.close(meta["sym"])
            o = wait_fill(api, res["id"]) if res.get("id") else {}
            exit_px = float(o.get("filled_avg_price") or 0) or price_hint
    except ApiError as e:
        if "position" in str(e).lower() and "not found" in str(e).lower():
            exit_px = price_hint                            # already gone (expired or closed by hand)
        else:
            log.append(f"{meta['sym']}: couldn't close ({e})")
            if tg:
                tg.say(f"⚠️ Couldn't sell {contract_name(meta['sym'])}: {e}\nPlease close it in the app.")
            return False
    nm = meta.get("name") or contract_name(meta["sym"])
    rest = round((exit_px - meta["entry"]) * 100 * meta["qty"], 2)
    pl = round(rest + meta.get("banked", 0), 2)                    # includes any half sold early
    won = pl > 0
    meta.update(pl=pl, exit=exit_px, why=why, closed=now.isoformat()[:19], closed_ts=now.timestamp())
    st["closed"].append(meta)
    st["_changed"] = True
    rec = st["record"]; rec["wins" if won else "losses"] += 1; rec["pl"] = round(rec["pl"] + rest, 2)   # the early half was booked already
    score(st, meta.get("votes", {}), won, True)
    meta["r"] = round(pl / meta["risk"], 2) if meta.get("risk") else 0
    for note_ in streak_update(st, meta["r"], now.timestamp()):
        if tg:
            tg.say(note_)
    feed_add(st, "event", f"{meta['pair']} closed", who="The Quant", ok=won,
             text=f"{nm} sold at ${exit_px:.2f} ({why}): {money(pl)} ({meta['r']:+.2f}R). "
                  f"Record {rec['wins']}W–{rec['losses']}L.")
    note = record_result(st, meta["pair"], meta.get("setup", "pullback"), meta["r"], approved_trade=True)
    if note and tg:
        tg.say(note)
    st["trades"].pop(tid, None)
    log.append(f"{meta['pair']}: CLOSED {meta['sym']} ({why}) {money(pl)}")
    if tg:
        icon = {"target": "🎯", "stop": "🛑", "breakeven": "🔒", "end of day": "🔔", "expired": "⌛", "trailing stop": "📈"}.get(why, "⚪")
        early = (f" (plus {meta['sold_early']} sold earlier for {money(meta['banked'])})" if meta.get("sold_early") else "")
        tg.say(f"{'✅' if won else '❌'} SOLD {nm} ({icon} {why})\n"
               f"Bought ${meta['entry']:.2f} → sold ${exit_px:.2f} × {meta['qty']}{early}\n"
               f"Result: {money(pl)}  ·  Record {rec['wins']}W–{rec['losses']}L, total {money(rec['pl'])}\n\n"
               f"👉 Robinhood: if you copied it, close your {nm} now (limit near ${exit_px:.2f}).")
    return True


def run(log, full=True):
    key, secret = os.environ.get("ALPACA_KEY_ID", "").strip(), os.environ.get("ALPACA_SECRET_KEY", "").strip()
    st = load_state()
    now = dt.datetime.now(dt.timezone.utc)
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    tg = Telegram(tg_token, st) if tg_token else None
    st["mode"] = ALPACA_ENV
    st["last_run"] = now.timestamp()
    if full:
        st["last_full_run"] = now.timestamp()
    if not key or not secret:
        log.append("ALPACA: add ALPACA_KEY_ID and ALPACA_SECRET_KEY secrets to start trading")
        save_state(st)
        return st
    api = Alpaca(key, secret)

    # 0) clock, account, phone commands
    try:
        ck = api.clock()
        CLOCK.update(is_open=ck.get("is_open"), next_open=ck.get("next_open"), next_close=ck.get("next_close"),
                     checked=now.timestamp())
    except ApiError as e:
        log.append(f"Clock unavailable ({e})")
    try:
        acc = api.account()
    except ApiError as e:
        bad_keys = "401" in str(e) or "403" in str(e)
        st["alpaca_error"] = {"t": now.timestamp(), "keys": bad_keys, "text": str(e)[:200]}
        log.append(f"ALPACA: couldn't open the account ({e})")
        if tg and now.timestamp() - st.get("alpaca_alerted", 0) > 3 * 3600:
            st["alpaca_alerted"] = now.timestamp()
            tg.say("⚠️ Vault Options can't log in to Alpaca, so it isn't trading.\n\n" + (
                "Alpaca says the keys are wrong (401). Make new PAPER keys in Alpaca (switch to your Paper account first), "
                "then paste them into the repo's ALPACA_KEY_ID and ALPACA_SECRET_KEY secrets."
                if bad_keys else f"Alpaca error: {str(e)[:150]}. I'll keep retrying."))
            tg.flush(log)
        save_state(st)
        return st
    st.pop("alpaca_error", None)
    if START_BALANCE and st.get("start_balance") != START_BALANCE:
        st.update(start_balance=START_BALANCE, peak=START_BALANCE, day={}, equity=[],
                  record={"wins": 0, "losses": 0, "pl": 0.0})
    if st.get("strategy") != STRATEGY:            # new strategy = fresh paper bankroll; the old record is kept for reference
        st.setdefault("old_records", []).append({"strategy": st.get("strategy") or "council setups", "record": st.get("record"),
                                                 "until": now.isoformat()[:19]})
        st.update(strategy=STRATEGY, record={"wins": 0, "losses": 0, "pl": 0.0}, peak=START_BALANCE, paused=None,
                  equity=[], day={}, streak={"losses": 0, "half": False, "wins_since": 0, "until": 0})
        st["_strategy_switched"] = True
    bal = START_BALANCE + st["record"]["pl"]
    unreal = sum(m.get("upl", 0) for m in st["trades"].values())
    nav = bal + unreal
    st["account"] = {"nav": nav, "balance": bal, "unrealized": unreal, "broker_equity": float(acc.get("equity", 0) or 0),
                     "options_level": acc.get("options_trading_level"), "updated": now.timestamp()}
    today_s = et(now).strftime("%Y-%m-%d")
    if st.get("day", {}).get("date") != today_s:
        st["day"] = {"date": today_s, "start_nav": nav, "halted": False}
    st["peak"] = max(st.get("peak", nav), nav)

    if tg:
        sync_menu(tg, st)
        for full_cmd in tg.commands():
            cmd, _, arg = full_cmd.partition(" ")
            cmd = cmd.split("@")[0].lower()
            if cmd in ("/help", "/commands"):
                tg.say(help_text())
                continue
            if cmd == "/live":
                on = {"on": True, "off": False}.get(arg.strip(), not st.get("live_tg"))
                st["live_tg"] = on
                tg.say("🎙 Live council is ON. You'll get every debate, including rejected setups. /live off to stop."
                       if on else "🔇 Live council is OFF. You'll still get trades, closes and reports.")
            elif cmd == "/pause":
                feed_add(st, "event", "Paused", who="Chair", text="You paused new trades. Open trades are still managed.")
                st["paused"] = "by you"; tg.say("⏸ Paused. No new trades. Open ones are still managed (stops and targets). /resume to restart.")
            elif cmd == "/resume":
                st["paused"] = None; st["peak"] = nav; tg.say("▶️ Resumed. Back to normal trading.")
                if st.get("streak", {}).get("until", 0) > time.time():
                    st["streak"]["until"] = 0
                    tg.say("🧊 Timeout ended early. Still trading at half size until 2 wins.")
                feed_add(st, "event", "Resumed", who="Chair", text="You resumed trading. Back to normal.")
            elif cmd == "/closeall":
                st["_closeall"] = True
                st["paused"] = "after /closeall"
            elif cmd == "/testtrade":
                st["test_pending"] = True
                tg.say("🧪 Got it. When the market is open I'll buy 1 very cheap SPY call on the practice account, sell it right away, and report back.")
            elif cmd in ("/happy", "/unhappy") or (cmd == "/start" and arg.strip() in ("happy", "unhappy")):
                good = cmd == "/happy" or arg.strip() == "happy"
                mood = st.setdefault("mood", {})
                m = mood.setdefault(today_s, {"happy": 0, "unhappy": 0})
                m["happy" if good else "unhappy"] += 1
                for k_ in sorted(mood)[:-14]:
                    mood.pop(k_, None)
                feed_add(st, "event", "Commander's feedback", who="Chair", ok=good,
                         text="The commander is happy. The council raises a glass! 🥂" if good else
                              "The commander isn't happy. Noted. We'll keep it disciplined and earn it back.")
                tg.say("🥂 Cheers from the council!" if good else "😔 Heard you, Commander. Noted for the daily report.")
            elif cmd in ("/status", "/start"):
                st["_status_requested"] = True
        if not st.get("welcomed") and tg.chat():
            tg.say(f"✅ Vault Options is connected ({ALPACA_ENV.upper()} account, trading a ${nav:,.0f} bankroll).\n\n"
                   f"Watchlist: {', '.join(PAIRS)}. Every 5 minutes during market hours the council looks for setups and, "
                   "when it approves, I buy a CALL (bullish) or PUT (bearish). 0DTE on SPY/QQQ/IWM before 1:30 PM, "
                   "weeklies otherwise. 0DTE is sold by 3:40 PM; weeklies can be held overnight and are sold the day before they expire.\n\n"
                   "Each BUY text has the exact contract so you can copy it on Robinhood.\n\n"
                   "Commands:\n/status – bankroll and open trades\n/pause – stop new trades\n/resume – start again\n"
                   "/closeall – sell everything now\n/live – every council debate on or off\n"
                   "/testtrade – buy and sell one tiny practice contract to prove orders work")
            st["welcomed"] = True
        if tg and not st.get("bot_username"):
            try:
                st["bot_username"] = tg.api("getMe").get("result", {}).get("username")
            except ApiError:
                pass

    # 1) safety limits
    if nav <= st["day"]["start_nav"] * (1 - DAILY_LOSS_LIMIT) and not st["day"]["halted"]:
        st["day"]["halted"] = True
        if tg:
            tg.say(f"🛑 Daily loss limit hit ({DAILY_LOSS_LIMIT:.0%}). No new trades until tomorrow.")
    if nav <= st["peak"] * (1 - MAX_DRAWDOWN) and not st.get("paused"):
        st["paused"] = f"drawdown over {MAX_DRAWDOWN:.0%}"
        if tg:
            tg.say(f"🛑 Bankroll is down {MAX_DRAWDOWN:.0%} from its peak. I've paused new trades. Review things, then send /resume.")

    news = load_news(st, log)
    if full and REPORTER:
        load_earnings(st, log)
    today = day_stats(st, nav)
    e_now = et(now)
    is_open = market_open_now(now)

    # 2) manage open trades
    if st["trades"]:
        try:
            pos = api.positions()
            qs = api.quotes([m["sym"] for m in st["trades"].values()] + [m["short"] for m in st["trades"].values() if m.get("short")])
        except ApiError as e:
            pos, qs = None, {}
            log.append(f"Couldn't read positions ({e})")
        for tid, meta in list(st["trades"].items()):
            q = qs.get(meta["sym"], {})
            bid = q.get("bid") or meta.get("price") or meta["entry"]
            if meta.get("short"):                              # spread: value = long leg - short leg (mid prices)
                qs_ = qs.get(meta["short"], {})
                if q.get("bid") and q.get("ask") and qs_.get("ask"):
                    bid = max(0.0, round((q["bid"] + q["ask"]) / 2 - (qs_.get("bid", 0) + qs_["ask"]) / 2, 2))
                else:
                    bid = meta.get("price") or meta["entry"]
            meta["price"] = bid
            meta["upl"] = round((bid - meta["entry"]) * 100 * meta["qty"], 2)
            if meta.get("trail") and bid:
                meta["peak"] = max(meta.get("peak", bid), bid)
                trail_sl = round(meta["peak"] * (1 - TRAIL_GIVEBACK), 2)
                if trail_sl > meta["sl"]:
                    meta["sl"] = trail_sl
            if pos is not None and meta["sym"] not in pos:
                close_trade(api, st, tid, meta, "expired" if meta.get("exp", "9") < today_s else "closed outside the bot",
                            bid if meta.get("exp", "9") >= today_s else 0.0, now, tg, log)
                continue
            if not is_open:
                continue
            zero = meta.get("exp") == today_s
            flat = (e_now.hour, e_now.minute) >= (FLATTEN_AT if not zero else (15, 30)) and must_sell_today(meta, today_s)
            if st.get("_closeall") or flat:
                close_trade(api, st, tid, meta, "end of day" if flat else "closed by you", bid, now, tg, log)
            elif meta.get("tp_on", True) and bid >= meta["tp"]:
                close_trade(api, st, tid, meta, "target", bid, now, tg, log)
            elif bid <= meta["sl"]:
                why_ = ("trailing stop" if meta.get("trail") and meta["sl"] > meta["entry"] else
                        "locked profit" if meta.get("locked") and meta["sl"] > meta["entry"] else
                        "breakeven" if meta.get("breakeven") else "stop")
                close_trade(api, st, tid, meta, why_, bid, now, tg, log)
            elif meta.get("orb5"):
                pass                                          # exits come from the ORB rules on IWM itself (below)
            elif PARTIAL and not meta.get("partial") and bid >= meta["entry"] * (1 + PARTIAL_AT):
                take_partial(api, st, meta, bid, now, tg, log)
            elif HER_EXITS:
                her_exits(api, st, meta, bid, tg, log)
            elif not meta.get("breakeven") and bid >= meta["entry"] * (1 + BREAKEVEN_AT):
                meta["breakeven"] = True; meta["sl"] = meta["entry"]
                feed_add(st, "event", f"{meta['pair']} stop to breakeven", who="Risk",
                         text=f"{contract_name(meta['sym'])} is up {BREAKEVEN_AT:.0%}. Stop moved to what we paid.")
                if tg:
                    tg.say(f"🔒 {contract_name(meta['sym'])} is up {BREAKEVEN_AT:.0%}. Stop moved to entry (${meta['entry']:.2f}). "
                           "This one can't lose now (barring a fast gap).")
    if st.pop("_closeall", False) and tg:
        tg.say("🧯 Sold everything and paused. /resume when you're ready.")

    bal = START_BALANCE + st["record"]["pl"]
    unreal = sum(m.get("upl", 0) for m in st["trades"].values())
    nav = bal + unreal
    st["account"].update(nav=nav, balance=bal, unrealized=unreal)
    st["peak"] = max(st.get("peak", nav), nav)
    if full:
        eq = st.setdefault("equity", [])
        if not eq or now.timestamp() - eq[-1][0] >= 14 * 60:
            eq.append([round(now.timestamp()), round(nav, 2)])
        del eq[:-1500]

    # 3) the council (full runs while the market is open)
    pairs = list(PAIRS)
    open_pairs = {m["pair"] for m in st["trades"].values()}
    m15s = h1s = {}
    if full and is_open:
        try:
            m15s = api.bars(pairs, "15Min", 14)
            h1s = api.bars(pairs, "1Hour", 75)
        except ApiError as e:
            log.append(f"Price history unavailable ({e})")
    for pair in (pairs if (full and is_open) else []):
        m15, h1 = m15s.get(pair, []), h1s.get(pair, [])
        if len(m15) < 60 or len(h1) < 210:
            log.append(f"{pair}: not enough price history yet ({len(m15)} 15-min bars, {len(h1)} hourly)")
            continue
        a = analyze(m15, h1)                  # 15-min setup, filtered by the 1-hour trend
        a.update(htf_label="1-hour trend", spread_max=MAX_SPREAD_ATR_15M)
        if a["setup"]:
            a["setup"] += " 15m"
        tfs = [("M15", a, m15)]
        prev_close = next((x["c"] for x in reversed(m15) if et(x["t"]).date() < e_now.date()), m15[0]["c"])
        st["pairs"][pair] = {"trend": a["trend"], "h4_trend": a["h4_trend"], "adx": a["adx"], "rsi": a["rsi"],
                             "atr_pips": a["atr"] or 0, "spread_pips": 0, "close": a["close"], "prec": 2,
                             "setup": a["setup"], "bar": a["time"], "tradeable": is_open,
                             "chg": (a["close"] - prev_close) / prev_close * 100}
        orb = opening_range(pair, m15, a, now)
        if orb and st.setdefault("lb_done", {}).get(pair) != today_s:
            tfs.append(("ORB", orb, m15))
        if a["atr"]:
            hyp = council(a, 0, True, "", now, pair)
            passed = sum(1 for o, _ in hyp.values() if o is True) + (1 if a["setup"] else 0)
            waiting = [] if a["setup"] else ["a pullback to the trend line or a fresh crossover"]
            waiting += [why for o, why in hyp.values() if o is False]
            st["pairs"][pair]["ready"] = {"passed": passed, "of": 6, "dir": a["trend"], "waiting": waiting[:2], "tf": "M15",
                                          "in_trade": pair in open_pairs, "session": in_session(pair, now)}

        # score rejected setups on the stock's own price
        for sh in [s for s in st["shadow"] if s["pair"] == pair]:
            hit = None
            for x in [x for x in m15 if x["t"] > sh["bar"]]:
                hit_sl = (x["l"] <= sh["sl"]) if sh["dir"] == 1 else (x["h"] >= sh["sl"])
                hit_tp = (x["h"] >= sh["tp"]) if sh["dir"] == 1 else (x["l"] <= sh["tp"])
                if hit_sl:
                    hit = False; break
                if hit_tp:
                    hit = True; break
            age = (now - dt.datetime.fromisoformat(sh["bar"]).replace(tzinfo=dt.timezone.utc)).days
            if hit is not None:
                score(st, sh["votes"], hit, False); st["shadow"].remove(sh)
                note = record_result(st, pair, sh.get("setup", "pullback"), TARGET_ATR / STOP_ATR if hit else -1.0)
                if note and tg:
                    tg.say(note)
            elif age > SHADOW_DAYS:
                st["shadow"].remove(sh)

        if not OLD_SETUPS:
            tfs = []
        for tf, a, _candles in tfs:
            bar_key = f"{pair}:{tf}"
            if st["last_bar"].get(bar_key) == a["time"]:
                continue
            st["last_bar"][bar_key] = a["time"]
            if not a["setup"] or not a["atr"]:
                log.append(f"{pair}: no setup on the {et(a['time']):%I:%M %p} candle")
                continue
            st["_changed"] = True
            d = a["trend"]
            ts = now.timestamp()

            # risk gate
            reasons = []
            if st.get("paused"):
                reasons.append(f"paused ({st['paused']})")
            if streak_status(st, ts) == "timeout":
                reasons.append(f"cooling off after {st['streak']['losses']} losses in a row")
            if st["day"].get("halted"):
                reasons.append("daily loss limit hit")
            if pair in open_pairs:
                reasons.append("already in a trade on this stock")
            if len(st["trades"]) >= MAX_OPEN_TRADES:
                reasons.append(f"{MAX_OPEN_TRADES} trades already open")
            hit = next((e for e in news if e["cur"] == "USD" and e["t"] - NEWS_BEFORE_MIN * 60 <= ts <= e["t"] + NEWS_AFTER_MIN * 60), None)
            if hit:
                reasons.append(f"news: {news_label(hit)}")
            bench = st.get("learn", {}).get("benched", {}).get(f"{pair}:{a['setup']}")
            if bench is not None:
                reasons.append(f"benched: {a['setup']} setups on {pair} have been losing ({bench:+.2f}R)")
            budget = nav * RISK_PCT * (0.5 if streak_status(st, ts) != "normal" else 1) / OPT_STOP
            pick = None
            if not reasons and in_session(pair, now):
                pick, why_not = pick_contract(api, pair, d, a["close"], budget, now)
                if not pick:
                    reasons.append(why_not)
                elif pick["sym"] in gap_fade_holds():
                    pick = None
                    reasons.append("the Gap Fade bot already holds that exact contract")
                elif NO_CHASE and f"{pair}:{d}" in st.get("dirs_today", {}).get(today_s, []):
                    pick = None
                    reasons.append("already traded this direction today (no chasing)")
                elif NO_CHASE:
                    fp = first_price_today(api, pick["sym"], now)
                    if fp and pick["ask"] >= fp * (1 + NO_CHASE):
                        reasons.append(f"chasing: the option already costs ${pick['ask']:.2f}, "
                                       f"{pick['ask'] / fp - 1:.0%} above its first price today (${fp:.2f})")
                        pick = None
            today["setups"] += 1
            if tf == "ORB":
                st.setdefault("lb_done", {})[pair] = today_s
                votes = council_orb(a, not reasons, "; ".join(reasons), now, pair)
            else:
                votes = council(a, 0, not reasons, "; ".join(reasons), now, pair)
            if pick and votes["Risk"][0]:
                votes["Risk"] = (True, f"{contract_name(pick['sym'])} at ${pick['ask']:.2f}, spread {pick['spread']:.0%}")
            rv, rwhy, rhard = reporter_vote(st, api, pair, d, pick, now, log)
            votes["The Reporter"] = (rv, rwhy)
            under = a["close"]
            if a.get("sl_price") is not None:
                u_sl = a["sl_price"]; u_tp = under + d * (TARGET_ATR / STOP_ATR) * abs(under - u_sl)
            else:
                u_sl = under - d * STOP_ATR * a["atr"]; u_tp = under + d * TARGET_ATR * a["atr"]
            yes, no = tally(votes)
            ok = approved(votes, voter_weights(st)) and pick is not None and not rhard   # earnings = hard veto
            label = f"{pair} {'CALL' if d == 1 else 'PUT'} ({a['setup']})"
            st["last_vote"] = {"label": label, "side": "CALL" if d == 1 else "PUT", "approved": ok, "when": ts,
                               "url": "https://app.alpaca.markets/", "ask": 0, "edge": 0,
                               "detail": (f"{contract_name(pick['sym'])} @ ${pick['ask']:.2f}" if pick else f"{pair} ${under:,.2f}")
                                         + f" · stock stop {u_sl:,.2f} · target {u_tp:,.2f}",
                               "votes": {n: [o, why] for n, (o, why) in votes.items()},
                               "chart": {"closes": [round(x["c"], 3) for x in _candles[-60:]], "entry": under,
                                         "sl": u_sl, "tp": u_tp, "prec": 2, "tf": tf}}
            plain_votes = {n: o for n, (o, _) in votes.items()}
            vote_entry = feed_add(st, "vote", label,
                                  lines=[{"who": n, "vote": o, "say": say(n, o, why, a["time"] + pair)} for n, (o, why) in votes.items()],
                                  verdict=(f"APPROVED {yes}–{no}. Buying {contract_name(pick['sym'])}." if ok else
                                           f"REJECTED {yes}–{no}. Logged on paper so we learn whether we were right."), ok=ok)
            if tg and st.get("live_tg") and not ok:
                tg.say(transcript(vote_entry))
            vote_text = "\n".join(f"{'✅' if o else '❌' if o is False else '➖'} {n}: {why}" for n, (o, why) in votes.items())
            if not ok:
                today["rejected"] += 1
                today["news"] += 1 if hit else 0
                today["benched"] += 1 if bench is not None else 0
                st["shadow"].append({"pair": pair, "dir": d, "bar": a["time"], "sl": u_sl, "tp": u_tp, "votes": plain_votes,
                                     "setup": a["setup"], "tf": tf})
                log.append(f"{pair}: council rejected {label} {yes}–{no}")
                continue

            # place it
            limit = tick_up(pick["ask"])
            try:
                res = api.buy(pick["sym"], pick["qty"], limit)
                o = wait_fill(api, res["id"])
            except ApiError as e:
                log.append(f"{pair}: order rejected ({e})")
                vote_entry["verdict"] = f"APPROVED {yes}–{no}, but the order was rejected: {e}"
                if tg:
                    tg.say(f"⚠️ Alpaca rejected {contract_name(pick['sym'])}: {e}")
                continue
            filled_qty = int(float(o.get("filled_qty") or 0))
            if o.get("status") != "filled":
                try:
                    api.cancel(res["id"])
                except ApiError:
                    pass
                if not filled_qty:
                    log.append(f"{pair}: {pick['sym']} not filled at ${limit:.2f}, canceled")
                    vote_entry["verdict"] = f"APPROVED {yes}–{no}, but it didn't fill at ${limit:.2f}. Canceled."
                    continue
            fill_px = float(o.get("filled_avg_price") or limit)
            qty = filled_qty or pick["qty"]
            risk_usd = round(fill_px * OPT_STOP * 100 * qty, 2)
            sl, tp = round(fill_px * (1 - OPT_STOP), 2), round(fill_px * (1 + OPT_TARGET), 2)
            tid = str(res["id"])
            st["trades"][tid] = {"pair": pair, "sym": pick["sym"], "dir": d, "qty": qty, "units": qty, "entry": fill_px,
                                 "sl": sl, "tp": tp, "exp": pick["exp"], "opened": now.isoformat()[:19], "risk": risk_usd,
                                 "votes": plain_votes, "setup": a["setup"], "tf": tf, "price": fill_px, "upl": 0.0,
                                 "qty0": qty, "tp_on": not HER_EXITS}
            st["dirs_today"] = {today_s: st.get("dirs_today", {}).get(today_s, []) + [f"{pair}:{d}"]}
            open_pairs.add(pair)
            today["approved"] += 1
            today["opened"] += 1
            vote_entry["verdict"] = (f"APPROVED {yes}–{no}. Bought {qty} × {contract_name(pick['sym'])} at ${fill_px:.2f}. "
                                     f"Target ${tp:.2f}, stop ${sl:.2f}.")
            log.append(f"{pair}: OPENED {qty} × {pick['sym']} @ {fill_px:.2f}")
            if tg:
                zero_txt = "Expires TODAY (0DTE). " if pick["zero"] else ""
                tg.say(f"{'🟢 CALL' if d == 1 else '🔴 PUT'} {pair}: {a['setup']}\n\n"
                       f"Bought {qty} × {contract_name(pick['sym'])} at ${fill_px:.2f} (${fill_px * 100 * qty:,.0f} total)\n"
                       + (f"🔐 At ${fill_px * (1 + LOCK_PROFIT_AT):.2f} (+{LOCK_PROFIT_AT:.0%}) the stop locks +{LOCK_PROFIT_AT:.0%}"
                          + (f"; at ${fill_px * (1 + HALF_AT):.2f} (+{HALF_AT:.0%}) sell half, the rest aims for ${fill_px * (1 + HALF_TARGET):.2f}\n"
                             if qty >= 2 else ", then it follows the price up (25% below its best)\n") if HER_EXITS else
                          f"🎯 Sell at ${tp:.2f} (+{OPT_TARGET:.0%})\n")
                       + f"🛑 Sell at ${sl:.2f} (−{OPT_STOP:.0%}), max loss ≈ ${risk_usd:,.0f}\n"
                       + (f"💰 At ${fill_px * (1 + PARTIAL_AT):.2f} (+{PARTIAL_AT:.0%}): " + (f"sell {qty // 2}, let the rest run" if qty >= 2 else f"lock in +{LOCK_AT:.0%} and let it run")
                          + f" (aims for +{RUNNER_TARGET:.0%} with a trailing stop)\n" if PARTIAL else "")
                       + f"🔔 {zero_txt}{sell_by_text(pick['exp'], pick['zero'])}\n\n"
                       f"👉 Robinhood: search {pair} → Trade → Trade Options → Buy → "
                       f"{'Call' if d == 1 else 'Put'} → expiration {dt.date.fromisoformat(pick['exp']):%b} {dt.date.fromisoformat(pick['exp']).day} → "
                       f"strike ${pick['strike']:g} → limit ${limit:.2f} or less, {qty} contract{'s' if qty > 1 else ''}. "
                       f"Skip it if it's already above ${limit * 1.1:.2f}.\n\n"
                       f"Council {yes}–{no}:\n{vote_text}")

    if full and is_open and st.get("pairs"):
        st["ticker"] = [{"s": pr, "p": f"{st['pairs'][pr]['close']:,.2f}", "c": round(st["pairs"][pr].get("chg", 0), 2)}
                        for pr in pairs if pr in st["pairs"]]
        notes, setups = [], 0
        for pr in pairs:
            pp = st["pairs"].get(pr)
            if not pp:
                continue
            setups += 1 if pp.get("setup") else 0
            arrow = "▲" if pp["trend"] == 1 else "▼"
            if any(m["pair"] == pr for m in st["trades"].values()):
                what = "in a trade"
            elif pp.get("setup"):
                what = f"{pp['setup']} setup"
            elif (pp.get("adx") or 0) < ADX_MIN:
                what = "trend too weak, waiting"
            else:
                what = "trending, waiting for a pullback"
            notes.append({"who": pr, "vote": None, "say": f"{arrow} ${pp['close']:,.2f}, ADX {(pp.get('adx') or 0):.0f}, RSI {(pp.get('rsi') or 0):.0f}: {what}"})
        if OLD_SETUPS:
            feed_add(st, "scan", f"Scanned {len(notes)} stock{'s' if len(notes) != 1 else ''}: {setups} setup{'s' if setups != 1 else ''} on the latest candle", lines=notes)

    # 3a) Easter's 5-minute opening range breakout
    if ORB5 and is_open:
        try:
            m5s = api.bars(pairs, "5Min", 1)
        except ApiError as e:
            m5s = {}
            log.append(f"5-min bars unavailable ({e})")
        for pair in pairs:
            bars = []
            for x in m5s.get(pair, []):
                e_ = et(x["t"])
                if e_.date() == e_now.date():
                    bars.append({"mins": e_.hour * 60 + e_.minute, "label": e_.strftime("%I:%M").lstrip("0"), "t": x["t"],
                                 "o": x["o"], "h": x["h"], "l": x["l"], "c": x["c"]})
            r = orb5(bars, False)
            o5all = st.setdefault("orb5", {})
            if o5all.get("date") != today_s or "pairs" not in o5all:
                o5all.clear(); o5all.update(date=today_s, pairs={})
            o5 = o5all["pairs"].setdefault(pair, {"sent": [], "entered": False})
            o5.update(pair=pair, orh=r["orh"], orl=r["orl"], phase=r["phase"], status=r["status"], events=r["events"][-14:],
                      trade=r["trade"], last=bars[-1]["c"] if bars else None, checked=now.timestamp(),
                      candles=[[b["label"], round(b["o"], 2), round(b["h"], 2), round(b["l"], 2), round(b["c"], 2)] for b in bars[-80:]])
            sent = o5["sent"]

            def tell(key, text, feed_title=None, ok=None):
                if key in sent:
                    return
                sent.append(key)
                st["_changed"] = True
                feed_add(st, "event", feed_title or f"{pair} opening range", who="The Quant", ok=ok, text=text)
                if tg:
                    tg.say(text)

            if r["box_bars"] >= 6:
                size = r["orh"] - r["orl"]
                tell("range", f"📏 {pair} opening range is set (9:30–10:00 ET)\nHigh {r['orh']:.2f} · Low {r['orl']:.2f} · size ${size:.2f}\n\n"
                              f"Watching 5-min candles until {ORB5_END[0] % 12 or 12}:{ORB5_END[1]:02d}. Signal = a strong candle closes outside "
                              f"the range, then the next one holds outside and makes a new high (calls) or low (puts).",
                     f"{pair} range set: {r['orl']:.2f}–{r['orh']:.2f}")
            open_orb = next(((tid, m) for tid, m in st["trades"].items() if m.get("orb5") and m["pair"] == pair), None)
            for ev_ in r["events"]:
                key = f"{ev_['mins']}:{ev_['kind']}"
                k = ev_["kind"]
                if k == "pend":
                    tell(key, f"👀 {pair} {ev_['t']} ET: {ev_['txt']}.", f"{pair} breakout watch")
                elif k == "fail":
                    tell(key, f"↩️ {pair} {ev_['t']} ET: {ev_['txt']}. No trade, still watching.", f"{pair} breakout failed", False)
                elif k == "none":
                    tell(key, f"😴 {pair}: {ev_['txt']}.", f"{pair}: no setup today")
                elif k == "be":
                    if open_orb:
                        open_orb[1]["orb5"]["be"] = True
                    tell(key, f"🔒 {pair} {ev_['t']} ET: {ev_['txt']} ({pair} level). The trade can't lose on {pair}'s move now.", f"{pair} stop to breakeven", True)
            t = r["trade"]
            # entry
            if t and not o5.get("entered"):
                o5["entered"] = True
                d = t["dir"]
                ts = now.timestamp()
                end_dt = dt.datetime.fromisoformat(t["entry_t"]).replace(tzinfo=dt.timezone.utc) + dt.timedelta(minutes=5)
                late = (now - end_dt).total_seconds() / 60 > ORB5_FRESH_MIN
                head = (f"{'🟢 CALLS' if d == 1 else '🔴 PUTS'} SIGNAL · {pair} opening range breakout ({t['entry_label']} ET)\n"
                        f"{pair} entry {t['entry']:.2f} · stop {t['stop0']:.2f} · target {t['target']:.2f} (1.5R)\n"
                        f"Breakeven: when {pair} reaches {t['entry'] + d * ORB5_BE_AT * t['one_r']:.2f}, the stop moves to {t['entry']:.2f}")
                reasons = []
                if late:
                    reasons.append(f"signal was {int((now - end_dt).total_seconds() // 60)} min old when I saw it (no late entries)")
                if st.get("paused"):
                    reasons.append(f"paused ({st['paused']})")
                if streak_status(st, ts) == "timeout":
                    reasons.append(f"cooling off after {st['streak']['losses']} losses in a row")
                if st["day"].get("halted"):
                    reasons.append("daily loss limit hit")
                if len(st["trades"]) >= MAX_OPEN_TRADES:
                    reasons.append(f"{MAX_OPEN_TRADES} trades already open")
                if pair in SIGNAL_ONLY:
                    reasons.insert(0, f"{pair} is signal-only while it proves itself live")
                hit = next((e for e in news if e["cur"] == "USD" and e["t"] - NEWS_BEFORE_MIN * 60 <= ts <= e["t"] + NEWS_AFTER_MIN * 60), None)
                budget = nav * RISK_PCT * (0.5 if streak_status(st, ts) != "normal" else 1) / OPT_STOP
                pick = None
                if not reasons:
                    pick, why_not = pick_contract(api, pair, d, t["entry"], budget, now)
                    if not pick and SPREADS and "over the" in (why_not or ""):
                        pick, why_sp = pick_spread(api, pair, d, t["entry"], t["target"], budget, now)
                        if not pick:
                            why_not += f"; {why_sp}"
                    if not pick:
                        reasons.append(why_not)
                    elif pick["sym"] in gap_fade_holds():
                        pick = None; reasons.append("the Gap Fade bot already holds that exact contract")
                votes = council_orb5(r, t, not reasons, "; ".join(reasons), hit, now, pair)
                if pick:
                    votes["Risk"] = (True, f"{pick_name(pick)} at ${pick['ask']:.2f}, spread {pick['spread']:.0%}")
                rv, rwhy, rhard = reporter_vote(st, api, pair, d, pick, now, log)
                votes["The Reporter"] = (rv, rwhy)
                yes, no = tally(votes)
                ok = pick is not None and votes["Risk"][0] is True and hit is None and not rhard
                today["setups"] += 1
                label = f"{pair} {'CALL' if d == 1 else 'PUT'} (opening range breakout)"
                vote_text = "\n".join(f"{'✅' if o_ else '❌' if o_ is False else '➖'} {n}: {why}" for n, (o_, why) in votes.items())
                st["last_vote"] = {"label": label, "side": "CALL" if d == 1 else "PUT", "approved": ok, "when": ts,
                                   "url": "https://app.alpaca.markets/", "ask": 0, "edge": 0,
                                   "detail": (f"{pick_name(pick)} @ ${pick['ask']:.2f}" if pick else f"{pair} ${t['entry']:,.2f}")
                                             + f" · stock stop {t['stop0']:,.2f} · target {t['target']:,.2f}",
                                   "votes": {n: [o_, why] for n, (o_, why) in votes.items()},
                                   "chart": {"closes": [b["c"] for b in bars[-60:]], "entry": t["entry"], "sl": t["stop0"],
                                             "tp": t["target"], "prec": 2, "tf": "5m"}}
                vote_entry = feed_add(st, "vote", label,
                                      lines=[{"who": n, "vote": o_, "say": say(n, o_, why, t["entry_t"] + pair)} for n, (o_, why) in votes.items()],
                                      verdict=(f"APPROVED {yes}–{no}. Buying {pick_name(pick)}." if ok else
                                               f"SIGNAL ONLY {yes}–{no}: {'; '.join(reasons) or 'news or earnings'}. Tracked on {pair}'s levels."), ok=ok)
                if not ok:
                    today["rejected"] += 1
                    o5["paper_only"] = True
                    idea = ""
                    if pair in SIGNAL_ONLY and len(reasons) == 1:
                        try:
                            sug, _ = pick_contract(api, pair, d, t["entry"], budget, now)
                        except Exception:
                            sug = None
                        if sug:
                            idea = (f"\nIf you want to take it yourself: {contract_name(sug['sym'])} around ${sug['ask']:.2f} "
                                    f"(${sug['ask'] * 100:,.0f} per contract).")
                    if tg:
                        tg.say(head + f"\n\n⚠️ The bot is NOT buying this one: {'; '.join(reasons) or (news_label(hit) if hit else 'earnings/news veto')}.{idea}\n"
                                      f"I'll still track it on {pair} and tell you how it ends.\n\nCouncil {yes}–{no}:\n{vote_text}")
                else:
                    limit = tick_up(pick["ask"]) if not pick.get("short") else round(pick["ask"] + 0.01, 2)
                    try:
                        if pick.get("short"):
                            res = api.buy_spread(pick["sym"], pick["short"], pick["qty"], limit)
                        else:
                            res = api.buy(pick["sym"], pick["qty"], limit)
                        o = wait_fill(api, res["id"])
                    except ApiError as e:
                        o, res = None, None
                        log.append(f"{pair}: order rejected ({e})")
                        vote_entry["verdict"] = f"APPROVED {yes}–{no}, but the order was rejected: {e}"
                        if tg:
                            tg.say(head + f"\n\n⚠️ Alpaca rejected {pick_name(pick)}: {e}")
                    filled_qty = int(float((o or {}).get("filled_qty") or 0))
                    if o is not None and o.get("status") != "filled":
                        try:
                            api.cancel(res["id"])
                        except ApiError:
                            pass
                    if o is not None and (o.get("status") == "filled" or filled_qty):
                        fill_px = abs(float(o.get("filled_avg_price") or limit)) or limit
                        qty = filled_qty or pick["qty"]
                        risk_usd = round(fill_px * OPT_STOP * 100 * qty, 2)
                        tid = str(res["id"])
                        st["trades"][tid] = {"pair": pair, "sym": pick["sym"], "dir": d, "qty": qty, "units": qty, "entry": fill_px,
                                             "sl": round(fill_px * (1 - ORB5_OPT_SAFETY), 2), "tp": 999.0, "tp_on": False,
                                             "exp": pick["exp"], "opened": now.isoformat()[:19], "risk": risk_usd,
                                             "votes": {n: o_ for n, (o_, _) in votes.items()}, "setup": "opening range breakout",
                                             "tf": "5m", "price": fill_px, "upl": 0.0, "qty0": qty,
                                             "orb5": {"date": today_s, "entry": t["entry"], "stop": t["stop0"], "target": t["target"], "be": False}}
                        if pick.get("short"):
                            st["trades"][tid].update(short=pick["short"], name=pick_name(pick), width=pick["width"])
                        today["approved"] += 1; today["opened"] += 1
                        vote_entry["verdict"] = f"APPROVED {yes}–{no}. Bought {qty} × {pick_name(pick)} at ${fill_px:.2f}."
                        log.append(f"{pair}: OPENED {qty} × {pick['sym']} @ {fill_px:.2f} (ORB)")
                        if tg:
                            rh = (f"👉 Robinhood: search {pair} → Trade → Trade Options → Buy → {'Call' if d == 1 else 'Put'} → "
                                  f"expiration {dt.date.fromisoformat(pick['exp']):%b} {dt.date.fromisoformat(pick['exp']).day} → strike ${pick['strike']:g} → "
                                  f"limit ${limit:.2f} or less. Skip it if it's already above ${limit * 1.1:.2f}.")
                            if pick.get("short"):
                                rh = (f"👉 Robinhood: search {pair} → Trade → Trade Options → tap Buy, then select TWO {'calls' if d == 1 else 'puts'} "
                                      f"with expiration {dt.date.fromisoformat(pick['exp']):%b} {dt.date.fromisoformat(pick['exp']).day}: "
                                      f"BUY the ${pick['strike']:g} and SELL the ${pick['short_strike']:g} ({'call debit' if d == 1 else 'put debit'} spread) → "
                                      f"limit debit ${limit:.2f} or less. Max loss = what you pay; max gain = ${pick['width']:g} minus what you pay.")
                            tg.say(head + f"\n\nBought {qty} × {pick_name(pick)} at ${fill_px:.2f} (${fill_px * 100 * qty:,.0f}) on the paper account.\n"
                                          f"Exits follow {pair}'s levels above, not the option's %. Closed by 3:50 PM ET either way.\n\n"
                                          f"{rh}\n\nCouncil {yes}–{no}:\n{vote_text}")
                    elif o is not None:
                        vote_entry["verdict"] = f"APPROVED {yes}–{no}, but it didn't fill at ${limit:.2f}. Canceled."
                        if tg:
                            tg.say(head + f"\n\n⚠️ {pick_name(pick)} didn't fill at ${limit:.2f}. Canceled; tracking on {pair} only.")
                        o5["paper_only"] = True
            # exits on IWM's levels
            open_orb = next(((tid, m) for tid, m in st["trades"].items() if m.get("orb5") and m["pair"] == pair), None)
            if t and t["result"] is not None:
                why_ = {ORB5_RR: f"target +{ORB5_RR:g}R on {pair}", -1.0: f"stop on {pair}", 0.0: f"breakeven on {pair}"}.get(t["result"], f"{pair} rule exit")
                if open_orb:
                    tid, meta = open_orb
                    close_trade(api, st, tid, meta, why_, meta.get("price") or meta["entry"], now, tg, log)
                    sent.append(f"exit:{t['exit_mins']}")
                else:
                    icon = "🎯" if t["result"] > 0 else "🛑" if t["result"] < 0 else "🔒"
                    extra = ""
                    if pair in SIGNAL_ONLY and f"exit:{t['exit_mins']}" not in sent:
                        pr = st.setdefault("probation", {}).setdefault(pair, {"n": 0, "wins": 0, "r": 0.0})
                        pr["n"] += 1; pr["wins"] += t["result"] > 0; pr["r"] = round(pr["r"] + t["result"], 2)
                        extra = (f"\n📋 {pair} signal-only record: {pr['n']} signals, {pr['wins']} wins, {pr['r']:+.1f}R total "
                                 f"({pr['r'] / pr['n']:+.2f}R each). After 20+ signals, if it's positive, it can start trading for real.")
                    tell(f"exit:{t['exit_mins']}", f"{icon} {pair} ORB signal finished on {pair}: {why_} ({t['result']:+.1f}R). "
                                                   f"No option was held{' (signal only)' if o5.get('paper_only') else ''}." + extra,
                         f"{pair} ORB signal finished", t["result"] > 0)
            elif open_orb and (e_now.hour, e_now.minute) >= ORB5_EOD:
                tid, meta = open_orb
                close_trade(api, st, tid, meta, "end of day (ORB is a day trade)", meta.get("price") or meta["entry"], now, tg, log)

    # 3b) test trade on request
    if st.get("test_pending"):
        if not is_open:
            if not st.get("test_waiting_said") and tg:
                tg.say("🧪 Test trade waiting: the market is closed. I'll do it at the next open.")
            st["test_waiting_said"] = True
        else:
            st.pop("test_waiting_said", None)
            try:
                chain = api.chain("SPY", "call", (e_now.date() + dt.timedelta(days=1)).isoformat(),
                                  (e_now.date() + dt.timedelta(days=10)).isoformat())
                cheap = sorted([(c["ask"], s) for s, c in chain.items() if 0.01 <= c["ask"] <= 0.15 and c["bid"] > 0])
                if not cheap:
                    raise ApiError("no cheap SPY call found to test with")
                ask_, sym_ = cheap[0]
                res = api.buy(sym_, 1, tick_up(ask_))
                o = wait_fill(api, res["id"])
                if o.get("status") != "filled":
                    try:
                        api.cancel(res["id"])
                    except ApiError:
                        pass
                    raise ApiError(f"order {o.get('status', 'not filled')}")
                cl = api.close(sym_)
                oc = wait_fill(api, cl["id"]) if cl.get("id") else {}
                msg = (f"✅ Test trade worked.\nBought 1 × {contract_name(sym_)} at ${float(o.get('filled_avg_price') or ask_):.2f}, "
                       f"sold at ${float(oc.get('filled_avg_price') or 0):.2f}.\nBuying and selling options both work.")
                feed_add(st, "event", "Test trade", who="Chair", ok=True, text=msg.replace("\n", " "))
                log.append("Test trade: OK")
            except ApiError as e:
                msg = (f"❌ Test trade failed: {e}\n"
                       f"Options level on the account: {st['account'].get('options_level')}. Send me this message.")
                log.append(f"Test trade failed ({e})")
            if tg:
                tg.say(msg)
            st["test_pending"] = False

    if st.pop("_strategy_switched", False):
        feed_add(st, "event", "New strategy", who="Chair", ok=True,
                 text=f"Vault Options now trades only Easter's 5-minute opening range breakout on {', '.join(PAIRS)}. Fresh ${START_BALANCE:,.0f} paper bankroll.")
        if tg:
            tg.say(f"🔄 Vault Options has a new strategy: the 5-minute opening range breakout on {', '.join(PAIRS)}.\n\n"
                   "1) 9:30–10:00 ET: I mark the high and low and text you the levels.\n"
                   "2) 10:00–12:00: I watch every 5-min candle. A strong candle (60%+ body) closing outside the range starts a watch.\n"
                   "3) If the next candle holds outside and makes a new high (or low), that's the SIGNAL: calls above, puts below.\n"
                   "4) Stop past the breakout candles, target 1.5× the risk, stop to breakeven at 1R. One trade a day, closed by 3:50 PM.\n\n"
                   f"Old setups are off. Fresh ${START_BALANCE:,.0f} paper bankroll, so the new strategy gets a clean scorecard.")

    # 4) status reply
    if tg and st.pop("_status_requested", False):
        lines = [f"📊 {ALPACA_ENV.upper()} bankroll: ${nav:,.2f}",
                 f"Today: {money(nav - st['day']['start_nav'])}  ·  Record {st['record']['wins']}W–{st['record']['losses']}L",
                 "Market: " + ("open" if is_open else "closed"), "Paused: " + (st["paused"] if st.get("paused") else "no")]
        for m in st["trades"].values():
            lines.append(f"• {m['qty']} × {contract_name(m['sym'])}: bought ${m['entry']:.2f}, now ${m.get('price', 0):.2f} ({money(m.get('upl', 0))})")
        if not st["trades"]:
            lines.append("No open trades.")
        tg.say("\n".join(lines))

    if in_session(PAIRS[0], now):
        st.pop("recess", None)
    else:
        nt, _ = next_vote(now)
        st["recess"] = "resumes " + nt.astimezone(LOCAL_TZ).strftime("%a %I:%M %p ET").replace(" 0", " ")

    # 5) daily / weekly reports
    if tg:
        for text in due_reports(st, nav):
            tg.say(text)
        tg.flush(log)
    elif not tg_token:
        log.append("Telegram: off (add TELEGRAM_BOT_TOKEN)")
    save_state(st)
    return st


ERROR_MARKERS = ("failed", "rejected", "unavailable", "skipped (", "not filled", "couldn't", "no chat", "ALPACA: add")


def record_errors(log, st):
    """Keep the last 20 problems from the run log so the watchdog can report them."""
    errs = st.setdefault("errors", [])
    for line in log:
        if any(m in line for m in ERROR_MARKERS) and "council rejected" not in line and not line.startswith("Council:"):
            errs.append({"t": time.time(), "text": line[:300]})
    del errs[:-20]


# ───────────────────────────── Live council feed ─────────────────────────────

FEED_MAX = 80
PERSONA = {   # how each agent talks: (YES lines, NO lines); {why} is the actual reason
    "The Quant": (["Numbers check out: {why}.", "The data says go. {why}.", "Clean read: {why}."],
                  ["Data's too weak: {why}.", "I need more than this. {why}.", "Doesn't pass my screen: {why}."]),
    "Risk":      (["I can live with this. {why}.", "Exposure's fine. {why}.", "Within limits: {why}."],
                  ["Hard no. {why}.", "Not on my watch: {why}.", "Blocked. {why}."]),
    "Momentum":  (["It's moving our way. {why}.", "Feel that push? {why}.", "Engine's running: {why}."],
                  ["No juice here. {why}.", "Stalling out: {why}.", "Nothing pushing it. {why}."]),
    "Time":      (["Timing's right. {why}.", "Good window. {why}.", "Clock agrees: {why}."],
                  ["Not now. {why}.", "Bad timing: {why}.", "Wait for it. {why}."]),
    "The Reporter": (["Checked the wires: {why}.", "Nothing in the press to scare me. {why}.", "Sources say we're clear: {why}."],
                     ["Hold the presses. {why}.", "Breaking: {why}.", "My sources say no. {why}."]),
    "Doubt":     (["...fine. {why}. I'll allow it.", "Hm. {why}. I can't argue.", "Begrudgingly yes: {why}."],
                  ["Something's off. {why}.", "I don't buy it: {why}.", "Too convenient. {why}."]),
}


def _pick(options, seed):
    return options[sum(map(ord, seed)) % len(options)]


def say(name, vote, why, seed=""):
    yes, no = PERSONA.get(name, (["{why}."], ["{why}."]))
    if vote is None:
        return f"Abstaining. {why}."
    return _pick(yes if vote else no, seed + name).format(why=why[0].upper() + why[1:] if why else "")


def feed_add(st, kind, title, lines=None, verdict=None, ok=None, who=None, text=None):
    """kind: vote | event | scan. A new scan entry replaces the previous scan entry if nothing happened in between."""
    feed = st.setdefault("feed", [])
    entry = {"t": time.time(), "kind": kind, "title": title, "lines": lines or [], "verdict": verdict, "ok": ok,
             "who": who, "text": text}
    if kind == "scan" and feed and feed[-1]["kind"] == "scan":
        feed[-1] = entry
    else:
        feed.append(entry)
    del feed[:-FEED_MAX]
    return entry


def transcript(entry):
    icon = {True: "✅", False: "❌", None: "➖"}
    out = [f"🏛 Council: {entry['title']}"]
    out += [f"{icon[l['vote']]} {l['who']}: {l['say']}" for l in entry["lines"]]
    if entry.get("verdict"):
        out += ["", f"⚖️ {entry['verdict']}"]
    return "\n".join(out)


# ───────────────────────────── Is it learning? ───────────────────────────────

def _avg(xs):
    return sum(xs) / len(xs) if xs else None


def learning_card(st):
    """The honest test: do the trades the council approves beat the ones it rejects, and is it improving?"""
    oc = st.get("learn", {}).get("outcomes", [])
    took = [o["r"] for o in oc if o["approved"]][-50:]
    skip = [o["r"] for o in oc if not o["approved"]][-50:]
    a_took, a_skip = _avg(took), _avg(skip)
    recent, before = [o["r"] for o in oc if o["approved"]][-20:], [o["r"] for o in oc if o["approved"]][-40:-20]
    if len(took) < 10 or len(skip) < 10:
        verdict, cls = (f"Not enough results yet ({len(took)} trades, {len(skip)} rejected setups scored; "
                        "the test needs 10 of each)."), "wait"
    elif a_took > a_skip + 0.1:
        verdict, cls = "The council is adding value: the trades it takes beat the ones it skips.", "good"
    elif a_took > a_skip - 0.1:
        verdict, cls = "No clear difference yet between what it takes and what it skips. Keep watching.", "wait"
    else:
        verdict, cls = "The council isn't helping right now: rejected setups are doing better than approved trades.", "bad"
    trend = None
    if len(recent) >= 10 and len(before) >= 10:
        delta = _avg(recent) - _avg(before)
        trend = (f"{'Improving' if delta > 0.05 else 'Slipping' if delta < -0.05 else 'Holding steady'}: "
                 f"last {len(recent)} trades average {_avg(recent):+.2f}R vs {_avg(before):+.2f}R before")
    return {"took": a_took, "skip": a_skip, "n_took": len(took), "n_skip": len(skip),
            "win": (sum(1 for r in took if r > 0) / len(took)) if took else None,
            "verdict": verdict, "cls": cls, "trend": trend}


def learning_html(st):
    c = learning_card(st)
    f = lambda x: "—" if x is None else f"{x:+.2f}R"
    return f"""<section class="learnwrap"><h2>Is it learning?</h2>
  <p class="lv {c['cls']}">{html.escape(c['verdict'])}</p>
  <div class="lgrid">
    <div><span>Trades it took</span><b class="{'pos' if (c['took'] or 0) > 0 else 'neg' if c['took'] is not None else ''}">{f(c['took'])}</b><small>avg of {c['n_took']}</small></div>
    <div><span>Setups it skipped</span><b>{f(c['skip'])}</b><small>avg of {c['n_skip']} (scored on paper)</small></div>
    <div><span>Win rate</span><b>{'—' if c['win'] is None else f"{c['win']:.0%}"}</b><small>needs ~40% at 1.5R targets to profit</small></div>
  </div>
  {f'<p class="muted">{html.escape(c["trend"])}</p>' if c['trend'] else ''}
  <p class="fine">R = profit measured in units of risk. +1.5R is a full win, −1R a full loss. The council earns its keep only if
  "took" beats "skipped". Vote weights adapt after {LEARN_MIN_CALLS} scored calls, and losing pair + setup combos get benched.</p>
</section>"""


# ───────────────────────────── Losing-streak breaker ─────────────────────────

def streak_update(st, r_mult, ts):
    """Count losses in a row. Half size at STREAK_HALF, a 24h timeout at STREAK_TIMEOUT, full size after wins."""
    sk = st.setdefault("streak", {"losses": 0, "half": False, "wins_since": 0, "until": 0})
    out = []
    if abs(r_mult) < 0.1:
        return out                                   # breakeven scratch: doesn't count either way
    if r_mult > 0:
        sk["losses"] = 0
        if sk["half"]:
            sk["wins_since"] += 1
            if sk["wins_since"] >= STREAK_RECOVER_WINS:
                sk.update(half=False, wins_since=0)
                out.append(f"💪 {STREAK_RECOVER_WINS} wins since the slump. Back to full size ({RISK_PCT:.0%} per trade).")
                feed_add(st, "event", "Back to full size", who="Risk", ok=True, text=out[-1][2:])
        return out
    sk["losses"] += 1
    sk["wins_since"] = 0
    n = sk["losses"]
    if n == STREAK_HALF:
        sk["half"] = True
        out.append(f"⚠️ {n} losses in a row. Cutting trade size in half ({RISK_PCT / 2:.1%} per trade) until "
                   f"{STREAK_RECOVER_WINS} wins. Streaks like this happen even when a strategy works; this keeps the damage small.")
        feed_add(st, "event", "Half size", who="Risk", ok=False, text=out[-1][2:])
    elif n >= STREAK_TIMEOUT and ts >= sk.get("until", 0):
        sk["until"] = ts + STREAK_TIMEOUT_HOURS * 3600
        sk["half"] = True
        back = dt.datetime.fromtimestamp(sk["until"], LOCAL_TZ).strftime("%a %I:%M %p ET").replace(" 0", " ")
        out.append(f"🧊 {n} losses in a row. Timeout: no new trades until {back}. Open trades keep their stops, "
                   "and the council keeps voting on paper so it keeps learning. Comes back at half size. /resume to end it early.")
        feed_add(st, "event", "Timeout", who="Risk", ok=False, text=out[-1][2:])
    return out


def streak_status(st, ts=None):
    sk = st.get("streak") or {}
    ts = ts or time.time()
    if sk.get("until", 0) > ts:
        return "timeout"
    return "half" if sk.get("half") else "normal"


# ───────────────────────────── Reports ───────────────────────────────────────

def _learning_lines(st):
    w = voter_weights(st)
    lowest = min((st.get("scoreboard", {}).get(n, {}).get("total", 0) for n in VOTERS), default=0)
    if all(x == 1.0 for x in w.values()):
        voters = f"Voter weights: all equal until each has {LEARN_MIN_CALLS} scored calls (lowest so far: {lowest})"
    else:
        voters = "Voter weights: " + " · ".join(f"{n} {w[n]:g}×" for n in sorted(VOTERS, key=lambda n: -w[n]))
    benched = st.get("learn", {}).get("benched", {})
    bench = ("Benched: " + ", ".join(f"{NAMES[c.split(':')[0]]} {c.split(':')[1]}" for c in benched)) if benched else "Benched: nothing"
    return voters, bench


def _closed_between(st, start, end):
    return [t for t in st.get("closed", []) if start <= t.get("closed_ts", 0) < end]


def _upcoming(st, hours):
    now = time.time()
    cur = set().union(*(pair_currencies(p) for p in PAIRS))
    evs = [e for e in st.get("news", {}).get("events", []) if e["cur"] in cur and now <= e["t"] <= now + hours * 3600]
    return "\n".join("   • " + news_label(e) for e in evs[:6]) or "   • nothing high-impact on the calendar"


def daily_report(st, day, nav):
    d = st.get("days", {}).get(day, {})
    start = dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=LOCAL_TZ).timestamp()
    closed = _closed_between(st, start, start + 86400)
    w = sum(1 for t in closed if t.get("pl", 0) > 0)
    pl = sum(t.get("pl", 0) for t in closed)
    change = nav - d["start_nav"] if d.get("start_nav") is not None else None
    voters, bench = _learning_lines(st)
    learned = [x["text"] for x in st.get("learn", {}).get("log", []) if x["t"] >= start]
    open_lines = [f"   • {m.get('qty', 1)} × {contract_name(m['sym'])}: {money(m.get('upl', 0))}"
                  + (" (🔒 breakeven)" if m.get("breakeven") else "") for m in st.get("trades", {}).values()]
    lines = [f"📅 Vault Options daily report: {dt.datetime.strptime(day, '%Y-%m-%d'):%a %b %d}", "",
             f"💰 Bankroll: ${nav:,.2f}" + (f" (today {money(change)})" if change is not None else ""),
             f"📈 Trades: {d.get('opened', 0)} opened · {len(closed)} closed"
             + (f" ({w}W–{len(closed) - w}L, {money(pl)})" if closed else ""),
             "📂 Open now:" if open_lines else "📂 Open now: none", *open_lines,
             f"🏛 Council: {d.get('setups', 0)} setups, {d.get('approved', 0)} approved, {d.get('rejected', 0)} rejected"
             + (f" ({d.get('news', 0)} for news, {d.get('benched', 0)} benched)" if d.get('rejected') else ""),
             "", "🧠 " + voters, "   " + bench]
    lines += ["   " + t.replace("🧠 ", "") for t in learned]
    lines += ["", "📰 Big US news in the next 24h:", _upcoming(st, 24)]
    ss = streak_status(st)
    if ss != "normal":
        sk = st.get("streak", {})
        lines += ["", "🧊 Timeout after a losing streak, back " + dt.datetime.fromtimestamp(sk["until"], LOCAL_TZ).strftime("%a %I:%M %p ET")
                  if ss == "timeout" else f"⚠️ Half size until {STREAK_RECOVER_WINS - sk.get('wins_since', 0)} more win(s)"]
    md = st.get("mood", {}).get(day, {})
    if md.get("happy") or md.get("unhappy"):
        lines += ["", f"🎖 Your feedback today: 🥂 {md.get('happy', 0)} happy · 👎 {md.get('unhappy', 0)} not happy"]
    if st.get("paused"):
        lines += ["", f"⏸ Paused ({st['paused']}). Send /resume to restart."]
    return "\n".join(lines)


def weekly_report(st, day, nav):
    end = dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=LOCAL_TZ).timestamp() + 86400
    start = end - 7 * 86400
    closed = _closed_between(st, start, end)
    w = sum(1 for t in closed if t.get("pl", 0) > 0)
    pl = sum(t.get("pl", 0) for t in closed)
    avg_r = sum(t.get("r", 0) for t in closed) / len(closed) if closed else 0
    by_pair = {}
    for t in closed:
        by_pair[t["pair"]] = by_pair.get(t["pair"], 0) + t.get("pl", 0)
    hist = [h for h in st.get("history", []) if start <= h["t"] < end]
    days = [v for k, v in st.get("days", {}).items()
            if start <= dt.datetime.strptime(k, "%Y-%m-%d").replace(tzinfo=LOCAL_TZ).timestamp() < end]
    tot = {k: sum(d_.get(k, 0) for d_ in days) for k in ("setups", "approved", "rejected", "news", "benched")}
    starts = [d_["start_nav"] for d_ in days if d_.get("start_nav") is not None]
    week_change = nav - starts[0] if starts else None
    board = st.get("scoreboard", {})
    ranked = sorted([(n, b["right"] / b["total"], b["total"]) for n, b in board.items() if b.get("total", 0) >= 5],
                    key=lambda x: -x[1])
    rec, cr = st.get("record", {}), st.get("council_record", {"right": 0, "total": 0})
    voters, bench = _learning_lines(st)
    learned = [x["text"] for x in st.get("learn", {}).get("log", []) if start <= x["t"] < end]
    lines = [f"📊 Vault Options weekly report (week ending {dt.datetime.strptime(day, '%Y-%m-%d'):%b %d})", "",
             f"💰 Bankroll: ${nav:,.2f}" + (f" (week {money(week_change)})" if week_change is not None else ""),
             (f"📈 Closed: {w}W–{len(closed) - w}L ({w / len(closed):.0%} win rate), {money(pl)}, avg {avg_r:+.2f}R"
              if closed else "📈 Closed: no trades this week")]
    for pr, v in sorted(by_pair.items(), key=lambda x: -x[1]):
        lines.append(f"   {NAMES[pr]}: {money(v)}")
    lines += [f"🏛 Setups: {tot['setups']} · approved {tot['approved']} · rejected {tot['rejected']} "
              f"({tot['news']} for news, {tot['benched']} benched)",
              f"   Council calls scored this week: {sum(1 for h in hist if h['right'])}/{len(hist)} right"
              if hist else "   Council calls scored this week: none yet",
              f"   Council all-time: {cr['right'] / cr['total']:.0%} ({cr['right']}/{cr['total']})" if cr["total"] else "   Council all-time: no scored calls yet",
              ""]
    if ranked:
        lines.append(f"⭐ Sharpest voter: {ranked[0][0]} ({ranked[0][1]:.0%} of {ranked[0][2]})")
        if len(ranked) > 1:
            lines.append(f"🤔 Least reliable: {ranked[-1][0]} ({ranked[-1][1]:.0%} of {ranked[-1][2]})")
    lc = learning_card(st)
    lines += ["🧠 " + voters, "   " + bench, "   Is it learning? " + lc["verdict"]]
    if lc["took"] is not None and lc["skip"] is not None:
        lines.append(f"   Took {lc['took']:+.2f}R avg ({lc['n_took']}) vs skipped {lc['skip']:+.2f}R avg ({lc['n_skip']})")
    if lc["trend"]:
        lines.append("   " + lc["trend"])
    lines += ["   " + t.replace("🧠 ", "") for t in learned]
    lines += ["", f"All-time: {rec.get('wins', 0)}W–{rec.get('losses', 0)}L, {money(rec.get('pl', 0))}"]
    return "\n".join(lines)


def due_reports(st, nav, now=None):
    now_et = dt.datetime.fromtimestamp(now or time.time(), LOCAL_TZ)
    day = now_et.strftime("%Y-%m-%d")
    out = []
    if now_et.hour < REPORT_HOUR_ET or now_et.weekday() == 5:
        return out
    if st.get("last_daily") != day:
        out.append(daily_report(st, day, nav)); st["last_daily"] = day
    if now_et.weekday() == WEEKLY_DAY and st.get("last_weekly") != day:
        out.append(weekly_report(st, day, nav)); st["last_weekly"] = day
    return out


# ───────────────────────────── Council room (3D) ─────────────────────────────

CHARACTERS = {
    # name: (color token, svg body)
    "The Quant": ("--c-quant", '<rect x="18" y="20" width="64" height="70" rx="6" fill="var(--c-quant)"/>'
                  '<rect x="28" y="38" width="18" height="13" rx="3" fill="none" stroke="var(--ink)" stroke-width="3"/>'
                  '<rect x="54" y="38" width="18" height="13" rx="3" fill="none" stroke="var(--ink)" stroke-width="3"/>'
                  '<line x1="46" y1="44" x2="54" y2="44" stroke="var(--ink)" stroke-width="3"/>'
                  '<circle cx="37" cy="45" r="2.5" fill="var(--ink)"/><circle cx="63" cy="45" r="2.5" fill="var(--ink)"/>'
                  '<path d="M38 68 h24" stroke="var(--ink)" stroke-width="3" stroke-linecap="round"/>'),
    "Risk": ("--c-risk", '<path d="M50 8 L58 22 L72 14 L72 30 L88 32 L78 46 L90 58 L76 64 L80 82 L64 78 L56 92 L50 80 '
             'L44 92 L36 78 L20 82 L24 64 L10 58 L22 46 L12 32 L28 30 L28 14 L42 22 Z" fill="var(--c-risk)"/>'
             '<circle cx="41" cy="46" r="7" fill="#fff"/><circle cx="59" cy="46" r="7" fill="#fff"/>'
             '<circle cx="43" cy="47" r="3" fill="var(--ink)"/><circle cx="57" cy="47" r="3" fill="var(--ink)"/>'
             '<path d="M33 37 l14 4 M67 37 l-14 4" stroke="var(--ink)" stroke-width="3" stroke-linecap="round"/>'
             '<path d="M42 66 q8 -6 16 0" fill="none" stroke="var(--ink)" stroke-width="3" stroke-linecap="round"/>'),
    "Momentum": ("--c-mom", '<path d="M6 40 h14 M2 52 h16 M6 64 h14" stroke="var(--c-mom)" stroke-width="4" stroke-linecap="round" opacity=".6"/>'
                 '<circle cx="56" cy="52" r="34" fill="var(--c-mom)"/>'
                 '<rect x="36" y="16" width="40" height="8" rx="2" fill="var(--ink)"/><rect x="44" y="2" width="24" height="18" rx="3" fill="var(--ink)"/>'
                 '<circle cx="47" cy="50" r="4" fill="var(--ink)"/><circle cx="67" cy="50" r="4" fill="var(--ink)"/>'
                 '<path d="M46 66 q10 8 20 0" fill="none" stroke="var(--ink)" stroke-width="3" stroke-linecap="round"/>'),
    "Time": ("--c-time", '<circle cx="50" cy="54" r="36" fill="var(--c-time)"/><circle cx="50" cy="54" r="27" fill="#fff" opacity=".9"/>'
             '<path class="hand" d="M50 54 V34" stroke="var(--ink)" stroke-width="4" stroke-linecap="round"/>'
             '<path d="M50 54 h12" stroke="var(--ink)" stroke-width="4" stroke-linecap="round"/>'
             '<circle cx="50" cy="54" r="3.5" fill="var(--ink)"/>'
             '<rect x="44" y="10" width="12" height="9" rx="2" fill="var(--c-time)"/>'),
    "The Reporter": ("--c-rep", '<rect x="22" y="30" width="56" height="62" rx="8" fill="var(--c-rep)"/>'
                     '<path d="M14 30 h72 l-8 -12 h-56 z" fill="var(--ink)"/><rect x="30" y="8" width="40" height="12" rx="3" fill="var(--ink)"/>'
                     '<rect x="56" y="11" width="13" height="7" fill="#fff"/><circle cx="41" cy="50" r="4" fill="var(--ink)"/>'
                     '<circle cx="59" cy="50" r="4" fill="var(--ink)"/><path d="M42 68 q8 5 16 0" fill="none" stroke="var(--ink)" stroke-width="3" stroke-linecap="round"/>'
                     '<rect x="66" y="62" width="22" height="28" rx="2" fill="#fff" stroke="var(--ink)" stroke-width="2"/>'
                     '<path d="M70 70 h14 M70 76 h14 M70 82 h10" stroke="var(--ink)" stroke-width="2"/>'),
    "Doubt": ("--c-doubt", '<ellipse cx="50" cy="56" rx="30" ry="38" fill="var(--c-doubt)"/>'
              '<circle cx="40" cy="48" r="4" fill="var(--ink)"/><circle cx="60" cy="48" r="4" fill="var(--ink)"/>'
              '<path d="M32 36 l14 -2 M54 32 l14 6" stroke="var(--ink)" stroke-width="3" stroke-linecap="round"/>'
              '<path d="M42 70 h16" stroke="var(--ink)" stroke-width="3" stroke-linecap="round"/>'
              '<path d="M68 64 q10 -2 10 8" fill="none" stroke="var(--ink)" stroke-width="2.5" stroke-linecap="round" opacity=".7"/>'),
}


def office_chatter(st):
    """What each council member says around the office, from live state: (lines per member, two-person chats)."""
    lines = {n: [] for n in VOTERS}
    dialogs = []
    rec = st.get("record", {"wins": 0, "losses": 0, "pl": 0.0})
    for m in list(st.get("trades", {}).values())[:2]:
        nm = contract_name(m["sym"])
        upl = m.get("upl", 0) or 0
        lines["The Quant"].append(f"{nm} is {money(upl)} right now.")
        lines["Risk"].append(f"Stop on {nm} is ${m.get('sl', 0):.2f}. Not moving it the wrong way.")
        if m.get("locked"):
            lines["Risk"].append(f"{nm} has profit locked in. Breathe easy.")
        dialogs.append(["Momentum", f"{nm} is moving!", "Doubt", "Moving is not the same as working. Let's see the close."]
                       if upl >= 0 else ["Doubt", f"{nm} is under water.", "The Quant", "One trade. The edge shows up over many."])
    if not st.get("trades"):
        lines["Momentum"].append("Nothing open. IWM better give us something soon.")
    if rec["wins"] + rec["losses"]:
        lines["Doubt"].append(f"{rec['wins']}W–{rec['losses']}L so far. Prove it's not luck.")
        lines["The Quant"].append(f"Total so far: {money(rec['pl'])}.")
        dialogs.append(["The Quant", f"We're {rec['wins']}W–{rec['losses']}L.", "Risk", "And no blowups. That's the job."])
    if st.get("paused"):
        lines["Risk"].append(f"We're paused: {st['paused']}. Nobody trades.")
    e = et(dt.datetime.now(dt.timezone.utc))
    if (e.hour, e.minute) < (10, 0):
        lines["Time"].append("First half hour is wild. I like to let it settle.")
    elif (e.hour, e.minute) >= (15, 0):
        lines["Time"].append("Late in the day. 0DTE gets jumpy now.")
    try:
        g = json.load(open("gap_state.json", encoding="utf-8"))
        day = g.get("days", {}).get(e.date().isoformat(), {})
        if day.get("gap") is not None:
            lines["The Reporter"].append(f"IWM opened {day['gap']:+.2f}% from yesterday. Boomer's watching that gap.")
            dialogs.append(["The Reporter", f"IWM gapped {day['gap']:+.2f}% this morning.", "Time", "Boomer handles gaps. We wait for our own setup."])
    except (OSError, ValueError):
        pass
    lv = st.get("last_vote")
    if lv:
        short = (lv.get("label") or "the last one").split(" (")[0]
        for n, v in (lv.get("votes") or {}).items():
            ok, why = (v if isinstance(v, (list, tuple)) else (v, ""))
            if n in lines and ok is not None and why:
                reason = why.split(";")[0].split(",")[0].strip()
                reason = reason if len(reason) <= 48 else reason[:46].rsplit(" ", 1)[0] + "…"
                lines[n].append(f"I voted {'YES' if ok else 'NO'} on {short}: {reason}.")
    return lines, dialogs


def council_room(state):
    lv = state.get("last_vote")
    board = state.get("scoreboard", {})
    cr = state.get("council_record", {"right": 0, "total": 0})
    overall = f"{cr['right'] / cr['total']:.0%}" if cr["total"] else "—"
    overall_sub = (f"{cr['right']} of {cr['total']} settled calls right" if cr["total"]
                   else "fills in as markets settle")
    seats, voters = [], []
    for i, name in enumerate(VOTERS):
        tok, body = CHARACTERS[name]
        ok, why = (lv["votes"].get(name, [None, "—"]) if lv else [None, "waiting for a trade"])
        sign = {True: "YES", False: "NO", None: "—"}[ok]
        cls = {True: "yes", False: "no", None: "abs"}[ok]
        b = board.get(name, {"right": 0, "total": 0})
        acc = f"{b['right'] / b['total']:.0%} right" if b["total"] else "no record yet"
        voters.append({"name": name, "vote": ok, "why": why, "acc": acc})
        seats.append(f"""<button class="seat" data-i="{i}" style="--i:{i}" aria-label="{name}: {sign}. {html.escape(why)}">
          <span class="sign {cls}">{sign}</span>
          <svg class="char" viewBox="0 0 100 100" aria-hidden="true">{body}</svg>
          <span class="nm">{name}</span><span class="acc">{acc}</span>
          <span class="why">{html.escape(why)}</span></button>""")
    verdict = None
    if lv:
        yes = sum(1 for ok, _ in lv["votes"].values() if ok is True)
        no = sum(1 for ok, _ in lv["votes"].values() if ok is False)
        verdict = f"{'APPROVED' if lv['approved'] else 'REJECTED'} {yes}–{no}"
        when = dt.datetime.fromtimestamp(lv["when"], LOCAL_TZ).strftime("%a %I:%M %p")
        docket = (f'<a href="{html.escape(lv["url"])}" target="_blank" rel="noopener">{html.escape(lv["label"])}</a>'
                  + (f'<small>{html.escape(lv["detail"])} · voted {when} (<span class="ago" data-t="{int(lv["when"])}"></span>)</small>' if lv.get("detail") else
                  f'<small>BUY {lv["side"]} @ {round(lv["ask"] * 100)}¢ · edge {round(lv["edge"] * 100)}¢ · voted {when}</small>'))
        stamp = f'<div class="stamp {"ok" if lv["approved"] else "rej"}">{verdict}</div>'
    else:
        docket = "<span>No trade on the table yet.</span><small>The council votes when a market clears your minimum edge.</small>"
        stamp = ""
    talk, dialogs = office_chatter(state)
    data = json.dumps({"voters": voters, "verdict": verdict, "approved": bool(lv and lv["approved"]),
                       "title": (lv or {}).get("label"), "chart": (lv or {}).get("chart"),
                       "ticker": state.get("ticker", []), "recess": state.get("recess"),
                       "chatter": talk, "dialogs": dialogs, "lastVoteT": int(lv["when"]) if lv else 0,
                       "scanT": int(state.get("last_run") or time.time()), "nextVote": 0, "parties": [],
                       "bot": "Vault Options", "key": "vop"})
    script = ROOM_JS.replace("__DATA__", data.replace("</", "<\\/"))
    return f"""<section class="room" id="room3d" aria-label="Council room">
  <div class="roomhead">
    <div class="overall"><span class="lbl">Council accuracy</span><b>{overall}</b><small>{overall_sub}</small></div>
    <div class="docket"><span class="lbl">{'Last vote' if lv else 'On the table'}</span>{docket}
      {'<small class="stale" id="stalenote" hidden>No new setup since then. The council only meets when a pair has a setup, so this stays up until the next one.</small>' if lv else ''}</div>
  </div>
  <script>(function(){{function f(){{document.querySelectorAll('.docket .ago').forEach(function(e){{var m=Math.round((Date.now()/1000-(+e.dataset.t))/60);
    e.textContent=m<1?'just now':m<60?m+' min ago':Math.floor(m/60)+'h '+(m%60)+'m ago';
    var n=document.getElementById('stalenote');if(n)n.hidden=m<16;}});}}f();setInterval(f,30000);}})();</script>
  <div class="stage" id="stage3d"><div class="stamp3d">{stamp}</div><div class="tip3d" id="tip3d" hidden></div></div>
  <div class="floor">
    <div class="table"><div class="gavel" aria-hidden="true"></div>{stamp}</div>
    <div class="seats">{"".join(seats)}</div>
  </div>
  <div class="mood" role="group" aria-label="Tell the council how you feel">
    <button type="button" class="mood-btn good" data-k="cheers">🥂 Happy</button>
    <button type="button" class="mood-btn bad" data-k="sad">👎 Not happy</button>
    <span class="mood-msg" id="moodmsg" aria-live="polite"></span>
  </div>
  {f'<p class="mood-tg">Want it on the record? <a href="https://t.me/{state["bot_username"]}?start=happy" target="_blank" rel="noopener">Send 🥂 to the bot</a> · <a href="https://t.me/{state["bot_username"]}?start=unhappy" target="_blank" rel="noopener">send 👎</a></p>' if state.get("bot_username") else ''}
  <div class="roomctl"><button type="button" id="callOrder">📣 Call a meeting</button></div>
  <p class="hint">Each member works in their own office and chats in the hallway. When a vote happens they walk to the conference room, vote one by one (YES/NO over their heads), and the Trade Vote screen shows the result. Tap anyone to hear from them.</p>
  <script>(function(){{var msg=document.getElementById('moodmsg'),room=document.getElementById('room3d'),busy=0;
    var lines={{cheers:['🥂 Cheers, Commander! The council raises a glass.','🥂 To the next one!','🥂 Hooah! Drinks are on The Quant.'],
               sad:['😔 Understood, Commander. We’ll tighten up.','😔 Heard. Back to the drawing board.','😔 Noted. Discipline over hope.']}};
    room.querySelectorAll('.mood-btn').forEach(function(b){{b.addEventListener('click',function(){{
      var k=b.dataset.k,now=Date.now();if(now-busy<1500)return;busy=now;
      if(window.councilReact)window.councilReact(k);
      var l=lines[k];msg.textContent=l[Math.floor(Math.random()*l.length)];msg.className='mood-msg show '+k;
      if(!room.classList.contains('is3d')){{room.classList.remove('flat-cheers','flat-sad');void room.offsetWidth;room.classList.add(k==='cheers'?'flat-cheers':'flat-sad');}}
      clearTimeout(msg._t);msg._t=setTimeout(function(){{msg.className='mood-msg';}},5000);
    }});}});}})();</script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/objects/Reflector.js"></script>
  <script>{script}</script>
</section>"""


ROOM_JS = r"""
(function () {
  var D = __DATA__;
  var room = document.getElementById('room3d'), stage = document.getElementById('stage3d'), tip = document.getElementById('tip3d');
  var stampEl = stage.querySelector('.stamp3d');
  if (!window.THREE) return;
  var renderer;
  try { renderer = new THREE.WebGLRenderer({ antialias: true }); } catch (e) { return; }
  if (!renderer.getContext()) return;
  room.classList.add('is3d');
  renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
  renderer.shadowMap.enabled = true; renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  stage.insertBefore(renderer.domElement, stage.firstChild);
  var KEY = D.key || 'vault';
  var BOT = D.bot || 'Vault';
  var sc = new THREE.Scene(); sc.background = new THREE.Color(0x0f1324);
  var cam = new THREE.OrthographicCamera(-12, 12, 10, -10, 0.1, 200);
  var hemi = new THREE.HemisphereLight(0xe6eeff, 0x40342a, 0.85); sc.add(hemi);
  var sun = new THREE.DirectionalLight(0xfff1dc, 0.75); sun.position.set(-8, 30, 12); sun.castShadow = true;
  sun.shadow.mapSize.set(2048, 2048); Object.assign(sun.shadow.camera, { left: -16, right: 16, top: 16, bottom: -16 }); sc.add(sun);
  function M(c, o) { return new THREE.MeshStandardMaterial(Object.assign({ color: c, roughness: 0.75, metalness: 0.04 }, o || {})); }
  function box(w, h, d, m, x, y, z, p) { var b = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), m); b.position.set(x, y, z); b.castShadow = b.receiveShadow = true; (p || sc).add(b); return b; }
  function cyl(r1, r2, h, m, x, y, z, p, s) { var b = new THREE.Mesh(new THREE.CylinderGeometry(r1, r2, h, s || 18), m); b.position.set(x, y, z); b.castShadow = true; (p || sc).add(b); return b; }
  function sph(r, m, x, y, z, p) { var b = new THREE.Mesh(new THREE.SphereGeometry(r, 18, 14), m); b.position.set(x, y, z); b.castShadow = true; (p || sc).add(b); return b; }
  function canvasTex(w, h, draw) { var c = document.createElement('canvas'); c.width = w; c.height = h; var g = c.getContext('2d'); draw(g, w, h); var t = new THREE.CanvasTexture(c); t.anisotropy = 4; t.userData = { c: c, g: g, draw: draw }; return t; }
  function plane(w, h, tex, x, y, z, rx, ry) { var m = new THREE.Mesh(new THREE.PlaneGeometry(w, h), new THREE.MeshBasicMaterial({ map: tex })); m.position.set(x, y, z); m.rotation.set(rx || 0, ry || 0, 0); sc.add(m); return m; }
  function font(g, s, w) { g.font = (w || 700) + ' ' + s + 'px system-ui,-apple-system,Segoe UI,sans-serif'; }

  // ── the cast: fresh business-casual looks
  var LOOK = {
    'The Quant':    { c: '#3ee6a1', skin: 0x8d5a3b, top: 0x1f3a5f, under: 0xffffff, pants: 0x2e3440, hair: 0x15100c, style: 'short', glasses: 1, tie: 0x3ee6a1,
                      lines: ['Running the numbers again.', 'ADX says trend. I listen to ADX.', 'Data over feelings, always.', 'Three backtests agree. I like that.'] },
    'Risk':         { c: '#ff5468', skin: 0xe0b18f, top: 0x4a4f63, under: 0xf3e9dc, pants: 0x24262e, hair: 0x6b3f22, style: 'bun', scarf: 0xff5468,
                      lines: ['Where is the stop? Always ask where the stop is.', 'Protect the bankroll first.', 'Small size, long life.', 'No trade is also a position.'] },
    'Momentum':     { c: '#ffb03b', skin: 0x5c3a21, top: 0xffb03b, under: 0x2b2b2b, pants: 0x3b3f4a, hair: 0x0c0c0c, style: 'fade', sneakers: 1, cardigan: 1,
                      lines: ['I can feel a move coming.', 'MACD is warming up!', 'Trend is your friend, let’s ride it.', 'Who’s ready? I’m ready.'] },
    'Time':         { c: '#4cb7ff', skin: 0xf1c9a5, top: 0x6e4b8b, under: 0xf6efe4, pants: 0x2b2f3a, hair: 0xd1a24f, style: 'long',
                      lines: ['Right setup, wrong hour is still wrong.', 'London and New York overlap is where the action is.', 'Patience. The clock matters.', 'Too stretched. Let it come back to us.'] },
    'Doubt':        { c: '#c9a7ff', skin: 0xc68642, top: 0x2a2f3a, under: 0xc9a7ff, pants: 0x3a3a3a, hair: 0x2b1d14, style: 'curly', glasses: 1, vest: 1,
                      lines: ['Convince me.', 'I’ve seen this fake-out before.', 'What does the bigger chart say?', 'Somebody has to ask the hard question.'] },
    'The Reporter': { c: '#f472b6', skin: 0xa86b45, top: 0x8a6a4a, under: 0xffffff, pants: 0x2a2a2a, hair: 0x1b1410, style: 'ponytail', badge: 1,
                      lines: ['Checking the earnings calendar.', 'Headlines are quiet… too quiet.', 'StockTwits crowd is loud today.', 'Breaking: nothing breaking. Yet.'] },
    'The Chair':    { c: '#f4c84a', skin: 0x7a4a2c, top: 0x111827, under: 0xffffff, pants: 0x111827, hair: 0x222222, style: 'bun', tie: 0xf4c84a,
                      lines: ['Let’s keep it disciplined, team.', 'One good trade beats ten rushed ones.', 'Remember the rules: analyze, discuss, vote.', 'Coffee’s fresh. Charts are fresher.'] }
  };
  var CREW = D.voters.map(function (v) { return v.name; }).concat(['The Chair']);
  var N = D.voters.length;

  function person(L) {
    var g = new THREE.Group(), skin = M(L.skin), top = M(L.top), under = M(L.under), pants = M(L.pants), shoe = M(L.sneakers ? 0xf2f2f2 : 0x1b1b1b);
    var body = new THREE.Group(); g.add(body);
    var hips = new THREE.Group(); hips.position.y = 0.92; body.add(hips);
    var legs = [];
    [-0.14, 0.14].forEach(function (x) {
      var leg = new THREE.Group(); leg.position.set(x, 0, 0); hips.add(leg);
      cyl(0.11, 0.1, 0.5, pants, 0, -0.25, 0, leg);
      var knee = new THREE.Group(); knee.position.y = -0.5; leg.add(knee);
      cyl(0.095, 0.085, 0.42, pants, 0, -0.21, 0, knee);
      box(0.18, 0.09, 0.3, shoe, 0, -0.44, 0.06, knee);
      legs.push({ leg: leg, knee: knee });
    });
    var torso = new THREE.Group(); torso.position.y = 0.92; body.add(torso);
    cyl(0.3, 0.27, 0.7, (L.vest || L.cardigan) ? under : top, 0, 0.36, 0, torso);
    if (L.vest || L.cardigan) { var v = cyl(0.31, 0.28, 0.6, top, 0, 0.33, 0, torso); v.scale.z = 1.02; box(0.12, 0.55, 0.05, under, 0, 0.36, 0.29, torso); }
    else box(0.16, 0.42, 0.05, under, 0, 0.45, 0.27, torso);
    sph(0.3, (L.vest || L.cardigan) ? under : top, 0, 0.7, 0, torso).scale.y = 0.55;
    if (L.tie) box(0.07, 0.36, 0.04, M(L.tie), 0, 0.48, 0.3, torso);
    if (L.scarf) { var s_ = new THREE.Mesh(new THREE.TorusGeometry(0.17, 0.05, 8, 18), M(L.scarf)); s_.rotation.x = Math.PI / 2; s_.position.y = 0.8; torso.add(s_); }
    if (L.badge) { box(0.12, 0.16, 0.02, M(0xffffff), 0.14, 0.42, 0.3, torso); box(0.02, 0.32, 0.02, M(0xf472b6), 0.1, 0.62, 0.29, torso); }
    var arms = [];
    [-1, 1].forEach(function (s) {
      var sh = new THREE.Group(); sh.position.set(s * 0.37, 0.68, 0); torso.add(sh);
      cyl(0.085, 0.08, 0.4, top, 0, -0.2, 0, sh);
      var el = new THREE.Group(); el.position.y = -0.4; sh.add(el);
      cyl(0.075, 0.07, 0.36, top, 0, -0.18, 0, el);
      sph(0.085, skin, 0, -0.4, 0, el);
      arms.push({ sh: sh, el: el, s: s });
    });
    cyl(0.09, 0.1, 0.12, skin, 0, 0.82, 0, torso);
    var head = new THREE.Group(); head.position.y = 1.07; torso.add(head);
    var hm = sph(0.24, skin, 0, 0, 0, head); hm.scale.set(0.95, 1.06, 1);
    var eyeM = M(0x101010);
    [-0.085, 0.085].forEach(function (x) { sph(0.03, eyeM, x, 0.03, 0.215, head); });
    var smile = new THREE.Mesh(new THREE.TorusGeometry(0.055, 0.013, 6, 12, Math.PI), M(0x5a2a1a)); smile.rotation.z = Math.PI; smile.position.set(0, -0.08, 0.22); head.add(smile);
    var hair = M(L.hair);
    if (L.style === 'short' || L.style === 'fade') { var h1 = new THREE.Mesh(new THREE.SphereGeometry(L.style === 'fade' ? 0.245 : 0.25, 18, 12, 0, Math.PI * 2, 0, Math.PI / 2.3), hair); h1.position.y = 0.05; head.add(h1); }
    else if (L.style === 'curly') { for (var k = 0; k < 9; k++) { var a = k / 9 * Math.PI * 2; sph(0.1, hair, Math.cos(a) * 0.17, 0.16 + (k % 2) * 0.03, Math.sin(a) * 0.15 - 0.02, head); } sph(0.16, hair, 0, 0.2, 0, head); }
    else if (L.style === 'bun') { var h2 = new THREE.Mesh(new THREE.SphereGeometry(0.25, 18, 12, 0, Math.PI * 2, 0, Math.PI / 2.1), hair); h2.position.y = 0.03; head.add(h2); sph(0.11, hair, 0, 0.2, -0.14, head); }
    else if (L.style === 'ponytail') { var h3 = new THREE.Mesh(new THREE.SphereGeometry(0.25, 18, 12, 0, Math.PI * 2, 0, Math.PI / 2.1), hair); h3.position.y = 0.03; head.add(h3); var pt = cyl(0.06, 0.03, 0.34, hair, 0, -0.08, -0.26, head); pt.rotation.x = 0.4; }
    else { var h4 = new THREE.Mesh(new THREE.SphereGeometry(0.255, 18, 12, 0, Math.PI * 2, 0, Math.PI / 1.8), hair); h4.position.y = 0.02; head.add(h4); box(0.46, 0.4, 0.12, hair, 0, -0.14, -0.14, head); }
    if (L.glasses) { var gm = M(0x111111); [-0.085, 0.085].forEach(function (x) { var r_ = new THREE.Mesh(new THREE.TorusGeometry(0.055, 0.012, 6, 16), gm); r_.position.set(x, 0.03, 0.225); head.add(r_); }); }
    g.userData = { legs: legs, arms: arms, torso: torso, hips: hips, head: head, body: body };
    g.scale.setScalar(1.4);
    return g;
  }
  function pose(g, sit, walk, t, extra) {
    var u = g.userData, s = Math.sin(t * 9) * walk;
    u.legs.forEach(function (l, i) {
      var sg = i ? -s : s;
      l.leg.rotation.x = -sit * 1.45 + sg * 0.6;
      l.knee.rotation.x = sit * 1.45 + Math.max(0, -sg) * 0.6;
    });
    var dropY = sit * 0.42;
    u.hips.position.y = 0.92 - dropY; u.torso.position.y = 0.92 - dropY;
    u.arms.forEach(function (a, i) {
      var sg = i ? -s : s;
      a.sh.rotation.x = sg * 0.6 - sit * 0.55; a.el.rotation.x = -sit * 0.7; a.sh.rotation.z = a.s * 0.06;
    });
    if (extra === 'cheer') u.arms.forEach(function (a) { a.sh.rotation.z = a.s * 2.6; a.sh.rotation.x = 0; a.el.rotation.x = 0; });
    if (extra === 'shrug') u.arms.forEach(function (a) { a.sh.rotation.z = a.s * 0.7; a.el.rotation.x = -1.2; });
    if (extra === 'type') u.arms.forEach(function (a, i) { a.sh.rotation.x = -0.9; a.el.rotation.x = -0.6 + Math.sin(t * 14 + i * 2) * 0.12; });
    if (extra === 'talk') u.arms.forEach(function (a, i) { if (i) { a.sh.rotation.x = -0.6 + Math.sin(t * 5) * 0.3; a.el.rotation.x = -0.9; } });
    u.head.rotation.x = extra === 'sad' ? 0.45 : 0;
  }

  // ── the building
  var X0 = -11.5, X1 = 11.5, BLOCK_TOP = -11.5, HALL_A = -7.2, HALL_B = -4.8, BLOCK_BOT = -0.8, CONF_BOT = 9.2;
  var carpet = M(0x4a5170, { roughness: 0.95 }), hallF = M(0x6d7488), wood = M(0x8a6a4a, { roughness: 0.55 }), navy = M(0x1f2a4a), wallC = M(0xdcd9d2), partM = M(0x2c3a63), trim = M(0xb08a5a);
  box(X1 - X0, 0.1, BLOCK_BOT - BLOCK_TOP, hallF, 0, -0.05, (BLOCK_TOP + BLOCK_BOT) / 2);
  box(X1 - X0, 0.1, CONF_BOT - BLOCK_BOT, M(0x1c2440, { roughness: 0.95 }), 0, -0.05, (BLOCK_BOT + CONF_BOT) / 2);
  // outer walls
  box(X1 - X0 + 0.3, 1.2, 0.25, navy, 0, 0.6, BLOCK_TOP);
  box(0.25, 1.2, CONF_BOT - BLOCK_TOP, navy, X0, 0.6, (BLOCK_TOP + CONF_BOT) / 2);
  box(0.25, 1.2, CONF_BOT - BLOCK_TOP, navy, X1, 0.6, (BLOCK_TOP + CONF_BOT) / 2);
  box(X1 - X0 + 0.3, 1.2, 0.25, navy, 0, 0.6, CONF_BOT);
  // wall between offices and conference room, with a door at the right
  var DOOR_X = 9.9;
  box(DOOR_X - 0.9 - X0, 1.2, 0.25, navy, (X0 + DOOR_X - 0.9) / 2, 0.6, BLOCK_BOT);
  box(X1 - (DOOR_X + 0.9), 1.2, 0.25, navy, (X1 + DOOR_X + 0.9) / 2, 0.6, BLOCK_BOT);
  // windows on the left
  for (var wz = -10.5; wz < -1.5; wz += 2.2) box(0.08, 0.9, 1.7, M(0x8fb7e6, { emissive: 0x3a6ea8, emissiveIntensity: 0.6 }), X0 + 0.14, 0.7, wz);
  // lobby (left) with the Chair's desk
  var LOBBY_X = -6.6;
  box(X0 + 0.2 - LOBBY_X, 0.02, 4.2, M(0x55607f), (X0 + LOBBY_X) / 2, 0.01, -2.9);
  box(2.6, 0.75, 0.9, wood, -9.0, 0.38, -9.7);
  var signTex = canvasTex(512, 160, function (g, w, h) { g.fillStyle = '#16204a'; g.fillRect(0, 0, w, h); g.strokeStyle = '#f4c84a'; g.lineWidth = 6; g.strokeRect(6, 6, w - 12, h - 12);
    font(g, 34, 800); g.fillStyle = '#f4c84a'; g.fillText('📈 ' + BOT.toUpperCase(), 28, 66); font(g, 46, 900); g.fillStyle = '#fff'; g.fillText('TRADE ROOM', 28, 126); });
  plane(3.6, 1.12, signTex, -9.1, 0.03, -11.0, -Math.PI / 2, 0);
  // couches in the lobby
  box(0.7, 0.45, 2.2, M(0x1b1f2a), X0 + 0.7, 0.23, -3.2); box(2.0, 0.45, 0.7, M(0x1b1f2a), -9.0, 0.23, -1.6);
  cyl(0.45, 0.45, 0.4, wood, -9.3, 0.2, -3.1);
  // right wall motto
  var mottoTex = canvasTex(256, 512, function (g, w, h) { g.fillStyle = '#16204a'; g.fillRect(0, 0, w, h); font(g, 36, 800); g.fillStyle = '#fff';
    ['ANALYZE', 'DISCUSS', 'VOTE', 'PROFIT'].forEach(function (t, i) { g.fillText(t, 28, 110 + i * 82); }); g.fillStyle = '#3ee6a1'; font(g, 48, 900); g.fillText('↗', 28, 470); });
  plane(1.5, 3.0, mottoTex, 10.6, 0.04, -9.3, -Math.PI / 2, 0);
  // offices along the hallway
  var OFF_X0 = -6.4, OFF_X1 = 8.6, nTop = Math.ceil(N / 2), nBot = N - nTop;
  var monitorTex = [], seats = {}, offices = [];
  function makeOffice(i, row, k, n) {
    var w = (OFF_X1 - OFF_X0) / n, x0 = OFF_X0 + k * w, cx = x0 + w / 2;
    var top = row === 0, zBack = top ? BLOCK_TOP : BLOCK_BOT, zFront = top ? HALL_A : HALL_B, cz = (zBack + zFront) / 2;
    box(w - 0.2, 0.02, Math.abs(zBack - zFront) - 0.2, carpet, cx, 0.01, cz);
    box(0.18, 1.0, Math.abs(zBack - zFront), partM, x0, 0.5, cz); if (k === n - 1) box(0.18, 1.0, Math.abs(zBack - zFront), partM, x0 + w, 0.5, cz);
    // front wall with a door gap
    var gap = 1.1, seg = (w - gap) / 2;
    box(seg, 1.0, 0.16, partM, x0 + seg / 2, 0.5, zFront); box(seg, 1.0, 0.16, partM, x0 + w - seg / 2, 0.5, zFront);
    var doorLeaf = box(0.06, 0.95, gap * 0.9, trim, x0 + seg + 0.05, 0.48, zFront + (top ? -gap * 0.45 : gap * 0.45));
    // desk against the back wall
    var dz = zBack + (top ? 0.9 : -0.9);
    box(2.0, 0.08, 0.95, wood, cx, 0.78, dz); box(0.08, 0.76, 0.85, M(0x6b5238), cx - 0.92, 0.38, dz); box(0.08, 0.76, 0.85, M(0x6b5238), cx + 0.92, 0.38, dz);
    var name = D.voters[i].name, L = LOOK[name] || LOOK['The Quant'];
    var mt = canvasTex(256, 160, function (g, w2, h2) { g.fillStyle = '#0b1022'; g.fillRect(0, 0, w2, h2); font(g, 20, 800); g.fillStyle = L.c; g.fillText(name, 10, 26);
      g.strokeStyle = L.c; g.lineWidth = 3; g.beginPath(); var y = 110; for (var q = 0; q <= 24; q++) { y += Math.sin(q * 0.7 + i) * 7 + (Math.random() - 0.5) * 6; g.lineTo(10 + q * 9.6, Math.max(40, Math.min(150, y))); } g.stroke(); });
    var mon = plane(0.95, 0.6, mt, cx, 1.22, dz + (top ? -0.25 : 0.25), 0, top ? 0 : Math.PI);
    mon.rotation.x = top ? -0.5 : 0.5; monitorTex.push(mt);
    box(0.5, 0.03, 0.2, M(0x2a2a2a), cx, 0.83, dz + (top ? 0.2 : -0.2));
    cyl(0.07, 0.06, 0.13, M(0xffffff), cx + 0.7, 0.88, dz);
    // plant + chair
    cyl(0.18, 0.14, 0.35, M(0xd8d2c4), x0 + w - 0.45, 0.18, zBack + (top ? 0.45 : -0.45)); sph(0.34, M(0x2f8a4a), x0 + w - 0.45, 0.6, zBack + (top ? 0.45 : -0.45));
    var chairZ = dz + (top ? 1.05 : -1.05);
    box(0.62, 0.08, 0.58, M(0x22262f), cx, 0.52, chairZ); box(0.62, 0.62, 0.08, M(0x22262f), cx, 0.85, chairZ + (top ? 0.3 : -0.3));
    // name plate on the floor at the door (readable from above)
    var pl = canvasTex(256, 64, function (g, w2, h2) { g.fillStyle = '#0d1322'; g.fillRect(0, 0, w2, h2); g.fillStyle = L.c; g.fillRect(0, 0, 8, h2); font(g, 28, 800); g.fillStyle = '#fff'; g.fillText(name, 18, 42); });
    plane(1.7, 0.42, pl, cx, 0.03, zFront + (top ? 0.45 : -0.45), -Math.PI / 2, 0);
    seats[name] = { pos: new THREE.Vector3(cx, 0, chairZ), face: top ? Math.PI : 0, door: new THREE.Vector3(x0 + seg + gap / 2, 0, zFront), cx: cx, top: top };
    offices.push({ name: name, cx: cx, top: top });
  }
  for (var i = 0; i < N; i++) { if (i < nTop) makeOffice(i, 0, i, nTop); else makeOffice(i, 1, i - nTop, Math.max(1, nBot)); }
  seats['The Chair'] = { pos: new THREE.Vector3(-9.0, 0, -8.75), face: Math.PI, door: new THREE.Vector3(LOBBY_X, 0, (HALL_A + HALL_B) / 2), cx: -9, top: true };
  // hallway plants + water cooler
  [[-6.0, HALL_A + 0.4], [2.0, HALL_B - 0.35], [8.9, HALL_A + 0.4]].forEach(function (p) { cyl(0.2, 0.16, 0.4, M(0xd8d2c4), p[0], 0.2, p[1]); sph(0.36, M(0x2f8a4a), p[0], 0.65, p[1]); });
  cyl(0.22, 0.22, 1.0, M(0xe8eef6), 10.8, 0.5, -6.0); cyl(0.2, 0.2, 0.45, M(0x9fd3ff, { transparent: true, opacity: 0.8 }), 10.8, 1.2, -6.0);

  // ── conference room
  var TC = new THREE.Vector3(0, 0, 4.3), TA = 4.4, TB = 1.75;
  var tbl = new THREE.Mesh(new THREE.CylinderGeometry(1, 1, 0.12, 64), M(0x4a2f1f, { roughness: 0.35 })); tbl.scale.set(TA, 1, TB); tbl.position.set(TC.x, 0.8, TC.z); tbl.castShadow = tbl.receiveShadow = true; sc.add(tbl);
  cyl(0.5, 0.7, 0.75, M(0x222222), TC.x, 0.38, TC.z);
  sph(0.3, M(0x2f8a4a), TC.x, 1.05, TC.z);
  // vote screen (left wall) and rules board (right wall)
  var voteState = { yes: 0, no: 0, verdict: null, title: D.title || 'No trade on the table', counting: false, note: '' };
  var voteTex = canvasTex(512, 720, function () {});
  function drawVote() {
    var g = voteTex.userData.g, w = 512, h = 720;
    g.fillStyle = '#0b1230'; g.fillRect(0, 0, w, h); g.fillStyle = '#38e1ff'; g.fillRect(0, 0, w, 10);
    font(g, 54, 900); g.fillStyle = '#fff'; g.fillText('TRADE VOTE', 40, 92);
    font(g, 28, 700); g.fillStyle = '#c9d3f5';
    var t = voteState.title || '', lines = [], cur = '';
    t.split(' ').forEach(function (wd) { if ((cur + ' ' + wd).length > 24) { lines.push(cur); cur = wd; } else cur = (cur ? cur + ' ' : '') + wd; }); if (cur) lines.push(cur);
    lines.slice(0, 3).forEach(function (l, k) { g.fillText(l, 40, 150 + k * 36); });
    font(g, 120, 900); g.fillStyle = '#3ee6a1'; g.fillText(String(voteState.yes), 60, 400); g.fillStyle = '#ff5468'; g.fillText(String(voteState.no), 330, 400);
    font(g, 34, 800); g.fillStyle = '#3ee6a1'; g.fillText('YES', 64, 450); g.fillStyle = '#ff5468'; g.fillText('NO', 336, 450);
    g.fillStyle = '#ffffff33'; g.fillRect(250, 300, 4, 160);
    if (voteState.verdict) { var ok = /APPROVED/.test(voteState.verdict); g.fillStyle = ok ? '#3ee6a1' : '#ff5468'; g.fillRect(30, 520, w - 60, 110); font(g, 46, 900); g.fillStyle = '#0b1230'; g.fillText(voteState.verdict, 52, 592); }
    else { font(g, 30, 700); g.fillStyle = '#8f97b8'; g.fillText(voteState.note || '', 40, 590); }
    voteTex.needsUpdate = true;
  }
  plane(3.2, 4.5, voteTex, X0 + 1.9, 0.04, 4.3, -Math.PI / 2, 0); box(3.4, 0.06, 4.7, M(0x0a0e1c), X0 + 1.9, 0.0, 4.3);
  var rulesTex = canvasTex(400, 520, function (g, w, h) { g.fillStyle = '#f7f7f2'; g.fillRect(0, 0, w, h); g.strokeStyle = '#9aa2c2'; g.lineWidth = 8; g.strokeRect(4, 4, w - 8, h - 8);
    font(g, 40, 900); g.fillStyle = '#16204a'; g.fillText('TRADE RULES', 34, 72); font(g, 34, 700);
    ['1. Analyze', '2. Discuss', '3. Vote', '4. Execute'].forEach(function (t, k) { g.fillText(t, 44, 150 + k * 70); }); font(g, 54, 900); g.fillStyle = '#3ee6a1'; g.fillText('↗', 300, 470); });
  plane(2.4, 3.1, rulesTex, X1 - 1.6, 0.04, 4.6, -Math.PI / 2, 0);
  [[X0 + 1.0, 0.6], [X0 + 1.0, 8.2], [X1 - 1.0, 8.2], [X1 - 1.0, 0.6]].forEach(function (p) { cyl(0.22, 0.17, 0.45, M(0xd8d2c4), p[0], 0.22, p[1]); sph(0.42, M(0x2f8a4a), p[0], 0.75, p[1]); });
  // seats around the table: voters + the Chair (Chair at the head, near the screen)
  var confSeats = {};
  CREW.forEach(function (name, k) {
    var ang = name === 'The Chair' ? Math.PI : -Math.PI + (k + 1) * (2 * Math.PI / (N + 1));
    var pos = new THREE.Vector3(TC.x + Math.cos(ang) * (TA + 0.75), 0, TC.z + Math.sin(ang) * (TB + 0.75));
    confSeats[name] = { pos: pos, face: Math.atan2(TC.x - pos.x, TC.z - pos.z), ang: ang,
                        ring: new THREE.Vector3(TC.x + Math.cos(ang) * (TA + 1.7), 0, TC.z + Math.sin(ang) * (TB + 1.5)) };
    var ch = new THREE.Group(); ch.position.copy(pos); ch.rotation.y = confSeats[name].face; sc.add(ch);
    box(0.62, 0.08, 0.58, M(0x1b1f2a), 0, 0.52, 0, ch); box(0.62, 0.6, 0.08, M(0x1b1f2a), 0, 0.84, -0.32, ch);
    var paper = box(0.32, 0.01, 0.42, M(0xffffff), 0, 0.87, 0, sc); paper.position.set(TC.x + Math.cos(ang) * (TA - 0.55), 0.87, TC.z + Math.sin(ang) * (TB - 0.35)); paper.rotation.y = -ang;
  });

  // ── people
  var people = CREW.map(function (name) {
    var g = person(LOOK[name] || LOOK['The Quant']); sc.add(g);
    var s = seats[name]; var u = g.userData;
    u.name = name; u.pos = s.pos.clone(); u.face = s.face; u.rot = s.face; u.sit = 1; u.route = []; u.wantSit = true; u.where = 'desk'; u.task = null; u.used = {};
    g.position.copy(u.pos); g.rotation.y = u.rot; return g;
  });
  function P(name) { return people[CREW.indexOf(name)]; }
  var HALL_Z = (HALL_A + HALL_B) / 2;
  function pathToConf(name) {
    var s = seats[name], c = confSeats[name], pts = [], AISLE = BLOCK_BOT + 1.0;
    pts.push(s.door.clone()); pts.push(new THREE.Vector3(s.door.x, 0, HALL_Z));
    pts.push(new THREE.Vector3(DOOR_X, 0, HALL_Z)); pts.push(new THREE.Vector3(DOOR_X, 0, AISLE));
    if (Math.sin(c.ang) <= 0.05) pts.push(new THREE.Vector3(c.ring.x, 0, AISLE));
    else { var sx = c.ring.x >= 0 ? TA + 2.0 : -(TA + 2.0); pts.push(new THREE.Vector3(sx, 0, AISLE)); pts.push(new THREE.Vector3(sx, 0, c.ring.z)); }
    pts.push(c.ring.clone()); pts.push(c.pos.clone()); return pts;
  }
  function pathToDesk(name) { return pathToConf(name).reverse().slice(1).concat([seats[name].pos.clone()]); }
  function walk(name, pts, face, sitAfter, where) { var u = P(name).userData; u.route = pts; u.face = face; u.wantSit = sitAfter; u.where = where; u.sitDelay = 0; }
  function toConf(name) { walk(name, pathToConf(name), confSeats[name].face, true, 'conf'); }
  function toDesk(name) { var u = P(name).userData; if (u.where === 'desk' && !u.route.length) return; walk(name, u.where === 'conf' ? pathToDesk(name) : [seats[name].door.clone(), seats[name].pos.clone()], seats[name].face, true, 'desk'); }
  function settled(name) { var u = P(name).userData; return !u.route.length && u.sit > 0.95; }

  // ── speech bubbles + vote tags
  var bubWrap = document.createElement('div'); bubWrap.className = 'bubbles'; stage.appendChild(bubWrap);
  var pill = document.createElement('div'); pill.className = 'phasepill'; stage.appendChild(pill);
  var banner = document.createElement('div'); banner.className = 'partybanner'; banner.hidden = true; stage.appendChild(banner);
  var bubs = [], tags = {}, V = new THREE.Vector3();
  function colorOf(name) { return (LOOK[name] || {}).c || '#fff'; }
  function speak(name, text, dur) {
    if (!text) return;
    bubs = bubs.filter(function (b) { if (b.name === name) { b.el.remove(); return false; } return true; });
    while (bubs.length >= 3) bubs.shift().el.remove();
    if (text.length > 96) text = text.slice(0, 94).replace(/\s+\S*$/, '') + '…';
    var el = document.createElement('div'); el.className = 'bub'; el.style.setProperty('--bc', colorOf(name));
    var b = document.createElement('b'); b.textContent = name; el.appendChild(b); el.appendChild(document.createTextNode(text));
    bubWrap.appendChild(el); requestAnimationFrame(function () { el.classList.add('on'); });
    var len = dur || Math.min(6, 2.4 + text.length * 0.045);
    bubs.push({ name: name, el: el, until: performance.now() / 1000 + len });
    P(name).userData.talkUntil = performance.now() / 1000 + Math.min(len, 2.6);
  }
  function tag(name, vote) {
    if (tags[name]) tags[name].remove();
    if (vote === undefined) { delete tags[name]; return; }
    var el = document.createElement('div'); el.className = 'vtag ' + (vote === true ? 'yes' : vote === false ? 'no' : 'abs');
    el.textContent = vote === true ? 'YES' : vote === false ? 'NO' : '—'; bubWrap.appendChild(el); tags[name] = el;
  }
  function screenPos(name, lift) {
    P(name).userData.head.getWorldPosition(V); V.y += lift; V.project(cam);
    var W = stage.clientWidth, H = renderer.domElement.clientHeight; return { x: (V.x + 1) / 2 * W, y: (1 - V.y) / 2 * H, W: W, H: H };
  }
  function placeOverlays(nowP) {
    var placed = [];
    bubs = bubs.filter(function (b) {
      if (nowP > b.until) { b.el.classList.remove('on'); setTimeout(function () { b.el.remove(); }, 300); return false; }
      var s = screenPos(b.name, 0.5), bw = b.el.offsetWidth || 130, bh = b.el.offsetHeight || 40;
      var left = Math.max(4, Math.min(s.W - bw - 4, s.x - bw / 2)), top = Math.max(bh + 4, Math.min(s.H - 4, s.y - 6));
      placed.forEach(function (p) { if (left < p.r && left + bw > p.l && top > p.t - bh - 4 && top - bh < p.t + 4) top = Math.max(bh + 4, p.t - p.h - 6); });
      placed.push({ l: left, r: left + bw, t: top, h: bh });
      b.el.style.left = left + 'px'; b.el.style.top = top + 'px'; b.el.style.setProperty('--tx', Math.max(12, Math.min(bw - 12, s.x - left)) + 'px');
      return true;
    });
    Object.keys(tags).forEach(function (name) { var s = screenPos(name, 0.25), el = tags[name]; el.style.left = (s.x - 18) + 'px'; el.style.top = (s.y - 14) + 'px'; });
  }
  function lineFor(name) {
    var live = ((D.chatter || {})[name] || []).slice(), pool = live.length ? (Math.random() < 0.75 ? live : live.concat((LOOK[name] || {}).lines || [])) : ((LOOK[name] || {}).lines || []);
    var u = P(name).userData, fresh = pool.filter(function (l) { return !u.used[l]; });
    if (!fresh.length) { u.used = {}; fresh = pool; }
    var l = fresh[Math.floor(Math.random() * fresh.length)]; u.used[l] = 1; return l;
  }
  function sget(k, ses) { try { return (ses ? sessionStorage : localStorage).getItem(KEY + k); } catch (e) { return null; } }
  function sset(k, v, ses) { try { (ses ? sessionStorage : localStorage).setItem(KEY + k, v); } catch (e) {} }
  function sdel(k, ses) { try { (ses ? sessionStorage : localStorage).removeItem(KEY + k); } catch (e) {} }

  // ── phases: work (at desks) → assemble (walk to the conference room) → hearing (vote) → back to work; party; recess
  var phase = { name: 'work', t: 0 }, nextChat = 2, nowS = function () { return Date.now() / 1000; }, confetti = [];
  function startWork(t, msg) { phase = { name: 'work', t: t }; CREW.forEach(function (n) { tag(n); toDesk(n); }); if (msg) speak('The Chair', msg, 3.5); stampEl.classList.remove('show'); voteState.verdict = null; voteState.note = nextNote(); drawVote(); }
  function startAssemble(t, then, msg) { phase = { name: 'assemble', t: t, then: then, readyAt: t + CREW.length * 0.35 + 0.8 }; CREW.forEach(function (n, k) { setTimeout(function () { toConf(n); }, k * 350); }); speak('The Chair', msg || 'Conference room, everyone. We have a setup.', 3.5); }
  function startHearing(t) {
    voteState = { yes: 0, no: 0, verdict: null, title: D.title || 'Last trade', note: '' }; drawVote();
    phase = { name: 'hearing', t: t, step: -1, nextAt: t + 1.2 };
    CREW.forEach(function (n) { var u = P(n).userData, c = confSeats[n]; if (u.pos.distanceTo(c.pos) > 0.05 || u.route.length) { u.pos.copy(c.pos); u.rot = u.face = c.face; u.route = []; u.wantSit = true; u.sit = 1; } u.where = 'conf'; });
    sset('Heard', String(D.lastVoteT));
  }
  function hearingTick(t) {
    if (t < phase.nextAt) return;
    phase.step++;
    if (phase.step === 0) { speak('The Chair', 'Order. On the table: ' + (D.title || 'the last setup') + '.', 4); phase.nextAt = t + 3.6; return; }
    var i = phase.step - 1;
    if (i < N) {
      var v = D.voters[i]; tag(v.name, v.vote);
      if (v.vote === true) voteState.yes++; else if (v.vote === false) voteState.no++; drawVote();
      speak(v.name, (v.vote === true ? 'YES. ' : v.vote === false ? 'NO. ' : 'Pass. ') + (v.why || ''), 4.2);
      phase.nextAt = t + 2.8; return;
    }
    if (i === N) {
      voteState.verdict = D.verdict; drawVote(); stampEl.classList.add('show');
      speak('The Chair', D.approved ? (D.verdict + '. We’re in. Risk, set the stop.') : (D.verdict + '. We pass on this one.'), 4.5);
      if (D.approved) CREW.forEach(function (n) { P(n).userData.cheerUntil = t + 2.5; });
      phase.nextAt = t + 6; return;
    }
    startWork(t, 'Back to your desks. Good meeting.');
  }
  function startParty(t, Pt) {
    phase = { name: 'party', t: t }; sset('Party', String(Pt.t));
    banner.textContent = ''; var h = document.createElement('b'); h.textContent = Pt.kind === 'win' ? '🎉 THE THEORY WORKED' : Pt.kind === 'comeback' ? '🎉 COMEBACK' : '🎉 THEORY PROVEN'; banner.appendChild(h);
    var a = document.createElement('span'); a.textContent = Pt.title || ''; banner.appendChild(a); var c = document.createElement('small'); c.textContent = Pt.detail || ''; banner.appendChild(c); banner.hidden = false;
    CREW.forEach(function (n) { toConf(n); });
    for (var k = 0; k < 120; k++) { var m = new THREE.Mesh(new THREE.PlaneGeometry(0.16, 0.26), new THREE.MeshBasicMaterial({ color: [0x3ee6a1, 0xf4c84a, 0x38e1ff, 0xff4fd8, 0xff5468][k % 5], side: 2 }));
      m.position.set(TC.x + (Math.random() - 0.5) * 12, 4 + Math.random() * 6, TC.z + (Math.random() - 0.5) * 6); m.userData.v = (0.02 + Math.random() * 0.03); sc.add(m); confetti.push(m); }
  }
  function endParty(t) { banner.hidden = true; confetti.forEach(function (m) { sc.remove(m); }); confetti = []; startWork(t, 'Okay okay, back to work. Great job, team.'); }
  function nextNote() { if (D.recess) return 'Off duty · market closed'; var nv = D.nextVote || 0, s = Math.max(0, Math.round(nv - nowS())); return nv ? 'Next vote in ' + Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0') : 'Waiting for a setup'; }
  function phaseLabel() {
    if (phase.name === 'hearing') return '⚖️ Vote in session';
    if (phase.name === 'assemble') return '📣 Heading to the conference room';
    if (phase.name === 'party') return '🎉 Party!';
    if (phase.name === 'wait') return '🗳 Counting votes…';
    if (D.recess) return '☕ Off duty · market closed';
    return '💼 At their desks · ' + nextNote();
  }

  // dialogs: two officers meet in the hallway
  function startDialog(t) {
    var ds = (D.dialogs || []).filter(function (d) { return CREW.indexOf(d[0]) >= 0 && CREW.indexOf(d[2]) >= 0; });
    if (!ds.length) return false;
    var d = ds[Math.floor(Math.random() * ds.length)], a = d[0], b = d[2];
    var mx = (seats[a].door.x + seats[b].door.x) / 2;
    var pa = new THREE.Vector3(mx - 0.55, 0, HALL_Z), pb = new THREE.Vector3(mx + 0.55, 0, HALL_Z);
    walk(a, [seats[a].door.clone(), new THREE.Vector3(seats[a].door.x, 0, HALL_Z), pa], Math.PI / 2, false, 'hall');
    walk(b, [seats[b].door.clone(), new THREE.Vector3(seats[b].door.x, 0, HALL_Z), pb], -Math.PI / 2, false, 'hall');
    phase.dialog = { a: a, b: b, l1: d[1], l2: d[3], stage: 0, t: t };
    return true;
  }
  function workTick(t) {
    var dg = phase.dialog;
    if (dg) {
      if (dg.stage === 0 && settled2(dg.a) && settled2(dg.b)) { speak(dg.a, dg.l1, 3.6); dg.stage = 1; dg.t = t; }
      else if (dg.stage === 1 && t > dg.t + 3.4) { speak(dg.b, dg.l2, 3.8); dg.stage = 2; dg.t = t; }
      else if (dg.stage === 2 && t > dg.t + 4.2) { toDesk(dg.a); toDesk(dg.b); phase.dialog = null; nextChat = t + 3; }
      else if (t > dg.t + 30) { toDesk(dg.a); toDesk(dg.b); phase.dialog = null; }
      return;
    }
    if (t > nextChat) {
      if (!D.recess && Math.random() < 0.35 && startDialog(t)) return;
      var who = CREW[Math.floor(Math.random() * CREW.length)];
      speak(who, D.recess && Math.random() < 0.4 ? 'Market’s closed. Catching up on my notes.' : lineFor(who));
      nextChat = t + 4.5 + Math.random() * 3.5;
    }
    // auto-assemble about a minute before a scheduled vote
    var nv = D.nextVote || 0, ns = nowS();
    if (!D.recess && nv && nv - ns < 60 && nv - ns > 0) { startAssemble(t, 'wait', 'Conference room in a minute, team. New candle coming.'); sset('Wait', String(nv), true); }
  }
  function settled2(name) { return !P(name).userData.route.length; }
  function waitTick(t) {
    var w = +(sget('Wait', true) || 0), ns = nowS();
    voteState.note = 'Counting votes…'; if (Math.floor(t) % 2 === 0) drawVote();
    if (w && ns > w + 75 && !phase.reloading) {
      phase.reloading = true; var tries = +(sget('Tries', true) || 0); sset('Tries', String(tries + 1), true);
      if (tries < 5) location.replace(location.pathname + '?r=' + Math.floor(ns)); else { sdel('Wait', true); startWork(t, 'Nothing new this candle. Back to work.'); }
    }
  }

  // ── controls the page already has
  window.councilReact = function (kind) {
    var t = performance.now() / 1000;
    CREW.forEach(function (n, k) { var u = P(n).userData; if (kind === 'cheers') u.cheerUntil = t + 2.5; else u.sadUntil = t + 3; });
    var who = CREW[Math.floor(Math.random() * N)];
    speak(who, kind === 'cheers' ? 'Thanks, Commander! 🥂' : 'Heard. We’ll tighten it up.', 3);
  };
  var callBtn = document.getElementById('callOrder'), partyBtn = document.getElementById('partyBtn');
  if (callBtn) {
    if (!D.verdict) callBtn.textContent = '📣 Call a meeting (no vote yet)';
    callBtn.addEventListener('click', function () {
      if (phase.name === 'hearing' || phase.name === 'assemble') return;
      if (phase.name === 'party') { banner.hidden = true; confetti.forEach(function (m) { sc.remove(m); }); confetti = []; }
      if (D.verdict) startAssemble(performance.now() / 1000, 'hearing', 'Meeting! Let’s go over the last vote.');
      else { startAssemble(performance.now() / 1000, 'nothing', 'Quick meeting: nothing to vote on yet.'); }
    });
  }
  var lastParty = (D.parties || [])[(D.parties || []).length - 1];
  if (partyBtn && lastParty) { partyBtn.hidden = false; partyBtn.addEventListener('click', function () { if (phase.name === 'work') startParty(performance.now() / 1000, lastParty); }); }

  // tap an officer to see their last vote
  var ray = new THREE.Raycaster(), ptr = new THREE.Vector2();
  renderer.domElement.addEventListener('click', function (e) {
    var r = renderer.domElement.getBoundingClientRect(); ptr.set((e.clientX - r.left) / r.width * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(ptr, cam); var best = null;
    people.forEach(function (g, k) { var hits = ray.intersectObject(g, true); if (hits.length && (!best || hits[0].distance < best.d)) best = { d: hits[0].distance, k: k }; });
    if (!best) { tip.hidden = true; return; }
    var name = CREW[best.k], v = D.voters.find(function (x) { return x.name === name; });
    tip.innerHTML = ''; var b = document.createElement('b'); b.textContent = name; tip.appendChild(b);
    var s1 = document.createElement('span'); s1.textContent = v ? ((v.vote === true ? 'Voted YES' : v.vote === false ? 'Voted NO' : 'No vote') + ': ' + (v.why || '')) : 'Runs the meeting'; tip.appendChild(s1);
    if (v) { var s2 = document.createElement('small'); s2.textContent = v.acc || ''; tip.appendChild(s2); }
    tip.hidden = false; clearTimeout(tip._t); tip._t = setTimeout(function () { tip.hidden = true; }, 6000);
    speak(name, lineFor(name));
  });

  // ── layout
  function size() {
    var w = stage.clientWidth || 360, h = Math.round(Math.min(680, Math.max(330, w * 0.92)));
    renderer.setSize(w, h);
    var halfW = 12.2, halfH = halfW * h / w;
    if (halfH < 10.9) { halfH = 10.9; halfW = halfH * w / h; }
    cam.left = -halfW; cam.right = halfW; cam.top = halfH; cam.bottom = -halfH;
    cam.position.set(0, 40, 13); cam.lookAt(0, 0, -1.0); cam.updateProjectionMatrix();
  }
  size(); window.addEventListener('resize', size);

  // ── boot
  (function boot() {
    var ns = nowS(), wait = +(sget('Wait', true) || 0);
    voteState.note = nextNote(); drawVote();
    var Pt = (D.parties || []).filter(function (p) { return p.t > +(sget('Party') || 0) && ns - p.t < 172800; }).pop();
    if (D.recess) { hemi.intensity = 0.55; sun.intensity = 0.45; }
    if (wait && !D.recess) {
      sdel('Wait', true);
      if (D.lastVoteT && D.lastVoteT >= wait - 120 && D.verdict) { sdel('Tries', true); startHearing(0); return; }
      if ((D.scanT || 0) < wait && ns < wait + 420) { CREW.forEach(function (n) { var u = P(n).userData, c = confSeats[n]; u.pos.copy(c.pos); u.rot = u.face = c.face; u.where = 'conf'; }); sset('Wait', String(wait), true); phase = { name: 'wait', t: 0 }; return; }
      sdel('Tries', true); startWork(0, 'No setups this candle. Back to work, team!'); return;
    }
    if (D.verdict && D.lastVoteT && ns - D.lastVoteT < 900 && sget('Heard') !== String(D.lastVoteT) && !D.recess) { startHearing(0); return; }
    if (Pt) { startParty(0, Pt); return; }
    startWork(0);
  })();

  // ── loop
  var last = performance.now() / 1000, t0 = last;
  function tick() {
    var now = performance.now() / 1000, dt = Math.min(0.05, now - last), t = now - t0; last = now;
    people.forEach(function (g) {
      var u = g.userData, walking = 0;
      if (u.route.length) {
        u.sit = Math.max(0, u.sit - dt * 3);
        if (u.sit < 0.05) {
          var tgt = u.route[0], d = tgt.clone().sub(u.pos); d.y = 0; var L = d.length(), step = 3.2 * dt;
          if (L <= step) { u.pos.copy(tgt); u.route.shift(); } else { u.pos.add(d.multiplyScalar(step / L)); u.rot = Math.atan2(tgt.x - u.pos.x, tgt.z - u.pos.z); walking = 1; }
        }
      } else {
        var dr = u.face - u.rot; while (dr > Math.PI) dr -= 2 * Math.PI; while (dr < -Math.PI) dr += 2 * Math.PI; u.rot += dr * Math.min(1, dt * 6);
        if (u.wantSit) u.sit = Math.min(1, u.sit + dt * 2.5); else u.sit = Math.max(0, u.sit - dt * 3);
      }
      if (walking) { var dr2 = u.rot - g.rotation.y; while (dr2 > Math.PI) dr2 -= 2 * Math.PI; while (dr2 < -Math.PI) dr2 += 2 * Math.PI; g.rotation.y += dr2 * Math.min(1, dt * 12); }
      else g.rotation.y = u.rot;
      g.position.copy(u.pos);
      var extra = now < (u.cheerUntil || 0) ? 'cheer' : now < (u.sadUntil || 0) ? 'sad' : now < (u.talkUntil || 0) ? 'talk' : (u.where === 'desk' && u.sit > 0.9 && phase.name === 'work') ? 'type' : (phase.name === 'party' && !u.route.length ? 'cheer' : null);
      if (extra === 'cheer' && u.sit < 0.5) g.position.y = Math.abs(Math.sin(t * 7 + u.pos.x)) * 0.25;
      pose(g, u.sit, walking, t, extra);
    });
    if (phase.name === 'work') workTick(t);
    else if (phase.name === 'assemble') {
      if (t > phase.readyAt && CREW.every(function (n) { return settled(n) && P(n).userData.where === 'conf'; })) { if (phase.then === 'hearing') startHearing(t); else if (phase.then === 'wait') phase = { name: 'wait', t: t }; else startWork(t, 'Nothing to vote on. As you were.'); }
    }
    else if (phase.name === 'hearing') hearingTick(t);
    else if (phase.name === 'wait') waitTick(t);
    else if (phase.name === 'party') { confetti.forEach(function (m) { m.position.y -= m.userData.v * 60 * dt; m.rotation.x += dt * 4; m.rotation.z += dt * 3; if (m.position.y < 0.05) m.position.y = 6 + Math.random() * 4; }); if (t > phase.t + 14) endParty(t); }
    if (Math.floor(t) !== Math.floor(t - dt) && phase.name === 'work') { voteState.note = nextNote(); drawVote(); }
    pill.textContent = phaseLabel(); pill.className = 'phasepill ' + phase.name;
    renderer.render(sc, cam); placeOverlays(now);
    requestAnimationFrame(tick);
  }
  window.__office = { people: people, ph: function () { return phase; } };
  requestAnimationFrame(tick);
})();

"""



# ───────────────────────────── Dashboard ─────────────────────────────────────

FEED_COLORS = {"The Quant": "#3ee6a1", "Risk": "#ff5468", "Momentum": "#ffb03b", "Time": "#4cb7ff", "Doubt": "#c9a7ff", "The Reporter": "#ff7ac8",
               "Chair": "#f4c84a"}


def feed_panel(st):
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    raw = f"https://raw.githubusercontent.com/{repo}/main/{STATE_FILE}" if repo else ""
    data = json.dumps({"feed": st.get("feed", [])[-40:], "last_run": st.get("last_run"), "raw": raw,
                       "colors": FEED_COLORS}).replace("</", "<\\/")
    return f"""<section class="feedwrap" aria-label="Live council feed">
  <div class="feedhead"><h2>Live council</h2><span class="beat" id="beat">…</span></div>
  <div id="feed" class="feed" aria-live="polite"></div>
  <script type="application/json" id="feeddata">{data}</script>
  <script>{FEED_JS}</script>
</section>"""


FEED_JS = r"""
(function () {
  var D = JSON.parse(document.getElementById('feeddata').textContent);
  var box = document.getElementById('feed'), beat = document.getElementById('beat'), seen = {};
  var still = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  function ago(t) {
    var s = Math.max(0, Date.now() / 1000 - t);
    return s < 60 ? 'just now' : s < 3600 ? Math.round(s / 60) + ' min ago' : s < 86400 ? Math.round(s / 3600) + 'h ago' : Math.round(s / 86400) + 'd ago';
  }
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function bubble(who, vote, text) {
    var row = el('div', 'msg');
    var dot = el('span', 'dot'); dot.style.background = D.colors[who] || '#9a92bf'; row.appendChild(dot);
    var body = el('div', 'body');
    var top = el('div', 'who', who); top.style.color = D.colors[who] || '#c9bdf0';
    if (vote === true || vote === false) top.appendChild(el('span', 'chip ' + (vote ? 'yes' : 'no'), vote ? 'YES' : 'NO'));
    body.appendChild(top); body.appendChild(el('div', 'say', text)); row.appendChild(body);
    return row;
  }
  function card(e, fresh) {
    var c = el('article', 'card ' + e.kind + (fresh && !still ? ' fresh' : ''));
    var h = el('header'); h.appendChild(el('b', null, e.title)); h.appendChild(el('time', null, ago(e.t)));
    c.appendChild(h);
    if (e.kind === 'event') c.appendChild(bubble(e.who || 'Chair', null, e.text || ''));
    (e.lines || []).forEach(function (l, i) {
      var b = e.kind === 'scan' ? (function () { var r = el('div', 'watch'); r.appendChild(el('b', null, l.who)); r.appendChild(el('span', null, l.say)); return r; })()
                                : bubble(l.who, l.vote, l.say);
      if (fresh && !still) b.style.animationDelay = (0.15 + i * 0.35) + 's';
      c.appendChild(b);
    });
    if (e.verdict) { var v = el('div', 'verdict ' + (e.ok ? 'ok' : 'rej'), '⚖️ ' + e.verdict);
      if (fresh && !still) v.style.animationDelay = (0.3 + (e.lines || []).length * 0.35) + 's'; c.appendChild(v); }
    return c;
  }
  function render(feed, animate) {
    var items = feed.slice().reverse();
    box.textContent = '';
    if (!items.length) { box.appendChild(el('p', 'empty', 'The council hasn’t met yet. Debates show up here as soon as a setup appears.')); }
    items.forEach(function (e) { var k = e.t + e.title; var fresh = animate && !seen[k]; seen[k] = 1; box.appendChild(card(e, fresh)); });
  }
  function heartbeat(t) {
    if (!t) { beat.textContent = 'waiting for first run'; beat.className = 'beat'; return; }
    var s = Date.now() / 1000 - t;
    beat.textContent = (s < 1200 ? '● live · ' : '○ ') + 'last check ' + ago(t);
    beat.className = 'beat ' + (s < 1200 ? 'on' : 'stale');
  }
  render(D.feed || [], false); heartbeat(D.last_run);
  function poll() {
    if (!D.raw) return;
    fetch(D.raw + '?t=' + Date.now(), { cache: 'no-store' }).then(function (r) { return r.ok ? r.json() : null; }).then(function (st) {
      if (!st) return;
      D.last_run = st.last_run; heartbeat(st.last_run);
      render((st.feed || []).slice(-40), true);
    }).catch(function () {});
  }
  setInterval(function () { heartbeat(D.last_run); }, 30000);
  setInterval(poll, 60000);
})();
"""


def recess_panel(st):
    now = dt.datetime.now(dt.timezone.utc)
    if in_session(PAIRS[0], now):
        return ""
    t, _ = next_vote(now)
    e = et(now)
    if market_open_now(now):
        early = (e.hour, e.minute) < ENTRY_WINDOW[0]
        return f"""<section class="recess night"><div><span class="rb">{'OPENING BELL' if early else 'LAST HOUR'}</span>
          <b>{'The council lets the first 15 minutes settle before trading.' if early else 'No new trades after 3:00 PM. Same-day options are sold by 3:40; weeklies can stay open overnight.'}</b>
          <p>{'First votes at 9:45 AM ET.' if early else 'Back tomorrow at 9:45 AM ET.'}</p></div></section>"""
    resume = t.astimezone(LOCAL_TZ)
    return f"""<section class="recess"><div><span class="rb">⚖️ COURT IS OUT OF SESSION</span>
      <b>The stock market is closed. The council resumes after the opening bell.</b>
      <p>First votes {resume.strftime('%a %I:%M %p').replace(' 0', ' ')} ET. {held_text(st)}</p></div>
      <div class="rc" id="rc" data-t="{int(t.timestamp())}">--:--:--</div>
      <script>(function(){{var e=document.getElementById('rc'),t=+e.dataset.t;function f(){{var s=Math.max(0,Math.round(t-Date.now()/1000));
      var d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60),x=s%60;
      e.textContent=s?((d?d+'d ':'')+h+'h '+String(m).padStart(2,'0')+'m '+String(x).padStart(2,'0')+'s'):'back in session';}}f();setInterval(f,1000);}})();</script>
    </section>"""


def next_panel(st):
    now = dt.datetime.now(dt.timezone.utc)
    t, live = next_vote(now)
    when = t.astimezone(LOCAL_TZ).strftime("%I:%M %p").lstrip("0")
    day = "" if t.astimezone(LOCAL_TZ).date() == now.astimezone(LOCAL_TZ).date() else t.astimezone(LOCAL_TZ).strftime(" %a")
    rows = []
    pairs = st.get("pairs", {})
    def order(p):
        r = pairs.get(p, {}).get("ready") or {}
        return (not r.get("in_trade"), not r.get("session"), -(r.get("passed") or 0))
    for p in sorted(PAIRS, key=order):
        r = (pairs.get(p) or {}).get("ready")
        if not r:
            continue
        side = "CALL" if r["dir"] == 1 else "PUT"
        if r["in_trade"]:
            note, cls = "In a trade. The council is managing it.", "trade"
        elif not r["session"]:
            note, cls = "Market closed or outside 9:45–3:00. It'll be judged at the next session.", "off"
        elif not r["waiting"]:
            note, cls = "Everything lines up. Expect a vote at the next candle.", "hot"
        else:
            note = "Holding back: " + "; ".join(w if w.startswith(("a pullback", "a fresh")) else w for w in r["waiting"])
            note = note.replace("Holding back: a pullback", "Needs a pullback")
            cls = "warm" if r["passed"] >= 4 else ""
        pct = round(100 * r["passed"] / r["of"])
        rows.append(f"""<li class="{cls}"><div class="rl"><b>{NAMES[p]}</b><span class="bias {'buy' if r['dir'] == 1 else 'sell'}">{side} bias · 15m</span>
          <span class="cnt">{r['passed']}/{r['of']} checks</span></div>
          <div class="rbar" role="img" aria-label="{r['passed']} of {r['of']} checks pass"><i style="width:{pct}%"></i></div>
          <p>{html.escape(note)}</p></li>""")
    body = "".join(rows) or "<li><p>Readiness shows up after the first full scan.</p></li>"
    return f"""<section class="nextwrap" aria-label="Next hearing">
  <div class="nh"><div><h2>Next hearing</h2><p class="muted"><span id="cdl">{when}{day}</span> ET candle · {len(live)} market{'s' if len(live) != 1 else ''} in session</p></div>
  <div class="cd" id="cd" data-t="{int(t.timestamp())}">--:--</div></div>
  <ol class="ready">{body}</ol>
  <script>(function(){{var e=document.getElementById('cd'),t=+e.dataset.t;function f(){{var s=Math.max(0,Math.round(t-Date.now()/1000));
  if(Date.now()/1000-t>60){{t+=900*Math.ceil((Date.now()/1000-t-60)/900);try{{document.getElementById('cdl').textContent=
    new Date(t*1000).toLocaleTimeString('en-US',{{hour:'numeric',minute:'2-digit',timeZone:'America/New_York'}});}}catch(_){{}}}}
  s=Math.max(0,Math.round(t-Date.now()/1000));
  var h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;e.textContent=s?((h?h+':'+String(m).padStart(2,'0'):m)+':'+String(x).padStart(2,'0')):'voting now';}}f();setInterval(f,1000);}})();</script>
</section>"""


def bankroll_chart(st):
    eq = st.get("equity", [])
    start = START_BALANCE or (eq[0][1] if eq else 0)
    if len(eq) < 2:
        return f"""<section class="chartwrap"><h2>Bankroll</h2><p class="muted">The chart starts after the first couple of scans.
          It tracks your ${start:,.0f} from here, with every closed trade marked.</p></section>"""
    W, H, L, R, T, B = 640, 230, 52, 14, 14, 26
    xs, ys = [e[0] for e in eq], [e[1] for e in eq]
    x0, x1 = xs[0], max(xs[-1], xs[0] + 3600)
    lo, hi = min(ys + [start]), max(ys + [start])
    pad = max((hi - lo) * 0.15, start * 0.005)
    lo, hi = lo - pad, hi + pad
    X = lambda t: L + (t - x0) / (x1 - x0) * (W - L - R)
    Y = lambda v: T + (hi - v) / (hi - lo) * (H - T - B)
    pts = " ".join(f"{X(t):.1f},{Y(v):.1f}" for t, v in eq)
    area = f"M{X(xs[0]):.1f},{Y(lo):.1f} L" + pts.replace(" ", " L") + f" L{X(xs[-1]):.1f},{Y(lo):.1f} Z"
    grid = ""
    for i in range(4):
        v = lo + (hi - lo) * i / 3
        grid += (f'<line x1="{L}" x2="{W - R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" class="gl"/>'
                 f'<text x="{L - 8}" y="{Y(v) + 4:.1f}" class="yl">${v:,.2f}</text>')
    fmt = lambda t: dt.datetime.fromtimestamp(t, LOCAL_TZ).strftime("%b %d %I:%M %p").replace(" 0", " ")
    xl = (f'<text x="{L}" y="{H - 6}" class="xl">{fmt(x0)}</text>'
          f'<text x="{W - R}" y="{H - 6}" class="xr">{fmt(xs[-1])}</text>')
    base = (f'<line x1="{L}" x2="{W - R}" y1="{Y(start):.1f}" y2="{Y(start):.1f}" class="base"/>'
            f'<text x="{W - R}" y="{Y(start) - 6:.1f}" class="bl">start ${start:,.0f}</text>')

    def nav_at(t):
        prev = eq[0]
        for e in eq:
            if e[0] >= t:
                return e[1]
            prev = e
        return prev[1]
    marks, mdata = "", []
    for tr in st.get("closed", []):
        t = tr.get("closed_ts")
        if not t or t < x0:
            continue
        v, won = nav_at(t), tr.get("pl", 0) > 0
        cx, cy = X(min(t, xs[-1])), Y(v)
        shape = (f'<path d="M{cx:.1f},{cy - 6:.1f} L{cx + 6:.1f},{cy + 4:.1f} L{cx - 6:.1f},{cy + 4:.1f} Z" class="mk win"/>' if won else
                 f'<path d="M{cx:.1f},{cy + 6:.1f} L{cx + 6:.1f},{cy - 4:.1f} L{cx - 6:.1f},{cy - 4:.1f} Z" class="mk loss"/>')
        marks += shape
        mdata.append([round(cx, 1), f"{contract_name(tr.get('sym', tr['pair']))} {money(tr.get('pl', 0))}"])
    last = ys[-1]
    chg = last - start
    data = json.dumps({"pts": [[round(X(t), 1), round(Y(v), 1), fmt(t), f"${v:,.2f}"] for t, v in eq], "marks": mdata})
    return f"""<section class="chartwrap"><div class="chd"><h2>Bankroll</h2>
  <span class="{'pos' if chg >= 0 else 'neg'}">${last:,.2f} ({money(chg)} since start)</span></div>
  <div class="chart" id="bkchart">
  <svg viewBox="0 0 {W} {H}" role="img" aria-label="Bankroll over time, from ${ys[0]:,.2f} to ${last:,.2f}">
    <defs><linearGradient id="bkg" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#4cb7ff" stop-opacity=".28"/>
      <stop offset="1" stop-color="#4cb7ff" stop-opacity="0"/></linearGradient></defs>
    {grid}{base}<path d="{area}" fill="url(#bkg)"/><polyline points="{pts}" class="ln"/>
    <circle cx="{X(xs[-1]):.1f}" cy="{Y(last):.1f}" r="4.5" class="end"/>{marks}{xl}
    <line id="bkx" class="cross" y1="{T}" y2="{H - B}" x1="-10" x2="-10"/><circle id="bkd" r="5" class="end" cx="-10" cy="-10"/>
    <rect x="{L}" y="0" width="{W - L - R}" height="{H}" fill="#000" fill-opacity="0" pointer-events="all" id="bkhit"/>
  </svg><div class="ctip" id="bktip" hidden></div></div>
  <p class="muted legend"><span class="win">▲</span> winning trade closed · <span class="loss">▼</span> losing trade closed · dashed line = starting bankroll</p>
  <script>(function(){{var D={data},svg=document.querySelector('#bkchart svg'),hit=document.getElementById('bkhit'),
  x=document.getElementById('bkx'),d=document.getElementById('bkd'),tip=document.getElementById('bktip');
  function mv(e){{var r=svg.getBoundingClientRect(),sx={W}/r.width,px=(e.clientX-r.left)*sx,b=D.pts[0];
  D.pts.forEach(function(p){{if(Math.abs(p[0]-px)<Math.abs(b[0]-px))b=p;}});
  x.setAttribute('x1',b[0]);x.setAttribute('x2',b[0]);d.setAttribute('cx',b[0]);d.setAttribute('cy',b[1]);
  var m=D.marks.filter(function(k){{return Math.abs(k[0]-b[0])<10;}}).map(function(k){{return k[1];}});
  tip.textContent='';var a=document.createElement('b');a.textContent=b[3];tip.appendChild(a);
  var s=document.createElement('span');s.textContent=b[2]+' ET';tip.appendChild(s);
  m.forEach(function(t){{var q=document.createElement('span');q.textContent=t;tip.appendChild(q);}});
  tip.hidden=false;var left=b[0]/sx;tip.style.left=Math.min(Math.max(left-70,0),r.width-150)+'px';}}
  function out(){{tip.hidden=true;x.setAttribute('x1',-10);x.setAttribute('x2',-10);d.setAttribute('cx',-10);}}
  hit.addEventListener('pointermove',mv);hit.addEventListener('pointerdown',mv);hit.addEventListener('pointerleave',out);}})();</script>
</section>"""


def alpaca_banner(st):
    e = st.get("alpaca_error")
    if not e:
        return ""
    when = dt.datetime.fromtimestamp(e["t"], LOCAL_TZ).strftime("%a %I:%M %p")
    fix = ("Alpaca rejected the API keys (401). In Alpaca, switch to your <b>Paper</b> account, open API Keys, click "
           "<b>Generate</b> (or Regenerate), and paste the two values into the repo's <b>ALPACA_KEY_ID</b> and "
           "<b>ALPACA_SECRET_KEY</b> secrets." if e.get("keys") else html.escape(e["text"]))
    return (f'<section style="border:2px solid #ff5468;border-radius:14px;padding:14px 16px;background:#2a1220;color:#ffd7dc">'
            f'<b style="color:#ff8a98">⚠️ Not trading: can\'t log in to Alpaca</b> (last try {when} ET)<br>{fix}</section>')


def held_text(st):
    n = len(st.get("trades", {}))
    if not n:
        return "Nothing is being held overnight."
    return (f"{n} weekly option{'s are' if n > 1 else ' is'} being held overnight. Stops and targets resume at the open "
            "(a gap can fill past the stop).")


def build_html(st, log):
    acc = st.get("account", {})
    nav = acc.get("nav")
    day = st.get("day", {})
    today = (nav - day.get("start_nav", nav)) if nav is not None else None
    rec = st.get("record", {"wins": 0, "losses": 0, "pl": 0.0})
    mode = st.get("mode", OANDA_ENV).upper()
    paused = st.get("paused")
    updated = dt.datetime.now(LOCAL_TZ).strftime("%a %b %d, %I:%M %p ET")

    def m(x):
        return "—" if x is None else money(x)

    def tr_row(tid, t):
        upl = t.get("upl", 0)
        return f"""<tr><td class="mkt">{html.escape(contract_name(t['sym']))}<small>{t.get('setup', '')} · bought {et(t['opened']):%I:%M %p} ET{' · 🔒 breakeven' if t.get('breakeven') else ''}</small></td>
          <td data-l="Type"><span class="side {'buy' if t['dir'] == 1 else 'sell'}">{'CALL' if t['dir'] == 1 else 'PUT'}</span></td>
          <td class="n" data-l="Qty">{t['qty']}</td><td class="n" data-l="Paid">${t['entry']:.2f}</td>
          <td class="n" data-l="Now">${(t.get('price') or 0):.2f}</td><td class="n" data-l="Stop">${t['sl']:.2f}</td>
          <td class="n" data-l="Target">${t['tp']:.2f}</td><td class="n {'pos' if upl >= 0 else 'neg'}" data-l="P&amp;L">{m(upl)}</td></tr>"""

    def cl_row(t):
        pl = t.get("pl", 0)
        return f"""<tr><td class="mkt">{html.escape(contract_name(t.get('sym', t['pair'])))}<small>{t.get('why', '')} · {et(t.get('closed', '2000-01-01T00:00:00')):%a %I:%M %p} ET</small></td>
          <td data-l="Type"><span class="side {'buy' if t['dir'] == 1 else 'sell'}">{'CALL' if t['dir'] == 1 else 'PUT'}</span></td>
          <td class="n" data-l="Qty">{t.get('qty', 1)}</td><td class="n {'pos' if pl >= 0 else 'neg'}" data-l="Result">{m(pl)}</td></tr>"""

    def pair_card(pair):
        p = st.get("pairs", {}).get(pair)
        if not p:
            return f'<div class="pc"><b>{NAMES[pair]}</b><span class="muted">waiting for first scan</span></div>'
        arrow = "▲ up" if p["trend"] == 1 else "▼ down"
        agree = "agrees" if p["trend"] == p["h4_trend"] else "disagrees"
        return f"""<div class="pc"><div class="pch"><b>{NAMES[pair]}</b><span class="tr {'up' if p['trend'] == 1 else 'dn'}">{arrow}</span></div>
          <dl><div><dt>Price</dt><dd>${p['close']:,.2f}</dd></div><div><dt>Today</dt><dd class="{'pos' if p.get('chg', 0) >= 0 else 'neg'}">{p.get('chg', 0):+.1f}%</dd></div>
          <div><dt>ADX</dt><dd>{(p['adx'] or 0):.0f}</dd></div><div><dt>RSI</dt><dd>{(p['rsi'] or 0):.0f}</dd></div></dl>
          <small>1-hour trend {agree} · {('setup: ' + p['setup']) if p['setup'] else 'no setup'}</small></div>"""

    open_rows = "".join(tr_row(k, v) for k, v in st.get("trades", {}).items()) or \
        '<tr><td colspan="8" class="empty">No open trades. The bot waits for the council to approve a setup during market hours.</td></tr>'
    closed = list(reversed(st.get("closed", [])))[:15]
    closed_rows = "".join(cl_row(t) for t in closed) or '<tr><td colspan="4" class="empty">No closed trades yet.</td></tr>'
    board = st.get("scoreboard", {})
    weights = voter_weights(st)
    board_rows = "".join(
        f"""<tr><td class="mkt">{n}<small>{ROLES[n]}</small></td>
        <td class="n" data-l="Called it">{(f"{board[n]['right'] / board[n]['total']:.0%}") if board.get(n, {}).get('total') else '—'}</td>
        <td class="n" data-l="Vote weight">{weights[n]:g}×</td>
        <td class="n" data-l="Right / scored">{board.get(n, {}).get('right', 0)} / {board.get(n, {}).get('total', 0)}</td></tr>"""
        for n in VOTERS)
    benched = st.get("learn", {}).get("benched", {})
    learn_line = ("Benched (losing lately, still scored on paper): " + ", ".join(
        f"{NAMES[c.split(':')[0]]} {c.split(':')[1]} ({v:+.2f}R)" for c, v in benched.items())) if benched else \
        f"Nothing benched. Vote weights start changing after {LEARN_MIN_CALLS} scored calls."
    soon = [e for e in st.get("news", {}).get("events", [])
            if e["cur"] in set().union(*(pair_currencies(p) for p in PAIRS)) and time.time() - NEWS_AFTER_MIN * 60 <= e["t"] <= time.time() + 36 * 3600][:5]
    news_rows = "".join(f"<li>{html.escape(news_label(e))}</li>" for e in soon) or "<li>Nothing high-impact in the next 36 hours.</li>"

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="300">
<title>Vault Options</title>
<style>
:root {{ --bg:#0f0c1d; --panel:#17132b; --fg:#ece8fb; --muted:#9a92bf; --line:#2c2550; --accent:#f4c84a;
  --pos:#53f0a6; --neg:#ff6b7c; --buy:#53f0a6; --sell:#ff6b7c; color-scheme:dark }}
* {{ box-sizing:border-box }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:14px/1.45 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif; padding:20px 16px }}
.wrap {{ max-width:1100px; margin:0 auto; display:grid; gap:18px }}
header {{ display:flex; flex-wrap:wrap; align-items:center; gap:10px; justify-content:space-between }}
header h1 {{ margin:0; font-size:22px; letter-spacing:-.01em }} header p {{ margin:2px 0 0; color:var(--muted) }}
.badge {{ font:700 11px/1 ui-monospace,Menlo,monospace; letter-spacing:.14em; padding:6px 10px; border-radius:3px; border:1px solid }}
.badge.practice {{ color:#4cb7ff }} .badge.live {{ color:var(--neg) }} .badge.paused {{ color:var(--accent) }}
.stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px }}
.stat {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:10px 14px }}
.stat span {{ color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.08em }}
.stat b {{ display:block; font-size:22px; font-variant-numeric:tabular-nums }}
.pos {{ color:var(--pos) }} .neg {{ color:var(--neg) }} .muted {{ color:var(--muted) }}
h2 {{ font-size:12px; text-transform:uppercase; letter-spacing:.1em; color:var(--muted); margin:6px 0 6px }}
.pairs {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:10px }}
.pc {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px 14px; display:grid; gap:8px }}
.pch {{ display:flex; justify-content:space-between }} .tr.up {{ color:var(--pos) }} .tr.dn {{ color:var(--neg) }}
.pc dl {{ margin:0; display:grid; grid-template-columns:repeat(4,1fr); gap:6px }} .pc dt {{ font-size:10px; color:var(--muted); text-transform:uppercase }}
.pc dd {{ margin:0; font-variant-numeric:tabular-nums }} .pc small {{ color:var(--muted) }}
.tbl {{ overflow-x:auto; background:var(--panel); border:1px solid var(--line); border-radius:8px }}
table {{ width:100%; border-collapse:collapse }}
th,td {{ padding:8px 10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top }}
th {{ font-size:11px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); font-weight:600 }}
td.n {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap }}
td.mkt small {{ display:block; color:var(--muted); font-size:12px }}
.side {{ font:700 11px/1 ui-monospace,Menlo,monospace; padding:3px 7px; border-radius:3px; border:1px solid currentColor }}
.side.buy {{ color:var(--buy) }} .side.sell {{ color:var(--sell) }}
.empty {{ color:var(--muted); text-align:center; padding:22px }}
.news {{ margin:0; padding-left:18px; display:grid; gap:4px }}
.learnwrap {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px; display:grid; gap:10px }}
.learnwrap h2 {{ margin:0 }} .learnwrap .fine {{ margin:0 }}
.lv {{ margin:0; padding:8px 10px; border-radius:8px; border:1px solid var(--line) }}
.lv.good {{ border-color:var(--pos); color:var(--pos) }} .lv.bad {{ border-color:var(--neg); color:var(--neg) }} .lv.wait {{ color:var(--accent) }}
.lgrid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:8px }}
.lgrid div {{ border:1px solid var(--line); border-radius:8px; padding:8px 10px; display:grid; gap:2px }}
.lgrid span {{ font-size:11px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted) }}
.lgrid b {{ font-size:20px; font-variant-numeric:tabular-nums }} .lgrid small {{ color:var(--muted); font-size:11px }}
.recess {{ display:flex; justify-content:space-between; align-items:center; gap:14px; flex-wrap:wrap; padding:16px;
  border-radius:12px; border:1px solid #f4c84a66; background:linear-gradient(135deg,#2a1f08,#15102a) }}
.recess.night {{ border-color:#4cb7ff66; background:linear-gradient(135deg,#0b1a33,#15102a) }}
.recess div {{ display:grid; gap:4px; min-width:0 }} .recess b {{ font-size:16px }} .recess p {{ margin:0; color:var(--muted); font-size:13px }}
.rb {{ font:800 12px/1 ui-monospace,Menlo,monospace; letter-spacing:.12em; color:var(--accent) }} .recess.night .rb {{ color:#4cb7ff }}
.rc {{ font:700 28px/1 ui-monospace,Menlo,monospace; color:var(--accent); font-variant-numeric:tabular-nums }}
.nextwrap, .chartwrap {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px; display:grid; gap:10px }}
.nextwrap h2, .chartwrap h2 {{ margin:0 }}
.nh {{ display:flex; justify-content:space-between; align-items:center; gap:12px }} .nh p {{ margin:4px 0 0; font-size:12px }}
.cd {{ font:700 26px/1 ui-monospace,Menlo,monospace; color:var(--accent); font-variant-numeric:tabular-nums }}
.ready {{ list-style:none; margin:0; padding:0; display:grid; gap:8px }}
.ready li {{ border:1px solid var(--line); border-radius:8px; padding:9px 11px; display:grid; gap:6px }}
.ready li.hot {{ border-color:var(--pos); background:#53f0a610 }} .ready li.warm {{ border-color:#f4c84a66 }}
.ready li.trade {{ border-color:#4cb7ff88 }} .ready li.off {{ opacity:.55 }}
.rl {{ display:flex; gap:8px; align-items:center; flex-wrap:wrap }} .rl .cnt {{ margin-left:auto; color:var(--muted); font-size:12px; font-variant-numeric:tabular-nums }}
.bias {{ font:700 10px/1 ui-monospace,Menlo,monospace; padding:3px 6px; border-radius:3px; border:1px solid currentColor }}
.bias.buy {{ color:var(--buy) }} .bias.sell {{ color:var(--sell) }}
.rbar {{ height:6px; border-radius:99px; background:var(--line); overflow:hidden }}
.rbar i {{ display:block; height:100%; border-radius:99px; background:var(--accent) }}
.ready li.hot .rbar i {{ background:var(--pos) }}
.ready p {{ margin:0; font-size:12.5px; color:var(--muted) }}
.chd {{ display:flex; justify-content:space-between; align-items:baseline; gap:10px; flex-wrap:wrap }} .chd span {{ font-variant-numeric:tabular-nums; font-weight:600 }}
.chart {{ position:relative }} .chart svg {{ width:100%; height:auto; display:block; touch-action:pan-y }}
.chart .gl {{ stroke:var(--line); stroke-width:1 }} .chart .yl, .chart .xl {{ fill:var(--muted); font-size:11px; text-anchor:end; font-variant-numeric:tabular-nums }}
.chart .xl {{ text-anchor:start }} .chart .xr {{ fill:var(--muted); font-size:11px; text-anchor:end }} .chart .base {{ stroke:var(--muted); stroke-dasharray:4 4; stroke-width:1 }}
.chart .bl {{ fill:var(--muted); font-size:11px; text-anchor:end }}
.chart .ln {{ fill:none; stroke:#4cb7ff; stroke-width:2; stroke-linejoin:round; stroke-linecap:round }}
.chart .end {{ fill:#4cb7ff; stroke:var(--panel); stroke-width:2 }}
.chart .mk {{ stroke:var(--panel); stroke-width:2 }} .chart .mk.win {{ fill:var(--pos) }} .chart .mk.loss {{ fill:var(--neg) }}
.chart .cross {{ stroke:var(--muted); stroke-width:1 }}
.ctip {{ position:absolute; top:6px; width:150px; background:#0f0c1dee; border:1px solid var(--line); border-radius:8px; padding:6px 9px;
  display:grid; gap:2px; font-size:12px; pointer-events:none }} .ctip[hidden] {{ display:none }} .ctip b {{ font-variant-numeric:tabular-nums }} .ctip span {{ color:var(--muted) }}
.legend {{ margin:0; font-size:12px }} .legend .win {{ color:var(--pos) }} .legend .loss {{ color:var(--neg) }}
.feedwrap {{ background:linear-gradient(180deg,#1a1436,#120e24); border:1px solid var(--line); border-radius:12px; padding:14px; display:grid; gap:10px }}
.feedhead {{ display:flex; justify-content:space-between; align-items:center; gap:10px }}
.feedhead h2 {{ margin:0 }}
.beat {{ font:600 11px/1 ui-monospace,Menlo,monospace; color:var(--muted) }} .beat.on {{ color:var(--pos) }} .beat.stale {{ color:var(--accent) }}
.feed {{ display:grid; gap:10px; max-height:560px; overflow-y:auto; padding-right:4px }}
.card {{ background:#0f0c1dcc; border:1px solid #2c2550; border-radius:10px; padding:10px 12px; display:grid; gap:8px }}
.card header {{ display:flex; justify-content:space-between; gap:8px; align-items:baseline }}
.card header b {{ font-size:14px }} .card header time {{ color:var(--muted); font-size:11px; white-space:nowrap }}
.card.scan {{ background:transparent; border-style:dashed }}
.msg {{ display:grid; grid-template-columns:10px 1fr; gap:8px; align-items:start }}
.dot {{ width:10px; height:10px; border-radius:50%; margin-top:5px; box-shadow:0 0 8px currentColor }}
.who {{ font-weight:700; font-size:12px; display:flex; gap:6px; align-items:center }}
.chip {{ font:700 10px/1 ui-monospace,Menlo,monospace; padding:2px 5px; border:1px solid currentColor; border-radius:3px }}
.chip.yes {{ color:var(--pos) }} .chip.no {{ color:var(--neg) }}
.say {{ font-size:13px; color:#e4def9 }}
.watch {{ display:grid; grid-template-columns:80px 1fr; gap:8px; font-size:12px; color:var(--muted) }} .watch b {{ color:var(--fg); font-weight:600 }}
.verdict {{ font:600 12px/1.4 ui-monospace,Menlo,monospace; padding:7px 9px; border-radius:6px; border:1px solid }}
.verdict.ok {{ color:var(--pos); background:#53f0a612 }} .verdict.rej {{ color:var(--neg); background:#ff6b7c12 }}
.feed .empty {{ color:var(--muted); margin:6px 0 }}
.fresh {{ animation:fin .4s ease-out both }} .fresh .msg, .fresh .verdict, .fresh .watch {{ animation:fin .4s ease-out both }}
@keyframes fin {{ from {{ opacity:0; transform:translateY(6px) }} to {{ opacity:1; transform:none }} }}
@media (prefers-reduced-motion: reduce) {{ .fresh, .fresh * {{ animation:none !important }} }}
details {{ color:var(--muted) }} .fine {{ color:var(--muted); font-size:12px; max-width:78ch }}
@media (max-width:720px) {{
  .tbl {{ background:none; border:0 }} .tbl table tr:first-child {{ display:none }}
  .tbl tr {{ display:grid; grid-template-columns:repeat(3,1fr); gap:6px 8px; background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:12px; margin-bottom:10px }}
  .tbl td {{ border:0; padding:0; text-align:left !important; min-width:0 }}
  .tbl td.mkt, .tbl td.empty {{ grid-column:1/-1 }}
  .tbl td[data-l]::before {{ content:attr(data-l); display:block; font-size:10px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted) }}
}}
.room {{ --ink:#1b1430; --c-quant:#7ccf8a; --c-risk:#e8565b; --c-mom:#f2a23c; --c-time:#5aa7e0; --c-doubt:#d9c3a5; --c-rep:#ff8fcf;
  background:radial-gradient(120% 90% at 50% 0%, #3a2a6b 0%, #1d1538 60%, #140f28 100%); color:#efe9ff;
  border-radius:14px; padding:16px; overflow:hidden; display:grid; gap:12px }}
.roomhead {{ display:grid; grid-template-columns:auto 1fr; gap:16px; align-items:end }}
.overall {{ display:grid; gap:2px; padding-right:16px; border-right:1px solid #4a3a85 }}
.overall b {{ font:800 40px/1 ui-sans-serif,-apple-system,sans-serif; color:#f4c84a; font-variant-numeric:tabular-nums; letter-spacing:-.02em }}
.overall small {{ color:#c9bdf0; font-size:11px }}
.overall .lbl {{ font-size:11px; letter-spacing:.1em; text-transform:uppercase; color:#b6a8e6 }}
.stage {{ display:none; position:relative; border-radius:10px; overflow:hidden; touch-action:pan-y }}
.stage canvas {{ display:block; width:100% !important; cursor:pointer }}
.is3d .stage {{ display:block }}
.is3d .table, .is3d .char, .is3d .seat .sign {{ display:none }}
.is3d .seats {{ margin-top:-4px }}
.is3d .seat:hover .why, .is3d .seat:focus .why {{ display:none }}
.mood {{ display:flex; gap:10px; align-items:center; flex-wrap:wrap; justify-content:center }}
.mood-btn {{ font:700 15px/1 ui-sans-serif,-apple-system,sans-serif; padding:12px 18px; border-radius:999px; border:2px solid; cursor:pointer;
  background:transparent; min-height:44px; transition:transform .12s, background .2s }}
.mood-btn:active {{ transform:scale(.95) }} .mood-btn:focus-visible {{ outline:2px solid #f4c84a; outline-offset:3px }}
.mood-btn.good {{ color:#53f0a6; border-color:#53f0a6 }} .mood-btn.good:hover {{ background:#53f0a61f }}
.mood-btn.bad {{ color:#ff6b7c; border-color:#ff6b7c }} .mood-btn.bad:hover {{ background:#ff6b7c1f }}
.mood-msg {{ flex-basis:100%; text-align:center; font-weight:600; min-height:1.4em; opacity:0; transition:opacity .3s }}
.mood-msg.show {{ opacity:1 }} .mood-msg.cheers {{ color:#f6d36b }} .mood-msg.sad {{ color:#ff9aa5 }}
.mood-tg {{ margin:0; text-align:center; font-size:12px; color:#9c90c8 }} .mood-tg a {{ color:#c9bdf0 }}
.room.flat-cheers .char {{ animation:hop .5s ease-in-out 4 }} .room.flat-sad .char {{ animation:droop 2.5s ease-in-out 1 }}
@keyframes hop {{ 50% {{ transform:translateY(-12px) rotate(-4deg) }} }}
@keyframes droop {{ 30%,70% {{ transform:translateY(6px) rotate(6deg); filter:grayscale(.6) }} }}
.stamp3d {{ position:absolute; left:50%; bottom:14px; transform:translateX(-50%); pointer-events:none; opacity:0; transition:opacity .4s }}
.stamp3d.show {{ opacity:1 }}
.stamp3d .stamp {{ animation:none; transform:none; font:600 12px/1 ui-monospace,SFMono-Regular,Menlo,monospace; letter-spacing:.22em;
  padding:8px 14px; border-width:1px; border-radius:2px; background:#0e0a1ecc; box-shadow:0 0 18px currentColor }}
.tip3d {{ position:absolute; left:10px; top:10px; max-width:min(260px, 70%); display:grid; gap:3px; background:#2b2150ee;
  border:1px solid #6a58b8; border-radius:10px; padding:8px 10px; font-size:12px; color:#efe9ff }}
.tip3d[hidden] {{ display:none }}
.bubbles {{ position:absolute; inset:0; pointer-events:none; overflow:hidden }}
.bub {{ position:absolute; max-width:min(150px, 46%); padding:6px 9px 7px; border-radius:12px; background:#0d1322f0; color:#eaf2ff;
  font:600 11.5px/1.3 ui-sans-serif,-apple-system,"Segoe UI",sans-serif; border:1.5px solid var(--bc); box-shadow:0 6px 16px #0009;
  transform:translateY(-100%) scale(.6); transform-origin:var(--tx, 50%) 100%; opacity:0; transition:opacity .22s, transform .28s cubic-bezier(.2,1.5,.4,1) }}
.bub.on {{ opacity:1; transform:translateY(-100%) scale(1) }}
.bub::after {{ content:""; position:absolute; bottom:-6px; left:var(--tx, 50%); width:10px; height:10px; background:#0d1322;
  border-right:1.5px solid var(--bc); border-bottom:1.5px solid var(--bc); transform:translateX(-50%) rotate(45deg) }}
.bub b {{ display:block; font-size:9.5px; letter-spacing:.07em; text-transform:uppercase; color:var(--bc); margin-bottom:1px }}
.vtag {{ position:absolute; font:800 11px/1 ui-sans-serif,-apple-system,sans-serif; padding:4px 7px; border-radius:7px; pointer-events:none; box-shadow:0 3px 10px #0007 }}
.vtag.yes {{ background:#3ee6a1; color:#062a1c }} .vtag.no {{ background:#ff5468; color:#fff }} .vtag.abs {{ background:#9aa2c2; color:#111 }}
.phasepill {{ position:absolute; left:10px; bottom:10px; pointer-events:none; font:700 11px/1 ui-sans-serif,-apple-system,sans-serif;
  color:#eaf2ff; background:#0d1322cc; border:1px solid #38e1ff66; padding:6px 9px; border-radius:99px; letter-spacing:.02em }}
.phasepill.hearing {{ border-color:#f4c84a; color:#f4c84a }} .phasepill.party {{ border-color:#ff4fd8; color:#ffd84a }}
.phasepill.wait, .phasepill.assemble {{ border-color:#ffb03b; color:#ffd59a }}
.partybanner {{ position:absolute; top:10px; left:50%; transform:translateX(-50%); width:max-content; max-width:calc(100% - 20px); text-align:center;
  pointer-events:none; background:linear-gradient(135deg,#3a1450ee,#14304aee); border:1.5px solid #ffd84a; border-radius:12px; padding:8px 14px;
  color:#fff; box-shadow:0 0 24px #ff4fd855; animation:bannerIn .5s cubic-bezier(.2,1.5,.4,1) both }}
.partybanner b {{ display:block; color:#ffd84a; font:800 12px/1.2 ui-sans-serif,-apple-system,sans-serif; letter-spacing:.12em }}
.partybanner span {{ display:block; font-weight:700; font-size:14px; margin-top:2px }} .partybanner small {{ color:#cfe3ff; font-size:11.5px }}
@keyframes bannerIn {{ from {{ opacity:0; transform:translate(-50%,-12px) scale(.8) }} }}
.roomctl {{ display:flex; gap:8px; flex-wrap:wrap }}
.roomctl button {{ font:700 13px/1 ui-sans-serif,-apple-system,sans-serif; color:#efe9ff; background:#2b2150; border:1px solid #6a58b8;
  border-radius:99px; padding:9px 13px; cursor:pointer }}
.roomctl button:focus-visible {{ outline:2px solid #f4c84a; outline-offset:2px }}
@media (prefers-reduced-motion: reduce) {{ .bub, .partybanner {{ transition:none; animation:none }} }}

.tip3d b {{ font-size:13px }} .tip3d small {{ color:#b6a8e6 }}
.docket {{ display:grid; gap:2px; min-width:0 }}
.docket .stale {{ color:#f4c84a; margin-top:4px }} .docket .stale[hidden] {{ display:none }}
.docket .lbl {{ font-size:11px; letter-spacing:.1em; text-transform:uppercase; color:#b6a8e6 }}
.docket a, .docket span {{ color:#fff; font-weight:650; font-size:16px; text-decoration:none }}
.docket small {{ color:#c9bdf0 }}
.floor {{ position:relative; display:grid; gap:10px }}
.table {{ position:relative; height:64px; margin:0 6%; border-radius:50%; background:radial-gradient(closest-side, #f4c84a55, #f4c84a10 70%, transparent);
  border:2px solid #f4c84a66; box-shadow:0 0 30px #f4c84a33 inset; display:grid; place-items:center }}
.gavel {{ position:absolute; left:14%; top:8px; width:34px; height:14px; background:#8a5a2b; border-radius:4px; transform-origin:100% 50%;
  animation:slam 1.2s 1.6s ease-in both }}
.gavel::after {{ content:""; position:absolute; right:-20px; top:4px; width:22px; height:5px; background:#5e3b1a; border-radius:3px }}
.stamp {{ font:800 15px/1 ui-sans-serif,-apple-system,sans-serif; letter-spacing:.08em; padding:7px 12px; border-radius:6px;
  border:3px solid currentColor; transform:rotate(-6deg) scale(0); animation:stamp .35s 2.5s cubic-bezier(.2,1.6,.4,1) both }}
.stamp.ok {{ color:#6ff0a8 }} .stamp.rej {{ color:#ff8a8e }}
.navlinks a {{ color:#38e1ff; font-weight:600; text-decoration:none }}
.seats {{ display:grid; grid-template-columns:repeat(6,1fr); gap:6px }}
.seat {{ all:unset; cursor:pointer; display:grid; justify-items:center; gap:3px; text-align:center; position:relative; min-width:0 }}
.seat:focus-visible {{ outline:2px solid #f4c84a; outline-offset:4px; border-radius:8px }}
.char {{ width:min(76px,100%); height:auto; animation:bob 2.6s calc(var(--i) * .3s) ease-in-out infinite; filter:drop-shadow(0 6px 6px #0006) }}
.char .hand {{ transform-origin:50px 54px; animation:tick 4s linear infinite }}
.sign {{ font:800 11px/1 ui-sans-serif,-apple-system,sans-serif; padding:5px 8px; border-radius:99px; background:#2b2150; border:2px solid;
  opacity:0; transform:translateY(10px); animation:raise .4s calc(.3s + var(--i) * .22s) ease-out both }}
.sign.yes {{ color:#6ff0a8 }} .sign.no {{ color:#ff8a8e }} .sign.abs {{ color:#a99fc9 }}
.nm {{ font-weight:700; font-size:12px }} .acc {{ font-size:11px; color:#b6a8e6 }}
.why {{ display:none; font-size:11px; color:#efe9ff; background:#2b2150; border:1px solid #4a3a85; border-radius:8px; padding:6px 8px; max-width:140px }}
.seat:focus .why, .seat:hover .why {{ display:block; position:absolute; top:100%; z-index:2; margin-top:4px }}
.hint {{ margin:0; font-size:11px; color:#9c90c8; text-align:center }}
@keyframes bob {{ 0%,100% {{ transform:translateY(0) }} 50% {{ transform:translateY(-5px) }} }}
@keyframes raise {{ to {{ opacity:1; transform:translateY(0) }} }}
@keyframes slam {{ 0% {{ transform:rotate(-40deg) }} 60% {{ transform:rotate(12deg) }} 75% {{ transform:rotate(4deg) }} 100% {{ transform:rotate(8deg) }} }}
@keyframes stamp {{ to {{ transform:rotate(-6deg) scale(1) }} }}
@keyframes tick {{ to {{ transform:rotate(360deg) }} }}
@media (prefers-reduced-motion: reduce) {{ .room * {{ animation-duration:0s !important; animation-delay:0s !important; animation-iteration-count:1 !important }} }}
@media (max-width:720px) {{ .nm {{ font-size:11px }} .acc {{ font-size:10px }}
  .roomhead {{ grid-template-columns:1fr; gap:10px }} .overall {{ border-right:0; padding-right:0; border-bottom:1px solid #4a3a85; padding-bottom:10px }} }}
</style></head><body><div class="wrap">
<header><div><h1>Vault Options</h1><p>Updated {updated} · day-trading calls and puts on 15-minute setups{f" · started with ${START_BALANCE:,.0f}" if START_BALANCE else ""}</p>
  <p class="navlinks"><a href="hq.html">🏛 HQ: all bots</a> · <a href="inspector.html">🕵️ Inspector's options report</a> · <a href="gap.html">🔄 Gap Fade (IWM)</a> · <a href="dinner.html">🍽 Dinner Table ETF</a></p></div>
  <div><span class="badge {mode.lower()}">{mode}</span> {f'<span class="badge paused">PAUSED · {html.escape(paused)}</span>' if paused else ''}</div></header>
{alpaca_banner(st)}
{council_room(st)}
{recess_panel(st)}
{next_panel(st)}
{feed_panel(st)}
<div class="stats">
  <div class="stat"><span>{'Bankroll' if START_BALANCE else 'Account value'}</span><b>{'—' if nav is None else f'${nav:,.2f}'}</b></div>
  <div class="stat"><span>Today</span><b class="{'pos' if (today or 0) >= 0 else 'neg'}">{m(today)}</b></div>
  <div class="stat"><span>Open P&amp;L</span><b class="{'pos' if acc.get('unrealized', 0) >= 0 else 'neg'}">{m(acc.get('unrealized'))}</b></div>
  <div class="stat"><span>Record</span><b>{rec['wins']}W–{rec['losses']}L</b></div>
  <div class="stat"><span>Trade size</span><b class="{'neg' if streak_status(st) != 'normal' else ''}">{ {'normal': f'{RISK_PCT:.0%} risk', 'half': f'{RISK_PCT / 2:.1%} (half)', 'timeout': 'Timeout'}[streak_status(st)] }</b></div>
  <div class="stat"><span>Closed P&amp;L</span><b class="{'pos' if rec['pl'] >= 0 else 'neg'}">{m(rec['pl'])}</b></div>
</div>
{bankroll_chart(st)}
<section><h2>Open trades</h2><div class="tbl"><table><tr><th>Contract</th><th>Type</th><th>Qty</th><th>Paid</th><th>Now</th><th>Stop</th><th>Target</th><th>P&amp;L</th></tr>{open_rows}</table></div></section>
<section><h2>Watchlist</h2><div class="pairs">{''.join(pair_card(p) for p in PAIRS)}</div></section>
<section><h2>Big news ahead</h2><ul class="news">{news_rows}</ul><p class="fine">No new trades from {NEWS_BEFORE_MIN} min before to {NEWS_AFTER_MIN} min after these.</p></section>
{learning_html(st)}
{orb5_panel(st)}
<section><h2>Who called it?</h2><p class="fine">{html.escape(learn_line)}</p><div class="tbl"><table><tr><th>Voter</th><th>Called it</th><th>Vote weight</th><th>Right / scored</th></tr>{board_rows}</table></div></section>
<section><h2>Recent closed trades</h2><div class="tbl"><table><tr><th>Contract</th><th>Type</th><th>Qty</th><th>Result</th></tr>{closed_rows}</table></div></section>
<details><summary>Scan log</summary><ul>{''.join(f'<li>{html.escape(l)}</li>' for l in log)}</ul></details>
<p class="fine">Strategy: Easter's 5-minute opening range breakout on {', '.join(PAIRS)}. The 9:30–10:00 ET high and low are marked; from 10:00 to 12:00 a strong 5-minute candle
(body at least {ORB5_BODY:.0%} of its range) must close outside the range, and the next candle must hold outside without touching it and make a new high (calls) or low (puts).
Entry at that close; stop past the breakout candles; target {ORB5_RR:g}× the risk on the stock; stop to breakeven at {ORB5_BE_AT:g}R; one trade a day, closed by 3:50 PM ET.
Exits follow the stock's price, not the option's %, with a safety sale if the option loses {ORB5_OPT_SAFETY:.0%}. The contract
(0DTE on SPY/QQQ/IWM before 1:30 PM, otherwise {WEEKLY_DAYS[0]}–{WEEKLY_DAYS[1]} day weeklies, delta {DELTA_RANGE[0]}–{DELTA_RANGE[1]}, spread under {MAX_SPREAD_PCT:.0%}).
Each trade is sized so a full stop (−{OPT_STOP:.0%} on the option) costs about {RISK_PCT:.0%} of the bankroll; target +{OPT_TARGET:.0%}; at +{PARTIAL_AT:.0%} it sells half (or locks +{LOCK_AT:.0%} on a single contract) and the rest runs toward +{RUNNER_TARGET:.0%} with a stop {TRAIL_GIVEBACK:.0%} below its best price;
0DTE is sold by 3:40 PM ET; weeklies can be held overnight (stops are only checked while the market is open, so a gap at the open can fill past the stop)
and are sold by 3:40 PM the trading day before they expire. Limits: {MAX_OPEN_TRADES} open trades, {DAILY_LOSS_LIMIT:.0%} daily loss stop, {MAX_DRAWDOWN:.0%} drawdown pause, half size after {STREAK_HALF} straight losses.
Stops and targets are checked every 5 minutes, so fast moves can fill worse than planned. Options can lose most or all of their value quickly,
and most short-term options traders lose money. Rejected setups are scored on the stock's own price so "Who called it?" learns from both sides.
This is software, not financial advice.</p>
</div></body></html>"""


def main():
    ap = argparse.ArgumentParser(description="Vault Options: day-trading options bot for Alpaca.")
    ap.add_argument("--out", default="site/index.html")
    ap.add_argument("--mode", default="auto", choices=["auto", "full"],
                    help="auto: full scan every 15 min, trade checks in between; full: always scan")
    args = ap.parse_args()
    mode = "full" if args.mode == "full" else plan(load_state())
    print(f"Mode: {mode}")

    def output(deploy):
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
                f.write(f"deploy={'true' if deploy else 'false'}\n")

    if mode == "skip":
        print("No open trades and the last full scan was under 15 minutes ago. Nothing to do.")
        output(False)
        return
    log = []
    try:
        st = run(log, full=(mode == "full"))
    except Exception as e:                      # never leave the dashboard blank
        log.append(f"Run failed: {e}")
        st = load_state()
    try:
        record_errors(log, st); save_state(st)
    except Exception:
        pass
    for line in log:
        print(line)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(build_html(st, log))
    deploy = mode == "full" and (time.time() - st.get("last_deploy", 0) >= DEPLOY_EVERY_MIN * 60 or st.pop("_changed", False))
    st.pop("_changed", None)
    if deploy:
        st["last_deploy"] = time.time()
    try:
        save_state(st)
    except Exception:
        pass
    output(deploy)                              # GitHub Pages has hourly limits; the live feed refreshes on its own


if __name__ == "__main__":
    main()
