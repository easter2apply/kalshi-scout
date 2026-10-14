"""
Strategy Lab: Inspector tests two strategies from your trading notes on real Alpaca price history.

  A) Heiken Ashi reversal + pullback (5-min and 15-min charts)
  B) Previous day's high/low + first 5-minute candle, break and retest (9:30–11:00 AM ET)

Each is tested trading the stock/ETF itself, with realistic costs, measured in R
(+1R = made what you risked, −1R = full stop). If a strategy has no edge on the shares,
buying options on it won't fix that: options add time decay and wider spreads on top.

Run:  python strategy_lab.py --months 12
"""

import argparse
import datetime as dt
import html
import json
import os
import sys
import time

import vault_options as vo

UTC = dt.timezone.utc
ET = vo.LOCAL_TZ
SYMBOLS = ["SPY", "QQQ", "IWM", "AAPL", "NVDA", "AMD", "TSLA", "META", "AMZN"]
SLIP = 0.02          # dollars per share lost to spread + slippage, round trip (in and out)
FLAT_HM = (15, 55)   # every trade is out by 3:55 PM ET (day trading, like calls/puts would be)


# ───────────────────────────── data ──────────────────────────────────────────

def fetch(api, sym, start):
    out, token = [], None
    for _ in range(400):
        p = {"symbols": sym, "timeframe": "5Min", "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": 10000,
             "feed": vo.STOCK_FEED, "adjustment": "split", "sort": "asc"}
        if token:
            p["page_token"] = token
        j = api.d("/v2/stocks/bars", p)
        for b in (j.get("bars") or {}).get(sym, []):
            t = dt.datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
            e = t.astimezone(ET)
            if (9, 30) <= (e.hour, e.minute) < (16, 0) and e.weekday() < 5:
                out.append({"ts": int(t.timestamp()), "e": e, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]})
        token = j.get("next_page_token")
        if not token:
            break
    return out


def by_day(bars):
    days = {}
    for b in bars:
        days.setdefault(b["e"].date(), []).append(b)
    return [days[d] for d in sorted(days)]


def resample(day, minutes):
    """5-min bars → N-minute bars within one session."""
    if minutes == 5:
        return day
    out, cur = [], None
    for b in day:
        k = (b["e"].hour * 60 + b["e"].minute - 570) // minutes
        if cur is None or cur["k"] != k:
            if cur:
                out.append(cur)
            cur = dict(b, k=k)
        else:
            cur.update(h=max(cur["h"], b["h"]), l=min(cur["l"], b["l"]), c=b["c"], e_end=b["e"])
    if cur:
        out.append(cur)
    return out


# ───────────────────────────── trade walker ──────────────────────────────────

def walk(day5, k, d, entry, stop, plan):
    """Follow a trade on 5-min bars from index k+1. plan: target R, partial half at R then exit rule.
    Returns R after costs."""
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    cost_r = SLIP / risk
    tgt = entry + d * plan["target"] * risk
    sl = stop
    half_done, banked = False, 0.0
    ha = plan.get("ha")                                  # HA exit: list of HA colors per 5-min index (or None)
    for i in range(k + 1, len(day5)):
        b = day5[i]
        lo, hi = b["l"], b["h"]
        if (lo <= sl) if d == 1 else (hi >= sl):          # stop first if both in one bar
            px = min(sl, b["o"]) if d == 1 else max(sl, b["o"])
            r = (px - entry) * d / risk
            return (banked + (r * (0.5 if half_done else 1.0))) - cost_r
        if (hi >= tgt) if d == 1 else (lo <= tgt):
            if plan.get("partial") and not half_done:
                banked += 0.5 * plan["target"]
                half_done = True
                sl = entry                                # stop to entry on the rest
                tgt = entry + d * 100 * risk              # rest has no fixed target
            else:
                return (banked + plan["target"] * (0.5 if half_done else 1.0)) - cost_r
        if half_done and ha is not None and plan.get("ha_exit"):
            col = ha.get(i)
            if col is not None and col != d:              # HA turned against us: close the rest at this bar's close
                r = (b["c"] - entry) * d / risk
                return banked + 0.5 * r - cost_r
        if (b["e"].hour, b["e"].minute) >= FLAT_HM or i == len(day5) - 1:
            r = (b["c"] - entry) * d / risk
            return (banked + r * (0.5 if half_done else 1.0)) - cost_r
    return None


# ───────────────────────────── A) Heiken Ashi reversal ───────────────────────

def heiken(bars):
    out, po, pc = [], None, None
    for b in bars:
        c = (b["o"] + b["h"] + b["l"] + b["c"]) / 4
        o = (b["o"] + b["c"]) / 2 if po is None else (po + pc) / 2
        out.append({"o": o, "c": c, "h": max(b["h"], o, c), "l": min(b["l"], o, c), "col": 1 if c > o else -1, "body": abs(c - o)})
        po, pc = o, c
    return out


def ha_trades(day, tf, run_min, plan_key):
    """Doc rules: a run of ≥run_min candles one color with the latest shrinking → flip → pullback (opposite color,
    not breaking the extreme) → confirming candle = entry. Stop at the pullback extreme."""
    bars = resample(day, tf)
    ha = heiken(bars)
    # map HA color onto 5-min indices so the exit can watch it (color of the tf-bar that has CLOSED)
    color5 = {}
    if tf == 5:
        color5 = {i: h["col"] for i, h in enumerate(ha)}
    else:
        end_idx = {}
        for i, b in enumerate(day):
            k = (b["e"].hour * 60 + b["e"].minute - 570) // tf
            end_idx[k] = i
        for j, b in enumerate(bars):
            color5[end_idx[b["k"]]] = ha[j]["col"]
    trades = []
    j = run_min
    busy_until = -1
    while j < len(ha) - 2:
        # find a flip at j: ha[j] opposite to a run before it
        prev = ha[j - 1]["col"]
        if ha[j]["col"] == prev:
            j += 1; continue
        run = 0
        while j - 1 - run >= 0 and ha[j - 1 - run]["col"] == prev:
            run += 1
        bodies = [ha[j - 1 - x]["body"] for x in range(run)][::-1]           # oldest → newest
        shrinking = run >= run_min and len(bodies) >= 3 and bodies[-1] < max(bodies) * 0.8
        if not shrinking:
            j += 1; continue
        d = -prev                                                            # trade the reversal
        extreme = min(b["l"] for b in bars[j - run:j]) if d == 1 else max(b["h"] for b in bars[j - run:j])
        # wait for a pullback (opposite-color HA candles) that holds the extreme, then a confirming candle
        k, pulled, pb_ext, entry_j = j + 1, False, None, None
        while k < len(ha) and k <= j + 12:
            if ha[k]["col"] == -d:
                if (bars[k]["l"] <= extreme) if d == 1 else (bars[k]["h"] >= extreme):
                    break                                                    # support/resistance failed
                pulled = True
                ext = bars[k]["l"] if d == 1 else bars[k]["h"]
                pb_ext = ext if pb_ext is None else (min(pb_ext, ext) if d == 1 else max(pb_ext, ext))
            elif pulled:
                entry_j = k
                break
            k += 1
        if entry_j is None:
            j += 1; continue
        e = bars[entry_j].get("e_end", bars[entry_j]["e"])
        if not ((9, 45) <= (e.hour, e.minute) <= (15, 0)):
            j = entry_j + 1; continue
        # index of the 5-min bar where the entry candle closed
        k5 = max(i for i, b in enumerate(day) if b["ts"] <= (bars[entry_j]["ts"] + (tf - 5) * 60))
        if k5 <= busy_until:
            j = entry_j + 1; continue
        entry = bars[entry_j]["c"]
        stop = pb_ext
        if stop is None or (entry - stop) * d <= 0:
            j = entry_j + 1; continue
        plan = dict(PLANS_HA[plan_key], ha=color5)
        r = walk(day, k5, d, entry, stop, plan)
        if r is not None:
            trades.append({"r": r, "d": d, "e": e})
            busy_until = k5 + 1
        j = entry_j + 1
    return trades


PLANS_HA = {
    "doc": {"label": "Doc's exit: half at 1.5R, stop to entry, rest out when HA flips", "target": 1.5, "partial": True, "ha_exit": True},
    "t15": {"label": "All out at 1.5R", "target": 1.5},
    "t2": {"label": "All out at 2R", "target": 2.0},
}


# ───────────────────────────── B) 5-min opening range break + retest ─────────

def orb_trades(day, prev, plan_key, level, retest=True, until=(11, 0)):
    """level='orb5': the 9:30–9:35 candle's high/low · level='pd': yesterday's high/low."""
    if len(day) < 10 or prev is None:
        return []
    pdh, pdl = max(b["h"] for b in prev), min(b["l"] for b in prev)
    hi, lo = (day[0]["h"], day[0]["l"]) if level == "orb5" else (pdh, pdl)
    plan = PLANS_ORB[plan_key]
    broke, out = None, []
    for k in range(1, len(day)):
        b = day[k]
        if (b["e"].hour, b["e"].minute) >= until:
            break
        if broke is None:
            if b["c"] > hi:
                broke, lvl, d = k, hi, 1
            elif b["c"] < lo:
                broke, lvl, d = k, lo, -1
            if broke is not None and not retest:
                return _orb_enter(day, k, d, lvl, b, plan, pdh, pdl)
            continue
        # after the break: wait for price to come back to the level and close back on the break side
        touched = (b["l"] <= lvl * 1.0005) if d == 1 else (b["h"] >= lvl * 0.9995)
        failed = (b["c"] < lvl) if d == 1 else (b["c"] > lvl)
        if failed:
            return []                                   # retest failed: no trade today
        if touched:
            strong = (b["c"] - b["l"]) / max(b["h"] - b["l"], 1e-9) >= 0.5 if d == 1 else (b["h"] - b["c"]) / max(b["h"] - b["l"], 1e-9) >= 0.5
            if strong:
                return _orb_enter(day, k, d, lvl, b, plan, pdh, pdl)
    return out


def _orb_enter(day, k, d, lvl, b, plan, pdh, pdl):
    entry = b["c"]
    stop = (min(b["l"], lvl) - 0.0005 * lvl) if d == 1 else (max(b["h"], lvl) + 0.0005 * lvl)
    risk = abs(entry - stop)
    if risk <= 0:
        return []
    p = dict(plan)
    if p.get("pd_target"):
        tgt_px = pdh if d == 1 else pdl
        tr = (tgt_px - entry) * d / risk
        if tr < 1.0:
            p["target"] = 2.0                           # yesterday's level too close: fall back to 2R
        else:
            p["target"] = tr
    r = walk(day, k, d, entry, stop, p)
    return [{"r": r, "d": d, "e": b["e"]}] if r is not None else []


PLANS_ORB = {
    "t15": {"label": "target 1.5R", "target": 1.5},
    "t2": {"label": "target 2R", "target": 2.0},
    "t3": {"label": "target 3R", "target": 3.0},
    "pd": {"label": "target = yesterday's high/low (min 1R)", "pd_target": True, "target": 2.0},
}


# ───────────────────────────── grading ───────────────────────────────────────

def stats(rs):
    n = len(rs)
    if not n:
        return {"n": 0, "win": None, "avg": None, "total": 0.0}
    return {"n": n, "win": sum(1 for r in rs if r > 0) / n, "avg": sum(rs) / n, "total": sum(rs)}


def max_dd(rs):
    peak = cur = dd = 0.0
    for r in rs:
        cur += r; peak = max(peak, cur); dd = max(dd, peak - cur)
    return dd


def run_all(data, split):
    rows = []
    variants = []
    for tf in (5, 15):
        for rm in (3, 4, 5):
            for pk in PLANS_HA:
                variants.append(("A", f"Heiken Ashi · {tf}-min · {rm}+ candle run · {PLANS_HA[pk]['label']}",
                                 lambda day, prev, tf=tf, rm=rm, pk=pk: ha_trades(day, tf, rm, pk),
                                 tf == 5 and rm == 4 and pk == "doc"))
    for level in ("orb5", "pd"):
        for rt in (True, False):
            for pk in PLANS_ORB:
                lab = ("first 5-min candle" if level == "orb5" else "yesterday's high/low") + (" · break + retest" if rt else " · break only")
                variants.append(("B", f"{lab} · {PLANS_ORB[pk]['label']} · entries 9:35–11:00",
                                 lambda day, prev, level=level, rt=rt, pk=pk: orb_trades(day, prev, pk, level, rt),
                                 level == "orb5" and rt and pk == "pd"))
    for strat, label, fn, is_doc in variants:
        per_sym, allt = {}, []
        for sym, days in data.items():
            ts = []
            for i, day in enumerate(days):
                ts += [dict(t, sym=sym) for t in fn(day, days[i - 1] if i else None)]
            per_sym[sym] = stats([t["r"] for t in ts])
            allt += ts
        allt.sort(key=lambda t: t["e"])
        tr = [t["r"] for t in allt if t["e"].timestamp() < split]
        te = [t["r"] for t in allt if t["e"].timestamp() >= split]
        mon = {}
        for t in allt:
            mon[t["e"].strftime("%Y-%m")] = mon.get(t["e"].strftime("%Y-%m"), 0) + t["r"]
        etf = [t["r"] for t in allt if t["sym"] in ("SPY", "QQQ", "IWM")]
        rows.append({"strategy": strat, "label": label, "doc": is_doc, "all": stats([t["r"] for t in allt]),
                     "train": stats(tr), "test": stats(te), "etf": stats(etf), "per_sym": per_sym,
                     "months_up": sum(1 for v in mon.values() if v > 0), "months": len(mon),
                     "max_dd": round(max_dd([t["r"] for t in allt]), 1), "monthly": {k: round(v, 1) for k, v in sorted(mon.items())}})
    return rows


def verdict(r):
    a, te = r["all"]["avg"], r["test"]["avg"]
    if a is None or r["all"]["n"] < 30:
        return "not enough trades"
    if a > 0.1 and te is not None and te > 0.05 and r["months_up"] >= r["months"] * 0.6:
        return "PROMISING"
    if a > 0:
        return "slightly positive, not convincing"
    return "loses money"


def summary(rows, months, split_label):
    lines = [f"🕵️ Strategy Lab: your two strategies, {months:g} months, 9 stocks/ETFs, real costs", ""]
    for strat, name in (("A", "A) Heiken Ashi reversal + pullback"), ("B", "B) Prev-day + 5-min break and retest")):
        rs = [r for r in rows if r["strategy"] == strat]
        doc = next(r for r in rs if r["doc"])
        best = max((r for r in rs if r["train"]["n"] >= 30), key=lambda r: r["train"]["avg"] or -9, default=None)
        pos = sum(1 for r in rs if (r["all"]["avg"] or -1) > 0)
        lines += [name,
                  f"• As written: {doc['all']['avg']:+.2f}R a trade over {doc['all']['n']} trades, {doc['all']['win']:.0%} wins, "
                  f"{doc['months_up']}/{doc['months']} months up → {verdict(doc)}",
                  f"• SPY/QQQ/IWM only: {doc['etf']['avg']:+.2f}R over {doc['etf']['n']}" if doc["etf"]["n"] else "• SPY/QQQ/IWM only: no trades"]
        if best:
            lines.append(f"• Best tweak ({best['label']}): tuned {best['train']['avg']:+.2f}R → unseen {best['test']['avg']:+.2f}R "
                         f"({best['all']['n']} trades) → {verdict(best)}")
        lines += [f"• {pos} of {len(rs)} versions made money overall", ""]
    lines.append(f"Tuned on months before {split_label}, checked after. R = profit in units of what you risked; costs of 2¢/share included.")
    return "\n".join(lines)


def render(rows, months, split_label, log):
    def tr(r):
        v = verdict(r)
        cls = "pos" if v == "PROMISING" else "neg" if v == "loses money" else "wait"
        return (f"<tr{' class=doc' if r['doc'] else ''}><td>{html.escape(r['label'])}{' <b>(as written)</b>' if r['doc'] else ''}</td>"
                f"<td class=n>{r['all']['n']}</td><td class=n>{(r['all']['win'] or 0):.0%}</td>"
                f"<td class='n {cls}'>{(r['all']['avg'] or 0):+.2f}R</td><td class=n>{(r['train']['avg'] or 0):+.2f}R</td>"
                f"<td class=n>{(r['test']['avg'] or 0):+.2f}R</td><td class=n>{r['months_up']}/{r['months']}</td><td>{v}</td></tr>")
    body = ""
    for strat, name in (("A", "A) Heiken Ashi reversal + pullback"), ("B", "B) Previous day + 5-minute break and retest")):
        rs = sorted([r for r in rows if r["strategy"] == strat], key=lambda r: -(r["all"]["avg"] or -9))
        body += (f"<section><h2>{name}</h2><div class=tbl><table><tr><th>Version</th><th>Trades</th><th>Win</th><th>Avg</th>"
                 f"<th>Tuning</th><th>Unseen</th><th>Months up</th><th>Verdict</th></tr>{''.join(tr(r) for r in rs)}</table></div></section>")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Strategy Lab</title><style>
:root{{--bg:#0f1220;--panel:#171b2e;--fg:#e8ecf8;--muted:#9aa3c0;--line:#2a3150;--good:#53f0a6;--bad:#ff6b7c;--wait:#f4c84a}}
@media (prefers-color-scheme: light){{:root:not([data-theme="dark"]){{--bg:#f4f5fa;--panel:#fff;--fg:#151a2c;--muted:#5b6380;--line:#dde1ee;--good:#138a55;--bad:#c23345;--wait:#9a7100}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif;padding:18px 16px 40px}}
.wrap{{max-width:1000px;margin:0 auto;display:grid;gap:16px}} section{{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px}}
h1{{margin:0}} h2{{margin:0 0 8px;font-size:13px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}}
.tbl{{overflow-x:auto}} table{{width:100%;border-collapse:collapse}} td,th{{padding:6px;border-bottom:1px solid var(--line);text-align:left}}
td.n{{text-align:right;font-variant-numeric:tabular-nums}} .pos{{color:var(--good)}} .neg{{color:var(--bad)}} .wait{{color:var(--wait)}}
tr.doc td{{background:color-mix(in srgb,var(--wait) 10%,transparent)}} p{{color:var(--muted)}}
</style></head><body><div class=wrap><header><h1>🕵️ Strategy Lab</h1>
<p>Your two strategies on {months:g} months of real 5-minute prices for {', '.join(SYMBOLS)} · tuned before {split_label}, checked after ·
traded on the shares with 2¢/share costs · everything out by 3:55 PM ET.</p></header>{body}
<details><summary>Log</summary><ul>{''.join(f'<li>{html.escape(l)}</li>' for l in log)}</ul></details></div></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=float, default=12)
    args = ap.parse_args()
    key, secret = os.environ.get("ALPACA_KEY_ID", "").strip(), os.environ.get("ALPACA_SECRET_KEY", "").strip()
    if not key or not secret:
        sys.exit("Add ALPACA_KEY_ID and ALPACA_SECRET_KEY.")
    api = vo.Alpaca(key, secret)
    t0 = time.time()
    start = dt.datetime.now(UTC) - dt.timedelta(days=args.months * 30.4 + 3)
    data, log = {}, []
    for sym in SYMBOLS:
        try:
            bars = fetch(api, sym, start)
        except vo.ApiError as e:
            log.append(f"{sym}: skipped ({e})"); continue
        data[sym] = by_day(bars)
        log.append(f"{sym}: {len(bars)} 5-min bars, {len(data[sym])} days")
        print(log[-1], flush=True)
    first = min(d[0][0]["ts"] for d in data.values())
    last = max(d[-1][-1]["ts"] for d in data.values())
    split = first + (last - first) * 2 / 3
    split_label = dt.datetime.fromtimestamp(split, ET).strftime("%b %d, %Y")
    rows = run_all(data, split)
    log.append(f"Tested {len(rows)} versions in {(time.time() - t0) / 60:.1f} min")
    msg = summary(rows, args.months, split_label)
    print(msg)
    with open("strategy_lab.json", "w") as f:
        json.dump({"when": t0, "months": args.months, "split": split_label,
                   "rows": [{k: v for k, v in r.items()} for r in rows]}, f, indent=1, default=str)
    with open("strategy_lab.html", "w") as f:
        f.write(render(rows, args.months, split_label, log))
    tg = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    try:
        chat = json.load(open(vo.STATE_FILE)).get("chat_id")
    except Exception:
        chat = None
    if tg and chat:
        try:
            vo.http("POST", f"https://api.telegram.org/bot{tg}/sendMessage", body={"chat_id": chat, "text": msg}, tries=2)
        except Exception as e:
            print(f"Telegram failed: {e}")


if __name__ == "__main__":
    main()
