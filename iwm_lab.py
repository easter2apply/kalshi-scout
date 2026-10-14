"""IWM Lab: can IWM put spreads grow a small account ($500-$1,000) to $2,000 so it can trade the SPY $2-wide setup?"""
import os
os.environ.setdefault("LAB_SYMS", "SPY,IWM")
import datetime as dt, itertools, json, statistics as stt, sys, time
import premium_lab as pl, vault_options as vo

SPY_WIN = dict(fam="put", sym="SPY", dte=0, delta=0.30, width=2, tp=None, stop=None, filt="trend", x21=False)


def main():
    t0 = time.time()
    api = vo.Alpaca(os.environ["ALPACA_KEY_ID"].strip(), os.environ["ALPACA_SECRET_KEY"].strip())
    daily, intra, vol, log = pl.load_data(api)
    mk = pl.Market(daily, intra, vol)
    real = pl.Real(api)
    cal = pl.calibrate(mk, real, log)
    rows, trades, paths = [], {}, {}
    for dte in (0, 30, 45):
        kind = "0dte" if dte == 0 else "swing"
        for delta in pl.DELTAS:
            for width in ([1, 2] if dte == 0 else [1, 2, 5]):
                k = ("IWM", dte, "put", delta, width)
                paths[k] = (pl.zero_paths(mk, "IWM", delta, width, "put", cal[("IWM", kind)]) if dte == 0 else
                            pl.swing_paths(mk, "IWM", dte, delta, width, "put", cal[("IWM", kind)]))
                x21s = [False, True] if dte else [False]
                for tp, st, f, x in itertools.product(pl.TPS, pl.STOPS, pl.FILTERS, x21s):
                    v = dict(fam="put", sym="IWM", dte=dte, delta=delta, width=width, tp=tp, stop=st, filt=f, x21=x)
                    ts = pl.run_version(v, paths[k], mk)
                    if len(ts) < 30:
                        continue
                    r = pl.summarize(v, ts)
                    r["id"] = len(rows)
                    rows.append(r)
                    trades[r["id"]] = ts
    log.append(f"IWM: {len(rows)} versions in {(time.time() - t0) / 60:.1f} min")
    print(log[-1], flush=True)

    def pick(cond):
        ok = [r for r in rows if cond(r["v"]) and r["train"]["n"] >= 40]
        return max(ok, key=lambda r: r["train"]["avg"]) if ok else None
    same = lambda w: next((r for r in rows if r["v"] == dict(SPY_WIN, sym="IWM", width=w)), None)
    checks = {"IWM 0DTE $1 best": pick(lambda v: v["dte"] == 0 and v["width"] == 1),
              "IWM 0DTE $2 best": pick(lambda v: v["dte"] == 0 and v["width"] == 2),
              "IWM 30-45 day $1-2 best": pick(lambda v: v["dte"] > 0 and v["width"] <= 2),
              "IWM same rules as SPY winner, $1": same(1),
              "IWM same rules as SPY winner, $2": same(2)}
    held = [r for r in rows if (r["train"]["avg"] or -1) > 0.02 and (r["test"]["avg"] or -1) > 0.02 and r["test"]["n"] >= 30]
    out = {"log": log, "cal": {f"{k[0]}-{k[1]}": v for k, v in cal.items()}, "versions": len(rows),
           "pos_test": sum(1 for r in rows if (r["test"]["avg"] or -1) > 0 and r["test"]["n"] >= 30),
           "n_test": sum(1 for r in rows if r["test"]["n"] >= 30), "held": len(held), "checks": {}}
    real_trades = {}
    for name, r in checks.items():
        if not r:
            continue
        v = r["v"]
        rt, skipped = pl.real_replay(v, paths[(v["sym"], v["dte"], v["fam"], v["delta"], v["width"])], mk, real)
        rt.sort(key=lambda t: t["d"])
        real_trades[name] = rt
        c = {k: r[k] for k in ("label", "train", "test", "years", "usd", "dd")}
        c["real"] = {"stats": pl.stats([t["r"] for t in rt]), "model": pl.stats([t["model_r"] for t in rt]), "skipped": skipped,
                     "avg_usd": round(stt.mean(t["usd"] for t in rt), 2) if rt else None,
                     "avg_risk": round(stt.mean(t["risk"] for t in rt), 1) if rt else None,
                     "per_month": round(len(rt) / max((rt[-1]["d"] - rt[0]["d"]).days / 30.4, 1), 1) if rt else None}
        for cash in (500, 1000):
            ac = pl.account(rt, cash)
            c["real"][f"acct_{cash}"] = {k: ac[k] for k in ("end", "dd", "months_up", "months", "skipped")} if ac else None
        out["checks"][name] = c
        print(name, json.dumps(c["real"]), flush=True)

    # SPY winner on real prices, for the ladder
    sp = pl.zero_paths(mk, "SPY", 0.30, 2, "put", cal[("SPY", "0dte")])
    spy_rt, _ = pl.real_replay(SPY_WIN, sp, mk, real)
    spy_by_day = {t["d"]: t for t in spy_rt}

    # Ladder: trade the IWM pick until the account reaches $2,000, then switch to SPY $2 wide
    def ladder(iwm_rt, cash, start):
        by_day = {t["d"]: t for t in iwm_rt}
        eq, peak, dd, hit = cash, cash, 0.0, None
        for d in sorted(set(by_day) | set(spy_by_day)):
            if d < start:
                continue
            use_spy = eq >= 2000
            t = spy_by_day.get(d) if use_spy else by_day.get(d)
            if not t or t["risk"] <= 0:
                continue
            n = int(0.10 * eq // t["risk"]) or (1 if t["risk"] <= 0.25 * eq else 0)
            if n == 0:
                continue
            eq += t["usd"] * n
            if eq >= 2000 and hit is None:
                hit = d
            peak, dd = max(peak, eq), min(dd, (eq - peak) / peak)
        return eq, hit, dd

    out["ladder"] = {}
    for name, rt in real_trades.items():
        if not rt or rt[0]["out"] != rt[0]["d"]:
            continue                                    # ladder only for same-day trades
        for cash in (500, 1000):
            runs = []
            months = sorted({(t["d"].year, t["d"].month) for t in rt})
            for y, m in months[:-3]:
                eq, hit, dd = ladder(rt, cash, dt.date(y, m, 1))
                runs.append({"start": f"{y}-{m:02d}", "end": round(eq), "months_to_2k":
                             round((hit - dt.date(y, m, 1)).days / 30.4, 1) if hit else None, "dd": round(dd * 100)})
            got = [x["months_to_2k"] for x in runs if x["months_to_2k"] is not None]
            out["ladder"][f"{name} from ${cash}"] = {
                "starts": len(runs), "reached_2k": len(got), "median_months": stt.median(got) if got else None,
                "blown_below_half": sum(1 for x in runs if x["end"] < cash / 2), "runs": runs}
            print(name, cash, out["ladder"][f"{name} from ${cash}"]["reached_2k"], "of", len(runs),
                  "median months", out["ladder"][f"{name} from ${cash}"]["median_months"], flush=True)
    json.dump(out, open("iwm_lab.json", "w"), indent=1, default=str)
    print("\n".join(log))


if __name__ == "__main__":
    main()
