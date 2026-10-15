"""
IWM Options Lab: the Vault Options strategy on IWM only, with Easter's new exit rules.

Exit styles tested (each with stop −30% / −40% / −50%):
  old       current live rules (take profit early at +30%, runner to +120% with a trailing stop)
  all30     sell EVERYTHING at +30%
  her_trail Easter's rules: at +30% the stop moves up to +30%. 2+ contracts: at +50% sell half, the rest targets +60%
            (stop stays at +30%). 1 contract: stop at +30%, then a trailing stop 25% below the best price (never below +30%).
  her_t60   same, but a single contract simply targets +60% once the stop is at +30%

Prices: 1) modeled (Black-Scholes on IWM's 15-min bars, same as the Inspector), 24 months
        2) REAL Alpaca IWM option prices (15-min bars of the exact contract) for the best versions
Pick → check: versions are chosen on the first 2/3 of the period and judged on the last 1/3 (unseen).
"""
import argparse
import datetime as dt
import itertools
import json
import os
import sys
import time

import vault_options as vo
import options_inspector as oi

UTC, ET = dt.timezone.utc, vo.LOCAL_TZ
SYM = "IWM"
EXITS = {f"{style}_s{int(stop * 100)}": {"style": style, "stop": stop}
         for style in ("old", "all30", "her_trail", "her_t60") for stop in (0.30, 0.40, 0.50)}
EXITS["now"] = {"style": "old", "stop": vo.OPT_STOP}          # the Inspector's replay needs a "now" key
NAMES = {"old": "current rules", "all30": "sell all at +30%", "her_trail": "your rules (1 contract trails)",
         "her_t60": "your rules (1 contract targets +60%)"}


def walker(entry, qty0, steps, rule):
    """steps yields (k, open, worst, best, close, last). Returns (pl $, how, k) or None."""
    stop, style = rule["stop"], rule["style"]
    sl = entry * (1 - stop)
    tp = entry * 1.30 if style == "all30" else entry * (1 + vo.OPT_TARGET) if style == "old" else None
    qty, banked, peak = qty0, 0.0, entry
    locked = half = trail = partial = breakeven = False
    k = None
    for k, opn, worst, best, close, last in steps:
        if trail:
            sl = max(sl, peak * (1 - vo.TRAIL_GIVEBACK))
        if worst <= sl:                                       # stop first when both happen in one candle
            px = min(sl, opn) if opn < sl else sl
            how = "stop" if px < entry else "locked profit" if px > entry * 1.01 else "breakeven"
            return (px - entry) * 100 * qty + banked, how, k
        if tp and best >= tp:
            return (tp - entry) * 100 * qty + banked, "target", k
        if style.startswith("her"):
            if not locked and best >= entry * 1.30:
                locked = True
                sl = max(sl, entry * 1.30)
                if qty0 == 1:
                    if style == "her_trail":
                        trail = True
                    else:
                        tp = entry * 1.60
            if qty0 >= 2 and not half and best >= entry * 1.50:
                half = True
                n = qty // 2
                banked += entry * 0.50 * 100 * n
                qty -= n
                sl = max(sl, entry * 1.30)
                tp = entry * 1.60
        elif style == "old":
            if not partial and best >= entry * (1 + vo.PARTIAL_AT):
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
        if last:
            return (close - entry) * 100 * qty + banked, "end of day", k
    return None


def sell_time(c, entry_day):
    if c["zero"]:
        return c["exp"], (15, 30)
    return oi.last_sell_day(c["exp"]), vo.FLATTEN_AT        # live bot holds weeklies until the day before expiry


def model_steps(c, m15, j, iv):
    sell_day, sell_hm = sell_time(c, None)
    d = 1 if c["kind"] == "call" else -1
    for k in range(j + 1, len(m15)):
        b = m15[k]
        t_end = dt.datetime.fromtimestamp(b["ts"] + 900, UTC)
        e = t_end.astimezone(ET)
        if e.date() > sell_day:
            px = oi.bid_at(c, m15[k - 1]["c"], t_end, iv)
            yield k, px, px, px, px, True
            return
        worst = oi.bid_at(c, b["l"] if d == 1 else b["h"], t_end, iv)
        best = oi.bid_at(c, b["h"] if d == 1 else b["l"], t_end, iv)
        opn = oi.bid_at(c, b["o"], t_end, iv)
        close = oi.bid_at(c, b["c"], t_end, iv)
        yield k, opn, worst, best, close, (e.date() == sell_day and (e.hour, e.minute) >= sell_hm)


def model_walk(c, m15, j, iv, plan):
    res = walker(c["ask"], c["qty"], model_steps(c, m15, j, iv), plan)
    if not res:
        return None
    pl, how, k = res
    risk = c["ask"] * plan["stop"] * 100 * c["qty"]
    return round(pl, 2), round(pl / risk, 3), how, k


def occ(c):
    return f"{SYM}{c['exp']:%y%m%d}{'C' if c['kind'] == 'call' else 'P'}{int(round(c['K'] * 1000)):08d}"


class RealPrices:
    def __init__(self, api):
        self.api, self.cache, self.calls = api, {}, 0

    def bars(self, sym, start, end):
        key = (sym, start, end)
        if key not in self.cache:
            out, token = [], None
            while True:
                p = {"symbols": sym, "timeframe": "15Min", "start": start, "end": end, "limit": 10000}
                if token:
                    p["page_token"] = token
                self.calls += 1
                if self.calls % 150 == 0:
                    time.sleep(20)
                try:
                    j = self.api.d("/v1beta1/options/bars", p)
                except vo.ApiError as e:
                    if "429" in str(e):
                        time.sleep(15)
                        continue
                    break
                out += (j.get("bars") or {}).get(sym, [])
                token = j.get("next_page_token")
                if not token:
                    break
            self.cache[key] = [(dt.datetime.fromisoformat(b["t"].replace("Z", "+00:00")), b["o"], b["h"], b["l"], b["c"])
                               for b in out]
        return self.cache[key]


def real_trade(rp, c, signal_ts, rule):
    """Same trade on the contract's real 15-min option bars. Buy at the next bar's open + 2%, sell at prices − 2%."""
    sell_day, sell_hm = sell_time(c, None)
    start = dt.datetime.fromtimestamp(signal_ts, UTC)
    end = dt.datetime(sell_day.year, sell_day.month, sell_day.day, 16, 0, tzinfo=ET).astimezone(UTC)
    bars = rp.bars(occ(c), start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ"))
    if not bars or (bars[0][0] - start).total_seconds() > 1800:
        return None                                            # no real trade near the signal time
    entry = round(bars[0][1] * 1.02 + 0.01, 2)
    qty = max(1, min(vo.MAX_CONTRACTS, int(oi.BUDGET // (entry * 100))))

    def steps():
        for i, (t, o, h, l, cl) in enumerate(bars):
            e = (t + dt.timedelta(minutes=15)).astimezone(ET)
            last = i == len(bars) - 1 or (e.date() == sell_day and (e.hour, e.minute) >= sell_hm)
            yield i, o * 0.98, l * 0.98, h * 0.98, cl * 0.98, last
            if last:
                return
    res = walker(entry, qty, steps(), rule)
    if not res:
        return None
    pl, how, _ = res
    return {"pl": round(pl, 2), "how": how, "entry": entry, "qty": qty, "model_ask": c["ask"]}


def take(setups, rule_key, f):
    return oi.take_trades(setups, rule_key, keep=f["keep"], council=f["council"])


def summarize(tk, split):
    rs = [t["pl"] / oi.RISK_USD for t in tk]
    tr = [t["pl"] / oi.RISK_USD for t in tk if t["ts"] < split]
    te = [t["pl"] / oi.RISK_USD for t in tk if t["ts"] >= split]
    mon = {}
    for t in tk:
        m = dt.datetime.fromtimestamp(t["ts"], ET).strftime("%Y-%m")
        mon[m] = mon.get(m, 0) + t["pl"]
    hows = {}
    for t in tk:
        hows[t["how"]] = hows.get(t["how"], 0) + 1
    return {"all": oi.stats(rs), "train": oi.stats(tr), "test": oi.stats(te), "usd": round(sum(t["pl"] for t in tk), 2),
            "months_up": sum(1 for v in mon.values() if v > 0), "months": len(mon), "dd": round(oi.max_dd([t["pl"] for t in tk]), 2),
            "hows": hows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=float, default=24)
    a = ap.parse_args()
    api = vo.Alpaca(os.environ["ALPACA_KEY_ID"].strip(), os.environ["ALPACA_SECRET_KEY"].strip())
    t0, now, log = time.time(), dt.datetime.now(UTC), []
    oi.ALL_EXITS = EXITS
    oi.walk = model_walk
    m15 = oi.fetch_bars(api, SYM, "15Min", now - dt.timedelta(days=a.months * 30.4 + 40))
    h1 = oi.fetch_bars(api, SYM, "1Hour", now - dt.timedelta(days=a.months * 30.4 + 75))
    test_from = (now - dt.timedelta(days=a.months * 30.4)).timestamp()
    setups = oi.replay(SYM, m15, h1, test_from, log)
    close_at = {b["ts"] + 900: (i, b) for i, b in enumerate(m15)}
    print(log[-1], flush=True)
    priced = sorted([s for s in setups if s.get("x")], key=lambda s: s["ts"])
    t_first, t_last = priced[0]["ts"], priced[-1]["ts"]
    split = t_first + (t_last - t_first) * 2 / 3
    split_label = dt.datetime.fromtimestamp(split, ET).strftime("%b %d, %Y")

    setup_f = {"all setups": None, "pullbacks": {"pullback 15m"}, "opening breakouts": {"opening breakout"},
               "crossovers": {"crossover 15m"}}
    kind_f = {"0DTE or weekly": None, "0DTE only": True, "weeklies only": False}
    dir_f = {"calls and puts": None, "calls only": 1, "puts only": -1}
    hour_f = {"all day": None, "10 AM–noon": (10, 12), "10 AM–2 PM": (10, 14), "noon–3 PM": (12, 15)}
    council_f = {"council approves": True, "every setup": False}
    rows = []
    for (sn, sv), (kn, kv), (dn, dv), (hn, hv), (cn, cv) in itertools.product(
            setup_f.items(), kind_f.items(), dir_f.items(), hour_f.items(), council_f.items()):
        def keep(s, sv=sv, kv=kv, dv=dv, hv=hv):
            return ((sv is None or s["setup"] in sv) and (kv is None or s.get("zero") == kv) and
                    (dv is None or s["dir"] == dv) and (hv is None or hv[0] <= s["hour"] < hv[1]))
        f = {"keep": keep, "council": cv}
        for ek in EXITS:
            if ek == "now":
                continue
            tk = take(priced, ek, f)
            if len(tk) < 20:
                continue
            r = summarize(tk, split)
            r.update(label=f"{NAMES[EXITS[ek]['style']]} · stop −{int(EXITS[ek]['stop'] * 100)}% · {sn} · {kn} · {dn} · {hn} · {cn}",
                     exit=ek, filters=[sn, kn, dn, hn, cn], id=len(rows))
            rows.append(r)
    log.append(f"Tested {len(rows)} IWM versions on {len(priced)} priced setups ({(time.time() - t0) / 60:.1f} min)")
    print(log[-1], flush=True)

    def find(ek, filt=("all setups", "0DTE or weekly", "calls and puts", "all day", "council approves")):
        return next((r for r in rows if r["exit"] == ek and r["filters"] == list(filt)), None)
    baseline = {ek: find(ek) for ek in ("old_s40", "all30_s40", "her_trail_s40", "her_t60_s40")}
    ok = [r for r in rows if r["train"]["n"] >= 30 and r["test"]["n"] >= 15]
    def dedupe(rs, n):
        out, seen = [], set()
        for r in rs:
            sig = (r["all"]["n"], r["usd"])
            if sig not in seen:
                seen.add(sig)
                out.append(r)
            if len(out) == n:
                break
        return out
    best_train = dedupe(sorted(ok, key=lambda r: -r["train"]["avg"]), 2)
    held = [r for r in ok if r["train"]["avg"] > 0.05 and r["test"]["avg"] > 0.05]
    robust = [r for r in dedupe(sorted(held, key=lambda r: -min(r["train"]["avg"], r["test"]["avg"])), 4) if r not in best_train][:3]

    # REAL option prices for the baselines + the picks
    rp = RealPrices(api)
    checks = [(f"baseline {k}", r) for k, r in baseline.items() if r] + [(f"best-on-first-part #{i + 1}", r) for i, r in enumerate(best_train[:2])] \
             + [(f"robust #{i + 1}", r) for i, r in enumerate(robust)]
    for name, r in checks:
        sv, kv, dv, hv, cv = (setup_f[r["filters"][0]], kind_f[r["filters"][1]], dir_f[r["filters"][2]],
                              hour_f[r["filters"][3]], council_f[r["filters"][4]])
        keep = (lambda s, sv=sv, kv=kv, dv=dv, hv=hv: (sv is None or s["setup"] in sv) and (kv is None or s.get("zero") == kv)
                and (dv is None or s["dir"] == dv) and (hv is None or hv[0] <= s["hour"] < hv[1]))
        tk = take(priced, r["exit"], {"keep": keep, "council": cv})
        real = []
        for t in tk:
            i, b = close_at.get(t["ts"], (None, None))
            if b is None:
                continue
            ct, _ = oi.pick(SYM, t["dir"], b["c"], t["iv"], dt.datetime.fromtimestamp(t["ts"], UTC))
            if not ct:
                continue
            rt = real_trade(rp, ct, t["ts"], EXITS[r["exit"]])
            if rt:
                real.append({**rt, "ts": t["ts"], "model_pl": t["pl"]})
        rr = [x["pl"] / oi.RISK_USD for x in real]
        mr = [x["model_pl"] / oi.RISK_USD for x in real]
        hows = {}
        for x in real:
            hows[x["how"]] = hows.get(x["how"], 0) + 1
        r["real"] = {"stats": oi.stats(rr), "model_same": oi.stats(mr), "usd": round(sum(x["pl"] for x in real), 2),
                     "missing": len(tk) - len(real), "hows": hows,
                     "entry_ratio": round(sorted(x["entry"] / x["model_ask"] for x in real)[len(real) // 2], 2) if real else None}
        r["check"] = name
        print(name, r["label"], json.dumps(r["real"]), f"({rp.calls} option calls)", flush=True)

    def line(r):
        s = (f"{r['label']}\n   {r['all']['n']} trades, {r['all']['win']:.0%} won, {r['all']['avg']:+.2f}R avg · "
             f"first part {r['train']['avg'] or 0:+.2f}R → unseen {r['test']['avg'] or 0:+.2f}R · "
             f"${r['usd']:+,.0f} at normal trade size · {r['months_up']}/{r['months']} months up")
        if r.get("real"):
            x = r["real"]
            s += (f"\n   REAL option prices: {x['stats']['avg'] or 0:+.2f}R avg on {x['stats']['n']} trades, "
                  f"${x['usd']:+,.0f} (model said {x['model_same']['avg'] or 0:+.2f}R) · exits {x['hows']}")
        return s
    parts = [f"🕵️ IWM Options Lab · {a.months:g} months · {len(rows)} versions · unseen after {split_label}", ""]
    parts += ["Same IWM setups, different exits (stop −40%, council approves, all day):"]
    for k, r in baseline.items():
        if r:
            parts += ["• " + line(r)]
    parts += ["", f"{len(held)} of {len(ok)} versions made money in BOTH parts (> +0.05R)."]
    for i, r in enumerate(best_train[:2]):
        parts += [f"Best on the first part #{i + 1}: " + line(r)]
    for i, r in enumerate(robust):
        parts += [f"Most robust #{i + 1}: " + line(r)]
    msg = "\n".join(parts)
    print(msg)
    log.append(f"Finished in {(time.time() - t0) / 60:.1f} min, {rp.calls} option price calls")
    json.dump({"when": t0, "split": split_label, "summary": msg, "log": log,
               "rows": sorted(rows, key=lambda r: -min(r["train"]["avg"] or -9, r["test"]["avg"] or -9))[:300],
               "checks": [r for _, r in checks]}, open("iwm_options_lab.json", "w"), indent=1, default=str)


if __name__ == "__main__":
    main()
