"""
Gap Fade (IWM) — paper bot, built from the Inspector's Entry Lab.

The rule (tested on 24 months of real IWM option prices):
  • If IWM opens 0.8% or more away from yesterday's close, bet on it moving back:
    a PUT after a gap up, a CALL after a gap down. Same-day (0DTE) contract, bought at 10:00 AM ET.
  • Exits (Easter's rules): stop −40%. At +30% the stop moves up to +30%.
    2+ contracts: at +50% sell half, the rest targets +60%. 1 contract: then trails 25% below its best price.
    Anything left is sold by 3:30 PM.
Runs inside the Vault Options workflow, after the main bot. Paper account only unless GAP_LIVE=1.
Run:  python gap_fade.py --out site/gap.html
"""
import argparse
import datetime as dt
import html
import json
import os
import sys

import vault_options as vo

STATE = "gap_state.json"
SYM = "IWM"
MIN_GAP = 0.008                  # 0.8%
ENTER_FROM, ENTER_UNTIL = (10, 0), (10, 30)
SELL_BY = (15, 30)
STOP, LOCK_AT, HALF_AT, TARGET, TRAIL = 0.40, 0.30, 0.50, 0.60, 0.25
START = vo.START_BALANCE
BUDGET = START * vo.RISK_PCT / STOP          # $62.50 per trade on the $250 bankroll


def load():
    try:
        st = json.load(open(STATE, encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    for k, v in (("trade", None), ("closed", []), ("days", {}), ("record", {"wins": 0, "losses": 0, "pl": 0.0})):
        st.setdefault(k, v)
    return st


def save(st):
    """Also writes the fields Vault HQ reads (last run, bankroll, open trade)."""
    t = st["trade"]
    upl = round((t.get("price", t["entry"]) - t["entry"]) * 100 * t["qty"] + t.get("banked", 0), 2) if t else 0.0
    nav = round(START + st["record"]["pl"] + upl, 2)
    today = vo.et(dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%d")
    if (st.get("day") or {}).get("date") != today:
        st["day"] = {"date": today, "start_nav": nav}
    st["account"] = {"nav": nav}
    st["trades"] = {"open": {"pair": SYM, "dir": t["dir"], "upl": upl}} if t else {}
    st["last_run"] = dt.datetime.now(dt.timezone.utc).timestamp()
    st["closed"] = st["closed"][-200:]
    st["days"] = dict(sorted(st["days"].items())[-60:])
    json.dump(st, open(STATE, "w", encoding="utf-8"), indent=1, sort_keys=True)


def say(text):
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    try:
        chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip() or json.load(open(vo.STATE_FILE)).get("chat_id")
    except Exception:
        chat = None
    if tok and chat:
        try:
            vo.http("POST", f"https://api.telegram.org/bot{tok}/sendMessage",
                    body={"chat_id": chat, "text": text, "disable_web_page_preview": True}, tries=2)
        except Exception as e:
            print(f"Telegram failed: {e}")


def vault_holds():
    try:
        return {m["sym"] for m in json.load(open(vo.STATE_FILE)).get("trades", {}).values()}
    except Exception:
        return set()


def todays_gap(api, today):
    """(gap, yesterday's close, today's open) from 15-minute bars."""
    bars = api.bars([SYM], "15Min", 6).get(SYM, [])
    prev = [b for b in bars if vo.et(b["t"]).date() < today]
    cur = [b for b in bars if vo.et(b["t"]).date() == today]
    if not prev or not cur:
        return None, None, None
    pc, op = prev[-1]["c"], cur[0]["o"]
    return op / pc - 1, pc, op


def sell(api, t, n, why, bid, now, log):
    """Sell n contracts of the open trade. Returns the fill price."""
    try:
        res = api.close(t["sym"], n)
        o = vo.wait_fill(api, res["id"]) if res.get("id") else {}
        return float(o.get("filled_avg_price") or 0) or bid
    except vo.ApiError as e:
        if "not found" in str(e).lower():
            return bid
        log.append(f"couldn't sell ({e})")
        say(f"⚠️ Gap Fade couldn't sell {vo.contract_name(t['sym'])}: {e}\nPlease close it in the app.")
        return None


def book(st, t, px, why, now):
    pl = round((px - t["entry"]) * 100 * t["qty"] + t.get("banked", 0), 2)
    t.update(exit=px, why=why, pl=pl, closed=now.isoformat()[:19])
    rec = st["record"]
    rec["wins" if pl > 0 else "losses"] += 1
    rec["pl"] = round(rec["pl"] + pl, 2)
    st["closed"].append(t)
    st["trade"] = None
    early = f" (+ {t['sold_half']} sold earlier at +{HALF_AT:.0%})" if t.get("sold_half") else ""
    say(f"{'✅' if pl > 0 else '❌'} GAP FADE SOLD {vo.contract_name(t['sym'])} ({why})\n"
        f"Bought ${t['entry']:.2f} → sold ${px:.2f}{early}\nResult: {vo.money(pl)} · Record {rec['wins']}W–{rec['losses']}L, "
        f"total {vo.money(rec['pl'])}\n\n👉 Robinhood: if you copied it, sell your {vo.contract_name(t['sym'])} now.")


def manage(api, st, now, log):
    t = st["trade"]
    if t["sym"] not in api.positions():
        book(st, t, t.get("price") or t["entry"], "closed outside the bot", now)
        return
    q = api.quotes([t["sym"]]).get(t["sym"], {})
    bid = q.get("bid") or t.get("price") or t["entry"]
    t["price"] = bid
    e = vo.et(now)
    entry = t["entry"]
    t["peak"] = max(t.get("peak", entry), bid)
    if t.get("trail"):
        t["sl"] = max(t["sl"], round(t["peak"] * (1 - TRAIL), 2))
    why = None
    if (e.hour, e.minute) >= SELL_BY:
        why = "end of day"
    elif bid <= t["sl"]:
        why = "stop" if t["sl"] < entry else "locked profit"
    elif t.get("tp") and bid >= t["tp"]:
        why = "target"
    if why:
        px = sell(api, t, t["qty"], why, bid, now, log)
        if px is not None:
            book(st, t, px, why, now)
        return
    if not t.get("locked") and bid >= entry * (1 + LOCK_AT):
        t["locked"] = True
        t["sl"] = max(t["sl"], round(entry * (1 + LOCK_AT), 2))
        if t["qty0"] == 1:
            t["trail"] = True
        say(f"🔐 Gap Fade: {vo.contract_name(t['sym'])} is up {LOCK_AT:.0%}. Stop raised to ${t['sl']:.2f} (locks +{LOCK_AT:.0%})"
            + (" and now trails 25% below its best price." if t["qty0"] == 1 else ". Half comes off at +50%."))
    if t["qty0"] >= 2 and not t.get("half") and bid >= entry * (1 + HALF_AT):
        n = t["qty"] // 2
        px = sell(api, t, n, "half at +50%", bid, now, log)
        if px is not None:
            t["half"] = True
            t["banked"] = round(t.get("banked", 0) + (px - entry) * 100 * n, 2)
            t["qty"] -= n
            t["sold_half"] = n
            t["tp"] = round(entry * (1 + TARGET), 2)
            say(f"💰 Gap Fade: sold {n} of {vo.contract_name(t['sym'])} at ${px:.2f} (+{HALF_AT:.0%}). "
                f"The rest aims for ${t['tp']:.2f} (+{TARGET:.0%}); stop stays at +{LOCK_AT:.0%}.")


def enter(api, st, now, log):
    e = vo.et(now)
    today = e.date()
    day = st["days"].setdefault(today.isoformat(), {})
    if day.get("done"):
        return
    gap, pc, op = todays_gap(api, today)
    if gap is None:
        log.append("no price data yet")
        return
    day.update(gap=round(gap * 100, 2), prev_close=pc, open=op)
    if abs(gap) < MIN_GAP:
        day.update(done=True, action=f"gap {gap:+.2%}: under {MIN_GAP:.1%}, no trade")
        log.append(day["action"])
        return
    d = -1 if gap > 0 else 1
    bars = api.bars([SYM], "15Min", 2).get(SYM, [])
    price = bars[-1]["c"] if bars else op
    pick, why = vo.pick_contract(api, SYM, d, price, BUDGET, now)
    if not pick:
        day.update(done=True, action=f"gap {gap:+.2%}: no contract ({why})")
        say(f"↔️ Gap Fade: IWM gapped {gap:+.2%} but no contract fit ({why}). No trade today.")
        return
    if pick["sym"] in vault_holds():
        day.update(done=True, action=f"gap {gap:+.2%}: Vault Options already holds {pick['sym']}, skipped")
        say(f"↔️ Gap Fade: IWM gapped {gap:+.2%}, but Vault Options already holds that exact contract. Skipped to keep them apart.")
        return
    limit = vo.tick_up(pick["ask"])
    try:
        res = api.buy(pick["sym"], pick["qty"], limit)
        o = vo.wait_fill(api, res["id"])
    except vo.ApiError as ex:
        day.update(done=True, action=f"order rejected: {ex}")
        say(f"⚠️ Gap Fade: Alpaca rejected {vo.contract_name(pick['sym'])}: {ex}")
        return
    filled = int(float(o.get("filled_qty") or 0))
    if o.get("status") != "filled":
        try:
            api.cancel(res["id"])
        except vo.ApiError:
            pass
    if not filled:
        log.append(f"not filled at {limit:.2f}; will retry next run")
        return                                         # try again on the next run inside the window
    fill = float(o.get("filled_avg_price") or limit)
    st["trade"] = {"sym": pick["sym"], "dir": d, "qty": filled, "qty0": filled, "entry": fill, "sl": round(fill * (1 - STOP), 2),
                   "tp": None, "opened": now.isoformat()[:19], "gap": round(gap * 100, 2), "price": fill}
    day.update(done=True, action=f"gap {gap:+.2%}: bought {filled} × {pick['sym']} at {fill:.2f}")
    kind = "PUT (betting the gap up fades)" if d == -1 else "CALL (betting the gap down fades)"
    exp = dt.date.fromisoformat(pick["exp"])
    say(f"{'🔴' if d == -1 else '🟢'} GAP FADE: IWM opened {gap:+.2%} from yesterday's close (${pc:.2f} → ${op:.2f})\n\n"
        f"Bought {filled} × {vo.contract_name(pick['sym'])} {kind} at ${fill:.2f}\n"
        f"🛑 Stop ${fill * (1 - STOP):.2f} (−{STOP:.0%})\n🔐 At ${fill * (1 + LOCK_AT):.2f} (+{LOCK_AT:.0%}) the stop locks +{LOCK_AT:.0%}"
        + (f"\n💰 At ${fill * (1 + HALF_AT):.2f} (+{HALF_AT:.0%}) sell half, the rest aims for ${fill * (1 + TARGET):.2f}" if filled >= 2 else
           ", then trails 25% below its best price")
        + f"\n🔔 Sold by 3:30 PM (expires today)\n\n👉 Robinhood: IWM → Trade Options → Buy → {'Put' if d == -1 else 'Call'} → "
        f"{exp:%b} {exp.day} → strike ${pick['strike']:g} → limit ${limit:.2f} or less.")


def page(st, out):
    t = st["trade"]
    rec = st["record"]
    rows = "".join(f"<tr><td>{html.escape(c['opened'][:10])}</td><td>{html.escape(vo.contract_name(c['sym']))}</td><td>{c['gap']:+.2f}%</td>"
                   f"<td>${c['entry']:.2f} → ${c['exit']:.2f}</td><td>{html.escape(c['why'])}</td>"
                   f"<td class='{'pos' if c['pl'] > 0 else 'neg'}'>{vo.money(c['pl'])}</td></tr>" for c in reversed(st["closed"][-40:]))
    days = "".join(f"<li>{d}: {html.escape(v.get('action', 'waiting'))}</li>" for d, v in sorted(st["days"].items(), reverse=True)[:10])
    open_html = (f"<p><b>Open:</b> {html.escape(vo.contract_name(t['sym']))} × {t['qty']} · bought ${t['entry']:.2f} · now ${t.get('price', t['entry']):.2f} · "
                 f"stop ${t['sl']:.2f}</p>" if t else "<p>No open trade.</p>")
    n = rec["wins"] + rec["losses"]
    open(out, "w", encoding="utf-8").write(f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gap Fade</title><style>body{{background:#0f1220;color:#e8ecf8;font:14px/1.45 system-ui;padding:16px;max-width:760px;margin:auto}}
table{{border-collapse:collapse;width:100%}}td,th{{padding:6px;border-bottom:1px solid #2a3150;text-align:left}}.pos{{color:#53f0a6}}.neg{{color:#ff6b7c}}
.big{{font-size:28px;font-weight:700}}a{{color:#8fb4ff}}</style></head><body>
<p><a href="index.html">← Vault Options</a> · <a href="hq.html">Vault HQ</a></p>
<h1>Gap Fade · IWM</h1><p class="big {'pos' if rec['pl'] >= 0 else 'neg'}">{vo.money(rec['pl'])}</p>
<p>{rec['wins']}W–{rec['losses']}L{f" · {rec['wins'] / n:.0%} won" if n else ""} · paper bankroll ${START + rec['pl']:,.2f} of ${START:,.0f}</p>
{open_html}<h2>Last 10 days</h2><ul>{days or '<li>Nothing yet</li>'}</ul>
<h2>Trades</h2><table><tr><th>Date</th><th>Contract</th><th>Gap</th><th>Price</th><th>Exit</th><th>Result</th></tr>{rows}</table>
<p style="opacity:.7">Rule: IWM opens {MIN_GAP:.1%}+ from yesterday's close → at 10:00 AM buy a same-day put (gap up) or call (gap down).
Stop −{STOP:.0%}; at +{LOCK_AT:.0%} the stop locks +{LOCK_AT:.0%}; 2+ contracts sell half at +{HALF_AT:.0%}, rest to +{TARGET:.0%}; 1 contract trails 25%. Out by 3:30 PM.
Inspector test (24 months, real option prices): 108 trades, 48% won, +$886 at this size. Paper trading only. Not financial advice.</p>
</body></html>""")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site/gap.html")
    a = ap.parse_args()
    if vo.ALPACA_ENV == "live" and os.environ.get("GAP_LIVE") != "1":
        print("Gap Fade only runs on the paper account (set GAP_LIVE=1 to allow live).")
        return
    key, secret = os.environ.get("ALPACA_KEY_ID", "").strip(), os.environ.get("ALPACA_SECRET_KEY", "").strip()
    if not key or not secret:
        sys.exit("Add ALPACA_KEY_ID and ALPACA_SECRET_KEY.")
    api = vo.Alpaca(key, secret)
    st = load()
    now = dt.datetime.now(dt.timezone.utc)
    log = []
    try:
        clock = api.clock()
        is_open = bool(clock.get("is_open"))
    except vo.ApiError:
        is_open = vo.market_open_now(now)
    e = vo.et(now)
    try:
        if is_open and st["trade"]:
            manage(api, st, now, log)
        elif is_open and not st["trade"] and ENTER_FROM <= (e.hour, e.minute) < ENTER_UNTIL:
            enter(api, st, now, log)
        elif not is_open and st["trade"] and vo.et(dt.datetime.fromisoformat(st["trade"]["opened"]).replace(tzinfo=dt.timezone.utc)).date() < e.date():
            t = st["trade"]                                   # a 0DTE left over from a day the bot missed: it expired
            pos = api.positions()
            if t["sym"] not in pos:
                book(st, t, 0.0, "expired", now)
    except vo.ApiError as ex:
        log.append(f"Alpaca error: {ex}")
    save(st)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    page(st, a.out)
    print("Gap Fade:", "; ".join(log) or "ok")


if __name__ == "__main__":
    main()
