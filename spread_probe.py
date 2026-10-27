"""Probe: can the paper account place and cancel a 2-leg (mleg) debit spread order?"""
import datetime as dt, json, os, time
import vault_options_spread as vo
api = vo.Alpaca(os.environ["ALPACA_KEY_ID"].strip(), os.environ["ALPACA_SECRET_KEY"].strip())
out = {}
now = dt.datetime.now(dt.timezone.utc)
for pair, price_key in (("AMZN", None), ("DIA", None)):
    bars = api.bars([pair], "5Min", 3)[pair]
    px = bars[-1]["c"]
    p, why = vo.pick_spread(api, pair, 1, px, px * 1.005, 228, now)
    single, why1 = vo.pick_contract(api, pair, 1, px, 228, now)
    out[pair] = {"price": px, "single": single, "single_why": why1, "spread": p, "spread_why": why}
p = out["AMZN"]["spread"] or out["DIA"]["spread"]
if p:
    try:
        r = api.buy_spread(p["sym"], p["short"], 1, 0.01)
        time.sleep(2)
        o = api.order(r["id"])
        out["order"] = {k: o.get(k) for k in ("status", "order_class", "type", "limit_price", "qty")}
        out["legs"] = [{k: l.get(k) for k in ("symbol", "side", "position_intent", "status")} for l in (o.get("legs") or [])]
        try:
            api.cancel(r["id"]); out["cancel"] = "ok"
        except vo.ApiError as e:
            out["cancel"] = str(e)
    except vo.ApiError as e:
        out["order_error"] = str(e)
print(json.dumps(out, indent=1, default=str))
json.dump(out, open("spread_probe.json", "w"), indent=1, default=str)
