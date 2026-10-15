"""
Entry Lab (IWM): four new entry ideas, tested on REAL IWM option prices over 24 months, with Easter's exit rules.

  1. No chasing     skip if the option already costs 50%+ more than its first price of the day,
                    or the bot already traded that direction today
  2. Momentum veto  skip when the Momentum officer votes no
  3. Early pullbacks only pullback setups, entered before 10:30 / 11:00 AM (no breakouts)
  4. Gap fade       a NEW setup: after a morning gap, bet on it reversing (puts after a gap up, calls after a gap down)

Each idea is judged on the first 16 months and, separately, on the last 8 months it was never tuned on.
One IWM trade at a time like the live bot. R = $25 (one normal trade's risk).
"""
import bisect
import datetime as dt
import json
import math
import os
import time

import vault_options as vo
import options_inspector as oi
import iwm_options_lab as L
import iwm_study as S

UTC, ET = dt.timezone.utc, vo.LOCAL_TZ
SYM = "IWM"
RULES = {"her_s30": {"style": "her_trail", "stop": 0.30}, "her_s40": {"style": "her_trail", "stop": 0.40}}
KEY = "her_s40"


def day_open_utc(d):
    return dt.datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET).astimezone(UTC)


def real_from_open(rp, ct, signal_ts):
    """Fetch the contract's 15-min bars from 9:30 that day; return its first price of the day + the real result."""
    sig = dt.datetime.fromtimestamp(signal_ts, UTC)
    d = sig.astimezone(ET).date()
    sell_day, sell_hm = L.sell_time(ct, None)
    end = dt.datetime(sell_day.year, sell_day.month, sell_day.day, 16, 0, tzinfo=ET).astimezone(UTC)
    allb = rp.bars(L.occ(ct), day_open_utc(d).strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ"))
    if not allb:
        return None
    first_px = allb[0][1]
    bars = [b for b in allb if b[0] >= sig]
    if not bars or (bars[0][0] - sig).total_seconds() > 1800:
        return None
    entry = round(bars[0][1] * 1.02 + 0.01, 2)
    qty = max(1, min(vo.MAX_CONTRACTS, int(oi.BUDGET // (entry * 100))))
    out = {"entry": entry, "qty": qty, "first_px": first_px, "run_up": bars[0][1] / first_px - 1 if first_px else 0}
    for key, rule in RULES.items():
        def steps():
            for i, (t, o, h, l, cl) in enumerate(bars):
                e = (t + dt.timedelta(minutes=15)).astimezone(ET)
                last = i == len(bars) - 1 or (e.date() == sell_day and (e.hour, e.minute) >= sell_hm)
                yield i, o * 0.98, l * 0.98, h * 0.98, cl * 0.98, last
                if last:
                    return
        res = L.walker(entry, qty, steps(), rule)
        if res:
            pl, how, k = res
            out[key] = {"pl": round(pl, 2), "r": round(pl / oi.RISK_USD, 3), "how": how,
                        "exit_ts": (bars[k][0] + dt.timedelta(minutes=15)).timestamp()}
    return out


def sequential(rows, key, keep=lambda r, ctx: True, no_reentry=False):
    """One trade at a time, in time order. keep(r, ctx) can look at what already happened today."""
    taken, busy, today = [], 0, {}
    for r in sorted(rows, key=lambda r: r["ts"]):
        if key not in r["real"] or r["ts"] < busy:
            continue
        d = dt.datetime.fromtimestamp(r["ts"], ET).date()
        ctx = today.setdefault(d, {"dirs": set(), "won_dirs": set()})
        if no_reentry and r["dir"] in ctx["dirs"]:
            continue
        if not keep(r, ctx):
            continue
        taken.append(r)
        busy = r["real"][key]["exit_ts"]
        ctx["dirs"].add(r["dir"])
        if r["real"][key]["pl"] > 0:
            ctx["won_dirs"].add(r["dir"])
    return taken


def gap_fade_setups(m15, iwm_d, prev_day, min_gap, at_hm, confirm):
    """Synthetic setups: after a gap of at least min_gap, fade it at at_hm (optionally only if the first bars already turned)."""
    days = S.by_day(m15)
    ends = [b["ts"] + 900 for b in m15]
    out = []
    for d, bars in sorted(days.items()):
        pd_ = prev_day.get(d)
        if not pd_ or pd_ not in iwm_d or not bars or bars[0]["o"] <= 0:
            continue
        prev_c = iwm_d[pd_]["c"]
        gap = bars[0]["o"] / prev_c - 1
        if abs(gap) < min_gap:
            continue
        sig_ts = dt.datetime(d.year, d.month, d.day, at_hm[0], at_hm[1], tzinfo=ET).timestamp()
        px = S.price_at(bars, sig_ts)
        if not px:
            continue
        D = -1 if gap > 0 else 1
        if confirm and (px - bars[0]["o"]) * D <= 0:
            continue                                   # price hasn't started moving back toward yesterday's close
        j = bisect.bisect_right(ends, sig_ts) - 1
        if j < 0:
            continue
        rv = oi.realized_vol(m15, j)
        if not rv:
            continue
        out.append({"ts": sig_ts, "dir": D, "px": px, "iv": max(oi.IV_FLOOR, rv * oi.IV_MULT), "gap": gap})
    return out


def main():
    t0 = time.time()
    api = vo.Alpaca(os.environ["ALPACA_KEY_ID"].strip(), os.environ["ALPACA_SECRET_KEY"].strip())
    now = dt.datetime.now(UTC)
    start = now - dt.timedelta(days=24 * 30.4)
    oi.ALL_EXITS = {"now": {"style": "her_trail", "stop": 0.40}}
    oi.walk = L.model_walk
    m15 = oi.fetch_bars(api, SYM, "15Min", start - dt.timedelta(days=40))
    h1 = oi.fetch_bars(api, SYM, "1Hour", start - dt.timedelta(days=75))
    iwm_d = S.daily_bars(api, SYM, start - dt.timedelta(days=10))
    ddays = sorted(iwm_d)
    prev_day = {b: a for a, b in zip(ddays, ddays[1:])}
    log = []
    setups = oi.replay(SYM, m15, h1, start.timestamp(), log)
    close_at = {b["ts"] + 900: b for b in m15}
    rp = L.RealPrices(api)
    rows = []
    for s in setups:
        if not s.get("contract"):
            continue
        b = close_at.get(s["ts"])
        ct, _ = oi.pick(SYM, s["dir"], b["c"], s["iv"], dt.datetime.fromtimestamp(s["ts"], UTC)) if b else (None, None)
        if not ct:
            continue
        ro = real_from_open(rp, ct, s["ts"])
        if not ro or KEY not in ro:
            continue
        e = dt.datetime.fromtimestamp(s["ts"], ET)
        rows.append({"ts": s["ts"], "dir": s["dir"], "setup": s["setup"], "approved": s["approved"], "votes": s["votes"],
                     "hm": e.hour * 60 + e.minute, "real": ro})
        if len(rows) % 300 == 0:
            print(f"{len(rows)} setups priced ({rp.calls} calls)", flush=True)
    log.append(f"{len(rows)} bot setups priced on real option bars")
    rows.sort(key=lambda r: r["ts"])
    split = rows[0]["ts"] + (rows[-1]["ts"] - rows[0]["ts"]) * 2 / 3
    split_label = dt.datetime.fromtimestamp(split, ET).strftime("%b %d, %Y")
    approved = [r for r in rows if r["approved"]]

    def res(taken, key=KEY):
        return S.summary(taken, key, split)

    ideas = {}
    ideas["Today's bot (council approves, your exits)"] = res(sequential(approved, KEY))
    # 1) no chasing
    for lim in (0.25, 0.50, 1.00):
        ideas[f"1. No chasing: skip if option already up {lim:.0%}+ today"] = res(
            sequential(approved, KEY, keep=lambda r, c, lim=lim: r["real"]["run_up"] < lim))
    ideas["1. No chasing: no second trade in the same direction that day"] = res(sequential(approved, KEY, no_reentry=True))
    ideas["1. No chasing: both rules (up 50%+ or same direction again)"] = res(
        sequential(approved, KEY, keep=lambda r, c: r["real"]["run_up"] < 0.5, no_reentry=True))
    # 2) momentum veto
    ideas["2. Momentum veto"] = res(sequential(approved, KEY, keep=lambda r, c: r["votes"].get("Momentum") is not False))
    # 3) early pullbacks only
    for cut, lab in ((630, "10:30"), (660, "11:00"), (720, "noon")):
        ideas[f"3. Pullbacks only, before {lab}"] = res(
            sequential(approved, KEY, keep=lambda r, c, cut=cut: r["setup"].startswith("pullback") and r["hm"] < cut))
    ideas["3. Pullbacks only, any time"] = res(sequential(approved, KEY, keep=lambda r, c: r["setup"].startswith("pullback")))
    # combos
    ideas["1+2: no chasing + Momentum veto"] = res(sequential(
        approved, KEY, keep=lambda r, c: r["real"]["run_up"] < 0.5 and r["votes"].get("Momentum") is not False, no_reentry=True))
    ideas["1+2+3: + pullbacks before 11:00"] = res(sequential(
        approved, KEY, keep=lambda r, c: r["real"]["run_up"] < 0.5 and r["votes"].get("Momentum") is not False
        and r["setup"].startswith("pullback") and r["hm"] < 660, no_reentry=True))
    ideas["1+2 with a −30% stop"] = res(sequential(
        approved, "her_s30", keep=lambda r, c: r["real"]["run_up"] < 0.5 and r["votes"].get("Momentum") is not False,
        no_reentry=True), "her_s30")
    # 4) gap fade (new setups)
    gaps = {}
    for min_gap in (0.003, 0.005, 0.008):
        for at in ((9, 45), (10, 0), (10, 30)):
            for confirm in (False, True):
                gs = gap_fade_setups(m15, iwm_d, prev_day, min_gap, at, confirm)
                grows = []
                for g in gs:
                    ct, _ = oi.pick(SYM, g["dir"], g["px"], g["iv"], dt.datetime.fromtimestamp(g["ts"], UTC))
                    if not ct:
                        continue
                    ro = real_from_open(rp, ct, g["ts"])
                    if ro and KEY in ro:
                        grows.append({"ts": g["ts"], "dir": g["dir"], "real": ro})
                if len(grows) >= 20:
                    name = (f"4. Gap fade: gap {min_gap:.1%}+, enter {at[0]}:{at[1]:02d}"
                            + (", only once it starts turning" if confirm else ""))
                    gaps[name] = res(sequential(grows, KEY))
    best_gap_train = max(gaps.items(), key=lambda kv: (kv[1]["first"] or -9) if kv[1]["n"] - kv[1]["n_unseen"] >= 15 else -9) if gaps else None
    out = {"when": t0, "split": split_label, "ideas": ideas, "gap_fade": gaps,
           "gap_fade_pick": best_gap_train[0] if best_gap_train else None, "log": log, "calls": rp.calls}
    log.append(f"Finished in {(time.time() - t0) / 60:.1f} min, {rp.calls} option price calls")
    json.dump(out, open("entry_lab.json", "w"), indent=1, default=str)
    for k, v in list(ideas.items()) + list(gaps.items()):
        print(f"{k}: n={v['n']} win={v['win']} avg={v['avg_r']} first={v['first']} unseen={v['unseen']} (n={v['n_unseen']}) "
              f"${v['usd']} unseen ${v['usd_unseen']} months {v['months_up']}/{v['months']}")
    print("\n".join(log))


if __name__ == "__main__":
    main()
