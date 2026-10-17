"""
VWAP Lab (IWM): four VWAP strategies on REAL IWM option prices over 24 months, with Easter's exit rules.

  1. VWAP bounce        price holding above (below) VWAP pulls back, touches it and closes back with a strong candle
  2. Reclaim / reject   price below VWAP all morning breaks back above and holds (or the opposite)
  3. Stretch            price stretched past VWAP's outer band -> bet it snaps back toward VWAP
  4. VWAP filter        today's bot setups, but calls only above VWAP and puts only below; same for Gap Fade

VWAP is built from the 15-min bars (typical price x volume), resetting at 9:30 each day.
One IWM trade at a time like the live bot. First 2/3 = train, last 1/3 = unseen. R = $25.
"""
import datetime as dt
import json
import math
import os
import time

import vault_options as vo
import options_inspector as oi
import iwm_options_lab as L
import iwm_study as S
import entry_lab as E

UTC, ET = dt.timezone.utc, vo.LOCAL_TZ
SYM = "IWM"
KEY = "her_s40"


def vwap_days(m15):
    """For each bar (by index): VWAP, band width (std) and the day's bar position, using bars up to and including it."""
    out = {}
    for d, bars in S.by_day(m15).items():
        pv = vv = pv2 = 0.0
        for k, b in enumerate(bars):
            tp = (b["h"] + b["l"] + b["c"]) / 3
            v = max(b.get("v", 0), 1.0)
            pv += tp * v; vv += v; pv2 += tp * tp * v
            vw = pv / vv
            sd = math.sqrt(max(pv2 / vv - vw * vw, 0))
            out[b["ts"]] = {"vwap": vw, "sd": sd, "k": k, "day": d}
    return out


def strong(b, d):
    body, rng = (b["c"] - b["o"]) * d, b["h"] - b["l"]
    return body > 0 and rng > 0 and body >= 0.5 * rng


def signals(m15, VW, start_ts):
    """All four strategy families -> {name: [setups]}"""
    S1, S1s, S2, S2h, S3a, S3b = [], [], [], [], [], []
    days = S.by_day(m15)
    for d, bars in sorted(days.items()):
        if not bars or bars[0]["ts"] < start_ts:
            continue
        done = set()
        for k, b in enumerate(bars):
            end = b["ts"] + 900
            e = dt.datetime.fromtimestamp(end, ET)
            hm = (e.hour, e.minute)
            w = VW[b["ts"]]
            vw, sd = w["vwap"], w["sd"]
            prev = [VW[x["ts"]] for x in bars[max(0, k - 4):k]]
            # 1) bounce: last 4 closes on one side, this bar touches VWAP and closes back strong
            if (10, 15) <= hm <= (14, 30) and len(prev) == 4:
                for D in (1, -1):
                    side = all((x["c"] - p["vwap"]) * D > 0 for x, p in zip(bars[k - 4:k], prev))
                    touch = (b["l"] <= vw) if D == 1 else (b["h"] >= vw)
                    if side and touch and (b["c"] - vw) * D > 0 and strong(b, D) and ("b", D) not in done:
                        sig = {"ts": end, "dir": D, "px": b["c"], "j": b["ts"]}
                        S1.append(sig)
                        if (vw - prev[0]["vwap"]) * D > 0:          # VWAP itself sloping our way
                            S1s.append(sig)
                        done.add(("b", D))
            # 2) reclaim / reject: below (above) VWAP for every close so far (at least 4), now closes back across
            if (10, 30) <= hm <= (13, 0) and k >= 4:
                for D in (1, -1):
                    before = ("r", D) not in done and all((x["c"] - VW[x["ts"]]["vwap"]) * D < 0 for x in bars[:k])
                    if before and (b["c"] - vw) * D > 0:
                        S2.append({"ts": end, "dir": D, "px": b["c"], "j": b["ts"]})
                        done.add(("r", D))
                    # "holds": crossed on the previous bar and this one also closes on the new side
                    if k >= 5 and ("rh", D) not in done:
                        p = bars[k - 1]
                        before2 = all((x["c"] - VW[x["ts"]]["vwap"]) * D < 0 for x in bars[:k - 1])
                        if before2 and (p["c"] - VW[p["ts"]]["vwap"]) * D > 0 and (b["c"] - vw) * D > 0:
                            S2h.append({"ts": end, "dir": D, "px": b["c"], "j": b["ts"]})
                            done.add(("rh", D))
            # 3) stretch: close beyond VWAP +- 2 (2.5) bands -> fade back toward VWAP
            if (10, 0) <= hm <= (14, 30) and sd > 0 and k >= 2:
                z = (b["c"] - vw) / sd
                for lim, lst, tag in ((2.0, S3a, "s2"), (2.5, S3b, "s25")):
                    if abs(z) >= lim and (tag, 0) not in done:
                        lst.append({"ts": end, "dir": -1 if z > 0 else 1, "px": b["c"], "j": b["ts"]})
                        done.add((tag, 0))
    return {"1. VWAP bounce": S1, "1. VWAP bounce, VWAP sloping our way": S1s,
            "2. Reclaim/reject (first close back across)": S2, "2. Reclaim/reject, holds 2 candles": S2h,
            "3. Stretch past 2 bands -> snap back": S3a, "3. Stretch past 2.5 bands -> snap back": S3b}


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
    VW = vwap_days(m15)
    vw_at_close = {b["ts"] + 900: VW[b["ts"]] for b in m15}
    idx_of = {b["ts"]: j for j, b in enumerate(m15)}
    log = [f"{len(m15)} IWM 15-min bars (volume feed: {vo.STOCK_FEED})"]
    rp = L.RealPrices(api)

    def priced(sigs):
        out = []
        for g in sigs:
            j = idx_of.get(g["j"])
            rv = oi.realized_vol(m15, j) if j is not None else None
            if not rv:
                continue
            iv = max(oi.IV_FLOOR, rv * oi.IV_MULT)
            ct, _ = oi.pick(SYM, g["dir"], g["px"], iv, dt.datetime.fromtimestamp(g["ts"], UTC))
            if not ct:
                continue
            ro = E.real_from_open(rp, ct, g["ts"])
            if ro and KEY in ro:
                out.append({"ts": g["ts"], "dir": g["dir"], "real": ro})
        return out

    # today's bot setups (for the split and the filter test)
    setups = oi.replay(SYM, m15, h1, start.timestamp(), log)
    close_at = {b["ts"] + 900: b for b in m15}
    rows = []
    for s in setups:
        if not s.get("contract") or not s["approved"]:
            continue
        b = close_at.get(s["ts"])
        ct, _ = oi.pick(SYM, s["dir"], b["c"], s["iv"], dt.datetime.fromtimestamp(s["ts"], UTC)) if b else (None, None)
        if not ct:
            continue
        ro = E.real_from_open(rp, ct, s["ts"])
        if ro and KEY in ro:
            w = vw_at_close.get(s["ts"])
            rows.append({"ts": s["ts"], "dir": s["dir"], "real": ro, "px": b["c"], "vwap": w["vwap"] if w else None})
    rows.sort(key=lambda r: r["ts"])
    split = rows[0]["ts"] + (rows[-1]["ts"] - rows[0]["ts"]) * 2 / 3
    res = lambda taken, key=KEY: S.summary(taken, key, split)
    print(f"{len(rows)} bot setups priced ({rp.calls} calls)", flush=True)

    out = {}
    out["Today's bot (council approves, your exits)"] = res(E.sequential(rows, KEY))
    out["4. Today's bot + VWAP filter (calls above, puts below)"] = res(E.sequential(
        rows, KEY, keep=lambda r, c: r["vwap"] is not None and (r["px"] - r["vwap"]) * r["dir"] > 0))
    out["4. Today's bot + VWAP filter + no chasing"] = res(E.sequential(
        rows, KEY, keep=lambda r, c: r["vwap"] is not None and (r["px"] - r["vwap"]) * r["dir"] > 0 and r["real"]["run_up"] < 0.5,
        no_reentry=True))

    # Gap Fade (live: 0.8% gap, enter 10:00) with and without VWAP
    gs = [g for g in E.gap_fade_setups(m15, iwm_d, prev_day, 0.008, (10, 0), False) if g["ts"] >= start.timestamp()]
    for g in gs:
        w = vw_at_close.get(g["ts"])
        g["vwap"] = w["vwap"] if w else None
        bar = close_at.get(g["ts"])
        g["j"] = bar["ts"] if bar else None
    gp = []
    for g in gs:
        if g["j"] is None:
            continue
        ct, _ = oi.pick(SYM, g["dir"], g["px"], g["iv"], dt.datetime.fromtimestamp(g["ts"], UTC))
        if not ct:
            continue
        ro = E.real_from_open(rp, ct, g["ts"])
        if ro and KEY in ro:
            gp.append({"ts": g["ts"], "dir": g["dir"], "real": ro, "px": g["px"], "vwap": g["vwap"]})
    out["Gap Fade as live (0.8% gap, 10:00)"] = res(E.sequential(gp, KEY))
    out["4. Gap Fade + price already crossed VWAP our way"] = res(E.sequential(
        gp, KEY, keep=lambda r, c: r["vwap"] is not None and (r["px"] - r["vwap"]) * r["dir"] > 0))
    out["4. Gap Fade + price still on the gap side of VWAP"] = res(E.sequential(
        gp, KEY, keep=lambda r, c: r["vwap"] is not None and (r["px"] - r["vwap"]) * r["dir"] < 0))
    print("filters done", flush=True)

    for name, sigs in signals(m15, VW, start.timestamp()).items():
        pr = priced(sigs)
        out[name] = res(E.sequential(pr, KEY)) if pr else {"n": 0}
        if pr:
            out[name + " (-30% stop)"] = res(E.sequential(pr, "her_s30"), "her_s30")
        print(name, len(sigs), "signals", flush=True)

    log.append(f"Finished in {(time.time() - t0) / 60:.1f} min, {rp.calls} option price calls")
    json.dump({"when": t0, "split": dt.datetime.fromtimestamp(split, ET).strftime("%b %d, %Y"), "results": out, "log": log},
              open("vwap_lab.json", "w"), indent=1, default=str)
    for k, v in out.items():
        if v.get("n"):
            print(f"{k}: n={v['n']} win={v['win']} avg={v['avg_r']} first={v['first']} unseen={v['unseen']} (n={v['n_unseen']}) "
                  f"${v['usd']} unseen ${v['usd_unseen']} months {v['months_up']}/{v['months']} dd {v['worst_drop']}")
    print("\n".join(log))


if __name__ == "__main__":
    main()
