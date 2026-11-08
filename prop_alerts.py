"""Prop Alerts: Telegram alerts for the Vault Prop Desk signals (Nasdaq, gold, Bitcoin).

Same rules as the Prop Desk page:
  1. Price is at support or resistance (1-hour swing highs/lows, merged; "at" = within 2.5 five-minute ATRs)
  2. A 5-minute candle confirms: engulfing, hammer or shooting star
  3. RSI(14, 5-minute) turns up from below 35 (buy) or down from above 65 (sell) within the last 6 candles
  4. No big scheduled news (CPI, jobs report, Fed decision) from 30 minutes before to 15 after
Stop = past the level and the signal candle + 0.25 ATR (never closer than 1 ATR).
TP1 = 1R (take half). TP2 = the next level, or 2R if that's closer than 1.5R.

Plus the tested FVG strategy (gold GLD, silver SLV, Nasdaq QQQ, 15-minute, 9:30-11:30 AM ET): see fvg_sim().

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
    step = {"5Min": 300, "15Min": 900, "1Hour": 3600}[tf]
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


# ───────────── FVG strategy (tested: 4 years, 15-minute GLD/SLV/QQQ, about +0.09R a trade) ─────────────
FVG_RR, FVG_BE, FVG_GAP, FVG_WAIT = 3.0, 1.5, 0.3, 20
FVG_MARKETS = [("Gold", "GLD", "XAU"), ("Silver", "SLV", "XAG"), ("Nasdaq", "QQQ", "NQ")]


def atr_wilder(b, n=14):
    out, a = [None] * len(b), None
    for i in range(1, len(b)):
        tr = max(b[i]["h"] - b[i]["l"], abs(b[i]["h"] - b[i - 1]["c"]), abs(b[i]["l"] - b[i - 1]["c"]))
        a = tr if a is None else (a * (n - 1) + tr) / n
        out[i] = a if i >= n else None
    return out


def ema_seeded(a, n):
    k, e, out = 2 / (n + 1), None, []
    for i, x in enumerate(a):
        e = x if e is None else x * k + e * (1 - k)
        out.append(e if i >= n - 1 else None)
    return out


def fvg_sim(b):
    """Replays the FVG rules over 15-minute bars (9:30-4:00 ET) and returns every event:
    setup (limit order placed), fill, be (stop to entry), exit (tp / sl / be / close), cancel."""
    for x in b:
        e = dt.datetime.fromtimestamp(x["t"], ET); x["day"] = e.date().isoformat(); x["m"] = e.hour * 60 + e.minute
    b = [x for x in b if 570 <= x["m"] < 960]
    atr_l, e200 = atr_wilder(b), ema_seeded([x["c"] for x in b], 200)
    ev, order, pos = [], None, None
    for i in range(3, len(b)):
        x = b[i]
        if x["day"] != b[i - 1]["day"]:
            order = None
            if pos:
                ev.append(dict(pos, kind="exit", how="close", r=pos["d"] * (b[i - 1]["c"] - pos["entry"]) / pos["R"], t=b[i - 1]["t"])); pos = None
        if pos:
            d = pos["d"]
            if (x["l"] <= pos["stop"]) if d == 1 else (x["h"] >= pos["stop"]):
                ev.append(dict(pos, kind="exit", how="be" if pos["be"] else "sl", r=d * (pos["stop"] - pos["entry"]) / pos["R"], t=x["t"])); pos = None
            elif (x["h"] >= pos["tp"]) if d == 1 else (x["l"] <= pos["tp"]):
                ev.append(dict(pos, kind="exit", how="tp", r=FVG_RR, t=x["t"])); pos = None
            elif not pos["be"] and ((x["h"] >= pos["entry"] + FVG_BE * pos["R"]) if d == 1 else (x["l"] <= pos["entry"] - FVG_BE * pos["R"])):
                pos["be"] = True; pos["stop"] = pos["entry"]; ev.append(dict(pos, kind="be", t=x["t"]))
            if pos and x["m"] + 15 >= 960:
                ev.append(dict(pos, kind="exit", how="close", r=d * (x["c"] - pos["entry"]) / pos["R"], t=x["t"])); pos = None
        if order and not pos:
            o, d = order, order["d"]
            touched = (x["l"] <= o["entry"]) if d == 1 else (x["h"] >= o["entry"])
            if i - o["born"] > FVG_WAIT:
                ev.append(dict(o, kind="cancel", why="no fill in 20 candles", t=x["t"])); order = None
            elif ((x["h"] >= o["tp"]) if d == 1 else (x["l"] <= o["tp"])):
                ev.append(dict(o, kind="cancel", why="price ran to the target without filling", t=x["t"])); order = None
            elif touched:
                fill = min(x["o"], o["entry"]) if d == 1 else max(x["o"], o["entry"])
                R = abs(fill - o["stop"])
                pos = {"d": d, "entry": fill, "stop": o["stop"], "tp": fill + d * FVG_RR * R, "R": R, "be": False}
                ev.append(dict(pos, kind="fill", t=x["t"])); order = None
                if (x["l"] <= pos["stop"]) if d == 1 else (x["h"] >= pos["stop"]):
                    ev.append(dict(pos, kind="exit", how="sl", r=-1.0, t=x["t"])); pos = None
            elif (x["c"] < o["far"]) if d == 1 else (x["c"] > o["far"]):
                ev.append(dict(o, kind="cancel", why="price closed back through the gap", t=x["t"])); order = None
        a, c1 = atr_l[i], b[i - 2]
        if a is None or e200[i] is None or pos or order or c1["day"] != x["day"] or not (570 <= x["m"] < 690):
            continue
        bull = x["l"] > c1["h"] and x["l"] - c1["h"] >= FVG_GAP * a and x["c"] > e200[i]
        bear = x["h"] < c1["l"] and c1["l"] - x["h"] >= FVG_GAP * a and x["c"] < e200[i]
        if bull or bear:
            d = 1 if bull else -1
            entry = x["l"] if d == 1 else x["h"]
            stop = (c1["l"] - 0.1 * a) if d == 1 else (c1["h"] + 0.1 * a)
            R = abs(entry - stop)
            if R > 0:
                order = {"d": d, "entry": entry, "stop": stop, "tp": entry + d * FVG_RR * R, "R": R, "far": c1["h"] if d == 1 else c1["l"], "born": i}
                ev.append(dict(order, kind="setup", t=x["t"]))
    return ev


def fvg_message(name, sym, vest, e):
    d, R = e["d"], e["R"]
    units = RISK_USD / R
    usd = lambda dist: units * abs(dist)
    pct = lambda p: (p - e["entry"]) / e["entry"] * 100
    side = "BUY" if d > 0 else "SELL"
    k = e["kind"]
    if k == "setup":
        return "\n".join([
            f"{'🟢' if d > 0 else '🔴'} FVG {side} LIMIT · {name} ({sym}, 15-min)",
            f"Fair value gap with the trend, 9:30-11:30 window.",
            "",
            f"Limit {side.lower()} at {f(e['entry'])}",
            f"Stop loss {f(e['stop'])} ({pct(e['stop']):+.2f}%) → −${usd(e['entry'] - e['stop']):,.2f}",
            f"Take profit {f(e['tp'])} ({pct(e['tp']):+.2f}%, 3R) → +${usd(e['tp'] - e['entry']):,.2f}",
            f"Move stop to entry at {f(e['entry'] + d * FVG_BE * R)} (+1.5R)",
            f"Size for ${RISK_USD:,.0f} risk: {units:.4g} shares",
            "",
            f"Order is good for 20 candles (5 hours). Cancel if a 15-min candle closes back through {f(e['far'])}. Close by 4 PM.",
            f"On {vest}: use the same % distances from your entry, or open the Prop Desk for exact {vest} prices."])
    if k == "fill":
        return f"✅ FVG {side} FILLED · {name} at {f(e['entry'])}. Stop {f(e['stop'])}, target {f(e['tp'])}."
    if k == "be":
        return f"🔒 {name} FVG is up 1.5R. Move your stop to entry ({f(e['entry'])}). This trade can't lose now."
    if k == "cancel":
        return f"✖️ {name} FVG {side.lower()} limit cancelled: {e['why']}."
    if k == "exit":
        word = {"tp": "🎯 Target hit", "sl": "🛑 Stopped out", "be": "🔒 Out at entry", "close": "🔔 Closed at 4 PM"}[e["how"]]
        return f"{word} · {name} FVG {side.lower()}: {e['r']:+.1f}R (≈ {'+' if e['r'] >= 0 else '−'}${abs(e['r']) * RISK_USD:,.2f} at ${RISK_USD:,.0f} risk)."
    return ""


def fvg_check(st, now):
    e = now.astimezone(ET)
    if e.weekday() >= 5 or not (570 <= e.hour * 60 + e.minute <= 975):
        return
    sent = st.setdefault("fvg_sent", [])
    for name, sym, vest in FVG_MARKETS:
        try:
            bars = alpaca_bars(sym, "15Min", 25, False)
            evs = fvg_sim(bars)
        except Exception as ex:
            st.setdefault("status", {})[sym + " FVG"] = f"failed: {str(ex)[:120]}"
            continue
        st.setdefault("status", {})[sym + " FVG"] = f"{len(bars)} candles · last event: {evs[-1]['kind'] if evs else 'none'}"
        for ev in evs:
            done_at = ev["t"] + 900
            key = f"{sym}:{ev['kind']}:{int(ev['t'])}"
            if key in sent or now.timestamp() - done_at > 25 * 60:
                continue
            sent.append(key)
            msg = fvg_message(name, sym, vest, ev)
            if ev["kind"] == "setup" and news_block(now):
                msg += f"\n⚠️ {news_block(now)} is in the news window. Consider skipping."
            say(msg)
    st["fvg_sent"] = sent[-300:]


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
            st.setdefault("status", {})[sym] = f"failed: {str(ex)[:120]}"
            continue
        print(sym, "signal" if s else "no signal", f"{m5[-1]['c'] if m5 else ''}")
        st.setdefault("status", {})[sym] = f"{'signal' if s else 'no signal'} · {len(h1)} 1h / {len(m5)} 5m candles · last {m5[-1]['c'] if m5 else '-'}"
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
    fvg_check(st, now)
    st["sent"] = sent[-200:]
    st["last_run"] = now.timestamp()
    json.dump(st, open(STATE, "w", encoding="utf-8"))


if __name__ == "__main__":
    main()
