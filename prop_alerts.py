"""Prop Alerts: Telegram alerts for the Vault Prop Desk signals (Nasdaq, gold, Bitcoin).

Same rules as the Prop Desk page:
  1. Price is at support or resistance (1-hour swing highs/lows, merged; "at" = within 2.5 five-minute ATRs)
  2. A 5-minute candle confirms: engulfing, hammer or shooting star
  3. RSI(14, 5-minute) turns up from below 35 (buy) or down from above 65 (sell) within the last 6 candles
  4. No big scheduled news (CPI, jobs report, Fed decision) from 30 minutes before to 15 after
Stop = past the level and the signal candle + 0.25 ATR (never closer than 1 ATR).
TP1 = 1R (take half). TP2 = the next level, or 2R if that's closer than 1.5R.

Data: Alpaca (QQQ for Nasdaq, BTC/USD) and Twelve Data for gold (XAU/USD) if TWELVEDATA_API_KEY is set.
Runs every 5 minutes from .github/workflows/prop_alerts.yml. Remembers what it already sent in prop_alerts_state.json.
"""
import datetime as dt
import json
import os
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
RISK_USD = float(os.environ.get("PROP_RISK") or 25)       # dollars risked per trade (change with the PROP_RISK variable)
STATE = "prop_alerts_state.json"
FRESH_MIN = 12                                             # only alert on a signal candle from the last 12 minutes

EVENTS = [("2026-11-06", "Jobs report", 510), ("2026-11-10", "CPI inflation report", 510), ("2026-12-04", "Jobs report", 510),
          ("2026-12-09", "Fed rate decision", 840), ("2026-12-10", "CPI inflation report", 510), ("2027-01-08", "Jobs report", 510),
          ("2027-01-13", "CPI inflation report", 510), ("2027-01-27", "Fed rate decision", 840), ("2027-02-05", "Jobs report", 510),
          ("2027-02-11", "CPI inflation report", 510), ("2027-03-05", "Jobs report", 510), ("2027-03-10", "CPI inflation report", 510),
          ("2027-03-17", "Fed rate decision", 840)]


def get(url, params=None, headers=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "prop-alerts", **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


# ───────────── data ─────────────
def alpaca_bars(sym, tf, days, crypto):
    h = {"APCA-API-KEY-ID": os.environ.get("ALPACA_KEY_ID", "").strip(), "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY", "").strip()}
    start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = "https://data.alpaca.markets/v1beta3/crypto/us/bars" if crypto else "https://data.alpaca.markets/v2/stocks/bars"
    out, token = [], None
    for _ in range(20):
        p = {"symbols": sym, "timeframe": tf, "start": start, "limit": 10000, "sort": "asc"}
        if not crypto:
            p.update(feed=os.environ.get("ALPACA_STOCK_FEED", "iex"), adjustment="raw")
        if token:
            p["page_token"] = token
        j = get(url, p, h)
        out += (j.get("bars") or {}).get(sym, [])
        token = j.get("next_page_token")
        if not token:
            break
    step = {"5Min": 300, "1Hour": 3600}[tf]
    now = time.time()
    bars = []
    for b in out:
        t = dt.datetime.fromisoformat(b["t"].replace("Z", "+00:00")).timestamp()
        if t + step <= now:                                    # finished candles only
            bars.append({"t": t, "o": float(b["o"]), "h": float(b["h"]), "l": float(b["l"]), "c": float(b["c"])})
    return bars


def twelve_bars(sym, interval, size):
    key = os.environ.get("TWELVEDATA_API_KEY", "").strip()
    j = get("https://api.twelvedata.com/time_series", {"symbol": sym, "interval": interval, "outputsize": size, "apikey": key, "timezone": "UTC"})
    if j.get("status") == "error":
        raise RuntimeError(j.get("message", "Twelve Data error"))
    step = {"5min": 300, "1h": 3600}[interval]
    now = time.time()
    bars = []
    for v in reversed(j.get("values", [])):
        t = dt.datetime.fromisoformat(v["datetime"]).replace(tzinfo=dt.timezone.utc).timestamp()
        if t + step <= now:
            bars.append({"t": t, "o": float(v["open"]), "h": float(v["high"]), "l": float(v["low"]), "c": float(v["close"])})
    return bars


# ───────────── rules (same as the Prop Desk page) ─────────────
def ema(a, n):
    out = [None] * len(a)
    if len(a) < n:
        return out
    e = sum(a[:n]) / n; out[n - 1] = e; k = 2 / (n + 1)
    for i in range(n, len(a)):
        e = a[i] * k + e * (1 - k); out[i] = e
    return out


def atr(b, n=14):
    if len(b) < n + 1:
        return None
    return sum(max(b[i]["h"] - b[i]["l"], abs(b[i]["h"] - b[i - 1]["c"]), abs(b[i]["l"] - b[i - 1]["c"])) for i in range(len(b) - n, len(b))) / n


def rsi(c, n=14):
    out = [None] * len(c)
    if len(c) <= n:
        return out
    g = sum(max(c[i] - c[i - 1], 0) for i in range(1, n + 1)) / n
    l = sum(max(c[i - 1] - c[i], 0) for i in range(1, n + 1)) / n
    out[n] = 100 if l == 0 else 100 - 100 / (1 + g / l)
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]
        g = (g * (n - 1) + max(d, 0)) / n; l = (l * (n - 1) + max(-d, 0)) / n
        out[i] = 100 if l == 0 else 100 - 100 / (1 + g / l)
    return out


def levels(h1, a1):
    pts = []
    for i in range(3, len(h1) - 3):
        w = h1[i - 3:i + 4]
        if h1[i]["h"] >= max(x["h"] for x in w):
            pts.append(h1[i]["h"])
        if h1[i]["l"] <= min(x["l"] for x in w):
            pts.append(h1[i]["l"])
    out = []
    for v in sorted(pts):
        if out and v - out[-1][0] < a1 * 0.5:
            m, n = out[-1]; out[-1] = ((m * n + v) / (n + 1), n + 1)
        else:
            out.append((v, 1))
    return [v for v, _ in out]


def candle(b, p):
    r = b["h"] - b["l"]; body = abs(b["c"] - b["o"])
    up = b["h"] - max(b["o"], b["c"]); dn = min(b["o"], b["c"]) - b["l"]
    if r <= 0:
        return None
    if b["c"] > b["o"] and p["c"] < p["o"] and b["c"] >= p["o"] and b["o"] <= p["c"]:
        return 1, "Bullish engulfing"
    if b["c"] < b["o"] and p["c"] > p["o"] and b["c"] <= p["o"] and b["o"] >= p["c"]:
        return -1, "Bearish engulfing"
    if dn >= 2 * body and dn >= 0.6 * r and up <= 0.25 * r:
        return 1, "Hammer"
    if up >= 2 * body and up >= 0.6 * r and dn <= 0.25 * r:
        return -1, "Shooting star"
    return None


def news_block(now):
    e = now.astimezone(ET); day = e.date().isoformat(); m = e.hour * 60 + e.minute
    return next((n for d, n, t in EVENTS if d == day and t - 30 <= m <= t + 15), None)


def signal(h1, m5):
    if len(h1) < 60 or len(m5) < 40:
        return None
    p = m5[-1]["c"]
    a1 = atr(h1) or p * 0.003
    a5 = atr(m5) or a1 / 3
    lv = levels(h1, a1)
    sup = max([v for v in lv if v < p], default=None)
    res = min([v for v in lv if v > p], default=None)
    near = 2.5 * a5
    at_s = sup is not None and p - sup <= near
    at_r = res is not None and res - p <= near
    d = 1 if at_s and (not at_r or p - sup < res - p) else -1 if at_r else 0
    if not d:
        return None
    N = len(m5) - 1
    cd, ci = None, None
    for i in (N, N - 1):
        c = candle(m5[i], m5[i - 1])
        if c:
            cd, ci = c, i
            break
    if not cd or cd[0] != d:
        return None
    r5 = rsi([b["c"] for b in m5])
    win = [x for x in r5[N - 6:N + 1] if x is not None]
    if not win:
        return None
    ok = (min(win) <= 35 and r5[N] > min(win) + 3) if d == 1 else (max(win) >= 65 and r5[N] < max(win) - 3)
    if not ok:
        return None
    lvl = sup if d == 1 else res
    ext = min(m5[ci]["l"], lvl) if d == 1 else max(m5[ci]["h"], lvl)
    stop = ext - d * 0.25 * a5
    if abs(p - stop) < a5:
        stop = p - d * a5
    R = abs(p - stop)
    opp = res if d == 1 else sup
    tp2, why = (opp, "next " + ("resistance" if d == 1 else "support")) if opp and abs(opp - p) >= 1.5 * R else (p + d * 2 * R, "2R")
    c1 = [b["c"] for b in h1]; e50, e200 = ema(c1, 50), ema(c1, 200)
    trend = 0
    if e200[-1] is not None:
        trend = 1 if p > e50[-1] > e200[-1] else -1 if p < e50[-1] < e200[-1] else 0
    return {"d": d, "entry": p, "stop": stop, "R": R, "tp1": p + d * R, "tp2": tp2, "tp_why": why, "rr": abs(tp2 - p) / R,
            "candle": cd[1], "rsi": r5[N], "level": lvl, "trend": trend, "t": m5[ci]["t"]}


# ───────────── telegram ─────────────
def chat_id():
    cid = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if cid:
        return cid
    try:
        return json.load(open("options_state.json", encoding="utf-8")).get("chat_id")
    except (OSError, ValueError):
        return None


def say(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    cid = chat_id()
    if os.environ.get("PROP_DRY") or not token or not cid:
        print("No Telegram token/chat; message was:\n" + text)
        return
    data = json.dumps({"chat_id": cid, "text": text, "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20).read()


def f(x):
    return f"{x:,.2f}" if abs(x) >= 10 else f"{x:,.4f}"


def message(name, sym, s):
    d = s["d"]
    units = RISK_USD / s["R"]
    usd = lambda a, b: units * abs(a - b)
    pct = lambda a: abs(a - s["entry"]) / s["entry"] * 100
    lines = [f"{'🟢 BUY' if d > 0 else '🔴 SELL'} SIGNAL · {name} ({sym})",
             f"{s['candle']} at {'support' if d > 0 else 'resistance'} {f(s['level'])} · RSI {s['rsi']:.0f} turning {'up' if d > 0 else 'down'}",
             "",
             f"Entry ≈ {f(s['entry'])}",
             f"Stop loss {f(s['stop'])} ({pct(s['stop']):.2f}% away) → −${usd(s['entry'], s['stop']):,.2f}",
             f"TP1 {f(s['tp1'])} (1R, take half) → +${usd(s['tp1'], s['entry']):,.2f} if all in",
             f"TP2 {f(s['tp2'])} ({s['rr']:.1f}R, {s['tp_why']}) → +${usd(s['tp2'], s['entry']):,.2f} if all in",
             f"Half at TP1 + half at TP2 → +${usd(s['tp1'], s['entry']) / 2 + usd(s['tp2'], s['entry']) / 2:,.2f}",
             f"Size for ${RISK_USD:,.0f} risk: {units:.4g} units ($1 per 1.0 move)"]
    if sym == "QQQ":
        lines.append("These are QQQ prices. On Vest NQ use the same % distances, or open the Prop Desk and type your NQ price to convert.")
    if s["trend"] and s["trend"] != d:
        lines.append("⚠️ Against the 1-hour trend: half size.")
    lines += ["", "Check the account guard on your Prop Desk before you take it. 2 losses today = done."]
    return "\n".join(lines)


def main():
    now = dt.datetime.now(dt.timezone.utc)
    try:
        st = json.load(open(STATE, encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    sent = st.setdefault("sent", [])
    e = now.astimezone(ET)
    weekday, mins = e.weekday() < 5, e.hour * 60 + e.minute
    blocked = news_block(now)
    markets = [("Bitcoin", "BTC/USD", lambda: (alpaca_bars("BTC/USD", "1Hour", 20, True), alpaca_bars("BTC/USD", "5Min", 2, True)), True)]
    if weekday and 570 <= mins < 960:
        markets.append(("Nasdaq", "QQQ", lambda: (alpaca_bars("QQQ", "1Hour", 60, False), alpaca_bars("QQQ", "5Min", 4, False)), True))
    gold_open = not (e.weekday() == 5 or (e.weekday() == 4 and e.hour >= 17) or (e.weekday() == 6 and e.hour < 18))
    if gold_open and os.environ.get("TWELVEDATA_API_KEY", "").strip():
        def gold():
            if time.time() - st.get("gold_h1_at", 0) > 3300 or not st.get("gold_h1"):
                st["gold_h1"] = twelve_bars("XAU/USD", "1h", 260); st["gold_h1_at"] = time.time()
            return st["gold_h1"], twelve_bars("XAU/USD", "5min", 300)
        markets.append(("Gold", "XAU/USD", gold, True))
    elif gold_open and not st.get("gold_note"):
        say("ℹ️ Prop Alerts is on for Bitcoin and Nasdaq. For gold alerts, add a free Twelve Data API key as the "
            "TWELVEDATA_API_KEY secret in the kalshi-scout repo.")
        st["gold_note"] = True
    for name, sym, fetch, _ in markets:
        try:
            h1, m5 = fetch()
            s = signal(h1, m5)
        except Exception as ex:
            print(sym, "failed:", ex)
            continue
        print(sym, "signal" if s else "no signal", f"{m5[-1]['c'] if m5 else ''}")
        if not s:
            continue
        key = f"{sym}:{s['d']}:{int(s['t'])}"
        if key in sent or now.timestamp() - (s["t"] + 300) > FRESH_MIN * 60:
            continue
        sent.append(key)
        if blocked:
            say(f"🔕 {name} {'buy' if s['d'] > 0 else 'sell'} setup skipped: {blocked} is in the news window.")
            continue
        if name == "Bitcoin" and not weekday:
            say(message(name, sym, s) + "\n\n🌙 Weekend Bitcoin is thin. Half size or skip.")
        else:
            say(message(name, sym, s))
    st["sent"] = sent[-200:]
    st["last_run"] = now.timestamp()
    json.dump(st, open(STATE, "w", encoding="utf-8"))


if __name__ == "__main__":
    main()
