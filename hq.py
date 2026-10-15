"""
Vault HQ: one page for every bot, plus a 9 AM morning brief on Telegram.

Runs inside the Vault Options workflow (every 5 minutes):
  python hq.py --out site/hq.html

Reads each bot's saved state (Vault Options locally; Vault FX and Bitcoin Scout from their public repos),
flags anything frozen or erroring, and once a day after 9:00 AM ET sends one Telegram summary.
"""

import argparse
import datetime as dt
import html
import json
import os
import time
import urllib.request
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
OWNER = (os.environ.get("GITHUB_REPOSITORY", "easter2apply/kalshi-scout").split("/") + [""])[0] or "easter2apply"
BRIEF_HOUR = 9               # morning brief after 9:00 AM ET, once a day
STALE_MIN = 20               # a bot that hasn't run in 20 minutes (while it should be running) is flagged
HQ_STATE = "hq_state.json"

BOTS = [
    {"id": "options", "name": "Vault Options", "icon": "📈", "local": "options_state.json", "start": 250.0,
     "url": f"https://{OWNER}.github.io/kalshi-scout/", "hours": "market"},
    {"id": "gap", "name": "Gap Fade (IWM)", "icon": "🔄", "local": "gap_state.json", "start": 250.0,
     "url": f"https://{OWNER}.github.io/kalshi-scout/gap.html", "hours": "market"},
    {"id": "fx", "name": "Vault FX", "icon": "💱", "raw": f"https://raw.githubusercontent.com/{OWNER}/vault-fx/main/fx_state.json",
     "start": 500.0, "url": f"https://{OWNER}.github.io/vault-fx/", "hours": "fx"},
    {"id": "btc", "name": "Bitcoin Scout", "icon": "₿", "raw": f"https://raw.githubusercontent.com/{OWNER}/btc-scout/main/btc_state.json",
     "start": 200.0, "url": f"https://{OWNER}.github.io/btc-scout/", "hours": "always"},
]


def get_state(bot):
    try:
        if bot.get("local"):
            with open(bot["local"], encoding="utf-8") as f:
                return json.load(f), None
        req = urllib.request.Request(bot["raw"] + f"?t={int(time.time())}", headers={"User-Agent": "vault-hq", "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode()), None
    except Exception as e:
        return None, str(e)[:120]


def should_run(hours, now):
    """Is this bot supposed to be running right now? (so a quiet weekend isn't flagged as frozen)"""
    e = now.astimezone(ET)
    if hours == "always":
        return True
    if hours == "fx":                    # forex: Sunday 5 PM – Friday 5 PM ET
        return not (e.weekday() == 5 or (e.weekday() == 4 and e.hour >= 17) or (e.weekday() == 6 and e.hour < 17))
    return e.weekday() < 5 and (9, 35) <= (e.hour, e.minute) < (16, 0)


def money(x, sign=True):
    s = f"${abs(x):,.2f}"
    return (("+" if x >= 0 else "−") + s) if sign else s


def summarize(bot, st, err, now):
    """Everything HQ shows about one bot."""
    out = {"id": bot["id"], "name": bot["name"], "icon": bot["icon"], "url": bot["url"], "issues": []}
    if st is None:
        out.update(status="down", headline="Couldn't read this bot's data", issues=[err or "no data"])
        return out
    last = st.get("last_run") or 0
    mins = (now.timestamp() - last) / 60 if last else None
    out["last_min"] = mins
    if should_run(bot["hours"], now) and (mins is None or mins > STALE_MIN):
        out["issues"].append(f"hasn't run in {mins:.0f} min" if mins else "hasn't run yet")
    recent = [e for e in st.get("errors", []) if now.timestamp() - e.get("t", 0) < 30 * 60]
    bad = [e["text"] for e in recent if any(k in e["text"] for k in ("401", "403", "Run failed", "couldn't open", "unauthorized"))]
    if bad:
        out["issues"].append(bad[-1][:140])
    if st.get("paused"):
        out["issues"].append(f"paused ({st['paused']})")
    rec = st.get("record", {})
    w, l = rec.get("wins", 0), rec.get("losses", 0)
    out["record"] = f"{w}W–{l}L" + (f" ({w / (w + l):.0%})" if w + l else "")
    if bot["id"] == "btc":
        pl = rec.get("dollars", 0.0)
        out["bank"] = bot["start"] + pl
        out["total"] = pl
        day = st.get("days", {}).get(now.astimezone(ET).strftime("%Y-%m-%d"), {})
        out["today_text"] = f"{day.get('buys', 0)} new bets today"
        live = [s for s in st.get("open", []) if not s.get("shadow") and s.get("exit_px") is None]
        out["open"] = [s.get("bet") or s.get("label", "") for s in live]
        out["kind"] = "paper bets you place yourself"
    else:
        acct = st.get("account", {})
        nav = acct.get("nav")
        out["bank"] = nav
        out["total"] = (nav - bot["start"]) if nav is not None else None
        start_nav = (st.get("day") or {}).get("start_nav")
        out["today"] = (nav - start_nav) if nav is not None and start_nav else None
        out["open"] = [f"{m.get('pair', '').replace('_', '/')} {('CALL' if m.get('dir') == 1 else 'PUT') if bot['id'] in ('options', 'gap') else ('BUY' if m.get('dir') == 1 else 'SELL')}"
                       + (f" {money(m.get('upl', 0))}" if m.get("upl") is not None else "") for m in st.get("trades", {}).values()]
        out["kind"] = "auto-trades a practice account"
        if bot["id"] == "fx" and st.get("doubt_job"):
            out["note"] = "Doubt: " + st["doubt_job"].get("text", "")
    out["status"] = "warn" if out["issues"] else "ok"
    out["headline"] = "Needs attention" if out["issues"] else ("Running" if should_run(bot["hours"], now) else "Market closed, waiting")
    return out


CSS = """
:root{--bg:#0d0f1a;--card:#161a2b;--fg:#eef0fa;--muted:#9aa2c2;--line:#262c45;--ok:#45e39b;--warn:#ffb547;--down:#ff6378;--acc:#38e1ff}
@media (prefers-color-scheme: light){:root:not([data-theme="dark"]){--bg:#f3f4f9;--card:#fff;--fg:#151a2c;--muted:#5b6380;--line:#dde1ee;--ok:#13804f;--warn:#a86a00;--down:#c42b40;--acc:#0b7fa0}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif;padding:18px 16px 40px}
.wrap{max-width:760px;margin:0 auto;display:grid;gap:14px}
header h1{margin:0;font-size:26px;letter-spacing:-.01em}header p{margin:2px 0 0;color:var(--muted);font-size:13px}
.sum{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.k{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 12px}
.k span{display:block;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}.k b{font-size:20px;font-variant-numeric:tabular-nums}
.bot{background:var(--card);border:1px solid var(--line);border-left:5px solid var(--ok);border-radius:14px;padding:14px 16px;display:grid;gap:8px;text-decoration:none;color:inherit}
.bot.warn{border-left-color:var(--warn)}.bot.down{border-left-color:var(--down)}
.top{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
.top h2{margin:0;font-size:18px}.top small{color:var(--muted)}
.st{font-size:12px;font-weight:700;padding:3px 9px;border-radius:99px;border:1px solid currentColor;white-space:nowrap}
.st.ok{color:var(--ok)}.st.warn{color:var(--warn)}.st.down{color:var(--down)}
.nums{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.nums div span{display:block;font-size:11px;color:var(--muted)}.nums div b{font-variant-numeric:tabular-nums}
.pos{color:var(--ok)}.neg{color:var(--down)}
ul{margin:0;padding-left:18px}li{margin:2px 0}.iss li{color:var(--warn)}
.fine{color:var(--muted);font-size:12px}
a.go{color:var(--acc);font-weight:600;text-decoration:none}
@media (max-width:520px){.nums{grid-template-columns:repeat(2,1fr)}.sum{grid-template-columns:1fr 1fr}.sum .k:last-child{grid-column:span 2}}
"""


def cls(x):
    return "" if x is None else ("pos" if x >= 0 else "neg")


def render(rows, now):
    total = sum(r.get("total") or 0 for r in rows)
    problems = sum(1 for r in rows if r["status"] != "ok")
    opens = sum(len(r.get("open") or []) for r in rows)
    cards = ""
    for r in rows:
        word = {"ok": "OK", "warn": "CHECK", "down": "DOWN"}[r["status"]]
        nums = ""
        if r.get("bank") is not None:
            today = (f"<b class='{cls(r.get('today'))}'>{money(r['today'])}</b>" if r.get("today") is not None
                     else f"<b>{html.escape(r.get('today_text', '—'))}</b>")
            nums = (f"<div class='nums'><div><span>Bankroll</span><b>{money(r['bank'], False)}</b></div>"
                    f"<div><span>Since start</span><b class='{cls(r.get('total'))}'>{money(r['total']) if r.get('total') is not None else '—'}</b></div>"
                    f"<div><span>Today</span>{today}</div><div><span>Record</span><b>{html.escape(r.get('record', '—'))}</b></div></div>")
        opn = "".join(f"<li>{html.escape(x)}</li>" for x in (r.get("open") or []))
        iss = "".join(f"<li>{html.escape(x)}</li>" for x in r["issues"])
        seen = f"last ran {r['last_min']:.0f} min ago" if r.get("last_min") is not None else ""
        cards += f"""<a class="bot {r['status']}" href="{html.escape(r['url'])}">
  <div class="top"><h2>{r['icon']} {html.escape(r['name'])}</h2><span class="st {r['status']}">{word}</span></div>
  <small class="fine">{html.escape(r['headline'])} · {html.escape(r.get('kind', ''))}{' · ' + seen if seen else ''}</small>
  {nums}
  {f"<ul class='iss'>{iss}</ul>" if iss else ""}
  {f"<div><span class='fine'>Open now</span><ul>{opn}</ul></div>" if opn else ""}
  {f"<small class='fine'>{html.escape(r['note'])}</small>" if r.get('note') else ""}
  <span class="go">Open dashboard →</span></a>"""
    stamp = now.astimezone(ET).strftime("%a %b %-d, %-I:%M %p ET")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="300"><title>Vault HQ</title><style>{CSS}</style></head><body><div class="wrap">
<header><h1>🏛 Vault HQ</h1><p>Every bot at a glance · updated {stamp} · refreshes every 5 minutes</p></header>
<div class="sum"><div class="k"><span>All bots since start</span><b class="{cls(total)}">{money(total)}</b></div>
<div class="k"><span>Open positions</span><b>{opens}</b></div>
<div class="k"><span>Need attention</span><b class="{'neg' if problems else 'pos'}">{problems}</b></div></div>
{cards}
<p class="fine">Green = running normally. Orange = frozen, erroring or paused (the reason is listed). All bots are on practice/paper money.
The class bot isn't shown because its repo is private.</p>
</div></body></html>"""


def brief(rows, opt_state, now):
    e = now.astimezone(ET)
    lines = [f"☀️ Morning brief · {e:%a %b} {e.day}", ""]
    for r in rows:
        head = f"{r['icon']} {r['name']}: " + ("✅" if r["status"] == "ok" else "⚠️")
        if r.get("bank") is not None:
            head += f" {money(r['bank'], False)}"
            if r.get("total") is not None:
                head += f" ({money(r['total'])} since start)"
        lines.append(head)
        if r.get("record"):
            lines.append(f"   Record {r['record']}" + (f" · {len(r['open'])} open" if r.get("open") else ""))
        for x in r["issues"]:
            lines.append(f"   ⚠️ {x}")
    news = [x for x in (opt_state or {}).get("news", {}).get("events", [])
            if x.get("cur") == "USD" and now.timestamp() <= x["t"] <= now.timestamp() + 16 * 3600]
    if news:
        lines += ["", "📰 Big US news today:"] + [f"   • {x['title']} ({dt.datetime.fromtimestamp(x['t'], ET):%-I:%M %p})" for x in news[:5]]
    today = e.date().isoformat()
    earn = [s for s, ds in ((opt_state or {}).get("earnings") or {}).get("dates", {}).items() for d in ds if d["date"] == today]
    if earn:
        lines += ["", "📣 Earnings today on the watchlist: " + ", ".join(sorted(set(earn)))]
    repo = os.environ.get("GITHUB_REPOSITORY", "easter2apply/kalshi-scout")
    lines += ["", f"🏛 HQ: https://{OWNER}.github.io/{repo.split('/')[-1]}/hq.html"]
    return "\n".join(lines)


def send(text, chat):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or not chat:
        return False
    data = json.dumps({"chat_id": chat, "text": text, "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site/hq.html")
    ap.add_argument("--brief-now", action="store_true", help="send the morning brief right away (testing)")
    args = ap.parse_args()
    now = dt.datetime.now(dt.timezone.utc)
    rows, opt_state = [], None
    for bot in BOTS:
        st, err = get_state(bot)
        if bot["id"] == "options":
            opt_state = st
        rows.append(summarize(bot, st, err, now))
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(render(rows, now))
    print("HQ:", ", ".join(f"{r['name']} {r['status']}" for r in rows))
    try:
        hq = json.load(open(HQ_STATE))
    except (OSError, ValueError):
        hq = {}
    e = now.astimezone(ET)
    day = e.strftime("%Y-%m-%d")
    if args.brief_now or (e.hour >= BRIEF_HOUR and hq.get("brief_day") != day):
        chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip() or (opt_state or {}).get("chat_id")
        try:
            if send(brief(rows, opt_state, now), chat):
                hq["brief_day"] = day
                print("Morning brief sent")
            else:
                print("Morning brief: no Telegram token or chat yet")
        except Exception as ex:
            print(f"Morning brief failed: {ex}")
    with open(HQ_STATE, "w", encoding="utf-8") as f:
        json.dump(hq, f)


if __name__ == "__main__":
    main()
