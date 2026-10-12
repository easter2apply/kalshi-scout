#!/usr/bin/env python3
"""
Kalshi Scout — scans Kalshi crypto and sports markets, estimates a fair
probability for each, and builds a dashboard page of possible edges.

It never places trades. You review the flags and trade yourself.

Run:
    python3 kalshi_scout.py            # scan once, open the dashboard
    (iPhone: works in the free a-Shell app. Android: Pydroid 3 or Termux.)
    python3 kalshi_scout.py --loop 10  # rescan every 10 minutes

Optional (Telegram alerts): set TELEGRAM_BOT_TOKEN from @BotFather, then message your bot once.

Optional (sports fair values): get a free key at https://the-odds-api.com
    export ODDS_API_KEY=your_key_here     (Mac/Linux)
    setx ODDS_API_KEY your_key_here       (Windows, then reopen the terminal)

Only uses the Python standard library — nothing to install.
"""

import argparse
import datetime as dt
import html
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/New_York")   # dashboard shows Eastern time

# ─────────────────────────── SETTINGS (edit these) ───────────────────────────
BANKROLL = 200.00          # dollars you're willing to keep on Kalshi
KELLY_FRACTION = 0.25      # quarter-Kelly; lower = more conservative
MAX_BET_PCT = 0.05         # never suggest more than 5% of bankroll on one trade
MIN_EDGE = 0.04            # flag only if expected edge after fees ≥ 4 cents/contract
MIN_PRICE, MAX_PRICE = 0.05, 0.95   # skip near-certain longshots/locks
MAX_HOURS_TO_CLOSE = 72    # ignore markets closing further out than this
STOP_LOSS_MIN = 0.05       # stop loss sits at least 5¢ below your entry
STOP_LOSS_MAX = 0.15       # ...and at most 15¢ below it
MAX_NEW_SIGNALS = 5        # most BUY alerts sent per scan
SIGNAL_COOLDOWN_HOURS = 6  # don't re-alert the same market for this long after it exits
STATE_FILE = "signals.json"  # remembers open signals between scans

CRYPTO_SERIES = {
    # Kalshi series ticker : Coinbase product
    "KXBTCD": "BTC-USD",   # Bitcoin daily above/below
    "KXBTC":  "BTC-USD",   # Bitcoin price range
    "KXETHD": "ETH-USD",   # Ethereum daily above/below
    "KXETH":  "ETH-USD",   # Ethereum price range
}

SPORTS_SERIES = {
    # Kalshi series ticker : The Odds API sport key
    "KXNFLGAME":   "americanfootball_nfl",
    "KXNCAAFGAME": "americanfootball_ncaaf",
    "KXMLBGAME":   "baseball_mlb",
    "KXNBAGAME":   "basketball_nba",
    "KXNHLGAME":   "icehockey_nhl",
}

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
OUTFILE = "kalshi_dashboard.html"  # override with --out
# ─────────────────────────────────────────────────────────────────────────────


def get_json(url, params=None, tries=3):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "kalshi-scout/1.0",
                                               "Accept": "application/json"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < tries - 1:   # rate limited
                time.sleep(2 * (attempt + 1))
                continue
            raise
        except urllib.error.URLError:
            if attempt < tries - 1:
                time.sleep(1.5)
                continue
            raise


# ───────────────────────────── Kalshi helpers ────────────────────────────────

def kalshi_events(series):
    """All open events (with their markets) for one series."""
    events, cursor = [], None
    for _ in range(10):
        params = {"series_ticker": series, "status": "open",
                  "with_nested_markets": "true", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = get_json(f"{KALSHI}/events", params)
        events += data.get("events", []) or []
        cursor = data.get("cursor")
        if not cursor:
            break
        time.sleep(0.25)
    return events


def price(m, side):
    """Price in dollars (0–1). Handles both new *_dollars and old cent fields."""
    v = m.get(f"{side}_dollars")
    if v not in (None, ""):
        try:
            return float(v)
        except ValueError:
            pass
    v = m.get(side)
    return v / 100 if isinstance(v, (int, float)) else None


def hours_left(m):
    t = m.get("close_time") or m.get("expected_expiration_time")
    if not t:
        return None
    close = dt.datetime.fromisoformat(t.replace("Z", "+00:00"))
    return (close - dt.datetime.now(dt.timezone.utc)).total_seconds() / 3600


def taker_fee(p):
    """Kalshi's published taker fee ≈ 0.07 × P × (1−P), rounded up to the cent."""
    return math.ceil(0.07 * p * (1 - p) * 100 - 1e-9) / 100


def volume(m):
    """24h volume, falling back to total. Handles number and *_fp string fields."""
    for key in ("volume_24h", "volume_24h_fp", "volume", "volume_fp"):
        v = m.get(key)
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if v > 0:
            return int(v)
    return 0


def market_url(m):
    return f"https://kalshi.com/markets/{(m.get('event_ticker') or '').split('-')[0].lower()}"


# ───────────────────────────── Crypto model ──────────────────────────────────

def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def coinbase_spot(product):
    return float(get_json(f"https://api.coinbase.com/v2/prices/{product}/spot")["data"]["amount"])


def coinbase_vol(product):
    """Annualized realized volatility from the last ~12 days of hourly candles."""
    candles = get_json(f"https://api.exchange.coinbase.com/products/{product}/candles",
                       {"granularity": 3600})
    closes = [c[4] for c in sorted(candles, key=lambda c: c[0])]
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) < 24:
        return 0.55
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var * 24 * 365)


def prob_above(spot, strike, vol, hours):
    if hours <= 0:
        return 1.0 if spot > strike else 0.0
    t = hours / (24 * 365)
    d2 = (math.log(spot / strike) - 0.5 * vol ** 2 * t) / (vol * math.sqrt(t))
    return norm_cdf(d2)


def crypto_fair(m, spot, vol, hours):
    st = (m.get("strike_type") or "").lower()
    lo, hi = m.get("floor_strike"), m.get("cap_strike")
    if st in ("greater", "greater_or_equal") and lo is not None:
        return prob_above(spot, lo, vol, hours)
    if st in ("less", "less_or_equal") and hi is not None:
        return 1 - prob_above(spot, hi, vol, hours)
    if st == "between" and lo is not None and hi is not None:
        return prob_above(spot, lo, vol, hours) - prob_above(spot, hi, vol, hours)
    return None


# ───────────────────────────── Sports model ──────────────────────────────────

def american_to_prob(odds):
    return 100 / (odds + 100) if odds > 0 else -odds / (-odds + 100)


def consensus_games(sport_key, api_key):
    """No-vig win probabilities averaged across US sportsbooks."""
    data = get_json(f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds",
                    {"apiKey": api_key, "regions": "us", "markets": "h2h",
                     "oddsFormat": "american"})
    games = []
    for g in data:
        sums, n = {}, 0
        for book in g.get("bookmakers", []):
            for mk in book.get("markets", []):
                if mk.get("key") != "h2h":
                    continue
                raw = {o["name"]: american_to_prob(o["price"]) for o in mk["outcomes"]}
                total = sum(raw.values())
                if total <= 0:
                    continue
                for name, p in raw.items():
                    sums[name] = sums.get(name, 0) + p / total
                n += 1
        if n:
            games.append({"home": g["home_team"], "away": g["away_team"], "books": n,
                          "probs": {k: v / n for k, v in sums.items()}})
    return games


STOP = {"the", "fc", "state", "university", "of", "st", "city"}


def tokens(name):
    return {w for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in STOP and len(w) > 1}


def same_team(a, b):
    ta, tb = tokens(a), tokens(b)
    return bool(ta and tb and (ta <= tb or tb <= ta or len(ta & tb) >= 1 and len(ta & tb) == min(len(ta), len(tb))))


def match_game(team_names, games):
    for g in games:
        sides = [g["home"], g["away"]]
        if all(any(same_team(t, s) for s in sides) for t in team_names):
            return g
    return None


# ───────────────────────────── Edge + sizing ─────────────────────────────────

def evaluate(m, fair, category, label, note):
    rows = []
    yes_ask, no_ask = price(m, "yes_ask"), price(m, "no_ask")
    for side, ask, p_win in (("YES", yes_ask, fair), ("NO", no_ask, 1 - fair)):
        if ask is None or not (MIN_PRICE <= ask <= MAX_PRICE):
            continue
        fee = taker_fee(ask)
        edge = p_win - ask - fee
        cost = ask + fee
        # Kelly for a binary contract paying $1: f = (p − cost) / (1 − cost)
        kelly = max(0.0, (p_win - cost) / (1 - cost)) * KELLY_FRACTION
        stake = min(kelly, MAX_BET_PCT) * BANKROLL
        # Take profit: sell once the market price reaches the model's fair value.
        # Stop loss: sell if the price falls 1.5× the edge below entry (5–15¢).
        tp = min(0.99, round(p_win, 2))
        sl = max(0.01, round(ask - min(STOP_LOSS_MAX, max(STOP_LOSS_MIN, 1.5 * max(edge, 0))), 2))
        rows.append({
            "category": category, "label": label, "side": side,
            "ticker": m.get("ticker", ""), "url": market_url(m),
            "ask": ask, "fair": p_win, "fee": fee, "edge": edge,
            "contracts": int(stake // cost) if cost > 0 else 0,
            "stake": stake, "hours": hours_left(m),
            "volume": volume(m),
            "note": note, "tp": tp, "sl": sl,
        })
    return rows


def scan():
    results, log = [], []

    # Crypto
    cache = {}
    for series, product in CRYPTO_SERIES.items():
        try:
            if product not in cache:
                cache[product] = (coinbase_spot(product), coinbase_vol(product))
            spot, vol = cache[product]
            events = kalshi_events(series)
        except Exception as e:
            log.append(f"{series}: skipped ({e})")
            continue
        n = 0
        for ev in events:
            for m in ev.get("markets", []) or []:
                h = hours_left(m)
                if h is None or h <= 0 or h > MAX_HOURS_TO_CLOSE:
                    continue
                fair = crypto_fair(m, spot, vol, h)
                if fair is None:
                    continue
                label = m.get("title") or ev.get("title", "")
                sub = m.get("yes_sub_title") or m.get("subtitle") or ""
                note = f"{product.split('-')[0]} ${spot:,.0f} · vol {vol:.0%}"
                results += evaluate(m, fair, "Crypto", f"{label} — {sub}".strip(" —"), note)
                n += 1
        log.append(f"{series}: {n} markets priced ({product} ${spot:,.0f}, vol {vol:.0%})")
        time.sleep(0.25)

    # Sports
    key = os.environ.get("ODDS_API_KEY", "").strip()
    for series, sport in SPORTS_SERIES.items():
        try:
            events = kalshi_events(series)
        except Exception as e:
            log.append(f"{series}: skipped ({e})")
            continue
        if not events:
            log.append(f"{series}: no open games")
            continue
        games = []
        if key:
            try:
                games = consensus_games(sport, key)
            except Exception as e:
                log.append(f"{sport} odds: unavailable ({e})")
        matched = 0
        for ev in events:
            mkts = [m for m in ev.get("markets", []) or []
                    if (hours_left(m) or -1) > 0 and hours_left(m) <= MAX_HOURS_TO_CLOSE * 3]
            teams = [m.get("yes_sub_title") or "" for m in mkts]
            g = match_game([t for t in teams if t], games) if games else None
            for m, team in zip(mkts, teams):
                if g:
                    p = next((v for k, v in g["probs"].items() if same_team(team, k)), None)
                    if p is None:
                        continue
                    matched += 1
                    results += evaluate(m, p, "Sports", ev.get("title", "") + f" — {team}",
                                        f"No-vig consensus of {g['books']} books")
                else:
                    # No fair value: show the market so you can judge it yourself
                    ya = price(m, "yes_ask")
                    if ya is None:
                        continue
                    results.append({
                        "category": "Sports (no model)", "label": ev.get("title", "") + f" — {team}",
                        "side": "—", "ticker": m.get("ticker", ""), "url": market_url(m),
                        "ask": ya, "fair": None, "fee": taker_fee(ya), "edge": None,
                        "contracts": 0, "stake": 0, "hours": hours_left(m),
                        "volume": volume(m),
                        "note": "Add ODDS_API_KEY for a fair value", "tp": None, "sl": None,
                    })
        log.append(f"{series}: {len(events)} games" + (f", {matched} sides matched to sportsbook odds" if key else ""))
        time.sleep(0.25)

    return results, log



# ───────────────────────────── Signals + Telegram ────────────────────────────

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    st.setdefault("open", [])
    st.setdefault("recent", {})
    return st


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1, sort_keys=True)


def telegram_chat_id(token, st):
    cid = os.environ.get("TELEGRAM_CHAT_ID", "").strip() or st.get("chat_id")
    if cid:
        return cid
    # First run: find the chat from the most recent message sent to the bot
    try:
        upd = get_json(f"https://api.telegram.org/bot{token}/getUpdates")
        for u in reversed(upd.get("result", [])):
            msg = u.get("message") or u.get("channel_post") or {}
            if msg.get("chat", {}).get("id"):
                st["chat_id"] = str(msg["chat"]["id"])
                return st["chat_id"]
    except Exception:
        pass
    return None


def send_telegram(token, chat_id, text):
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text,
                                   "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


def c(x):
    return f"{round(x * 100)}¢"


def buy_message(r):
    return (f"🟢 BUY {r['side']}\n{r['label']}\n\n"
            f"Price: {c(r['ask'])}  ·  Fair: {c(r['fair'])}  ·  Edge: {c(r['edge'])}\n"
            f"Size: {r['contracts']} contracts (~${r['stake']:.2f})\n"
            f"🎯 Take profit: sell at {c(r['tp'])} or higher\n"
            f"🛑 Stop loss: sell at {c(r['sl'])} or lower\n"
            f"Closes in {fmt_hours(r['hours'])}\n{r['url']}")


def manage_signals(results, log):
    """Track BUY signals, send SELL alerts when take profit / stop loss hits."""
    st = load_state()
    now = time.time()
    msgs = []

    # 1) Check open signals for exits
    still_open = []
    for sig in st["open"]:
        try:
            m = get_json(f"{KALSHI}/markets/{sig['ticker']}").get("market", {})
        except Exception:
            still_open.append(sig)
            continue
        status = (m.get("status") or "").lower()
        bid = price(m, f"{sig['side'].lower()}_bid")
        if status not in ("open", "active") or (hours_left(m) or 0) <= 0:
            res = (m.get("result") or "").upper()
            outcome = "✅ won" if res == sig["side"] else "❌ lost" if res else "closed"
            msgs.append(f"⚪ CLOSED: {sig['side']} — {sig['label']}\nMarket {outcome}. Signal entry was {c(sig['entry'])}.")
            st["recent"][sig["key"]] = now
            continue
        if bid is not None and bid >= sig["tp"]:
            msgs.append(f"🔴 SELL {sig['side']} — TAKE PROFIT\n{sig['label']}\n\n"
                        f"Sell price now: {c(bid)} (entry {c(sig['entry'])}) → +{round((bid - sig['entry']) * 100)}¢ per contract\n{sig['url']}")
            st["recent"][sig["key"]] = now
            continue
        if bid is not None and bid <= sig["sl"]:
            msgs.append(f"🔴 SELL {sig['side']} — STOP LOSS\n{sig['label']}\n\n"
                        f"Sell price now: {c(bid)} (entry {c(sig['entry'])}) → {round((bid - sig['entry']) * 100)}¢ per contract\n"
                        f"Cutting it here limits the loss.\n{sig['url']}")
            st["recent"][sig["key"]] = now
            continue
        sig["last_bid"] = bid
        still_open.append(sig)
    st["open"] = still_open

    # 2) New BUY signals
    open_keys = {s["key"] for s in st["open"]}
    st["recent"] = {k: t for k, t in st["recent"].items() if now - t < SIGNAL_COOLDOWN_HOURS * 3600}
    flagged = sorted([r for r in results if r["edge"] is not None and r["edge"] >= MIN_EDGE
                      and r["contracts"] > 0], key=lambda r: -r["edge"])
    new = 0
    for r in flagged:
        key = f"{r['ticker']}:{r['side']}"
        # one side per market at a time
        if key in open_keys or key in st["recent"] or any(s["ticker"] == r["ticker"] for s in st["open"]):
            continue
        if new >= MAX_NEW_SIGNALS:
            break
        st["open"].append({"key": key, "ticker": r["ticker"], "side": r["side"], "label": r["label"],
                           "url": r["url"], "entry": r["ask"], "tp": r["tp"], "sl": r["sl"],
                           "fair": r["fair"], "contracts": r["contracts"], "opened": now, "last_bid": None})
        open_keys.add(key)
        msgs.append(buy_message(r))
        new += 1

    # 3) Send to Telegram
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if token and not st.get("welcomed"):
        if telegram_chat_id(token, st):
            msgs.insert(0, "✅ Kalshi Scout is connected.\n\nYou'll get a 🟢 BUY alert when a trade clears your edge, "
                           "a 🔴 SELL alert when it hits take profit or stop loss, and a ⚪ note when the market closes.\n\n"
                           "No messages = no edges right now. Scans run every 15 minutes.")
            st["welcomed"] = True
    if token and msgs:
        cid = telegram_chat_id(token, st)
        if cid:
            sent = 0
            for text in msgs:
                try:
                    send_telegram(token, cid, text)
                    sent += 1
                    time.sleep(0.4)
                except Exception as e:
                    log.append(f"Telegram: send failed ({e})")
                    break
            log.append(f"Telegram: sent {sent} alert(s)")
        else:
            log.append("Telegram: no chat found. Send your bot any message, then rerun.")
    elif not token:
        log.append("Telegram: off (add TELEGRAM_BOT_TOKEN to turn on alerts)")
    else:
        log.append("Telegram: nothing new to send")

    save_state(st)
    return st["open"]

# ───────────────────────────── Dashboard page ────────────────────────────────

def pct(x):
    return "—" if x is None else f"{x * 100:.0f}¢" if x >= 0 else f"−{abs(x) * 100:.0f}¢"


def fmt_hours(h):
    if h is None:
        return "—"
    return f"{h * 60:.0f}m" if h < 1 else f"{h:.0f}h" if h < 48 else f"{h / 24:.1f}d"


def build_html(results, log, refresh_min, open_signals=()):
    flagged = sorted([r for r in results if r["edge"] is not None and r["edge"] >= MIN_EDGE],
                     key=lambda r: -r["edge"])
    priced = [r for r in results if r["edge"] is not None]
    watch = sorted([r for r in results if r["edge"] is None], key=lambda r: r["hours"] or 1e9)
    now = dt.datetime.now(LOCAL_TZ).strftime("%a %b %d, %I:%M %p ET")

    def row(r, show_edge=True):
        e = r["edge"]
        cls = "" if e is None else "hot" if e >= 2 * MIN_EDGE else "warm" if e >= MIN_EDGE else ""
        side = f'<span class="side side-{r["side"].lower()}">BUY {html.escape(r["side"])}</span>' if r["side"] != "—" else "—"
        return f"""<tr class="{cls}">
          <td class="mkt"><a href="{html.escape(r['url'])}" target="_blank" rel="noopener">{html.escape(r['label'])}</a>
              <small>{html.escape(r['ticker'])} · {html.escape(r['note'])}</small></td>
          <td data-l="Buy">{side}</td><td class="n" data-l="Ask">{pct(r['ask'])}</td><td class="n" data-l="Fair">{pct(r['fair'])}</td>
          <td class="n" data-l="Fee">{pct(r['fee'])}</td><td class="n edge" data-l="Edge">{pct(e) if show_edge else '—'}</td>
          <td class="n" data-l="Qty">{r['contracts'] if r['contracts'] else '—'}</td>
          <td class="n" data-l="Stake">{('$%.2f' % r['stake']) if r['stake'] else '—'}</td>
          <td class="n" data-l="Take profit">{pct(r.get('tp'))}</td><td class="n" data-l="Stop loss">{pct(r.get('sl'))}</td>
          <td class="n" data-l="Closes">{fmt_hours(r['hours'])}</td><td class="n" data-l="Vol">{r['volume']:,}</td></tr>"""

    head = """<tr><th>Market</th><th>Buy</th><th>Ask</th><th>Fair</th><th>Fee</th>
              <th>Edge</th><th>Qty</th><th>Stake</th><th>Target</th><th>Stop</th><th>Closes</th><th>Vol</th></tr>"""
    flagged_rows = "".join(row(r) for r in flagged) or \
        '<tr><td colspan="12" class="empty">No trades clear your edge threshold right now. That is a normal result.</td></tr>'
    near = sorted([r for r in priced if r not in flagged], key=lambda r: -r["edge"])[:25]
    def sig_row(g):
        lb = g.get("last_bid")
        state = "near target" if lb is not None and lb >= g["tp"] - 0.02 else \
                "near stop" if lb is not None and lb <= g["sl"] + 0.02 else "holding"
        return f"""<tr><td class="mkt"><a href="{html.escape(g['url'])}" target="_blank" rel="noopener">{html.escape(g['label'])}</a>
          <small>{html.escape(g['ticker'])} · signaled {dt.datetime.fromtimestamp(g['opened'], LOCAL_TZ):%a %I:%M %p}</small></td>
          <td data-l="Signal"><span class="side side-{g['side'].lower()}">BUY {g['side']}</span></td>
          <td class="n" data-l="Entry">{pct(g['entry'])}</td><td class="n" data-l="Sell now">{pct(lb)}</td>
          <td class="n" data-l="Take profit">{pct(g['tp'])}</td><td class="n" data-l="Stop loss">{pct(g['sl'])}</td>
          <td class="n" data-l="Qty">{g['contracts']}</td><td data-l="Status">{state}</td></tr>"""
    sig_head = """<tr><th>Market</th><th>Signal</th><th>Entry</th><th>Sell now</th><th>Target</th><th>Stop</th><th>Qty</th><th>Status</th></tr>"""
    sig_rows = "".join(sig_row(g) for g in open_signals) or \
        '<tr><td colspan="8" class="empty">No open signals. New BUY alerts show up here and on Telegram.</td></tr>'
    meta = f'<meta http-equiv="refresh" content="{refresh_min * 60}">' if refresh_min else ""

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">{meta}
<title>Kalshi Scout</title>
<style>
:root {{ --bg:#f5f6f3; --panel:#ffffff; --fg:#17201b; --muted:#5d6a62; --line:#dde2dc;
  --accent:#1f6f5c; --hot:#d9f2e6; --warm:#eef7f1; --yes:#1f6f5c; --no:#9a3b2f; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#101513; --panel:#171e1b; --fg:#e4ebe6;
  --muted:#93a39a; --line:#29332e; --accent:#5cc4a3; --hot:#16392d; --warm:#132a22; --yes:#5cc4a3; --no:#e48a7c; }} }}
* {{ box-sizing:border-box }}
body {{ margin:0; background:var(--bg); color:var(--fg);
  font:14px/1.45 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif; padding:24px 16px; }}
.wrap {{ max-width:1200px; margin:0 auto; display:grid; gap:20px }}
header h1 {{ margin:0; font-size:22px; letter-spacing:-.01em }}
header p {{ margin:4px 0 0; color:var(--muted) }}
.stats {{ display:flex; flex-wrap:wrap; gap:10px }}
.stat {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:10px 14px; min-width:130px }}
.stat b {{ display:block; font-size:20px; font-variant-numeric:tabular-nums }}
.stat span {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.06em }}
h2 {{ font-size:13px; text-transform:uppercase; letter-spacing:.08em; color:var(--muted); margin:8px 0 6px }}
.tbl {{ overflow-x:auto; background:var(--panel); border:1px solid var(--line); border-radius:8px }}
table {{ width:100%; border-collapse:collapse; min-width:860px }}
th,td {{ padding:8px 10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top }}
th {{ font-size:11px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); font-weight:600 }}
td.n,th:nth-child(n+3) {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap }}
td.mkt a {{ color:var(--fg); text-decoration:none; font-weight:550 }}
td.mkt a:hover {{ color:var(--accent); text-decoration:underline }}
td.mkt small {{ display:block; color:var(--muted); font-size:12px }}
tr.hot {{ background:var(--hot) }} tr.warm {{ background:var(--warm) }}
td.edge {{ font-weight:700 }}
.side {{ font-weight:700; font-size:12px; padding:2px 7px; border-radius:99px; border:1px solid currentColor }}
.side-yes {{ color:var(--yes) }} .side-no {{ color:var(--no) }}
.empty {{ color:var(--muted); text-align:center; padding:24px }}
details {{ color:var(--muted) }} details li {{ margin:2px 0 }}
.fine {{ color:var(--muted); font-size:12px; max-width:75ch }}
@media (max-width:720px) {{
  body {{ padding:16px 12px }}
  .stat {{ flex:1 1 40%; min-width:0 }}
  .tbl {{ background:none; border:0; overflow:visible }}
  table {{ min-width:0 }}
  table tr:first-child {{ display:none }}
  tr {{ display:grid; grid-template-columns:repeat(4,1fr); gap:6px 8px; background:var(--panel);
       border:1px solid var(--line); border-radius:10px; padding:12px; margin-bottom:10px }}
  tr.hot {{ border-color:var(--accent) }}
  td {{ border:0; padding:0; text-align:left !important; min-width:0 }}
  td.mkt {{ grid-column:1/-1; font-size:15px; margin-bottom:4px }}
  td[data-l]::before {{ content:attr(data-l); display:block; font-size:10px; text-transform:uppercase;
       letter-spacing:.06em; color:var(--muted) }}
  td.empty {{ grid-column:1/-1; text-align:center !important }}
}}
</style></head><body><div class="wrap">
<header><h1>Kalshi Scout</h1><p>Scanned {now}{f" · refreshes every {refresh_min} min" if refresh_min else ""}</p></header>
<div class="stats">
  <div class="stat"><span>Open signals</span><b>{len(open_signals)}</b></div>
  <div class="stat"><span>Flagged</span><b>{len(flagged)}</b></div>
  <div class="stat"><span>Priced sides</span><b>{len(priced)}</b></div>
  <div class="stat"><span>Min edge</span><b>{MIN_EDGE * 100:.0f}¢</b></div>
  <div class="stat"><span>Bankroll</span><b>${BANKROLL:,.0f}</b></div>
</div>
<section><h2>Open signals</h2><div class="tbl"><table>{sig_head}{sig_rows}</table></div></section>
<section><h2>Possible edges</h2><div class="tbl"><table>{head}{flagged_rows}</table></div></section>
<section><h2>Closest misses</h2><div class="tbl"><table>{head}{"".join(row(r) for r in near) or '<tr><td colspan="12" class="empty">Nothing priced yet.</td></tr>'}</table></div></section>
{f'<section><h2>Sports watchlist (no fair value)</h2><div class="tbl"><table>{head}{"".join(row(r, False) for r in watch[:40])}</table></div></section>' if watch else ''}
<details><summary>Scan log</summary><ul>{"".join(f"<li>{html.escape(l)}</li>" for l in log)}</ul></details>
<p class="fine">Fair values are estimates. Crypto uses spot price and recent realized volatility in a simple lognormal model;
sports uses the no-vig average of US sportsbook lines. Edge = fair probability − ask − estimated taker fee, per $1 contract.
Take profit = sell once the price reaches fair value. Stop loss = sell if the price drops 1.5× the edge below entry ({STOP_LOSS_MIN*100:.0f}–{STOP_LOSS_MAX*100:.0f}¢).
Kalshi has no automatic stop orders here, so sell manually when alerted. Stake is {KELLY_FRACTION:g}× Kelly, capped at {MAX_BET_PCT:.0%} of bankroll. Check each market's rules on Kalshi before trading.
This tool does not place orders and is not financial advice.</p>
</div></body></html>"""


def main():
    ap = argparse.ArgumentParser(description="Scan Kalshi crypto and sports markets for possible edges.")
    ap.add_argument("--loop", type=int, default=0, metavar="MIN", help="rescan every MIN minutes")
    ap.add_argument("--no-open", action="store_true", help="don't open the browser")
    ap.add_argument("--out", default=OUTFILE, help="where to write the dashboard page")
    args = ap.parse_args()

    opened = False
    while True:
        print(f"[{dt.datetime.now():%H:%M:%S}] Scanning…", flush=True)
        try:
            results, log = scan()
            open_signals = manage_signals(results, log)
        except Exception as e:
            print(f"Scan failed: {e}", file=sys.stderr)
            results, log, open_signals = [], [f"Scan failed: {e}"], []
        for line in log:
            print("  " + line)
        path = os.path.abspath(args.out)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(build_html(results, log, args.loop, open_signals))
        flagged = sum(1 for r in results if r["edge"] is not None and r["edge"] >= MIN_EDGE)
        print(f"  {flagged} possible edges → {path}")
        if not opened and not args.no_open:
            try:
                opened = webbrowser.open("file://" + path)
            except Exception:
                opened = False
            if not opened:
                print("  Open it with:  open " + os.path.basename(path))
                opened = True
        if not args.loop:
            break
        time.sleep(args.loop * 60)


if __name__ == "__main__":
    main()
