import datetime as dt, json, os
os.environ["PROP_DRY"] = "1"
import prop_alerts as P
out = {}
for name, sym, vest in P.FVG_MARKETS:
    bars = P.alpaca_bars(sym, "15Min", 120, False)
    ev = P.fvg_sim(bars)
    ex = [e for e in ev if e["kind"] == "exit"]
    out[sym] = {"bars": len(bars), "events": len(ev), "trades": len(ex), "avg_r": round(sum(e["r"] for e in ex) / len(ex), 3) if ex else None,
                "last": [{"kind": e["kind"], "t": dt.datetime.fromtimestamp(e["t"], P.ET).strftime("%m-%d %H:%M"), "r": round(e.get("r", 0), 2)} for e in ev[-6:]],
                "sample": [P.fvg_message(name, sym, vest, e) for e in ev if e["kind"] in ("setup", "exit")][-2:]}
json.dump(out, open("fvg_alert_check.json", "w"), indent=1)
print(json.dumps(out, indent=1))
