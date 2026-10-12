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
MAX_NEW_SIGNALS = 4        # most BUY alerts sent per scan
TOP_N = 4                  # never more than 4 open signals; the dashboard shows only the best 4
SIGNAL_COOLDOWN_HOURS = 6  # don't re-alert the same market for this long after it exits
STATE_FILE = "signals.json"  # remembers open signals between scans
COUNCIL_MIN_YES = 3        # a BUY alert needs at least this many council YES votes
MAX_SPREAD = 0.06          # Risk votes NO if YES ask + NO ask − $1 is wider than this
MIN_VOLUME = 50            # Risk votes NO below this many contracts traded
TIME_WINDOW_HOURS = (0.5, 48)  # Time votes YES only inside this window
COUNCIL_MIN_SHARE = 0.6    # ...and the weighted YES share must be at least this (learning shifts the weights)

# News filter: high-impact economic releases (CPI, jobs, Fed) whip crypto around
NEWS_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NEWS_REFRESH_HOURS = 6
NEWS_BUFFER_MIN = 30       # Risk votes NO on crypto if a big USD release lands between 30 min ago and the market's close

# Sports filters
ODDS_REFRESH_HOURS = 8     # the free Odds API plan has 500 calls/month: 5 sports × 3 a day stays under it
LINE_MOVE_MAX = 0.05       # Risk votes NO if the sportsbook line moved 5+ points (injury or news?)
SPORTS_START_HOURS = (1, 48)  # Time votes YES only for games starting 1–48 hours out

# Learning
LEARN_MIN_CALLS = 20       # a voter's weight starts changing after 20 settled calls
EDGE_WINDOW = 15           # review each category's minimum edge after every 15 settled signals
EDGE_MAX = 0.10            # the learned minimum edge never goes above 10¢

# Reports (Eastern time)
REPORT_HOUR_ET = 21        # daily report after 9 PM
WEEKLY_DAY = 6             # weekly report on Sunday (Mon=0 … Sun=6)

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
    ev = (m.get("event_ticker") or "").lower()
    series = ev.split("-")[0]
    slug = re.sub(r"[^a-z0-9]+", "-", (m.get("_ev_title") or m.get("title") or series).lower()).strip("-")[:60] or series
    return f"https://kalshi.com/markets/{series}/{slug}/{ev}" if ev else "https://kalshi.com/markets"


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
            try:
                start = dt.datetime.fromisoformat(g["commence_time"].replace("Z", "+00:00")).timestamp()
            except (KeyError, ValueError):
                start = None
            games.append({"id": g.get("id") or f"{g['home_team']}|{g['away_team']}", "commence": start,
                          "home": g["home_team"], "away": g["away_team"], "books": n,
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


# ───────────────────────────── News + odds caches ────────────────────────────

def load_news(st, log):
    """High-impact events this week from the Forex Factory calendar feed (cached)."""
    cal = st.get("news") or {}
    if time.time() - cal.get("fetched", 0) > NEWS_REFRESH_HOURS * 3600:
        try:
            evs = []
            for e in get_json(NEWS_URL):
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


def sports_odds(st, sport, key, log):
    """Sportsbook consensus, refreshed every ODDS_REFRESH_HOURS to stay inside the free plan."""
    cache = st.setdefault("odds", {}).setdefault(sport, {})
    if time.time() - cache.get("fetched", 0) < ODDS_REFRESH_HOURS * 3600:
        return cache.get("games", []), cache.get("fetched", 0)
    try:
        games = consensus_games(sport, key)
    except Exception as e:
        log.append(f"{sport} odds: unavailable ({e})")
        return cache.get("games", []), cache.get("fetched", 0)
    prev = {g["id"]: g["probs"] for g in cache.get("games", [])}
    for g in games:
        old = prev.get(g["id"])
        g["move"] = max((abs(p - old[k]) for k, p in g["probs"].items() if k in old), default=0) if old else 0
    cache.update(fetched=time.time(), games=games)
    month = dt.datetime.now(LOCAL_TZ).strftime("%Y-%m")
    calls = st.setdefault("odds_calls", {})
    calls[month] = calls.get(month, 0) + 1
    return games, cache["fetched"]


# ───────────────────────────── Edge + sizing ─────────────────────────────────

def evaluate(m, fair, category, label, note, alt_fairs=None, books=None):
    rows = []
    yes_ask, no_ask = price(m, "yes_ask"), price(m, "no_ask")
    for side, ask, p_win in (("YES", yes_ask, fair), ("NO", no_ask, 1 - fair)):
        if ask is None or not (MIN_PRICE <= ask <= MAX_PRICE):
            continue
        fee = taker_fee(ask)
        edge = p_win - ask - fee
        alts = [(a if side == "YES" else 1 - a) for a in (alt_fairs or [])]
        robust_edge = min([a - ask - fee for a in alts], default=None)
        spread = (yes_ask + no_ask - 1) if yes_ask is not None and no_ask is not None else None
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
            "robust_edge": robust_edge, "books": books, "spread": spread, "event": m.get("event_ticker"),
        })
    return rows


def quality(r):
    """Ranking score: edge, discounted when the edge is fragile or the market is thin."""
    e = r.get("edge") or 0
    sure = 1.0
    if r.get("robust_edge") is not None and r["robust_edge"] <= 0:
        sure *= 0.5                                   # disappears if volatility is a bit off
    if r.get("books") is not None and r["books"] < 3:
        sure *= 0.6
    vol = r.get("volume") or 0
    sure *= 0.5 + 0.5 * min(1, vol / 500)              # thin markets are harder to fill at that price
    if r.get("approved"):
        sure *= 1.25
    return e * sure


def scan(st=None):
    st = st if st is not None else {}
    results, log = [], []
    now = time.time()
    news = load_news(st, log)

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
                m["_ev_title"] = ev.get("title", "")
                h = hours_left(m)
                if h is None or h <= 0 or h > MAX_HOURS_TO_CLOSE:
                    continue
                fair = crypto_fair(m, spot, vol, h)
                if fair is None:
                    continue
                label = m.get("title") or ev.get("title", "")
                sub = m.get("yes_sub_title") or m.get("subtitle") or ""
                note = f"{product.split('-')[0]} ${spot:,.0f} · vol {vol:.0%}"
                alts = [crypto_fair(m, spot, vol * k, h) for k in (0.8, 1.2)]
                rows = evaluate(m, fair, "Crypto", f"{label} — {sub}".strip(" —"), note,
                                alt_fairs=[a for a in alts if a is not None])
                hit = next((e for e in news if e["cur"] == "USD"
                            and now - NEWS_BUFFER_MIN * 60 <= e["t"] <= now + h * 3600), None)
                for row in rows:
                    row["news"] = news_label(hit) if hit else None
                results += rows
                n += 1
        log.append(f"{series}: {n} markets priced ({product} ${spot:,.0f}, vol {vol:.0%})")
        time.sleep(0.25)

    # Sports
    key = os.environ.get("ODDS_API_KEY", "").strip()
    st["sports_on"] = bool(key)
    for series, sport in SPORTS_SERIES.items():
        try:
            events = kalshi_events(series)
        except Exception as e:
            log.append(f"{series}: skipped ({e})")
            continue
        if not events:
            log.append(f"{series}: no open games")
            continue
        games, fetched = sports_odds(st, sport, key, log) if key else ([], 0)
        matched = 0
        for ev in events:
            for m in ev.get("markets", []) or []:
                m["_ev_title"] = ev.get("title", "")
            mkts = [m for m in ev.get("markets", []) or []
                    if (hours_left(m) or -1) > 0 and hours_left(m) <= MAX_HOURS_TO_CLOSE * 3]
            teams = [m.get("yes_sub_title") or "" for m in mkts]
            g = match_game([t for t in teams if t], games) if games else None
            live = g is not None and g.get("commence") is not None and g["commence"] <= now
            for m, team in zip(mkts, teams):
                if g and not live:
                    p = next((v for k, v in g["probs"].items() if same_team(team, k)), None)
                    if p is None:
                        continue
                    matched += 1
                    age = (now - fetched) / 3600 if fetched else 0
                    rows = evaluate(m, p, "Sports", ev.get("title", "") + f" — {team}",
                                    f"No-vig consensus of {g['books']} books, {age:.0f}h old", books=g["books"])
                    for row in rows:
                        row["line_move"] = g.get("move", 0)
                        row["starts_in"] = (g["commence"] - now) / 3600 if g.get("commence") else None
                    results += rows
                # no sportsbook price (no key, no match, or game started): nothing to compare, so skip it
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


VOTERS = ["The Quant", "Risk", "Momentum", "Time", "Doubt"]


def council(r, last_px):
    """Five rule-based voters. Each returns (True=YES / False=NO / None=abstain, reason)."""
    v = {}
    ret = r["edge"] / (r["ask"] + r["fee"]) if r["ask"] else 0
    v["The Quant"] = (ret >= 0.15, f"{ret:.0%} expected return on cost")
    sp, vol = r.get("spread"), r.get("volume") or 0
    if r.get("news"):
        v["Risk"] = (False, f"news risk: {r['news']} lands before this closes")
    elif (r.get("line_move") or 0) >= LINE_MOVE_MAX:
        v["Risk"] = (False, f"line moved {round(r['line_move'] * 100)} pts since last check (injury or news?)")
    elif sp is None:
        v["Risk"] = (None, "no spread data")
    else:
        ok = sp <= MAX_SPREAD and vol >= MIN_VOLUME
        v["Risk"] = (ok, f"spread {round(sp * 100)}¢, volume {vol:,}")
    prev = last_px.get(f"{r['ticker']}:{r['side']}")
    if prev is None:
        v["Momentum"] = (None, "first look at this market")
    else:
        move = r["ask"] - prev
        v["Momentum"] = (move >= -0.01, f"price {'+' if move >= 0 else '−'}{abs(round(move * 100))}¢ since last scan")
    if r.get("starts_in") is not None:
        lo, hi = SPORTS_START_HOURS
        v["Time"] = (lo <= r["starts_in"] <= hi, f"game starts in {fmt_hours(r['starts_in'])}")
    else:
        h = r.get("hours") or 0
        lo, hi = TIME_WINDOW_HOURS
        v["Time"] = (lo <= h <= hi, f"closes in {fmt_hours(h)}")
    if r.get("robust_edge") is not None:
        v["Doubt"] = (r["robust_edge"] > 0, f"edge {'holds' if r['robust_edge'] > 0 else 'disappears'} if volatility is ±20%")
    elif r.get("books") is not None:
        v["Doubt"] = (r["books"] >= 3, f"{r['books']} sportsbooks agree on the line")
    else:
        v["Doubt"] = (None, "no second opinion available")
    return v


def tally(votes):
    yes = sum(1 for ok, _ in votes.values() if ok is True)
    no = sum(1 for ok, _ in votes.values() if ok is False)
    return yes, no


def voter_weights(st):
    """Voters who call it right more often get a louder vote (after LEARN_MIN_CALLS settled calls)."""
    w = {}
    for n in VOTERS:
        b = (st or {}).get("scoreboard", {}).get(n, {"right": 0, "total": 0})
        if b["total"] < LEARN_MIN_CALLS:
            w[n] = 1.0
            continue
        acc = (b["right"] + 2) / (b["total"] + 4)          # shrunk toward 50% so a lucky streak can't dominate
        w[n] = round(max(0.3, min(2.0, 1 + (acc - 0.5) * 4)), 2)
    return w


def approved(votes, weights=None):
    yes, no = tally(votes)
    if yes < COUNCIL_MIN_YES:
        return False
    w = weights or {}
    ys = sum(w.get(n, 1.0) for n, (ok, _) in votes.items() if ok is True)
    ns = sum(w.get(n, 1.0) for n, (ok, _) in votes.items() if ok is False)
    return ys / (ys + ns) >= COUNCIL_MIN_SHARE - 1e-9


def min_edge(st, cat):
    return (st or {}).get("learn", {}).get("edge", {}).get(cat, MIN_EDGE)


def learn_edge(st, cat, pnl):
    """After every EDGE_WINDOW settled signals in a category, nudge that category's minimum edge."""
    L = st.setdefault("learn", {})
    win = L.setdefault("window", {}).setdefault(cat, [])
    win.append(pnl)
    if len(win) < EDGE_WINDOW:
        return None
    avg = sum(win) / len(win)
    cur = min_edge(st, cat)
    new = min(EDGE_MAX, cur + 0.01) if avg < 0 else max(MIN_EDGE, cur - 0.01) if avg >= 0.03 and cur > MIN_EDGE else cur
    L["window"][cat] = []
    if round(new, 2) == round(cur, 2):
        return None
    L.setdefault("edge", {})[cat] = round(new, 2)
    why = "losing" if avg < 0 else "winning comfortably"
    note = (f"🧠 Learned: {cat} signals were {why} ({'+' if avg >= 0 else '−'}{abs(round(avg * 100))}¢ avg over the last "
            f"{EDGE_WINDOW}), so the minimum edge for {cat} is now {round(new * 100)}¢ (was {round(cur * 100)}¢).")
    L.setdefault("log", []).append({"t": time.time(), "text": note})
    L["log"] = L["log"][-40:]
    return note


def day_stats(st, when=None):
    d = dt.datetime.fromtimestamp(when or time.time(), LOCAL_TZ).strftime("%Y-%m-%d")
    days = st.setdefault("days", {})
    for k in sorted(days)[:-14]:
        days.pop(k, None)
    return days.setdefault(d, {"buys": 0, "rejected": 0, "tp": 0, "sl": 0, "news": 0, "lines": 0})


def vote_lines(votes):
    icon = {True: "✅", False: "❌", None: "➖"}
    return "\n".join(f"{icon[ok]} {name}: {why}" for name, (ok, why) in votes.items())


def buy_message(r, votes):
    yes, no = tally(votes)
    return (f"🟢 BUY {r['side']}\n{r['label']}\n\n"
            f"Price: {c(r['ask'])}  ·  Fair: {c(r['fair'])}  ·  Edge: {c(r['edge'])}\n"
            f"Size: {r['contracts']} contracts (~${r['stake']:.2f})\n"
            f"🎯 Take profit: sell at {c(r['tp'])} or higher\n"
            f"🛑 Stop loss: sell at {c(r['sl'])} or lower\n"
            f"Closes in {fmt_hours(r['hours'])}\n\n"
            f"👉 On Kalshi: find \"{r['label'].split(' — ')[-1]}\", tap {r['side'].title()}, choose a limit order at "
            f"{round(r['ask'] * 100)}¢ or less, quantity {r['contracts']}. Skip it if the price is already above {round(r['ask'] * 100) + 2}¢.\n\n"
            f"Council: {yes}–{no} YES\n{vote_lines(votes)}\n\n{r['url']}")


def score(st, sig, won):
    """Credit each voter who called it right; update history and learning. Returns a learning note or None."""
    board = st.setdefault("scoreboard", {})
    for name, vote in (sig.get("votes") or {}).items():
        if vote is None:
            continue
        b = board.setdefault(name, {"right": 0, "total": 0})
        b["total"] += 1
        if (vote and won) or (not vote and not won):
            b["right"] += 1
    if sig.get("votes"):
        cr = st.setdefault("council_record", {"right": 0, "total": 0})
        cr["total"] += 1
        if (won and not sig.get("shadow")) or (not won and sig.get("shadow")):
            cr["right"] += 1                  # approved a winner, or rejected a loser
    right = (won and not sig.get("shadow")) or (not won and sig.get("shadow"))
    entry = {"t": time.time(), "shadow": bool(sig.get("shadow")), "won": won, "right": bool(right),
             "cat": sig.get("cat", "Crypto")}
    note = None
    if not sig.get("shadow"):
        rec = st.setdefault("record", {"wins": 0, "losses": 0, "pnl": 0.0})
        rec["wins" if won else "losses"] += 1
        exit_px = sig.get("exit_px")
        pnl = (exit_px if exit_px is not None else (1.0 if won else 0.0)) - sig["entry"]
        rec["pnl"] = round(rec["pnl"] + pnl, 4)
        entry["pnl"] = round(pnl, 4)
        note = learn_edge(st, sig.get("cat", "Crypto"), pnl)
    st.setdefault("history", []).append(entry)
    st["history"] = st["history"][-400:]
    return note


def manage_signals(results, log, st=None):
    """Council votes on candidates, tracks BUY signals, sends SELL/close alerts, keeps the scoreboard."""
    st = st if st is not None else load_state()
    st.setdefault("shadow", [])
    st.setdefault("last_px", {})
    now = time.time()
    msgs = []

    # 1) Follow open signals (and rejected "shadow" picks) until the market settles
    def follow(sig):
        try:
            m = get_json(f"{KALSHI}/markets/{sig['ticker']}").get("market", {})
        except Exception:
            return True
        time.sleep(0.1)
        result = (m.get("result") or "").lower()
        if result in ("yes", "no"):
            won = result == sig["side"].lower()
            note = score(st, sig, won)
            if note:
                msgs.append(note)
            if not sig.get("shadow"):
                tag = "✅ WON" if won else "❌ LOST"
                msgs.append(f"⚪ SETTLED: {sig['side']} — {sig['label']}\n{tag}. Entry was {c(sig['entry'])}."
                            + (" (You were told to sell earlier.)" if sig.get("exit_px") is not None else ""))
            st["recent"][sig["key"]] = now
            return False
        if now - sig["opened"] > 10 * 86400:
            return False                      # give up on anything stuck for 10 days
        status = (m.get("status") or "").lower()
        if sig.get("shadow") or sig.get("exit_px") is not None or status not in ("open", "active"):
            return True                       # just waiting for the result
        bid = price(m, f"{sig['side'].lower()}_bid")
        sig["last_bid"] = bid
        if bid is not None and bid >= sig["tp"]:
            sig["exit_px"] = bid; sig["exit_t"] = now; sig["exit_reason"] = "take profit"
            day_stats(st)["tp"] += 1
            msgs.append(f"🔴 SELL {sig['side']} — TAKE PROFIT\n{sig['label']}\n\n"
                        f"Sell price now: {c(bid)} (entry {c(sig['entry'])}) → +{round((bid - sig['entry']) * 100)}¢ per contract\n{sig['url']}")
        elif bid is not None and bid <= sig["sl"]:
            sig["exit_px"] = bid; sig["exit_t"] = now; sig["exit_reason"] = "stop loss"
            day_stats(st)["sl"] += 1
            msgs.append(f"🔴 SELL {sig['side']} — STOP LOSS\n{sig['label']}\n\n"
                        f"Sell price now: {c(bid)} (entry {c(sig['entry'])}) → {round((bid - sig['entry']) * 100)}¢ per contract\n"
                        f"Cutting it here limits the loss.\n{sig['url']}")
        return True

    st["open"] = [s for s in st["open"] if follow(s)]
    st["shadow"] = [s for s in st["shadow"] if follow(s)]

    # 2) Council votes on new candidates
    st["recent"] = {k: t for k, t in st["recent"].items() if now - t < SIGNAL_COOLDOWN_HOURS * 3600}
    tracked = {s["ticker"] for s in st["open"]}
    shadow_by_ticker = {s["ticker"]: s for s in st["shadow"]}
    weights = voter_weights(st)
    candidates = sorted([r for r in results if r["edge"] is not None and r["edge"] >= min_edge(st, r["category"])
                         and r["contracts"] > 0], key=lambda r: -quality(r))
    active = [s for s in st["open"] if s.get("exit_px") is None]
    slots = max(0, TOP_N - len(active))
    new = rejected = 0
    today = day_stats(st)
    taken_events = {s.get("event") for s in st["open"] if s.get("exit_px") is None}
    for r in candidates:
        key = f"{r['ticker']}:{r['side']}"
        votes = council(r, st["last_px"])
        r["votes"] = votes
        veto = r.get("news") or (r.get("line_move") or 0) >= LINE_MOVE_MAX
        r["approved"] = approved(votes, weights) and not veto     # news and line moves are hard vetoes
        if r["ticker"] in tracked or key in st["recent"]:
            continue
        if r.get("event") in taken_events:
            continue                            # one bet per event: strikes on the same Bitcoin hour (or both sides of a game) move together
        prior = shadow_by_ticker.get(r["ticker"])
        if prior and not r["approved"]:
            continue                            # still rejected; keep tracking the original pick
        if prior:
            st["shadow"].remove(prior)          # council changed its mind: promote to a real signal
        sig = {"key": key, "ticker": r["ticker"], "side": r["side"], "label": r["label"],
               "url": r["url"], "entry": r["ask"], "tp": r["tp"], "sl": r["sl"],
               "fair": r["fair"], "contracts": r["contracts"], "opened": now, "last_bid": None,
               "cat": r["category"], "event": r.get("event"), "votes": {n: ok for n, (ok, _) in votes.items()},
               "edge": r["edge"], "stake": r["stake"], "close_ts": now + (r.get("hours") or 0) * 3600,
               "reasons": [why for ok, why in votes.values() if ok][:3]}
        if r["approved"]:
            if new >= min(MAX_NEW_SIGNALS, slots):
                continue                        # only the best 4 at a time
            st["open"].append(sig)
            msgs.append(buy_message(r, votes))
            new += 1
            today["buys"] += 1
            taken_events.add(r.get("event"))
        elif len(st["shadow"]) < 60:
            sig["shadow"] = True                # rejected: tracked quietly so the scoreboard learns
            st["shadow"].append(sig)
            rejected += 1
            today["rejected"] += 1
            if r.get("news"):
                today["news"] += 1
            elif (r.get("line_move") or 0) >= LINE_MOVE_MAX:
                today["lines"] += 1
        tracked.add(r["ticker"])
    if candidates:
        top = candidates[0]
        st["last_vote"] = {"label": top["label"], "side": top["side"], "ask": top["ask"], "edge": top["edge"],
                           "url": top["url"], "when": now, "approved": top["approved"],
                           "votes": {n: [ok, why] for n, (ok, why) in top["votes"].items()}}
    log.append(f"Council: {new} approved, {rejected} rejected (tracked for the scoreboard)")

    # Remember prices for next scan's Momentum vote
    st["last_px"] = {f"{r['ticker']}:{r['side']}": r["ask"] for r in results
                     if r["edge"] is not None and r["edge"] >= MIN_EDGE / 2}

    st["last_run"] = time.time()               # heartbeat for the watchdog

    # 3) Daily / weekly reports
    msgs += due_reports(st)

    # 4) Send to Telegram
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if token and not st.get("welcomed"):
        if telegram_chat_id(token, st):
            msgs.insert(0, "✅ Kalshi Scout is connected.\n\nYou'll get a 🟢 BUY alert when a trade clears your edge, "
                           "a 🔴 SELL alert when it hits take profit or stop loss, and a ⚪ note when the market closes.\n\n"
                           "No messages = no edges right now. Scans run every 15 minutes.")
            st["welcomed"] = True
    if token and not st.get("council_intro"):
        msgs.insert(0, "🏛 The council is in session.\n\nEvery trade now gets 5 votes before you hear about it: "
                       "The Quant (return), Risk (spread + volume), Momentum (price direction), Time (closing window) "
                       f"and Doubt (does the edge survive a second opinion). A BUY needs {COUNCIL_MIN_YES}+ YES votes.\n\n"
                       "Rejected trades are tracked quietly. The dashboard's \"Who called it?\" board shows which voters are actually right.")
        st["council_intro"] = True
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

    record_errors(log, st)
    save_state(st)
    return st


ERROR_MARKERS = ("failed", "rejected (", "unavailable", "skipped (", "not filled", "couldn't", "no chat", "OANDA: add")


def record_errors(log, st):
    """Keep the last 20 problems from the run log so the watchdog can report them."""
    errs = st.setdefault("errors", [])
    for line in log:
        if any(m in line for m in ERROR_MARKERS) and "council rejected" not in line and not line.startswith("Council:"):
            errs.append({"t": time.time(), "text": line[:300]})
    del errs[:-20]


# ───────────────────────────── Reports ───────────────────────────────────────

def _acc_line(st):
    cr = st.get("council_record", {"right": 0, "total": 0})
    return f"{cr['right'] / cr['total']:.0%} ({cr['right']}/{cr['total']})" if cr["total"] else "no settled calls yet"


def _learning_lines(st):
    w = voter_weights(st)
    settled = min((st.get("scoreboard", {}).get(n, {}).get("total", 0) for n in VOTERS), default=0)
    if all(x == 1.0 for x in w.values()):
        voters = f"Voter weights: all equal until each has {LEARN_MIN_CALLS} settled calls (lowest so far: {settled})"
    else:
        voters = "Voter weights: " + " · ".join(f"{n} {w[n]:g}×" for n in sorted(VOTERS, key=lambda n: -w[n]))
    edges = "Min edge: " + " · ".join(f"{cat} {round(min_edge(st, cat) * 100)}¢" for cat in ("Crypto", "Sports"))
    return voters, edges


def cents(x):
    """+18¢ under a dollar, +$4.20 above it."""
    sign = "+" if x >= 0 else "−"
    return f"{sign}{abs(round(x * 100))}¢" if abs(x) < 1 else f"{sign}${abs(x):,.2f}"


def _summ(hist):
    real = [h for h in hist if not h["shadow"]]
    w = sum(1 for h in real if h["won"])
    pnl = sum(h.get("pnl", 0) for h in real)
    calls = len(hist)
    right = sum(1 for h in hist if h["right"])
    return real, w, len(real) - w, pnl, calls, right


def _upcoming(st, hours):
    now = time.time()
    evs = [e for e in st.get("news", {}).get("events", []) if e["cur"] == "USD" and now <= e["t"] <= now + hours * 3600]
    return ", ".join(news_label(e) for e in evs[:4]) or "none on the calendar"


def daily_report(st, day):
    d = st.get("days", {}).get(day, {})
    start = dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=LOCAL_TZ).timestamp()
    hist = [h for h in st.get("history", []) if start <= h["t"] < start + 86400]
    real, w, l, pnl, calls, right = _summ(hist)
    voters, edges = _learning_lines(st)
    learned = [x["text"] for x in st.get("learn", {}).get("log", []) if x["t"] >= start]
    label = dt.datetime.strptime(day, "%Y-%m-%d").strftime("%a %b %d")
    lines = [f"📅 Kalshi Scout daily report: {label}", "",
             f"🟢 BUY signals: {d.get('buys', 0)}   ·   ❌ rejected: {d.get('rejected', 0)}",
             f"🔴 Sell alerts: {d.get('tp', 0)} take profit, {d.get('sl', 0)} stop loss",
             f"⚪ Settled: {w}W–{l}L, {cents(pnl)} (1 contract each)" if real else "⚪ Settled: none today",
             f"🏛 Council calls settled today: {right}/{calls} right" if calls else "🏛 Council calls settled today: none",
             f"🚧 Blocked: {d.get('news', 0)} for news, {d.get('lines', 0)} for sports line moves",
             f"📂 Open signals: {len([s for s in st.get('open', []) if s.get('exit_px') is None])}",
             "", "🧠 " + voters, "   " + edges]
    lines += ["   " + t.replace("🧠 ", "") for t in learned]
    lines += ["", f"📰 Next 24h: {_upcoming(st, 24)}"]
    return "\n".join(lines)


def weekly_report(st, end_day):
    end = dt.datetime.strptime(end_day, "%Y-%m-%d").replace(tzinfo=LOCAL_TZ).timestamp() + 86400
    start = end - 7 * 86400
    hist = [h for h in st.get("history", []) if start <= h["t"] < end]
    real, w, l, pnl, calls, right = _summ(hist)
    days = [v for k, v in st.get("days", {}).items()
            if start <= dt.datetime.strptime(k, "%Y-%m-%d").replace(tzinfo=LOCAL_TZ).timestamp() < end]
    tot = {k: sum(d.get(k, 0) for d in days) for k in ("buys", "rejected", "tp", "sl", "news", "lines")}
    by_cat = {}
    for h in real:
        c_ = by_cat.setdefault(h["cat"], [0, 0, 0.0]); c_[0 if h["won"] else 1] += 1; c_[2] += h.get("pnl", 0)
    board = st.get("scoreboard", {})
    ranked = sorted([(n, b["right"] / b["total"], b["total"]) for n, b in board.items() if b.get("total", 0) >= 5],
                    key=lambda x: -x[1])
    rec = st.get("record", {"wins": 0, "losses": 0, "pnl": 0.0})
    voters, edges = _learning_lines(st)
    learned = [x["text"] for x in st.get("learn", {}).get("log", []) if start <= x["t"] < end]
    lines = [f"📊 Kalshi Scout weekly report (week ending {dt.datetime.strptime(end_day, '%Y-%m-%d'):%b %d})", "",
             f"Signals: {tot['buys']} BUY · {tot['rejected']} rejected · {tot['tp']} take profit · {tot['sl']} stop loss",
             (f"Settled: {w}W–{l}L ({w / (w + l):.0%} win rate), {cents(pnl)} (1 contract each)"
              if real else "Settled: none this week")]
    for cat, (cw, cl, cp) in sorted(by_cat.items()):
        lines.append(f"   {cat}: {cw}W–{cl}L, {cents(cp)}")
    lines += [f"Council this week: {right}/{calls} calls right" if calls else "Council this week: no settled calls",
              f"Council all-time: {_acc_line(st)}",
              f"Blocked: {tot['news']} for news, {tot['lines']} for sports line moves", ""]
    if ranked:
        lines.append(f"⭐ Sharpest voter: {ranked[0][0]} ({ranked[0][1]:.0%} of {ranked[0][2]})")
        if len(ranked) > 1:
            lines.append(f"🤔 Least reliable: {ranked[-1][0]} ({ranked[-1][1]:.0%} of {ranked[-1][2]})")
    lines += ["🧠 " + voters, "   " + edges]
    lines += ["   " + t.replace("🧠 ", "") for t in learned]
    lines += ["", f"All-time: {rec['wins']}W–{rec['losses']}L, {cents(rec['pnl'])} (1 contract each)",
              f"Odds API calls this month: {st.get('odds_calls', {}).get(dt.datetime.now(LOCAL_TZ).strftime('%Y-%m'), 0)} of ~500 free"]
    return "\n".join(lines)


def due_reports(st, now=None):
    now_et = dt.datetime.fromtimestamp(now or time.time(), LOCAL_TZ)
    day = now_et.strftime("%Y-%m-%d")
    out = []
    if now_et.hour < REPORT_HOUR_ET:
        return out
    if st.get("last_daily") != day:
        out.append(daily_report(st, day)); st["last_daily"] = day
    if now_et.weekday() == WEEKLY_DAY and st.get("last_weekly") != day:
        out.append(weekly_report(st, day)); st["last_weekly"] = day
    return out


# ───────────────────────────── Council room (visual) ─────────────────────────

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
    "Doubt": ("--c-doubt", '<ellipse cx="50" cy="56" rx="30" ry="38" fill="var(--c-doubt)"/>'
              '<circle cx="40" cy="48" r="4" fill="var(--ink)"/><circle cx="60" cy="48" r="4" fill="var(--ink)"/>'
              '<path d="M32 36 l14 -2 M54 32 l14 6" stroke="var(--ink)" stroke-width="3" stroke-linecap="round"/>'
              '<path d="M42 70 h16" stroke="var(--ink)" stroke-width="3" stroke-linecap="round"/>'
              '<path d="M68 64 q10 -2 10 8" fill="none" stroke="var(--ink)" stroke-width="2.5" stroke-linecap="round" opacity=".7"/>'),
}


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
                  + (f'<small>{html.escape(lv["detail"])} · voted {when}</small>' if lv.get("detail") else
                  f'<small>BUY {lv["side"]} @ {round(lv["ask"] * 100)}¢ · edge {round(lv["edge"] * 100)}¢ · voted {when}</small>'))
        stamp = f'<div class="stamp {"ok" if lv["approved"] else "rej"}">{verdict}</div>'
    else:
        docket = "<span>No trade on the table yet.</span><small>The council votes when a market clears your minimum edge.</small>"
        stamp = ""
    data = json.dumps({"voters": voters, "verdict": verdict, "approved": bool(lv and lv["approved"]),
                       "title": (lv or {}).get("label"), "chart": (lv or {}).get("chart"),
                       "ticker": state.get("ticker", []), "recess": state.get("recess")})
    script = ROOM_JS.replace("__DATA__", data.replace("</", "<\\/"))
    return f"""<section class="room" id="room3d" aria-label="Council room">
  <div class="roomhead">
    <div class="overall"><span class="lbl">Council accuracy</span><b>{overall}</b><small>{overall_sub}</small></div>
    <div class="docket"><span class="lbl">On the table</span>{docket}</div>
  </div>
  <div class="stage" id="stage3d"><div class="stamp3d">{stamp}</div><div class="tip3d" id="tip3d" hidden></div></div>
  <div class="floor">
    <div class="table"><div class="gavel" aria-hidden="true"></div>{stamp}</div>
    <div class="seats">{"".join(seats)}</div>
  </div>
  <p class="hint">Each member walks to the speaking ring to cast a vote, then The Chair rules. Tap a member to read their reasoning. Drag to look around.</p>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/objects/Reflector.js"></script>
  <script>{script}</script>
</section>"""


ROOM_JS = r"""
(function () {
  var D = __DATA__;
  if (D.recess) D.verdict = null;          // court adjourned: everyone stays seated
  var room = document.getElementById('room3d'), stage = document.getElementById('stage3d'), tip = document.getElementById('tip3d');
  var stampEl = stage.querySelector('.stamp3d');
  if (!window.THREE) return;
  var renderer;
  try { renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false }); } catch (e) { return; }
  if (!renderer.getContext()) return;
  room.classList.add('is3d');
  var still = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  var small = (stage.clientWidth || 360) < 520;
  renderer.setPixelRatio(Math.min(small ? 1.75 : 2, window.devicePixelRatio || 1));
  renderer.setClearColor(0x05060d, 1);
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  stage.insertBefore(renderer.domElement, stage.firstChild);

  var scene = new THREE.Scene();
  scene.fog = new THREE.Fog(0x05060d, 11, 24);
  var cam = new THREE.PerspectiveCamera(38, 1, 0.1, 100);
  var world = new THREE.Group(); scene.add(world);

  // ── lighting: dim room, hard key light with shadows, colored rims, glow from the holotable
  scene.add(new THREE.HemisphereLight(0x8aa2ff, 0x140c24, 0.6)); scene.add(new THREE.AmbientLight(0x404a70, 0.35));
  var key = new THREE.SpotLight(0xdcebff, 2.3, 28, 0.7, 0.5, 1.0);
  key.position.set(-2.5, 9.5, 5.5); key.target.position.set(0, 0.5, -1.6);
  key.castShadow = true; key.shadow.mapSize.set(small ? 1024 : 2048, small ? 1024 : 2048); key.shadow.bias = -0.0004;
  key.shadow.camera.near = 3; key.shadow.camera.far = 22;
  scene.add(key); scene.add(key.target);
  var rimC = new THREE.DirectionalLight(0x22d3ee, 0.8); rimC.position.set(-6, 3, -6); scene.add(rimC);
  var rimM = new THREE.DirectionalLight(0xc026d3, 0.65); rimM.position.set(6, 3, -5); scene.add(rimM);
  var holoLight = new THREE.PointLight(0x38e1ff, 1.0, 7, 2); holoLight.position.set(0, 1.5, 1.0); scene.add(holoLight);
  var verdictLight = new THREE.PointLight(0x53f0a6, 0, 12, 2); verdictLight.position.set(0, 3.5, 0.5); scene.add(verdictLight);

  var METAL = new THREE.MeshStandardMaterial({ color: 0x2a2f45, metalness: 0.9, roughness: 0.28 });
  var METAL2 = new THREE.MeshStandardMaterial({ color: 0x40465f, metalness: 0.85, roughness: 0.35 });
  var STEEL = new THREE.MeshStandardMaterial({ color: 0x1a1f30, metalness: 0.8, roughness: 0.35 });
  var GLASS = new THREE.MeshStandardMaterial({ color: 0x2a3b66, metalness: 0.1, roughness: 0.05, transparent: true, opacity: 0.28 });
  function glowMat(c) { return new THREE.MeshBasicMaterial({ color: c }); }
  function basic(c, o) { return new THREE.MeshBasicMaterial({ color: c, transparent: true, opacity: o === undefined ? 1 : o, depthWrite: false }); }
  function addv(c, o) { return new THREE.MeshBasicMaterial({ color: c, transparent: true, opacity: o, depthWrite: false, blending: THREE.AdditiveBlending, side: THREE.DoubleSide }); }
  function M(g, m) { return new THREE.Mesh(g, m); }
  function at(m, x, y, z) { m.position.set(x, y, z); world.add(m); return m; }
  function shadowy(o, cast, recv) { o.traverse(function (k) { if (k.isMesh) { k.castShadow = cast; k.receiveShadow = recv; } }); return o; }

  // soft glow sprite (fake bloom)
  var GLOW = (function () {
    var c = document.createElement('canvas'); c.width = c.height = 128; var x = c.getContext('2d');
    var g = x.createRadialGradient(64, 64, 0, 64, 64, 64);
    g.addColorStop(0, 'rgba(255,255,255,1)'); g.addColorStop(0.25, 'rgba(255,255,255,0.55)'); g.addColorStop(1, 'rgba(255,255,255,0)');
    x.fillStyle = g; x.fillRect(0, 0, 128, 128); return new THREE.CanvasTexture(c);
  })();
  function glow(color, size, opacity) {
    var s = new THREE.Sprite(new THREE.SpriteMaterial({ map: GLOW, color: color, transparent: true, opacity: opacity || 0.8,
      depthWrite: false, blending: THREE.AdditiveBlending }));
    s.scale.set(size, size, 1); return s;
  }

  // ── floor: mirror underneath, translucent tech tiles on top
  if (THREE.Reflector) {
    var mirror = new THREE.Reflector(new THREE.PlaneGeometry(30, 30), {
      textureWidth: Math.round((stage.clientWidth || 360) * (small ? 0.6 : 0.9)),
      textureHeight: Math.round((stage.clientWidth || 360) * (small ? 0.45 : 0.6)), color: 0x6a7090 });
    mirror.rotation.x = -Math.PI / 2; at(mirror, 0, -0.002, 0);
  }
  var tileTex = (function () {
    var c = document.createElement('canvas'); c.width = c.height = 256; var x = c.getContext('2d');
    x.fillStyle = 'rgba(10,12,24,0.86)'; x.fillRect(0, 0, 256, 256);
    x.strokeStyle = 'rgba(56,225,255,0.18)'; x.lineWidth = 2; x.strokeRect(1, 1, 254, 254);
    x.strokeStyle = 'rgba(56,225,255,0.07)'; x.lineWidth = 1; x.strokeRect(64, 64, 128, 128);
    var t = new THREE.CanvasTexture(c); t.wrapS = t.wrapT = THREE.RepeatWrapping; t.repeat.set(15, 15); return t;
  })();
  var tiles = M(new THREE.PlaneGeometry(30, 30), new THREE.MeshStandardMaterial({ map: tileTex, transparent: true, roughness: 0.4, metalness: 0.5,
    opacity: THREE.Reflector ? 0.9 : 1 }));
  tiles.rotation.x = -Math.PI / 2; tiles.receiveShadow = true; at(tiles, 0, 0, 0);

  // ── curved data wall with a scrolling ticker
  function dataWallTex() {
    var c = document.createElement('canvas'); c.width = 1024; c.height = 256; var x = c.getContext('2d');
    x.fillStyle = '#070a16'; x.fillRect(0, 0, 1024, 256);
    for (var p = 0; p < 16; p++) {                       // little chart panels
      var px = p * 64 + 4, py = 18 + (p % 2) * 112;
      x.strokeStyle = 'rgba(56,225,255,0.25)'; x.strokeRect(px, py, 56, 96);
      x.beginPath(); var v = 50 + (p * 13 % 30);
      for (var i = 0; i < 12; i++) { v += Math.sin(p * 7 + i * 1.7) * 7; x.lineTo(px + 4 + i * 4.4, py + 20 + v * 0.8 % 70); }
      x.strokeStyle = p % 3 ? 'rgba(56,225,255,0.7)' : 'rgba(192,38,211,0.7)'; x.lineWidth = 1.5; x.stroke();
      x.fillStyle = 'rgba(160,190,255,0.45)'; x.font = '9px monospace'; x.fillText((1 + (p * 0.037)).toFixed(4), px + 4, py + 92);
    }
    var t = new THREE.CanvasTexture(c); t.wrapS = THREE.RepeatWrapping; t.repeat.set(-2, 1); return t;
  }
  var wallTex = dataWallTex();
  var wall = M(new THREE.CylinderGeometry(9, 9, 5.2, 64, 1, true, Math.PI * 0.62, Math.PI * 0.76),
    new THREE.MeshBasicMaterial({ map: wallTex, side: THREE.BackSide }));
  at(wall, 0, 2.6, 1.5);
  var tickerText = (D.ticker && D.ticker.length) ? D.ticker.map(function (t) {
    return t.s + ' ' + t.p + ' ' + (t.c >= 0 ? '▲' : '▼') + Math.abs(t.c).toFixed(2) + '%'; }).join('     ') : (D.recess ? 'COURT ADJOURNED  ·  ' + D.recess.toUpperCase() : 'THE COUNCIL  ·  IN SESSION');
  var tickCanvas = document.createElement('canvas'); tickCanvas.width = 2048; tickCanvas.height = 64;
  (function () {
    var x = tickCanvas.getContext('2d'); x.fillStyle = '#0b1022'; x.fillRect(0, 0, 2048, 64);
    x.fillStyle = 'rgba(56,225,255,0.9)'; x.fillRect(0, 0, 2048, 2); x.fillRect(0, 62, 2048, 2);
    x.font = '600 30px ui-monospace, Menlo, monospace'; x.textBaseline = 'middle';
    var s = tickerText + '     ·     ', w = x.measureText(s).width, n = Math.max(1, Math.floor(2048 / w)), step = 2048 / n;
    for (var rep = 0; rep < n; rep++) {
      var pos = rep * step;
      s.split(/(▲[\d.]+%|▼[\d.]+%)/).forEach(function (pt) { if (!pt) return; x.fillStyle = pt[0] === '▲' ? '#53f0a6' : pt[0] === '▼' ? '#ff6b7c' : '#cfe3ff';
        x.fillText(pt, pos, 34); pos += x.measureText(pt).width; });
    }
  })();
  var tickTex = new THREE.CanvasTexture(tickCanvas); tickTex.wrapS = THREE.RepeatWrapping; tickTex.repeat.set(-2, 1);
  var ticker = M(new THREE.CylinderGeometry(8.9, 8.9, 0.42, 64, 1, true, Math.PI * 0.62, Math.PI * 0.76),
    new THREE.MeshBasicMaterial({ map: tickTex, side: THREE.BackSide }));
  at(ticker, 0, 4.45, 1.5);
  // ceiling ring + pillars
  var ring = M(new THREE.TorusGeometry(6.5, 0.05, 8, 96), addv(0x38e1ff, 0.6)); ring.rotation.x = Math.PI / 2; at(ring, 0, 6.2, -1.2);
  [-1, 1].forEach(function (sd) {
    for (var k = 0; k < 2; k++) {
      var px = sd * (5.2 + k * 1.6), pz = -2.8 + k * 1.8;
      at(shadowy(M(new THREE.BoxGeometry(0.35, 6.4, 0.35), STEEL), true, true), px, 3.2, pz);
      at(M(new THREE.BoxGeometry(0.04, 5.8, 0.04), glowMat(sd < 0 ? 0x22d3ee : 0xc026d3)), px + (sd < 0 ? 0.19 : -0.19), 3.2, pz + 0.19);
    }
  });

  // ── holotable at the center with the live chart hologram above it
  var table = new THREE.Group(); table.position.set(0, 0, 1.0); table.scale.setScalar(0.78); world.add(table);
  table.add(shadowy(M(new THREE.CylinderGeometry(1.15, 1.3, 0.12, 6), STEEL), true, true)).position.y = 0.86;
  table.add(shadowy(M(new THREE.CylinderGeometry(0.5, 0.75, 0.8, 6), METAL2), true, true)).position.y = 0.4;
  var rim = M(new THREE.TorusGeometry(1.17, 0.018, 6, 6), glowMat(0x38e1ff)); rim.rotation.x = Math.PI / 2; rim.rotation.z = Math.PI / 6; rim.position.y = 0.93; table.add(rim);
  var lens = M(new THREE.CircleGeometry(0.85, 6), addv(0x38e1ff, 0.35)); lens.rotation.x = -Math.PI / 2; lens.position.y = 0.935; table.add(lens);
  var beam = M(new THREE.CylinderGeometry(1.9, 0.7, 3.4, 6, 1, true), addv(0x38e1ff, 0.045)); beam.position.y = 2.6; table.add(beam);
  table.add(glow(0x38e1ff, 2.6, 0.55)).position.y = 1.0;

  function chartTexture() {
    var W = 1024, H = 560, c = document.createElement('canvas'); c.width = W; c.height = H; var x = c.getContext('2d');
    x.fillStyle = 'rgba(8,30,48,0.35)'; x.fillRect(0, 0, W, H);
    x.strokeStyle = 'rgba(56,225,255,0.9)'; x.lineWidth = 4; x.strokeRect(6, 6, W - 12, H - 12);
    for (var gy = 1; gy < 5; gy++) { x.strokeStyle = 'rgba(56,225,255,0.12)'; x.lineWidth = 1; x.beginPath(); x.moveTo(30, 90 + gy * 88); x.lineTo(W - 30, 90 + gy * 88); x.stroke(); }
    x.fillStyle = '#cff6ff'; x.font = '700 46px ui-monospace, Menlo, monospace'; x.textBaseline = 'middle';
    x.fillText(D.recess ? 'THE COUNCIL' : D.title ? String(D.title).slice(0, 34) : 'COURT IN RECESS', 34, 50);
    var ch = D.chart;
    if (D.recess) {
      x.fillStyle = 'rgba(8,30,48,0.6)'; x.fillRect(12, 80, W - 24, H - 92);
      x.fillStyle = '#f4c84a'; x.font = '800 64px ui-monospace, Menlo, monospace'; x.textAlign = 'center';
      x.fillText('COURT ADJOURNED', W / 2, H / 2 - 10);
      x.fillStyle = '#cff6ff'; x.font = '500 38px ui-monospace, Menlo, monospace'; x.fillText(String(D.recess), W / 2, H / 2 + 60);
      x.textAlign = 'left';
    } else if (ch && ch.closes && ch.closes.length > 2) {
      var vals = ch.closes.slice(), lv = [ch.entry, ch.sl, ch.tp].filter(function (v) { return v != null; });
      var lo = Math.min.apply(null, vals.concat(lv)), hi = Math.max.apply(null, vals.concat(lv)), pad = (hi - lo) * 0.08 || 1e-4;
      lo -= pad; hi += pad;
      var X = function (i) { return 40 + i / (vals.length - 1) * (W - 260); }, Y = function (v) { return 110 + (hi - v) / (hi - lo) * (H - 150); };
      x.beginPath(); vals.forEach(function (v, i) { if (i) x.lineTo(X(i), Y(v)); else x.moveTo(X(i), Y(v)); });
      x.lineTo(X(vals.length - 1), H - 30); x.lineTo(X(0), H - 30); x.closePath();
      var gr = x.createLinearGradient(0, 110, 0, H); gr.addColorStop(0, 'rgba(56,225,255,0.35)'); gr.addColorStop(1, 'rgba(56,225,255,0)');
      x.fillStyle = gr; x.fill();
      x.beginPath(); vals.forEach(function (v, i) { if (i) x.lineTo(X(i), Y(v)); else x.moveTo(X(i), Y(v)); });
      x.strokeStyle = '#7ff3ff'; x.lineWidth = 5; x.lineJoin = 'round'; x.stroke();
      function level(v, col, name) {
        if (v == null) return; x.setLineDash([16, 10]); x.strokeStyle = col; x.lineWidth = 3;
        x.beginPath(); x.moveTo(40, Y(v)); x.lineTo(W - 220, Y(v)); x.stroke(); x.setLineDash([]);
        x.fillStyle = col; x.font = '600 30px ui-monospace, Menlo, monospace'; x.fillText(name + ' ' + v.toFixed(ch.prec || 5), W - 212, Y(v));
      }
      level(ch.tp, '#53f0a6', 'TP'); level(ch.entry, '#ffffff', 'IN'); level(ch.sl, '#ff6b7c', 'SL');
    } else {
      x.fillStyle = 'rgba(207,246,255,0.7)'; x.font = '500 34px ui-monospace, Menlo, monospace';
      x.fillText(D.title ? 'live chart appears with the next vote' : 'waiting for a setup…', 34, H / 2 + 20);
    }
    return new THREE.CanvasTexture(c);
  }
  var holo = new THREE.Group(); holo.position.set(0, 3.7, 0.6); world.add(holo);
  var holoMat = new THREE.MeshBasicMaterial({ map: chartTexture(), transparent: true, opacity: 0.95, depthWrite: false,
    blending: THREE.AdditiveBlending, side: THREE.DoubleSide });
  var holoPlane = M(new THREE.PlaneGeometry(3.0, 1.64), holoMat); holo.add(holoPlane);
  var scan = M(new THREE.PlaneGeometry(3.0, 0.03), addv(0x9ff8ff, 0.5)); scan.position.z = 0.01; holo.add(scan);

  // ── The Chair's raised dais at the back
  var dais = at(shadowy(M(new THREE.CylinderGeometry(1.6, 1.9, 0.8, 8), STEEL), true, true), 0, 0.4, -5.9);
  var daisRim = M(new THREE.TorusGeometry(1.62, 0.03, 6, 8), glowMat(0xf4c84a)); daisRim.rotation.x = Math.PI / 2; at(daisRim, 0, 0.81, -5.9);
  var bench = at(shadowy(M(new THREE.BoxGeometry(2.6, 1.0, 0.5), STEEL), true, true), 0, 1.3, -5.05);
  at(M(new THREE.BoxGeometry(2.62, 0.05, 0.05), glowMat(0xf4c84a)), 0, 1.78, -4.79);
  var plateC = document.createElement('canvas'); plateC.width = 512; plateC.height = 64;
  (function () { var x = plateC.getContext('2d'); x.fillStyle = '#0b0e1c'; x.fillRect(0, 0, 512, 64);
    x.fillStyle = '#f4c84a'; x.font = '700 30px ui-monospace, Menlo, monospace'; x.textAlign = 'center'; x.textBaseline = 'middle'; x.fillText('THE CHAIR', 256, 33); })();
  at(M(new THREE.PlaneGeometry(1.3, 0.16), new THREE.MeshBasicMaterial({ map: new THREE.CanvasTexture(plateC) })), 0, 1.45, -4.79);

  var judge = new THREE.Group(); judge.position.set(0, 0.8, -5.75); world.add(judge);
  var ROBE = new THREE.MeshStandardMaterial({ color: 0x1b2433, roughness: 0.7, metalness: 0.1 });
  judge.add(shadowy(M(new THREE.ConeGeometry(0.46, 1.6, 24), ROBE), true, false)).position.y = 1.0;
  var trim = M(new THREE.TorusGeometry(0.2, 0.025, 8, 24), glowMat(0xf4c84a)); trim.rotation.x = Math.PI / 2; trim.position.y = 1.72; judge.add(trim);
  var jhead = new THREE.Group(); jhead.position.y = 1.96; judge.add(jhead);
  var JSKIN = new THREE.MeshStandardMaterial({ color: 0xa86b45, roughness: 0.65 });
  jhead.add(shadowy(M(new THREE.SphereGeometry(0.24, 24, 16), JSKIN), true, false));
  [-1, 1].forEach(function (sd) { var e = M(new THREE.SphereGeometry(0.03, 8, 6), new THREE.MeshBasicMaterial({ color: 0x111111 })); e.position.set(sd * 0.085, 0.02, 0.215); jhead.add(e);
    var br = M(new THREE.BoxGeometry(0.1, 0.025, 0.02), new THREE.MeshStandardMaterial({ color: 0xdddddd })); br.position.set(sd * 0.085, 0.09, 0.22); jhead.add(br); });
  var jcap = M(new THREE.CylinderGeometry(0.28, 0.23, 0.13, 28), new THREE.MeshStandardMaterial({ color: 0x1b2433, roughness: 0.6 })); jcap.position.y = 0.21; jhead.add(jcap);
  var braid = M(new THREE.CylinderGeometry(0.235, 0.235, 0.05, 28), new THREE.MeshStandardMaterial({ color: 0xd9b44a, metalness: 0.85, roughness: 0.3 })); braid.position.y = 0.16; jhead.add(braid);
  var jpeak = M(new THREE.CylinderGeometry(0.18, 0.18, 0.015, 24, 1, false, -Math.PI / 2, Math.PI), new THREE.MeshStandardMaterial({ color: 0x0b0b0e })); jpeak.position.set(0, 0.14, 0.13); jpeak.rotation.x = 0.25; jhead.add(jpeak);
  var halo = new THREE.Group(); jhead.add(halo);
  [-1, 1].forEach(function (sd) { for (var k = 0; k < 4; k++) { var star = M(new THREE.OctahedronGeometry(0.025, 0), new THREE.MeshBasicMaterial({ color: 0xf4c84a }));
    star.position.set(sd * (0.36 + k * 0.0) , 1.62 + 0, 0.05 - k * 0.06); judge.add(star); } });
  var arm = new THREE.Group(); arm.position.set(0.32, 1.55, 0.12); judge.add(arm);
  var fore = M(new THREE.CylinderGeometry(0.05, 0.045, 0.5, 10), ROBE); fore.rotation.x = Math.PI / 2; fore.position.z = 0.25; arm.add(fore);
  var gavel = new THREE.Group(); gavel.position.set(0, 0, 0.5); arm.add(gavel);
  var gh = M(new THREE.CylinderGeometry(0.02, 0.02, 0.42, 8), METAL2); gh.rotation.x = Math.PI / 2; gh.position.z = 0.15; gavel.add(gh);
  var ghead = M(new THREE.CylinderGeometry(0.075, 0.075, 0.24, 16), glowMat(0xf4c84a)); ghead.rotation.z = Math.PI / 2; ghead.position.z = 0.36; gavel.add(ghead);
  var gglow = glow(0xf4c84a, 0.5, 0.0); gglow.position.z = 0.36; gavel.add(gglow);

  // ── atmosphere: light shafts and drifting dust
  [[-3.0, -2.3], [0, -3.4], [3.0, -2.3], [0, 1.0]].forEach(function (p, i) {
    var shaft = M(new THREE.CylinderGeometry(0.15, i === 3 ? 1.5 : 0.95, 6.2, 24, 1, true), addv(i === 3 ? 0x38e1ff : 0xbfd4ff, i === 3 ? 0.035 : 0.045));
    at(shaft, p[0], 3.1, p[1]);
  });
  var dustN = small ? 220 : 380, dustG = new THREE.BufferGeometry(), dp = new Float32Array(dustN * 3);
  for (var di = 0; di < dustN; di++) { dp[di * 3] = (Math.random() - 0.5) * 9; dp[di * 3 + 1] = Math.random() * 5.5; dp[di * 3 + 2] = (Math.random() - 0.7) * 7; }
  dustG.setAttribute('position', new THREE.BufferAttribute(dp, 3));
  var dust = new THREE.Points(dustG, new THREE.PointsMaterial({ color: 0xbfe9ff, size: 0.035, transparent: true, opacity: 0.55,
    depthWrite: false, blending: THREE.AdditiveBlending }));
  world.add(dust);

  // ── the council as little military officers: big heads, uniforms, unique headgear and insignia
  var COLORS = { 'The Quant': 0x3ee6a1, 'Risk': 0xff5468, 'Momentum': 0xffb03b, 'Time': 0x4cb7ff, 'Doubt': 0xc9a7ff };
  function std(c, r, m) { return new THREE.MeshStandardMaterial({ color: c, roughness: r === undefined ? 0.75 : r, metalness: m || 0 }); }
  var BOOT = std(0x15130f, 0.45, 0.2), GOLD = std(0xd9b44a, 0.3, 0.85), BLACK = std(0x0b0b0e, 0.6), WHITE = std(0xf2f2ee, 0.5);
  var EYE = new THREE.MeshBasicMaterial({ color: 0x111111 });
  function bar(parent, w, h, x, y, z, rz, mat) { var b = M(new THREE.BoxGeometry(w, h, 0.03), mat); b.position.set(x, y, z); b.rotation.z = rz || 0; parent.add(b); return b; }
  // two-part limb: shoulder/hip → elbow/knee → hand/boot
  function limb(g, x, y, len, r, mat, endMat) {
    var piv = new THREE.Group(); piv.position.set(x, y, 0);
    var half = len / 2, leg = y < 1.0;
    var up = M(new THREE.CylinderGeometry(r * 1.08, r * 0.95, half, 12), mat); up.position.y = -half / 2; piv.add(up);
    var joint = new THREE.Group(); joint.position.y = -half; piv.add(joint);
    joint.add(M(new THREE.SphereGeometry(r * 1.0, 10, 8), mat));
    var lo = M(new THREE.CylinderGeometry(r * 0.95, r * 0.85, half, 12), leg ? BOOT : mat); lo.position.y = -half / 2; joint.add(lo);
    if (leg) { var boot = M(new THREE.BoxGeometry(r * 2.3, r * 1.3, r * 3.8), BOOT); boot.position.set(0, -half - r * 0.25, r * 0.9); joint.add(boot); }
    else { var hand = M(new THREE.SphereGeometry(r * 1.15, 10, 8), endMat); hand.position.y = -half - r * 0.3; joint.add(hand); }
    piv.userData.joint = joint;
    g.add(piv); return piv;
  }
  // shared body: boots, trousers, tunic, belt, collar, big head with eyes/brows/mouth
  function soldier(g, u, o) {
    var TUNIC = std(o.tunic, 0.8), PANTS = std(o.pants || o.tunic, 0.8), SKIN = std(o.skin, 0.65);
    var w = o.wide || 1;
    u.legL = limb(g, -0.1 * w, 0.62, 0.6, 0.065 * w, PANTS); u.legR = limb(g, 0.1 * w, 0.62, 0.6, 0.065 * w, PANTS);
    var torso = M(new THREE.BoxGeometry(0.44 * w, 0.58, 0.28 * Math.max(1, w * 0.9)), TUNIC); torso.position.y = 0.93; g.add(torso);
    var belly = M(new THREE.BoxGeometry(0.46 * w, 0.08, 0.3 * Math.max(1, w * 0.9)), BLACK); belly.position.y = 0.72; g.add(belly);
    var buckle = M(new THREE.BoxGeometry(0.07, 0.06, 0.02), GOLD); buckle.position.set(0, 0.72, 0.16 * Math.max(1, w * 0.9)); g.add(buckle);
    for (var b = 0; b < 3; b++) { var btn = M(new THREE.SphereGeometry(0.018, 8, 6), GOLD); btn.position.set(0, 0.84 + b * 0.1, 0.145 * Math.max(1, w * 0.9)); g.add(btn); }
    [-1, 1].forEach(function (sd) {                                   // epaulettes
      var ep = M(new THREE.BoxGeometry(0.13, 0.03, 0.2), o.epaulette ? GOLD : TUNIC); ep.position.set(sd * 0.2 * w, 1.23, 0); g.add(ep);
    });
    var badge = M(new THREE.BoxGeometry(0.08, 0.06, 0.02), new THREE.MeshBasicMaterial({ color: u.col }));   // unit patch in their color
    badge.position.set(-0.12 * w, 1.06, 0.145 * Math.max(1, w * 0.9)); g.add(badge); u.patch = badge;
    if (o.medals) for (var m = 0; m < o.medals; m++) {
      var md = M(new THREE.BoxGeometry(0.035, 0.05, 0.02), [GOLD, std(0xb03a2e), std(0x2e5cb0)][m % 3]);
      md.position.set(0.07 * w + m * 0.045, 1.07, 0.145 * Math.max(1, w * 0.9)); g.add(md);
    }
    u.armL = limb(g, -0.27 * w, 1.2, 0.56, 0.055 * w, TUNIC, SKIN); u.armR = limb(g, 0.27 * w, 1.2, 0.56, 0.055 * w, TUNIC, SKIN);
    var neck = M(new THREE.CylinderGeometry(0.07, 0.08, 0.08, 12), SKIN); neck.position.y = 1.27; g.add(neck);
    var head = new THREE.Group(); head.position.y = 1.5; g.add(head); u.head = head;
    var skull = M(new THREE.SphereGeometry(0.235, 28, 20), SKIN); skull.scale.set(1, 1.05, 0.98); head.add(skull);
    [-1, 1].forEach(function (sd) { var ear = M(new THREE.SphereGeometry(0.05, 10, 8), SKIN); ear.position.set(sd * 0.235, 0, 0); ear.scale.set(0.5, 1, 0.8); head.add(ear); });
    u.glows = [];
    [-1, 1].forEach(function (sd) {
      var e = M(new THREE.SphereGeometry(0.03, 10, 8), EYE); e.position.set(sd * 0.085, 0.02, 0.215); head.add(e); u.glows.push(e);
      var br = bar(head, 0.09, 0.022, sd * 0.085, 0.09 + (o.browUp && sd > 0 ? 0.03 : 0), 0.22, sd * (o.browTilt || 0), BLACK);
    });
    var mouth = M(new THREE.BoxGeometry(o.mouthW || 0.08, 0.018, 0.02), std(0x5a2a22)); mouth.position.set(o.mouthX || 0, -0.1, 0.22); mouth.rotation.z = o.mouthTilt || 0; head.add(mouth); u.mouth = mouth;
    var nose = M(new THREE.SphereGeometry(0.028, 8, 6), SKIN); nose.position.set(0, -0.03, 0.235); head.add(nose);
    return { head: head, TUNIC: TUNIC, SKIN: SKIN };
  }
  var BUILD = {
    // intelligence officer: olive uniform, beret, round glasses, tablet
    'The Quant': function (g, u, col) {
      var s = soldier(g, u, { tunic: 0x4b5a3a, skin: 0xe0ac69, medals: 1 }), h = s.head;
      var beret = M(new THREE.SphereGeometry(0.25, 20, 12, 0, Math.PI * 2, 0, Math.PI / 2), std(0x1f5e3a)); beret.scale.set(1.05, 0.45, 1.05);
      beret.position.set(0.03, 0.12, -0.01); beret.rotation.z = -0.25; h.add(beret);
      var flash = M(new THREE.BoxGeometry(0.05, 0.05, 0.02), GOLD); flash.position.set(-0.11, 0.18, 0.18); h.add(flash);
      [-1, 1].forEach(function (sd) { var lens = M(new THREE.TorusGeometry(0.05, 0.008, 6, 20), BLACK); lens.position.set(sd * 0.085, 0.02, 0.23); h.add(lens); });
      var tab = M(new THREE.BoxGeometry(0.2, 0.26, 0.02), BLACK); tab.position.set(0, -0.06, 0.08); tab.rotation.x = -0.8;
      var scr = M(new THREE.PlaneGeometry(0.17, 0.22), new THREE.MeshBasicMaterial({ color: col })); scr.position.z = 0.012; tab.add(scr);
      u.armL.userData.joint.add(tab);
    },
    // military police / drill sergeant: broad, campaign hat, red armband, scowl
    'Risk': function (g, u, col) {
      var s = soldier(g, u, { tunic: 0x6b5a3e, pants: 0x4a3f2c, skin: 0x8d5524, wide: 1.22, browTilt: 0.35, mouthW: 0.1, medals: 2 }), h = s.head;
      var brim = M(new THREE.CylinderGeometry(0.4, 0.4, 0.02, 32), std(0x8a6a3a)); brim.position.y = 0.14; h.add(brim);
      var crown = M(new THREE.ConeGeometry(0.2, 0.22, 4), std(0x8a6a3a)); crown.position.y = 0.26; crown.rotation.y = Math.PI / 4; h.add(crown);
      var band = M(new THREE.CylinderGeometry(0.21, 0.21, 0.04, 20), BLACK); band.position.y = 0.17; h.add(band);
      var arm = M(new THREE.CylinderGeometry(0.075, 0.075, 0.1, 12), new THREE.MeshStandardMaterial({ color: 0xc0262e, roughness: 0.7 }));
      arm.position.y = -0.1; u.armL.add(arm);
      var mp = M(new THREE.BoxGeometry(0.05, 0.04, 0.005), WHITE); mp.position.set(0, -0.1, 0.078); u.armL.add(mp);
      var whistle = M(new THREE.CylinderGeometry(0.012, 0.012, 0.05, 8), GOLD); whistle.position.set(0.1, 1.12, 0.17); whistle.rotation.z = 1.2; g.add(whistle);
    },
    // paratrooper: lean, helmet with goggles, forward-leaning, speed lines when walking
    'Momentum': function (g, u, col) {
      var s = soldier(g, u, { tunic: 0x5a4a32, pants: 0x4d4030, skin: 0xc68642, mouthW: 0.1, mouthTilt: -0.15, browUp: true }), h = s.head;
      var helm = M(new THREE.SphereGeometry(0.27, 24, 14, 0, Math.PI * 2, 0, Math.PI / 2), std(0x4f5a34, 0.85)); helm.position.y = 0.04; h.add(helm);
      var rim = M(new THREE.TorusGeometry(0.27, 0.02, 6, 32), std(0x4f5a34, 0.85)); rim.rotation.x = Math.PI / 2; rim.position.y = 0.04; h.add(rim);
      var strap = M(new THREE.BoxGeometry(0.5, 0.035, 0.02), BLACK); strap.position.set(0, 0.17, 0.2); h.add(strap);
      [-1, 1].forEach(function (sd) { var gg = M(new THREE.CylinderGeometry(0.055, 0.055, 0.04, 16), new THREE.MeshBasicMaterial({ color: col })); gg.rotation.x = Math.PI / 2; gg.position.set(sd * 0.08, 0.17, 0.22); h.add(gg); });
      var pack = M(new THREE.BoxGeometry(0.34, 0.4, 0.14), std(0x3d4529)); pack.position.set(0, 0.95, -0.21); g.add(pack);
      u.trail = [];
      for (var i = 0; i < 4; i++) { var tp = M(new THREE.SphereGeometry(0.045 - i * 0.008, 8, 6), basic(col, 0.7 - i * 0.15)); world.add(tp); u.trail.push(tp); }
    },
    // navy officer / timekeeper: white peaked cap, navy tunic, glowing wristwatch, stout
    'Time': function (g, u, col) {
      var s = soldier(g, u, { tunic: 0x1c2a4a, skin: 0xf1c27d, wide: 1.12, epaulette: true, medals: 2 }), h = s.head;
      var cap = M(new THREE.CylinderGeometry(0.26, 0.22, 0.12, 28), WHITE); cap.position.y = 0.2; h.add(cap);
      var band = M(new THREE.CylinderGeometry(0.225, 0.225, 0.05, 28), BLACK); band.position.y = 0.15; h.add(band);
      var peak = M(new THREE.CylinderGeometry(0.17, 0.17, 0.015, 24, 1, false, -Math.PI / 2, Math.PI), BLACK); peak.position.set(0, 0.13, 0.12); peak.rotation.x = 0.25; h.add(peak);
      var crest = M(new THREE.SphereGeometry(0.035, 10, 8), GOLD); crest.position.set(0, 0.17, 0.225); h.add(crest);
      var watch = M(new THREE.CylinderGeometry(0.06, 0.06, 0.03, 16), new THREE.MeshBasicMaterial({ color: col })); watch.rotation.z = Math.PI / 2;
      watch.position.set(0.06, -0.2, 0); u.armL.userData.joint.add(watch);
      var hands = new THREE.Group(); hands.position.set(0.078, -0.2, 0); hands.rotation.y = Math.PI / 2; u.armL.userData.joint.add(hands); u.hands = hands;
      bar(hands, 0.008, 0.045, 0, 0.02, 0, 0, BLACK);
    },
    // grizzled colonel: garrison cap tilted, grey mustache, monocle, lots of medals
    'Doubt': function (g, u, col) {
      var s = soldier(g, u, { tunic: 0x3a3f4a, skin: 0x5c3a21, medals: 4, browUp: true, mouthX: 0.03, mouthTilt: -0.2, epaulette: true }), h = s.head;
      var cap = M(new THREE.BoxGeometry(0.3, 0.1, 0.4), std(0x2c313b)); cap.position.set(0.04, 0.24, 0); cap.rotation.z = -0.3; h.add(cap);
      var pip = M(new THREE.BoxGeometry(0.04, 0.04, 0.02), GOLD); pip.position.set(-0.08, 0.27, 0.19); h.add(pip);
      var stache = M(new THREE.BoxGeometry(0.16, 0.035, 0.04), std(0x9a9a9a)); stache.position.set(0, -0.065, 0.225); h.add(stache);
      var mono = M(new THREE.TorusGeometry(0.05, 0.01, 6, 20), GOLD); mono.position.set(0.085, 0.02, 0.235); h.add(mono);
      var chain = M(new THREE.CylinderGeometry(0.004, 0.004, 0.25, 4), GOLD); chain.position.set(0.12, -0.1, 0.22); chain.rotation.z = 0.3; h.add(chain);
      head_tilt = h; h.rotation.z = 0.12;
    }
  };
  var head_tilt;
  function agent(name) {
    var col = COLORS[name] || 0xffffff, g = new THREE.Group(), u = g.userData;
    u.col = col;
    (BUILD[name] || BUILD['Doubt'])(g, u, col);
    var halo = M(new THREE.RingGeometry(0.34, 0.42, 40), basic(col, 0.45)); halo.rotation.x = -Math.PI / 2; halo.position.y = 0.02; g.add(halo); u.halo = halo;
    return g;
  }

  // ── member stations in an arc behind the holotable
  var people = [], signs = [], rings = [], N = D.voters.length;
  var SPEAK = new THREE.Vector3(1.75, 0, 2.55);
  function seatPos(i) {
    var a = (200 + i * (140 / Math.max(1, N - 1))) * Math.PI / 180;
    return new THREE.Vector3(Math.cos(a) * 3.5, 0, Math.sin(a) * 2.0 - 1.45);
  }
  function screenTex(name, col) {
    var c = document.createElement('canvas'); c.width = 256; c.height = 96; var x = c.getContext('2d');
    var hex = '#' + col.toString(16).padStart(6, '0');
    x.fillStyle = '#081020'; x.fillRect(0, 0, 256, 96); x.strokeStyle = hex; x.lineWidth = 4; x.strokeRect(2, 2, 252, 92);
    x.fillStyle = hex; x.font = '700 26px ui-monospace, Menlo, monospace'; x.textAlign = 'center'; x.textBaseline = 'middle';
    x.fillText(name.toUpperCase(), 128, 36);
    x.fillStyle = 'rgba(207,230,255,0.6)'; x.font = '500 16px ui-monospace, Menlo, monospace'; x.fillText('COUNCIL MEMBER', 128, 68);
    return new THREE.CanvasTexture(c);
  }
  function signTex(text, color) {
    var c = document.createElement('canvas'); c.width = 256; c.height = 112; var x = c.getContext('2d');
    x.fillStyle = 'rgba(6,10,24,0.85)'; x.fillRect(0, 0, 256, 112);
    x.strokeStyle = color; x.lineWidth = 4; x.strokeRect(4, 4, 248, 104);
    x.fillStyle = color; x.fillRect(4, 4, 248, 6);
    x.font = '700 50px ui-monospace, SFMono-Regular, Menlo, monospace'; x.textAlign = 'center'; x.textBaseline = 'middle';
    x.fillText(text, 128, 62);
    return new THREE.CanvasTexture(c);
  }
  D.voters.forEach(function (v, i) {
    var seat = seatPos(i);
    var face = Math.atan2(-seat.x, -seat.z);
    var g = agent(v.name), u = g.userData;
    u.i = i; u.seat = seat; u.faceSeat = face; u.yaw = face; u.phase = i * 1.37;
    u.hip = u.legL ? u.legL.position.y : 0.6; u.prev = seat.clone(); u.blinkAt = 1 + i * 0.9;
    // glow parts: the colored visor/face meshes, plus a halo sprite in front of the face
    u.glows = u.glows || [];
    var fg = glow(u.col, 0.35, 0.4); if (u.patch) { fg.position.copy(u.patch.position); fg.position.z += 0.03; g.add(fg); } u.faceGlow = fg;
    shadowy(g, true, false);
    g.position.copy(seat); world.add(g); people.push(g);
    // station: sleek chair + console with a screen and a floor ring that lights with the vote
    var st = new THREE.Group(); st.position.copy(seat); st.rotation.y = face; world.add(st);
    var seatTop = 0.5;
    st.add(shadowy(M(new THREE.BoxGeometry(0.66, 0.08, 0.58), STEEL), true, true)).position.set(0, seatTop - 0.04, -0.05);
    var back = M(new THREE.BoxGeometry(0.62, 1.45, 0.06), GLASS); back.position.set(0, seatTop + 0.66, -0.36); st.add(back);
    st.add(M(new THREE.BoxGeometry(0.03, 1.45, 0.07), glowMat(u.col))).position.set(-0.31, seatTop + 0.66, -0.36);
    st.add(M(new THREE.BoxGeometry(0.03, 1.45, 0.07), glowMat(u.col))).position.set(0.31, seatTop + 0.66, -0.36);
    st.add(shadowy(M(new THREE.CylinderGeometry(0.06, 0.12, seatTop - 0.08, 10), METAL2), true, true)).position.set(0, (seatTop - 0.08) / 2, -0.05);
    var con = new THREE.Group(); con.position.set(0.62, 0, -0.2); con.scale.setScalar(0.8); st.add(con);
    con.add(shadowy(M(new THREE.BoxGeometry(0.16, 0.9, 0.16), STEEL), true, true)).position.y = 0.45;
    var scr = M(new THREE.PlaneGeometry(0.64, 0.24), new THREE.MeshBasicMaterial({ map: screenTex(v.name, u.col) }));
    scr.position.set(0, 1.02, 0.06); scr.rotation.x = -0.35; con.add(scr);
    var ringMat = addv(0x334466, 0.5);
    var fr = M(new THREE.RingGeometry(0.46, 0.56, 48), ringMat); fr.rotation.x = -Math.PI / 2; fr.position.y = 0.012; st.add(fr);
    var rg = glow(0xffffff, 1.6, 0); rg.position.y = 0.2; st.add(rg);
    rings.push({ mat: ringMat, glow: rg, vote: v.vote });
    var word = v.vote === true ? 'YES' : v.vote === false ? 'NO' : 'PASS';
    var color = v.vote === true ? '#53f0a6' : v.vote === false ? '#ff6b7c' : '#a99fc9';
    var sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: signTex(word, color), transparent: true, depthWrite: false }));
    sp.scale.set(0.001, 0.001, 1); world.add(sp); signs.push(sp);
  });
  // speaking spot marker
  var spot = M(new THREE.RingGeometry(0.42, 0.5, 48), addv(0xf4c84a, 0.5)); spot.rotation.x = -Math.PI / 2; at(spot, SPEAK.x, 0.013, SPEAK.z);

  // paths around the holotable (out on one lane, back on another)
  var B = new THREE.Vector3(0, 0, -0.95), R = new THREE.Vector3(2.35, 0, 1.0);
  function pathFor(u, back) {
    var off = back ? -0.28 : 0.28, pts = [u.seat.clone()];
    if (u.seat.x < 0.9) pts.push(B.clone().add(new THREE.Vector3(0, 0, off)));
    pts.push(R.clone().add(new THREE.Vector3(off, 0, 0)));
    pts.push(SPEAK.clone().add(new THREE.Vector3(0, 0, -0.1)));
    return back ? pts.reverse() : pts;
  }
  function along(pts, k) {
    var L = 0, seg = []; for (var i = 1; i < pts.length; i++) { var d = pts[i].distanceTo(pts[i - 1]); seg.push(d); L += d; }
    var want = k * L;
    for (var j = 0; j < seg.length; j++) { if (want <= seg[j] || j === seg.length - 1) return pts[j].clone().lerp(pts[j + 1], Math.min(1, want / seg[j])); want -= seg[j]; }
    return pts[pts.length - 1].clone();
  }
  people.forEach(function (g) { g.userData.out = pathFor(g.userData, false); g.userData.back = pathFor(g.userData, true); });

  // ── layout
  function size() {
    var w = stage.clientWidth || 360, h = Math.round(Math.min(520, Math.max(320, w * 0.82)));
    renderer.setSize(w, h); cam.aspect = w / h;
    var narrow = w < 520;
    cam.position.set(0, narrow ? 3.9 : 3.6, narrow ? 10.8 : 9.6); cam.lookAt(0, 1.9, -1.3);
    cam.updateProjectionMatrix();
  }
  size(); window.addEventListener('resize', size);

  // ── interaction
  var ray = new THREE.Raycaster(), ptr = new THREE.Vector2(), drag = null, turn = 0, picked = -1, canvas = renderer.domElement;
  canvas.addEventListener('pointerdown', function (e) { drag = { x: e.clientX, t: turn, moved: 0 }; canvas.setPointerCapture(e.pointerId); });
  canvas.addEventListener('pointermove', function (e) {
    if (!drag) return; var dx = e.clientX - drag.x; drag.moved = Math.max(drag.moved, Math.abs(dx));
    turn = Math.max(-0.7, Math.min(0.7, drag.t + dx * 0.005));
  });
  canvas.addEventListener('pointerup', function (e) {
    var tap = drag && drag.moved < 6; drag = null; if (!tap) return;
    var r = canvas.getBoundingClientRect();
    ptr.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(ptr, cam);
    var hit = ray.intersectObjects(people, true)[0], o = hit && hit.object;
    while (o && o.userData.i === undefined) o = o.parent;
    show(o ? o.userData.i : -1);
  });
  function show(i) {
    picked = (picked === i) ? -1 : i;
    if (picked < 0) { tip.hidden = true; return; }
    var v = D.voters[picked], word = v.vote === true ? 'YES' : v.vote === false ? 'NO' : 'abstained';
    tip.textContent = '';
    var b = document.createElement('b'); b.textContent = v.name + ' · ' + word; tip.appendChild(b);
    var a = document.createElement('span'); a.textContent = v.why; tip.appendChild(a);
    var c = document.createElement('small'); c.textContent = v.acc; tip.appendChild(c);
    tip.hidden = false;
  }
  Array.prototype.forEach.call(room.querySelectorAll('.seat'), function (btn) {
    btn.addEventListener('click', function () { show(+btn.getAttribute('data-i')); });
  });

  // ── choreography: stand → walk around the table to the speaking spot → vote → walk back → sit → gavel
  var WALK = 2.2, STAGGER = 4.2, START = 1.0, TURN = 0.5 + WALK + 1.4 + WALK + 0.5;
  var VERDICT_AT = START + (N - 1) * STAGGER + TURN + 0.4, CYCLE = VERDICT_AT + 6.5;
  var VCOL = new THREE.Color(D.approved ? 0x53f0a6 : 0xff6b7c), CYAN = new THREE.Color(0x38e1ff);
  function clamp(x) { return Math.max(0, Math.min(1, x)); }
  function smooth(x) { x = clamp(x); return x * x * (3 - 2 * x); }
  function pop(x) { x = clamp(x); var c = 1.6; return 1 + (c + 1) * Math.pow(x - 1, 3) + c * Math.pow(x - 1, 2); }
  function plan(i, c) {
    var u = people[i].userData, tau = c - (START + i * STAGGER), t1 = 0.5, t2 = t1 + WALK, t3 = t2 + 1.4, t4 = t3 + WALK;
    if (!D.verdict || tau < 0) return { pos: u.seat, sit: 1 };
    if (tau < t1) return { pos: u.seat, sit: 1 - smooth(tau / t1) };
    if (tau < t2) return { pos: along(u.out, smooth((tau - t1) / WALK)), sit: 0 };
    if (tau < t3) return { pos: u.out[u.out.length - 1], sit: 0, at: true, k: tau - t2, spoke: tau > t2 + 0.25 };
    if (tau < t4) return { pos: along(u.back, smooth((tau - t3) / WALK)), sit: 0, spoke: true };
    return { pos: u.seat, sit: smooth((tau - t4) / 0.5), spoke: true };
  }
  var t0 = performance.now(), lastT = 0;
  function frame(now) {
    if (!stage.offsetParent) { requestAnimationFrame(frame); return; }   // hidden (collapsed section): skip work
    var t = (now - t0) / 1000, dt = Math.min(0.05, t - lastT); lastT = t;
    var c = still ? CYCLE - 3 : (D.verdict ? t % CYCLE : 0);
    var deliberating = D.verdict && c < CYCLE - 0.6;
    var verdictOn = D.verdict && c >= VERDICT_AT + 0.45 && c < CYCLE - 0.6;
    if (stampEl) stampEl.classList.toggle('show', !!verdictOn);
    world.rotation.y += (turn + (still ? 0 : Math.sin(t * 0.12) * 0.05) - world.rotation.y) * 0.06;

    var speaker = -1;
    people.forEach(function (g, i) {
      var u = g.userData, p = plan(i, c);
      if (p.at) speaker = i;
      u.prev.copy(g.position);
      var drop = (u.hip - 0.52) * p.sit;
      g.position.set(p.pos.x, -drop, p.pos.z);
      var vx = (g.position.x - u.prev.x) / Math.max(dt, 1e-3), vz = (g.position.z - u.prev.z) / Math.max(dt, 1e-3);
      var speed = Math.hypot(vx, vz);
      var target = p.at ? Math.atan2(cam.position.x - g.position.x, cam.position.z - g.position.z) - world.rotation.y
                 : speed > 0.15 ? Math.atan2(vx, vz) : u.faceSeat;
      var d = Math.atan2(Math.sin(target - u.yaw), Math.cos(target - u.yaw)); u.yaw += d * 0.12; g.rotation.y = u.yaw;
      var stride = Math.min(1, speed / 0.9), ph = t * 8 + u.phase, sw = Math.sin(ph) * 0.6 * stride;
      if (u.legL) {
        u.legL.rotation.x = sw - 1.45 * p.sit; u.legR.rotation.x = -sw - 1.45 * p.sit;
        u.legL.userData.joint.rotation.x = Math.max(0, -Math.sin(ph)) * 0.9 * stride + 1.45 * p.sit;
        u.legR.userData.joint.rotation.x = Math.max(0, Math.sin(ph)) * 0.9 * stride + 1.45 * p.sit;
      }
      var raise = p.at ? pop((p.k - 0.15) / 0.45) * (1 - smooth((p.k - 1.1) / 0.3)) : 0;
      if (u.armL) { u.armL.rotation.x = -sw * 0.7 - 0.45 * p.sit; u.armL.userData.joint.rotation.x = -0.25 * stride - 0.9 * p.sit; }
      if (u.armR) { u.armR.rotation.x = sw * 0.7 * (1 - raise) - raise * 2.8 - 0.45 * p.sit;
        u.armR.userData.joint.rotation.x = (-0.25 * stride - 0.9 * p.sit) * (1 - raise) - 0.15 * raise; }
      g.position.y += Math.abs(Math.sin(ph)) * 0.03 * stride;
      if (u.head) u.head.rotation.y = p.at ? 0 : (still ? 0 : 0.4 * Math.sin(t * 0.5 + u.phase * 3)) * (p.sit > 0.5 ? 1 : 0.3);
      if (u.spin) u.spin.rotation.z = t * 1.8;
      if (u.gyro) { u.gyro.rotation.y = t * 1.4; u.gyro.rotation.x = t * 0.7; }
      if (u.hands) u.hands.rotation.z = -t * 1.2;
      if (u.trail) u.trail.forEach(function (tp, k) {
        var bk = 0.25 + k * 0.16;
        tp.position.set(g.position.x - Math.sin(u.yaw) * bk * (0.3 + stride), 1.2 - k * 0.05, g.position.z - Math.cos(u.yaw) * bk * (0.3 + stride));
        tp.visible = stride > 0.2;
      });
      // blink + speaking pulse
      if (!still && t > u.blinkAt) { u.blinkAt = t + 2.5 + ((i * 1.7 + t) % 3); u.blinkEnd = t + 0.12; }
      var blink = (!still && u.blinkEnd && t < u.blinkEnd) ? 0.15 : 1;
      var talk = p.at && p.k > 0.25 && p.k < 1.3 ? 0.5 + 0.5 * Math.sin(t * 22) : 0;
      u.glows.forEach(function (m) { m.scale.y = blink; });
      u.faceGlow.material.opacity = 0.25 + talk * 0.6; u.faceGlow.scale.setScalar(0.3 + talk * 0.3);
      if (u.mouth) u.mouth.scale.y = 1 + talk * 3.5;
      u.halo.visible = p.sit < 0.5;
      var sel = picked === i ? 1.08 : 1; g.scale.setScalar(g.scale.x + (sel - g.scale.x) * 0.2);
      u.halo.material.opacity = picked === i ? 0.95 : 0.4;
      // vote sign + station ring
      var sp = signs[i], showSign = deliberating && (p.spoke || (still && D.verdict));
      var k = showSign ? (p.at ? pop((p.k - 0.25) / 0.4) : 0.72) : 0;
      sp.position.set(g.position.x, (p.at ? 2.35 : 2.1) + g.position.y + (p.sit ? -0.1 : 0), g.position.z);
      sp.scale.set(0.8 * k + 0.001, 0.35 * k + 0.001, 1);
      var rr = rings[i], lit = showSign && rr.vote !== null && rr.vote !== undefined;
      var lc = rr.vote === true ? 0x53f0a6 : 0xff6b7c;
      rr.mat.color.setHex(lit ? lc : 0x334466); rr.mat.opacity = lit ? 0.85 : 0.35;
      rr.glow.material.color.setHex(lc); rr.glow.material.opacity = lit ? 0.35 + 0.1 * Math.sin(t * 4) : 0;
    });

    // The Chair follows the speaker, then swings the gavel
    var look = speaker >= 0 ? Math.atan2(people[speaker].position.x - judge.position.x, people[speaker].position.z - judge.position.z) * 0.6
             : (still ? 0 : 0.25 * Math.sin(t * 0.35));
    jhead.rotation.y += (look - jhead.rotation.y) * 0.08;
    halo.rotation.z = t * 0.6;
    var gt = D.verdict ? (c - VERDICT_AT) : -1;
    var swing = gt < 0 ? 0 : gt < 0.35 ? -smooth(gt / 0.35) * 1.1 : gt < 0.5 ? -1.1 + smooth((gt - 0.35) / 0.15) * 1.5 : 0.4 - smooth((gt - 0.5) / 0.6) * 0.4;
    arm.rotation.x = still ? 0 : swing;
    var flash = verdictOn ? Math.max(0, 1 - (c - VERDICT_AT - 0.45) / 1.6) : 0;
    gglow.material.opacity = flash * 0.9;
    verdictLight.color.copy(VCOL); verdictLight.intensity = verdictOn ? 0.6 + flash * 3.5 : 0;
    var hc = verdictOn ? VCOL : CYAN;
    holoLight.color.lerp(hc, 0.1); rim.material.color.lerp(hc, 0.1); lens.material.color.lerp(hc, 0.1);
    beam.material.color.lerp(hc, 0.1); beam.material.opacity = 0.07 + flash * 0.12;

    // hologram: faces the viewer, gentle hover, scanline + flicker
    holo.rotation.y = -world.rotation.y * 0.85;
    holo.position.y = 3.7 + (still ? 0 : Math.sin(t * 1.1) * 0.04);
    holoMat.opacity = still ? 0.95 : 0.88 + 0.07 * Math.sin(t * 13) * Math.sin(t * 3.1);
    scan.position.y = still ? -0.3 : ((t * 0.5) % 1) * 1.64 - 0.82;
    wallTex.offset.x = still ? 0 : t * 0.004; tickTex.offset.x = still ? 0 : t * 0.02;
    var pos = dust.geometry.attributes.position;
    if (!still) { for (var q = 0; q < dustN; q++) { var yy = pos.getY(q) + 0.12 * dt; pos.setY(q, yy > 5.6 ? 0.1 : yy); pos.setX(q, pos.getX(q) + Math.sin(t + q) * 0.0015); } pos.needsUpdate = true; }

    renderer.render(scene, cam);
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
})();
"""

# ───────────────────────────── Dashboard page ────────────────────────────────

def pct(x):
    return "—" if x is None else f"{x * 100:.0f}¢" if x >= 0 else f"−{abs(x) * 100:.0f}¢"


def fmt_hours(h):
    if h is None:
        return "—"
    return f"{h * 60:.0f}m" if h < 1 else f"{h:.0f}h" if h < 48 else f"{h / 24:.1f}d"


def action_center(state, flagged_count, results=()):
    now = time.time()
    cards, counts = [], {"buy": 0, "hold": 0, "sell": 0}
    def prio(g):
        if g.get("exit_px") is not None:
            return 0                                   # sell alerts first
        return 1 if now - g.get("opened", now) < 3600 else 2
    shown = 0
    for g in sorted(state.get("open", []), key=lambda g: (prio(g), -g.get("opened", 0))):
        if shown >= TOP_N:
            break
        side, label = g["side"], g["label"]
        strike = label.split(" — ")[-1]
        url = html.escape(g.get("url", "#"))
        entry, tp, sl = g["entry"], g["tp"], g["sl"]
        bid = g.get("last_bid")
        closes = g.get("close_ts")
        close_txt = f"Closes in {fmt_hours((closes - now) / 3600)}" if closes and closes > now else "Closing / settling"
        why = g.get("reasons") or []
        edge = g.get("edge")
        if g.get("exit_px") is not None:
            if now - g.get("exit_t", now) > 6 * 3600:
                continue                                      # old sell alert; it'll show up in the record when it settles
            counts["sell"] += 1
            kind, badge = "sell", "SELL NOW"
            reason = g.get("exit_reason", "exit")
            head = f"Sell your {side} on {html.escape(strike)}"
            body = (f"<p class='big'>It hit the <b>{reason}</b> at <b>{c(g['exit_px'])}</b> (you bought around {c(entry)}).</p>"
                    "<ol><li>Open the market on Kalshi (button below) or go to <b>Portfolio</b>.</li>"
                    f"<li>Tap your <b>{side}</b> position on <b>{html.escape(strike)}</b> → <b>Sell</b>.</li>"
                    f"<li>Use a limit order at about <b>{c(g['exit_px'])}</b>, all {g.get('contracts', '')} contracts.</li></ol>"
                    "<p class='muted'>Didn't buy it? Nothing to do.</p>")
        elif now - g.get("opened", now) < 3600:
            counts["buy"] += 1
            kind, badge = "buy", "BUY NOW"
            head = f"Buy {side} on {html.escape(strike)}"
            body = (f"<p class='big'>Kalshi sells it for <b>{c(entry)}</b>. The bot thinks it's worth <b>{c(g.get('fair', entry))}</b>"
                    + (f", an edge of <b>{c(edge)}</b> per contract after fees." if edge is not None else ".") + "</p>"
                    "<ol><li>Tap <b>Open on Kalshi</b> below.</li>"
                    f"<li>Find <b>{html.escape(strike)}</b> and tap <b>{side.title()}</b>.</li>"
                    f"<li>Choose a <b>limit order</b> at <b>{c(entry)} or less</b>, quantity <b>{g.get('contracts', 1)}</b>"
                    + (f" (about ${g['stake']:.2f})" if g.get("stake") else "") + ".</li>"
                    "<li>That's it. You'll get a Telegram text when it's time to sell.</li></ol>"
                    f"<p class='muted'>Skip it if the price is already above {round(entry * 100) + 2}¢, since the edge is gone. {close_txt}.</p>")
        else:
            counts["hold"] += 1
            kind, badge = "hold", "HOLD"
            head = f"Holding {side} on {html.escape(strike)}"
            if bid is not None and tp > sl:
                pos = max(0, min(1, (bid - sl) / (tp - sl)))
                track = (f"<div class='track'><div class='rail'><i style='left:{pos * 100:.0f}%'></i>"
                         f"<span class='e' style='left:{max(0, min(1, (entry - sl) / (tp - sl))) * 100:.0f}%'></span></div>"
                         f"<div class='tl'><span>stop {c(sl)}</span><span>now {c(bid)}</span><span>target {c(tp)}</span></div></div>")
            else:
                track = ""
            body = (f"<p class='big'>Bought around <b>{c(entry)}</b>. Sell if it reaches <b>{c(tp)}</b> or drops to <b>{c(sl)}</b>. "
                    "You'll get a text either way.</p>" + track + f"<p class='muted'>{close_txt}. If you didn't buy it, ignore this.</p>")
        why_html = ("<p class='why'>Why: " + html.escape("; ".join(why)) + "</p>") if why else ""
        cards.append(f"""<article class="act {kind}"><div class="ah"><span class="rank">#{shown + 1}</span><span class="badge {kind}">{badge}</span>
          <b>{head}</b></div><p class="mk">{html.escape(label)}</p>{body}{why_html}
          <a class="go" href="{url}" target="_blank" rel="noopener">Open on Kalshi ↗</a></article>""")
        shown += 1
    # fill the rest of the top 4 with the closest calls, clearly marked "not yet"
    held = {g["ticker"] for g in state.get("open", [])}
    watch = sorted([r for r in results if r.get("edge") is not None and r["edge"] > 0 and r["ticker"] not in held],
                   key=lambda r: -quality(r))
    seen_events = {g.get("event") for g in state.get("open", [])}
    for r in watch:
        if shown >= TOP_N:
            break
        if r.get("event") in seen_events:
            continue
        seen_events.add(r.get("event"))
        strike = r["label"].split(" — ")[-1]
        need = min_edge(state, r["category"])
        if r["edge"] < need:
            why_not = f"Edge is {c(r['edge'])}; it needs {round(need * 100)}¢ to be worth the risk."
        elif r.get("votes") and not r.get("approved"):
            noes = [w for ok, w in r["votes"].values() if ok is False]
            why_not = "The council said no: " + "; ".join(noes[:2]) + "."
        else:
            why_not = "Close. It may turn into a BUY on the next scan."
        cards.append(f"""<article class="act watch"><div class="ah"><span class="rank">#{shown + 1}</span><span class="badge watch">WATCHING</span>
          <b>{r['side']} on {html.escape(strike)} at {c(r['ask'])}</b></div><p class="mk">{html.escape(r['label'])}</p>
          <p class="big">Not a buy yet. {html.escape(why_not)}</p>
          <p class="muted">Fair value {c(r['fair'])} · {r['category']} · {('closes in ' + fmt_hours(r['hours'])) if r.get('hours') else ''}</p>
          <a class="go" href="{html.escape(r['url'])}" target="_blank" rel="noopener">Look on Kalshi ↗</a></article>""")
        shown += 1
    summary = " · ".join(x for x in (f"🟢 {counts['buy']} to buy" if counts["buy"] else "",
                                       f"🔴 {counts['sell']} to sell" if counts["sell"] else "",
                                       f"🟡 {counts['hold']} holding" if counts["hold"] else "") if x)
    if not cards:
        cards.append("""<article class="act idle"><div class="ah"><span class="badge idle">ALL CLEAR</span><b>Nothing to do right now</b></div>
          <p class="big">Kalshi Scout checks every 15 minutes and texts you the moment there's a trade worth taking.</p>
          <p class="muted">""" + (f"{flagged_count} market(s) cleared the edge but the council said no. See “Close calls” below." if flagged_count else
                                   "No market has a big enough edge right now. That's normal; most scans find nothing.") + "</p></article>")
    sports = "" if state.get("sports_on") else (
        '<p class="note">🏈 Sports is off. Add a free key from the-odds-api.com as the <b>ODDS_API_KEY</b> secret to turn it on.</p>')
    return f"""<section class="actions"><div class="ach"><h2>Top {TOP_N} right now</h2><span>{summary or 'nothing to act on'}</span></div>{''.join(cards)}{sports}</section>"""


def build_html(results, log, refresh_min, state=None):
    state = state or {}
    open_signals = [g for g in state.get("open", []) if g.get("exit_px") is None]
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
              <small>{html.escape(r['ticker'])} · {html.escape(r['note'])}{council_note(r)}</small></td>
          <td data-l="Buy">{side}</td><td class="n" data-l="Ask">{pct(r['ask'])}</td><td class="n" data-l="Fair">{pct(r['fair'])}</td>
          <td class="n" data-l="Fee">{pct(r['fee'])}</td><td class="n edge" data-l="Edge">{pct(e) if show_edge else '—'}</td>
          <td class="n" data-l="Qty">{r['contracts'] if r['contracts'] else '—'}</td>
          <td class="n" data-l="Stake">{('$%.2f' % r['stake']) if r['stake'] else '—'}</td>
          <td class="n" data-l="Take profit">{pct(r.get('tp'))}</td><td class="n" data-l="Stop loss">{pct(r.get('sl'))}</td>
          <td class="n" data-l="Closes">{fmt_hours(r['hours'])}</td><td class="n" data-l="Vol">{r['volume']:,}</td></tr>"""

    def council_note(r):
        v = r.get("votes")
        if not v:
            return ""
        yes, no = tally(v)
        verdict = "approved" if r.get("approved", approved(v)) else "rejected"
        who = ", ".join(f"{n} {'✓' if ok else '✗' if ok is False else '–'}" for n, (ok, _) in v.items())
        return f" · Council {yes}–{no} {verdict} ({html.escape(who)})"

    board = state.get("scoreboard", {})
    weights = voter_weights(state)
    rec = state.get("record", {"wins": 0, "losses": 0, "pnl": 0.0})
    n_set = rec["wins"] + rec["losses"]
    def board_row(name):
        b = board.get(name, {"right": 0, "total": 0})
        acc = b["right"] / b["total"] if b["total"] else None
        bar = f'<span class="bar"><i style="width:{(acc or 0) * 100:.0f}%"></i></span>'
        return f"""<tr><td class="mkt">{name}</td><td class="n" data-l="Called it">{f"{acc:.0%}" if acc is not None else "—"}</td>
          <td class="n" data-l="Vote weight">{weights[name]:g}×</td>
          <td data-l="Accuracy">{bar}</td><td class="n" data-l="Right / settled">{b['right']} / {b['total']}</td></tr>"""
    board_rows = "".join(board_row(nm) for nm in sorted(VOTERS, key=lambda n: -(board.get(n, {}).get("right", 0) / max(1, board.get(n, {}).get("total", 0)))))
    board_head = "<tr><th>Voter</th><th>Called it</th><th>Vote weight</th><th>Accuracy</th><th>Right / settled</th></tr>"
    record_line = (f"Signals settled: {n_set} · {rec['wins']}W–{rec['losses']}L · "
                   f"{'+' if rec['pnl'] >= 0 else '−'}{abs(round(rec['pnl'] * 100))}¢ per contract total"
                   if n_set else "No signals have settled yet. Accuracy fills in as markets close; give it a few weeks before trusting it.")

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
.actions {{ display:grid; gap:12px }}
.ach {{ display:flex; justify-content:space-between; align-items:baseline; gap:10px; flex-wrap:wrap }}
.ach h2 {{ margin:0; font-size:15px; color:var(--fg); letter-spacing:.04em }} .ach span {{ color:var(--muted); font-size:13px }}
.act {{ background:var(--panel); border:1px solid var(--line); border-left-width:5px; border-radius:12px; padding:14px 16px; display:grid; gap:8px }}
.act.buy {{ border-left-color:var(--yes) }} .act.sell {{ border-left-color:var(--no) }} .act.hold {{ border-left-color:#c9a227 }} .act.idle {{ border-left-color:var(--line) }}
.ah {{ display:flex; gap:10px; align-items:center; flex-wrap:wrap }} .ah b {{ font-size:17px }}
.badge {{ font:800 11px/1 ui-monospace,Menlo,monospace; letter-spacing:.08em; padding:5px 8px; border-radius:5px; color:#fff }}
.rank {{ font:800 13px/1 ui-monospace,Menlo,monospace; color:var(--muted) }}
.badge.watch {{ background:#5b6170 }} .act.watch {{ border-left-color:#5b6170; opacity:.92 }}
.actions .note {{ margin:0; color:var(--muted); font-size:13px }}
.badge.buy {{ background:var(--yes) }} .badge.sell {{ background:var(--no) }} .badge.hold {{ background:#a8861c }} .badge.idle {{ background:var(--muted) }}
.act .mk {{ margin:0; color:var(--muted); font-size:12.5px }}
.act .big {{ margin:0; font-size:15px; line-height:1.5 }}
.act ol {{ margin:0; padding-left:20px; display:grid; gap:4px; font-size:14px }}
.act .why {{ margin:0; font-size:12.5px; color:var(--muted); border-top:1px dashed var(--line); padding-top:8px }}
.act .muted {{ margin:0; color:var(--muted); font-size:13px }}
.go {{ justify-self:start; font-weight:700; color:var(--accent); text-decoration:none; padding:8px 12px; border:1px solid var(--accent); border-radius:8px }}
.go:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px }}
.track {{ display:grid; gap:4px }} .rail {{ position:relative; height:8px; border-radius:99px;
  background:linear-gradient(90deg, var(--no), #8a8a8a 45%, var(--yes)) }}
.rail i {{ position:absolute; top:-5px; width:4px; height:18px; margin-left:-2px; background:var(--fg); border-radius:2px }}
.rail .e {{ position:absolute; top:-3px; width:2px; height:14px; background:var(--panel); opacity:.9 }}
.tl {{ display:flex; justify-content:space-between; font-size:12px; color:var(--muted); font-variant-numeric:tabular-nums }}
.stat small {{ display:block; color:var(--muted); font-size:11px }}
details.more {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:4px 12px; color:var(--fg) }}
details.more > summary {{ cursor:pointer; padding:10px 0; font-weight:650; list-style-position:inside }}
details.more[open] > summary {{ border-bottom:1px solid var(--line); margin-bottom:10px }}
details.more section {{ margin-bottom:10px }}
.howto dl {{ margin:0; display:grid; gap:4px }} .howto dt {{ font-weight:700 }} .howto dd {{ margin:0 0 6px; color:var(--muted) }}
.rec {{ margin:0 0 8px; color:var(--muted) }}
table.board {{ min-width:0 }}
.bar {{ display:inline-block; width:120px; max-width:100%; height:8px; border-radius:99px; background:var(--line); overflow:hidden; vertical-align:middle }}
.bar i {{ display:block; height:100%; background:var(--accent) }}
.room {{ --ink:#1b1430; --c-quant:#7ccf8a; --c-risk:#e8565b; --c-mom:#f2a23c; --c-time:#5aa7e0; --c-doubt:#d9c3a5;
  background:radial-gradient(120% 90% at 50% 0%, #3a2a6b 0%, #1d1538 60%, #140f28 100%); color:#efe9ff;
  border-radius:14px; padding:16px; overflow:hidden; display:grid; gap:12px }}
.roomhead {{ display:grid; grid-template-columns:auto 1fr; gap:16px; align-items:end }}
.overall {{ display:grid; gap:2px; padding-right:16px; border-right:1px solid #4a3a85 }}
.overall b {{ font:800 40px/1 ui-sans-serif,-apple-system,sans-serif; color:#f4c84a; font-variant-numeric:tabular-nums; letter-spacing:-.02em }}
.overall small {{ color:#c9bdf0; font-size:11px }}
.overall .lbl {{ font-size:11px; letter-spacing:.1em; text-transform:uppercase; color:#b6a8e6 }}
.stage {{ display:none; position:relative; border-radius:10px; overflow:hidden; touch-action:pan-y }}
.stage canvas {{ display:block; width:100% !important; cursor:grab }}
.is3d .stage {{ display:block }}
.is3d .table, .is3d .char, .is3d .seat .sign {{ display:none }}
.is3d .seats {{ margin-top:-4px }}
.is3d .seat:hover .why, .is3d .seat:focus .why {{ display:none }}
.stamp3d {{ position:absolute; left:50%; bottom:14px; transform:translateX(-50%); pointer-events:none; opacity:0; transition:opacity .4s }}
.stamp3d.show {{ opacity:1 }}
.stamp3d .stamp {{ animation:none; transform:none; font:600 12px/1 ui-monospace,SFMono-Regular,Menlo,monospace; letter-spacing:.22em;
  padding:8px 14px; border-width:1px; border-radius:2px; background:#0e0a1ecc; box-shadow:0 0 18px currentColor }}
.tip3d {{ position:absolute; left:10px; top:10px; max-width:min(260px, 70%); display:grid; gap:3px; background:#2b2150ee;
  border:1px solid #6a58b8; border-radius:10px; padding:8px 10px; font-size:12px; color:#efe9ff }}
.tip3d[hidden] {{ display:none }}
.tip3d b {{ font-size:13px }} .tip3d small {{ color:#b6a8e6 }}
.docket {{ display:grid; gap:2px; min-width:0 }}
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
.seats {{ display:grid; grid-template-columns:repeat(5,1fr); gap:6px }}
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
{action_center(state, len(flagged), results)}
<div class="stats">
  <div class="stat"><span>Record</span><b>{rec['wins']}W–{rec['losses']}L</b></div>
  <div class="stat"><span>Profit</span><b>{cents(rec['pnl'])}</b><small>per contract, all signals</small></div>
  <div class="stat"><span>Council right</span><b>{(f"{state.get('council_record', {}).get('right', 0) / state['council_record']['total']:.0%}") if state.get('council_record', {}).get('total') else '—'}</b></div>
  <div class="stat"><span>Markets checked</span><b>{len(priced)}</b><small>last scan</small></div>
</div>
<details class="more"><summary>🏛 Watch the council debate</summary>{council_room(state)}</details>
<details class="more"><summary>🧠 Who called it? (learning)</summary><section><p class="rec">{record_line}<br>Learned minimum edge: {" · ".join(f"{cat} {round(min_edge(state, cat) * 100)}¢" for cat in ("Crypto", "Sports"))}. Vote weights start changing after {LEARN_MIN_CALLS} settled calls.</p><div class="tbl"><table class="board">{board_head}{board_rows}</table></div></section></details>
<details class="more"><summary>📊 Everything the bot checked</summary><section><h2>Open signals</h2><div class="tbl"><table>{sig_head}{sig_rows}</table></div></section><section><h2>Possible edges</h2><div class="tbl"><table>{head}{flagged_rows}</table></div></section>
<section><h2>Close calls</h2><div class="tbl"><table>{head}{"".join(row(r) for r in near) or '<tr><td colspan="12" class="empty">Nothing priced yet.</td></tr>'}</table></div></section></details>
<details class="more"><summary>❓ How to read this</summary><section class="howto"><dl>
  <dt>Price / ask</dt><dd>What Kalshi charges for one contract. A contract pays $1 if you're right and $0 if not.</dd>
  <dt>Fair value</dt><dd>What the bot thinks the contract is really worth, from live crypto prices or sportsbook odds.</dd>
  <dt>Edge</dt><dd>Fair value minus price minus Kalshi's fee. Bigger is better; the bot only alerts at {MIN_EDGE * 100:.0f}¢+.</dd>
  <dt>Take profit / stop loss</dt><dd>Sell early if the price rises to the target (lock in profit) or falls to the stop (limit the loss). Otherwise hold until it settles.</dd>
  <dt>Contracts</dt><dd>How many to buy so a bad trade costs only a small slice of your ${BANKROLL:,.0f} bankroll.</dd>
</dl></section></details>
<details class="more"><summary>🔧 Scan log</summary><ul>{"".join(f"<li>{html.escape(l)}</li>" for l in log)}</ul></details>
<p class="fine">Fair values are estimates. Crypto uses spot price and recent realized volatility in a simple lognormal model;
sports uses the no-vig average of US sportsbook lines. Edge = fair probability − ask − estimated taker fee, per $1 contract.
The council: five rule-based voters check each candidate, and a BUY alert needs {COUNCIL_MIN_YES}+ YES votes. Rejected candidates are tracked to settlement so "Who called it?" can score every voter.
Take profit = sell once the price reaches fair value. Stop loss = sell if the price drops 1.5× the edge below entry ({STOP_LOSS_MIN*100:.0f}–{STOP_LOSS_MAX*100:.0f}¢).
Kalshi has no automatic stop orders here, so sell manually when alerted. Stake is {KELLY_FRACTION:g}× Kelly, capped at {MAX_BET_PCT:.0%} of bankroll. Check each market's rules on Kalshi before trading.
This tool does not place orders and is not financial advice.</p>
<script>document.querySelectorAll('details.more').forEach(function(d){{d.addEventListener('toggle',function(){{window.dispatchEvent(new Event('resize'));}});}});</script>
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
            st = load_state()
            results, log = scan(st)
            state = manage_signals(results, log, st)
        except Exception as e:
            print(f"Scan failed: {e}", file=sys.stderr)
            results, log, state = [], [f"Scan failed: {e}"], {}
            try:
                st = load_state(); record_errors(log, st); save_state(st); state = st
            except Exception:
                pass
        for line in log:
            print("  " + line)
        path = os.path.abspath(args.out)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(build_html(results, log, args.loop, state))
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
