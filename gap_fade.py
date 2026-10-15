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
                    body={"chat_id": chat, "text": "🪃 Boomer · Gap Fade\n" + text, "disable_web_page_preview": True}, tries=2)
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


def mood(st):
    """What Boomer is doing right now: (state, speech bubble)."""
    t, now = st["trade"], dt.datetime.now(dt.timezone.utc)
    e = vo.et(now)
    today = e.date().isoformat()
    day = st["days"].get(today, {})
    last = st["closed"][-1] if st["closed"] else None
    if t:
        chg = (t.get("price", t["entry"]) / t["entry"] - 1) if t["entry"] else 0
        side = "the gap up to fade" if t["dir"] == -1 else "the gap down to bounce back"
        return "flying", (f"Boomerang's in the air! {'Put' if t['dir'] == -1 else 'Call'} on IWM, betting {side}. "
                          f"{chg:+.0%} so far. Stop ${t['sl']:.2f}.")
    if last and last.get("closed", "")[:10] == today:
        if last["pl"] > 0:
            return "win", f"It came back! {vo.money(last['pl'])} today. That's how a gap fade works."
        return "loss", f"Didn't come back today: {vo.money(last['pl'])}. Stop did its job. Next gap."
    if e.weekday() >= 5:
        return "rest", "Weekend. Markets are closed, so I'm off the clock. Back Monday at 10."
    if day.get("action"):
        g = day.get("gap", 0)
        if abs(g) < MIN_GAP * 100:
            return "shrug", f"IWM opened {g:+.2f}% from yesterday. Not a big enough gap (I need {MIN_GAP:.1%}). No throw today."
        return "shrug", day["action"][:140]
    if (e.hour, e.minute) < ENTER_FROM:
        return "watch", "Watching the open. If IWM gaps 0.8% or more, I throw at 10:00."
    if (e.hour, e.minute) >= (16, 0):
        return "rest", "Market's closed. I'll check the gap again tomorrow at 10."
    return "watch", "Checking today's gap…"


CHAR_JS = r"""
(function(){
const el=document.getElementById('stage');const W=()=>el.clientWidth,H=()=>el.clientHeight;
const r=new THREE.WebGLRenderer({antialias:true,alpha:true});r.setPixelRatio(Math.min(2,devicePixelRatio));r.setSize(W(),H());el.appendChild(r.domElement);
const sc=new THREE.Scene();const cam=new THREE.PerspectiveCamera(38,W()/H(),0.1,100);cam.position.set(0,2.1,6.8);cam.lookAt(-.1,1.8,0);
sc.add(new THREE.HemisphereLight(0xbfd8ff,0x1a1030,0.9));const sun=new THREE.DirectionalLight(0xffd2a0,0.9);sun.position.set(3,6,4);sc.add(sun);
const rim=new THREE.PointLight(0x38e1ff,0.8,12);rim.position.set(-3,3,-2);sc.add(rim);
const M=(c,o)=>new THREE.MeshStandardMaterial(Object.assign({color:c,roughness:.6,metalness:.05},o||{}));
// rooftop + city glow
const floor=new THREE.Mesh(new THREE.CylinderGeometry(3.2,3.2,.2,48),M(0x232845));floor.position.y=-.1;sc.add(floor);
const ring=new THREE.Mesh(new THREE.TorusGeometry(3.2,.04,8,64),M(0x38e1ff,{emissive:0x38e1ff,emissiveIntensity:.6}));ring.rotation.x=Math.PI/2;sc.add(ring);
for(let i=0;i<14;i++){const h=1+Math.random()*3;const b=new THREE.Mesh(new THREE.BoxGeometry(.7,h,.7),M(0x151a33,{emissive:0x2a2f66,emissiveIntensity:.25}));
 const a=Math.PI*(0.15+0.7*i/13);b.position.set(Math.cos(a)*-7.5,h/2-1,-Math.sin(a)*6-3);sc.add(b);}
// gap meter screen
const cv=document.createElement('canvas');cv.width=1024;cv.height=640;const g=cv.getContext('2d');const tex=new THREE.CanvasTexture(cv);tex.anisotropy=4;
const scr=new THREE.Mesh(new THREE.PlaneGeometry(2.4,1.5),new THREE.MeshBasicMaterial({map:tex}));scr.position.set(.95,1.85,-.4);scr.rotation.y=-.2;sc.add(scr);
const frame=new THREE.Mesh(new THREE.BoxGeometry(2.5,1.6,.06),M(0x0b0e1c,{emissive:0x38e1ff,emissiveIntensity:.08}));frame.position.copy(scr.position);frame.position.z-=.04;frame.rotation.y=scr.rotation.y;sc.add(frame);
const pole=new THREE.Mesh(new THREE.CylinderGeometry(.06,.06,.8,10),M(0x40465f));pole.position.set(1.2,.4,-.6);sc.add(pole);
function rr(x,y,w,h,r,c){g.fillStyle=c;g.beginPath();g.moveTo(x+r,y);g.arcTo(x+w,y,x+w,y+h,r);g.arcTo(x+w,y+h,x,y+h,r);g.arcTo(x,y+h,x,y,r);g.arcTo(x,y,x+w,y,r);g.fill();}
function txt(t,x,y,size,c,w,al){g.font=(w||'600')+' '+size+'px system-ui,sans-serif';g.fillStyle=c;g.textAlign=al||'left';g.fillText(t,x,y);g.textAlign='left';}
const MU='#8f97b8',OK='#45e39b',BAD='#ff6378',AC='#ffb547',CY='#38e1ff',FG='#eef0fa';
function money(v){return (v>=0?'+$':'−$')+Math.abs(v).toFixed(2);}
function drawScreen(){const L=DATA.live||{},v=DATA.gap,big=v!=null&&Math.abs(v)>=DATA.min,T=DATA.trade;
 g.fillStyle='#0a0f22';g.fillRect(0,0,1024,640);g.fillStyle=CY;g.fillRect(0,0,1024,8);
 txt('IWM GAP DESK',40,70,40,CY,'800');
 txt("TODAY'S GAP",40,150,34,MU,'700');txt(v==null?'—':(v>0?'+':'')+v.toFixed(2)+'%',40,260,120,v==null?MU:(big?AC:OK),'900');
 txt(v==null?'waiting':big?'THROW':'NO THROW',984,260,56,big?AC:MU,'900','right');
 const f=L.filled;txt('GAP FILLED',40,340,34,MU,'700');rr(40,362,944,44,22,'#1d2444');
 if(f!=null){rr(40,362,Math.max(44,944*Math.max(0,Math.min(1,f))),44,22,f>=1?OK:CY);txt((f*100).toFixed(0)+'%',984,460,56,f>=1?OK:FG,'900','right');}
 if(T){const pc=T.now/T.entry-1;txt('TRADE',40,540,34,MU,'700');txt((pc>=0?'+':'')+(pc*100).toFixed(0)+'%',40,615,80,pc>=0?OK:BAD,'900');}
 else{txt('RECORD '+DATA.rec,40,560,44,FG,'800');txt(money(DATA.pl),40,620,48,DATA.pl>=0?OK:BAD,'900');}
 tex.needsUpdate=true;}
drawScreen();
// Boomer
const B=new THREE.Group();sc.add(B);B.position.set(-1.25,0,.6);
const skin=M(0x8d5a3b),hood=M(0x14b8a6),pants=M(0x2b3150),shoe=M(0xf2f2f2),cap=M(0xffb547);
const legL=new THREE.Mesh(new THREE.CylinderGeometry(.13,.12,.8,12),pants);legL.position.set(-.16,.4,0);B.add(legL);
const legR=legL.clone();legR.position.x=.16;B.add(legR);
[legL,legR].forEach(l=>{const s=new THREE.Mesh(new THREE.BoxGeometry(.24,.12,.36),shoe);s.position.set(0,-.4,.06);l.add(s);});
const body=new THREE.Mesh(new THREE.CylinderGeometry(.36,.42,.95,20),hood);body.position.y=1.28;B.add(body);
const pocket=new THREE.Mesh(new THREE.BoxGeometry(.4,.16,.05),M(0x0f8f82));pocket.position.set(0,1.08,.4);B.add(pocket);
const head=new THREE.Group();head.position.y=2.05;B.add(head);
head.add(new THREE.Mesh(new THREE.SphereGeometry(.33,24,24),skin));
const eyeM=M(0x111111);[-.11,.11].forEach(x=>{const e=new THREE.Mesh(new THREE.SphereGeometry(.045,10,10),eyeM);e.position.set(x,.04,.3);head.add(e);});
const smile=new THREE.Mesh(new THREE.TorusGeometry(.1,.018,6,16,Math.PI),M(0x3a1e12));smile.rotation.z=Math.PI;smile.position.set(0,-.09,.3);head.add(smile);
const capTop=new THREE.Mesh(new THREE.SphereGeometry(.345,24,12,0,Math.PI*2,0,Math.PI/2),cap);capTop.position.y=.04;head.add(capTop);
const brim=new THREE.Mesh(new THREE.BoxGeometry(.36,.04,.3),cap);brim.position.set(0,.06,-.38);head.add(brim);   // backwards cap
const mic=new THREE.Mesh(new THREE.TorusGeometry(.36,.02,6,20,Math.PI*.9),M(0x222));mic.rotation.set(0,Math.PI/2,0);mic.position.y=.05;head.add(mic);
function arm(side){const sh=new THREE.Group();sh.position.set(side*.45,1.65,0);B.add(sh);
 const a=new THREE.Mesh(new THREE.CylinderGeometry(.09,.08,.7,10),hood);a.position.y=-.35;sh.add(a);
 const h=new THREE.Mesh(new THREE.SphereGeometry(.1,12,12),skin);h.position.y=-.72;sh.add(h);return sh;}
const armL=arm(-1),armR=arm(1);
// boomerang
function boomerang(){const gr=new THREE.Group();const m=M(0xffb547,{emissive:0x7a4a00,emissiveIntensity:.35});
 const a=new THREE.Mesh(new THREE.BoxGeometry(.6,.05,.13),m);a.position.x=.25;a.rotation.y=.5;gr.add(a);
 const b=a.clone();b.position.x=-.25;b.rotation.y=-.5;gr.add(b);const c=new THREE.Mesh(new THREE.CylinderGeometry(.08,.08,.05,12),m);c.position.z=-.12;gr.add(c);return gr;}
const boom=boomerang();sc.add(boom);
// confetti
const conf=[];function burst(){for(let i=0;i<80;i++){const p=new THREE.Mesh(new THREE.PlaneGeometry(.07,.12),new THREE.MeshBasicMaterial({color:[0x45e39b,0xffb547,0x38e1ff,0xff6ad5][i%4],side:2}));
 p.position.set(-1.2+(Math.random()-.5)*4,3+Math.random()*2.5,(Math.random()-.5)*2);p.userData.v=new THREE.Vector3((Math.random()-.5)*.02,-.005-Math.random()*.01,(Math.random()-.5)*.02);sc.add(p);conf.push(p);}}
if(DATA.state==='win')burst();
const bubble=document.getElementById('bubble');bubble.textContent=DATA.say;
let t0=performance.now();
function tick(now){const t=(now-t0)/1000;const S=DATA.state;
 B.position.y=Math.sin(t*2)*.03;head.rotation.y=Math.sin(t*.7)*.25;
 armL.rotation.z=-.15;armR.rotation.z=.15;armL.rotation.x=armR.rotation.x=0;head.rotation.x=0;
 const hand=new THREE.Vector3();
 if(S==='flying'){armR.rotation.x=-2.4+Math.sin(t*3)*.1;const a=t*1.4;boom.position.set(Math.cos(a)*2.4-1.2,1.9+Math.sin(a*2)*.35,Math.sin(a)*1.6+.2);boom.rotation.set(Math.PI/2,0,t*12);head.rotation.y=Math.atan2(boom.position.x+1.25,boom.position.z+2)*.6;}
 else{ if(S==='win'){armL.rotation.z=-2.6+Math.sin(t*8)*.15;armR.rotation.z=2.6-Math.sin(t*8)*.15;B.position.y=Math.abs(Math.sin(t*5))*.18;}
  else if(S==='loss'){head.rotation.x=.45;armL.rotation.z=-.05;armR.rotation.z=.05;}
  else if(S==='shrug'){const s=(Math.sin(t*2.2)+1)/2;armL.rotation.z=-.6-s*.5;armR.rotation.z=.6+s*.5;}
  else if(S==='rest'){head.rotation.x=.2;}
  else{armR.rotation.x=-.4+Math.sin(t*1.5)*.08;head.rotation.y=Math.sin(t*.6)*.6;}
  armR.children[1].getWorldPosition(hand);boom.position.copy(hand);boom.position.y+=.05;boom.rotation.set(0,t*.5,0);}
 for(const p of conf){p.position.add(p.userData.v);p.userData.v.y-=.0004;p.rotation.x+=.1;p.rotation.y+=.07;if(p.position.y<0)p.position.y=3+Math.random()*2.5;}
 cam.position.x=Math.sin(t*.15)*.25;cam.lookAt(-.1,1.8,0);r.render(sc,cam);requestAnimationFrame(tick);}
requestAnimationFrame(tick);
addEventListener('resize',()=>{cam.aspect=W()/H();cam.updateProjectionMatrix();r.setSize(W(),H());});
})();
"""


CHART_JS = r"""
(function(){
const L=DATA.live||{},bars=(L.ybars||[]).map(b=>b.concat([1])).concat((L.bars||[]).map(b=>b.concat([0])));
const cv=document.getElementById('chart'),notes=document.getElementById('notes');
if(!bars.length||!L.prev){cv.style.display='none';notes.innerHTML='<div>The chart fills in once the market opens.</div>';return;}
const dpr=Math.min(2,devicePixelRatio||1),Wd=cv.clientWidth,Hd=cv.clientHeight;cv.width=Wd*dpr;cv.height=Hd*dpr;const g=cv.getContext('2d');g.scale(dpr,dpr);
const pad={l:8,r:84,t:18,b:26},W=Wd-pad.l-pad.r,H=Hd-pad.t-pad.b;
let lo=Math.min(...bars.map(b=>b[3]),L.prev,L.open),hi=Math.max(...bars.map(b=>b[2]),L.prev,L.open);const m=(hi-lo)*.12||0.5;lo-=m;hi+=m;
const Y=p=>pad.t+(hi-p)/(hi-lo)*H,n=bars.length,bw=W/Math.max(n,26),X=i=>pad.l+bw*(i+.5);
const C={mu:'#8f97b8',ok:'#45e39b',bad:'#ff6378',ac:'#ffb547',cy:'#38e1ff',fg:'#eef0fa',line:'#262c45'};
const font=(s,w)=>g.font=(w||600)+' '+s+'px system-ui,sans-serif';
// grid + price axis
g.strokeStyle=C.line;g.lineWidth=1;font(11);g.fillStyle=C.mu;
for(let k=0;k<=4;k++){const p=lo+(hi-lo)*k/4,y=Y(p);g.beginPath();g.moveTo(pad.l,y);g.lineTo(pad.l+W,y);g.stroke();g.fillText(p.toFixed(2),pad.l+W+6,y+4);}
// yesterday / today divider
const first=bars.findIndex(b=>!b[6]);if(first>0){const x=pad.l+bw*first;g.setLineDash([2,4]);g.strokeStyle=C.mu;g.beginPath();g.moveTo(x,pad.t);g.lineTo(x,pad.t+H);g.stroke();g.setLineDash([]);
 font(11);g.fillStyle=C.mu;g.fillText('yesterday',pad.l+2,pad.t+10);g.fillText('today',x+4,pad.t+H-4);}
// the gap zone
const up=L.open>L.prev,gx=first>0?pad.l+bw*first:pad.l;g.fillStyle=up?'rgba(255,99,120,.13)':'rgba(69,227,155,.13)';
g.fillRect(gx,Math.min(Y(L.prev),Y(L.open)),pad.l+W-gx,Math.abs(Y(L.prev)-Y(L.open)));
function hline(p,c,label,dash){const y=Y(p);g.strokeStyle=c;g.lineWidth=1.5;g.setLineDash(dash||[]);g.beginPath();g.moveTo(pad.l,y);g.lineTo(pad.l+W,y);g.stroke();g.setLineDash([]);
 font(10,700);const tw=g.measureText(label).width+8;g.fillStyle=c;g.fillRect(pad.l+W+2,y-8,tw,16);g.fillStyle='#0d0f1a';g.fillText(label,pad.l+W+6,y+4);}
// VWAP (today)
let pv=0,vv=0;const vw=[];bars.forEach((b,i)=>{if(b[6])return;const tp=(b[2]+b[3]+b[4])/3;pv+=tp*(b[5]||1);vv+=(b[5]||1);vw.push([i,pv/vv]);});
if(vw.length>1){g.strokeStyle='#b388ff';g.lineWidth=1.5;g.beginPath();vw.forEach(([i,p],k)=>k?g.lineTo(X(i),Y(p)):g.moveTo(X(i),Y(p)));g.stroke();font(10,700);g.fillStyle='#b388ff';g.fillText('VWAP',X(vw[vw.length-1][0])-34,Y(vw[vw.length-1][1])-6);}
// candles
bars.forEach((b,i)=>{const [t,o,h,l,c]=b,col=c>=o?C.ok:C.bad;g.globalAlpha=b[6]?.45:1;g.strokeStyle=col;g.lineWidth=1;g.beginPath();g.moveTo(X(i),Y(h));g.lineTo(X(i),Y(l));g.stroke();
 g.fillStyle=col;const y1=Y(Math.max(o,c)),y2=Y(Math.min(o,c));g.fillRect(X(i)-bw*.32,y1,bw*.64,Math.max(1,y2-y1));g.globalAlpha=1;});
hline(L.prev,C.ac,'fill '+L.prev.toFixed(2),[6,4]);hline(L.open,C.cy,'open '+L.open.toFixed(2));
const last=bars[bars.length-1][4];hline(last,C.fg,'now '+last.toFixed(2),[2,3]);
// time labels for today
font(10);g.fillStyle=C.mu;bars.forEach((b,i)=>{if(!b[6]&&(b[0].endsWith(':00'))&&['10:00','12:00','14:00'].includes(b[0]))g.fillText(b[0].replace(/^0/,''),X(i)-12,pad.t+H+18);});
// throw window + trade marks
const iAt=t=>bars.findIndex(b=>!b[6]&&b[0]>=t);
const i10=iAt('10:00');if(i10>=0){const x=X(i10)-bw/2;g.strokeStyle=C.ac;g.setLineDash([4,4]);g.beginPath();g.moveTo(x,pad.t);g.lineTo(x,pad.t+H);g.stroke();g.setLineDash([]);font(10,800);g.fillStyle=C.ac;g.fillText('10:00 throw',x+4,pad.t+22);}
(DATA.marks||[]).forEach(mk=>{const i=iAt(mk.at);if(i<0)return;const b=bars[i],x=X(i);
 if(mk.kind==='in'){const y=mk.dir===-1?Y(b[2])-8:Y(b[3])+8;g.fillStyle=C.ac;g.beginPath();if(mk.dir===-1){g.moveTo(x,y);g.lineTo(x-7,y-12);g.lineTo(x+7,y-12);}else{g.moveTo(x,y);g.lineTo(x-7,y+12);g.lineTo(x+7,y+12);}g.fill();
  font(11,800);g.fillText(mk.dir===-1?'PUT':'CALL',x-14,mk.dir===-1?y-16:y+26);}
 else{g.fillStyle=mk.pl>0?C.ok:C.bad;g.beginPath();g.arc(x,Y(b[4]),6,0,7);g.fill();font(11,800);g.fillText((mk.pl>0?'+':'−')+'$'+Math.abs(mk.pl).toFixed(0),x+8,Y(b[4])+4);}});
// arrow: where a fade goes
if(Math.abs(DATA.gap||0)>=DATA.min&&(L.open-last)/(L.open-L.prev)<1){const x=pad.l+W-24,y1=Y(last),y2=Y(L.prev);g.strokeStyle=C.ac;g.lineWidth=2.5;g.beginPath();g.moveTo(x,y1);g.lineTo(x,y2+(y2>y1?-8:8));g.stroke();
 g.fillStyle=C.ac;g.beginPath();g.moveTo(x,y2);g.lineTo(x-6,y2+(y2>y1?-10:10));g.lineTo(x+6,y2+(y2>y1?-10:10));g.fill();}
// Boomer's notes
const out=[],gp=DATA.gap,fill=(L.open-last)/(L.open-L.prev);
out.push(`Gap ${up?'up':'down'} ${gp>0?'+':''}${(gp||0).toFixed(2)}%: opened at $${L.open.toFixed(2)} vs $${L.prev.toFixed(2)} yesterday. ${Math.abs(gp||0)>=DATA.min?'Big enough to throw.':'Under '+DATA.min.toFixed(1)+'%, so no throw.'}`);
if(isFinite(fill))out.push(fill>=1?'The gap is fully filled: price got all the way back to yesterday\'s close.':fill>0?`${(fill*100).toFixed(0)}% of the gap has filled. $${Math.abs(last-L.prev).toFixed(2)} to go to the fill line.`:`The gap is getting bigger, not filling (${(fill*100).toFixed(0)}%). The fade is against us so far.`);
const tb=bars.filter(b=>!b[6]);if(tb.length>=2){const fh=tb.slice(0,2),hh=Math.max(...fh.map(b=>b[2])),ll=Math.min(...fh.map(b=>b[3]));out.push(`First 30 minutes ranged $${ll.toFixed(2)}–$${hh.toFixed(2)}. Now ${last>hh?'above that range (pushing away from the fill)':last<ll?'below that range':'inside that range'}.`);}
if(vw.length){const v=vw[vw.length-1][1];out.push(`Price is ${last>v?'above':'below'} VWAP ($${v.toFixed(2)}): ${(last>v)===up?'buyers still control the gap side':'the move back toward the fill has momentum'}.`);}
notes.innerHTML=out.map(t=>'<div>'+t+'</div>').join('');
})();
"""


def page(st, out):
    t = st["trade"]
    rec = st["record"]
    state, line = mood(st)
    today = vo.et(dt.datetime.now(dt.timezone.utc)).date().isoformat()
    gap_today = st["days"].get(today, {}).get("gap")
    L = st.get("live") or {}
    live = {}
    if L.get("date") == today:
        live = {"prev": L.get("prev"), "open": L.get("open"), "now": L.get("now")}
        if L.get("prev") and L.get("open") and L.get("now") and L["open"] != L["prev"]:
            live["filled"] = round((L["open"] - L["now"]) / (L["open"] - L["prev"]), 3)
    tr = None
    if t:
        tr = {"name": vo.contract_name(t["sym"]),
              "entry": t["entry"], "now": t.get("price", t["entry"]), "sl": t["sl"], "locked": bool(t.get("locked"))}
    last = st["closed"][-1] if st["closed"] else None
    gaps = []
    for d_, v in sorted(st["days"].items())[-10:]:
        if v.get("gap") is None:
            continue
        c = next((c for c in st["closed"] if c.get("opened", "")[:10] == d_), None)
        gaps.append({"g": v["gap"], "t": bool(c) or "bought" in v.get("action", ""), "w": bool(c and c["pl"] > 0)})
    if L.get("date") == today:
        live.update(bars=L.get("bars", []), ybars=L.get("ybars", []))
    hm = lambda iso: vo.et(dt.datetime.fromisoformat(iso).replace(tzinfo=dt.timezone.utc)).strftime("%H:%M") if iso else None
    marks = []
    if t and t.get("opened", "")[:10] == today:
        marks.append({"at": hm(t["opened"]), "kind": "in", "dir": t["dir"]})
    for c in st["closed"]:
        if c.get("opened", "")[:10] == today:
            marks += [{"at": hm(c["opened"]), "kind": "in", "dir": c["dir"]}, {"at": hm(c.get("closed")), "kind": "out", "pl": c["pl"]}]
    data = json.dumps({"state": state, "say": line, "gap": gap_today, "min": MIN_GAP * 100, "live": live, "trade": tr, "marks": marks,
                       "last": (f"Last: {last['opened'][:10]} {vo.money(last['pl'])} ({last['why']})" if last else None),
                       "rec": f"{rec['wins']}W–{rec['losses']}L", "pl": rec["pl"], "gaps": gaps,
                       "clock": vo.et(dt.datetime.now(dt.timezone.utc)).strftime("%a %b %-d · %-I:%M %p ET")})
    rows = "".join(f"<tr><td>{html.escape(c['opened'][:10])}</td><td>{html.escape(vo.contract_name(c['sym']))}</td><td>{c['gap']:+.2f}%</td>"
                   f"<td>${c['entry']:.2f} → ${c['exit']:.2f}</td><td>{html.escape(c['why'])}</td>"
                   f"<td class='{'pos' if c['pl'] > 0 else 'neg'}'>{vo.money(c['pl'])}</td></tr>" for c in reversed(st["closed"][-40:]))
    days = "".join(f"<li><b>{d}</b> · {html.escape(v.get('action', 'waiting'))}</li>" for d, v in sorted(st["days"].items(), reverse=True)[:10])
    Lt = st.get("live") or {}
    filled = None
    if Lt.get("date") == today and Lt.get("open") and Lt.get("prev") and Lt["open"] != Lt["prev"]:
        filled = (Lt["open"] - Lt["now"]) / (Lt["open"] - Lt["prev"])
    tiles = [("Today's gap", "—" if gap_today is None else f"{gap_today:+.2f}%",
              "acc" if gap_today is not None and abs(gap_today) >= MIN_GAP * 100 else ""),
             ("Gap filled", "—" if filled is None else f"{filled:.0%}", "pos" if filled is not None and filled >= 1 else "")]
    if t:
        pc = t.get("price", t["entry"]) / t["entry"] - 1
        tiles += [("Option", f"${t['entry']:.2f} → ${t.get('price', t['entry']):.2f}", ""),
                  ("Trade P/L", f"{pc:+.0%} ({vo.money((t.get('price', t['entry']) - t['entry']) * 100 * t['qty'] + t.get('banked', 0))})", "pos" if pc >= 0 else "neg"),
                  ("Stop", f"${t['sl']:.2f}" + (" 🔐" if t.get("locked") else ""), ""), ("Sells by", "3:30 PM", "")]
    else:
        tiles += [("Trade", "None open", ""), ("Next check", "10:00 AM", "")]
    open_html = ("<h2>Today</h2><div class='tiles'>" + "".join(
        f"<div class='k'><span>{a_}</span><b class='{c_}'>{html.escape(b_)}</b></div>" for a_, b_, c_ in tiles) + "</div>"
        + (f"<p class='fine' style='margin:0'>{html.escape(vo.contract_name(t['sym']))} × {t['qty']}</p>" if t else ""))
    n = rec["wins"] + rec["losses"]
    open(out, "w", encoding="utf-8").write(f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gap Fade · Boomer</title><style>
:root{{--bg:#0d0f1a;--card:#161a2b;--fg:#eef0fa;--muted:#9aa2c2;--line:#262c45;--ok:#45e39b;--bad:#ff6378;--acc:#ffb547}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}}
.wrap{{max-width:760px;margin:0 auto;padding:16px;display:grid;gap:14px}}a{{color:#8fb4ff}}
.hero{{position:relative;height:380px;border-radius:16px;overflow:hidden;background:radial-gradient(ellipse at 50% 120%,#3b2a6b 0%,#14182e 55%,#0d0f1a 100%);border:1px solid var(--line)}}
#stage{{position:absolute;inset:0}}
#bubble{{position:absolute;left:12px;right:12px;top:12px;background:#fff;color:#151a2c;border-radius:14px;padding:10px 12px;font-size:14px;font-weight:600;box-shadow:0 6px 20px #0006}}
#bubble:before{{content:"🪃 Boomer";display:block;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#a86a00;margin-bottom:2px}}
.stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}}.k,.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 12px}}
.tiles{{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}}@media(min-width:620px){{.tiles{{grid-template-columns:repeat(3,1fr)}}}}.acc{{color:var(--acc)}}
.k span{{display:block;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}}.k b{{font-size:20px}}
table{{border-collapse:collapse;width:100%}}td,th{{padding:6px;border-bottom:1px solid var(--line);text-align:left;font-size:14px}}.pos{{color:var(--ok)}}.neg{{color:var(--bad)}}
ul{{margin:0;padding-left:18px}}.chart canvas{{width:100%;height:300px;display:block}}#notes{{margin-top:8px;font-size:14px;display:grid;gap:4px}}
#notes div:before{{content:"🪃 ";}}.fine{{color:var(--muted);font-size:13px}}h2{{margin:6px 0 0;font-size:17px}}
</style></head><body><div class="wrap">
<p><a href="index.html">← Vault Options</a> · <a href="hq.html">Vault HQ</a></p>
<h1 style="margin:0">Gap Fade · IWM</h1>
<div class="hero"><div id="stage"></div><div id="bubble"></div></div>
<div class="stats"><div class="k"><span>Total</span><b class="{'pos' if rec['pl'] >= 0 else 'neg'}">{vo.money(rec['pl'])}</b></div>
<div class="k"><span>Record</span><b>{rec['wins']}W–{rec['losses']}L</b></div>
<div class="k"><span>Bankroll</span><b>${START + rec['pl']:,.0f}</b></div></div>
{open_html}
<h2>Boomer's chart</h2><div class="card chart"><canvas id="chart"></canvas><div id="notes"></div></div>
<h2>Last 10 days</h2><div class="card"><ul>{days or '<li>Nothing yet</li>'}</ul></div>
<h2>Trades</h2><div class="card"><table><tr><th>Date</th><th>Contract</th><th>Gap</th><th>Price</th><th>Exit</th><th>Result</th></tr>{rows or '<tr><td colspan=6>No trades yet</td></tr>'}</table></div>
<p class="fine">Boomer's rule: if IWM opens {MIN_GAP:.1%}+ from yesterday's close, at 10:00 AM he buys a same-day put (gap up) or call (gap down), betting it comes back like a boomerang.
Stop −{STOP:.0%}; at +{LOCK_AT:.0%} the stop locks +{LOCK_AT:.0%}; 2+ contracts sell half at +{HALF_AT:.0%}, rest to +{TARGET:.0%}; 1 contract trails 25%. Out by 3:30 PM.
Inspector test (24 months, real option prices): 108 trades, 48% won, +$886 at this size. Paper trading only. Not financial advice.</p>
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script>const DATA={data};{CHAR_JS}{CHART_JS}</script></body></html>""")


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
    if is_open:
        try:
            g_, pc, op = todays_gap(api, e.date())
            allb = api.bars([SYM], "15Min", 6).get(SYM, [])
            tb = [b_ for b_ in allb if vo.et(b_["t"]).date() == e.date()]
            yb = [b_ for b_ in allb if vo.et(b_["t"]).date() < e.date()][-8:]
            row = lambda b_: [vo.et(b_["t"]).strftime("%H:%M"), b_["o"], b_["h"], b_["l"], b_["c"], b_.get("v", 0)]
            if pc:
                st["live"] = {"date": e.date().isoformat(), "prev": round(pc, 2), "open": round(op, 2),
                              "now": round(tb[-1]["c"], 2) if tb else round(op, 2),
                              "bars": [row(b_) for b_ in tb], "ybars": [row(b_) for b_ in yb]}
        except vo.ApiError as ex:
            log.append(f"price check failed ({ex})")
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
