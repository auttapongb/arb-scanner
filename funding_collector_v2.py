#!/usr/bin/env python3
"""
Funding Collector v2 — short perps with highest positive funding on Bybit.
Simulates funding PAYMENTS at each 8h settlement boundary.
Pure short, directional risk via stop-loss.
"""
import os, sys, json, time, subprocess
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BYBIT_API_KEY = os.environ.get("BYBIT_API_KEY", "")
BYBIT_PRIV_KEY_PATH = os.environ.get("BYBIT_API_PRIVATE_KEY_PATH",
    "/root/arb-scanner/bybit_private_key_rsa.pem")
BYBIT_BASE_URL = "https://api.bybit.com"

# Paper trade defaults (overridden by --simulate or cron)
PAPER_TRADE = True
LIVE_MODE = False

MAX_POSITIONS = 5
POSITION_SIZE_USDT = 20
MIN_FUNDING_RATE_PCT = 0.05
EXIT_FUNDING_RATE_PCT = 0.01
STOP_LOSS_PRICE_PCT = 3.0
MIN_HOLD_HOURS = 4  # don't exit on funding drop in first 4h
MAX_HOLD_HOURS = 48
RE_ENTRY_COOLDOWN_MINUTES = 60  # don't re-enter same symbol within 1h of exit
LIMIT_FEE_RATE = 0.0002
FEE_RATE = 0.001
TRADE_LOG = os.path.join(BASE_DIR, "funding_trades.json")

# ===================== BYBIT API =====================
def bybit_sign(method, path, query="", body=""):
    ts = str(int(time.time() * 1000))
    rw = "5000"
    ps = f"{ts}{BYBIT_API_KEY}{rw}{query}" if method == "GET" else f"{ts}{BYBIT_API_KEY}{rw}{body}"
    proc = subprocess.run(["openssl","dgst","-sha256","-sign",BYBIT_PRIV_KEY_PATH,"-binary"],
        input=ps.encode(), capture_output=True, timeout=5)
    sig = subprocess.run(["base64","-w0"], input=proc.stdout, capture_output=True, timeout=5).stdout.decode().strip()
    h = {"X-BAPI-API-KEY": BYBIT_API_KEY, "X-BAPI-TIMESTAMP": ts, "X-BAPI-SIGN": sig,
         "X-BAPI-RECV-WINDOW": rw, "X-BAPI-SIGN-TYPE": "2", "User-Agent": "funding-v2/1.0"}
    if method == "POST":
        h["Content-Type"] = "application/json"
    return h, ts

def bybit_get(path, params=None):
    import urllib.request, urllib.parse
    q = urllib.parse.urlencode(params) if params else ""
    url = f"{BYBIT_BASE_URL}{path}" + (f"?{q}" if q else "")
    h, _ = bybit_sign("GET", path, q)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"retCode": -1, "retMsg": str(e)}

def bybit_post(path, body):
    import urllib.request
    bs = json.dumps(body, separators=(",",":"))
    h, _ = bybit_sign("POST", path, body=bs)
    try:
        with urllib.request.urlopen(urllib.request.Request(
            f"{BYBIT_BASE_URL}{path}", data=bs.encode(), headers=h), timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"retCode": -1, "retMsg": str(e)}

# ===================== TRADE STORE =====================
def load_trades():
    if os.path.exists(TRADE_LOG):
        try:
            with open(TRADE_LOG) as f:
                return json.load(f)
        except:
            return []
    return []

def save_trades(trades):
    with open(TRADE_LOG, "w") as f:
        json.dump(trades, f, indent=2, default=str)

def get_active_positions(trades):
    # Normalize: some records have 'timestamp' instead of 'ts'
    for t in trades:
        if "timestamp" in t and "ts" not in t:
            t["ts"] = t.pop("timestamp")
    entries, exits, fundings = {}, set(), {}
    for t in trades:
        if t["type"] == "EXIT":
            exits.add(t["symbol"])
        elif t["type"] == "ENTRY":
            s = t["symbol"]
            if s not in entries or t["ts"] > entries[s]["ts"]:
                entries[s] = t
        elif t["type"] == "FUNDING":
            fundings.setdefault(t["symbol"], []).append(t)
    result = {}
    for s, e in entries.items():
        if s in exits:
            continue
        pos = dict(e)
        f_recs = fundings.get(s, [])
        pos["total_funding_collected"] = sum(f.get("net_payment", 0) for f in f_recs)
        sorted_f = sorted(f_recs, key=lambda x: x["ts"])
        pos["last_funding_payment_ts"] = sorted_f[-1]["ts"] if sorted_f else None
        result[s] = pos
    return result

def now_iso():
    return datetime.now(timezone.utc).isoformat()

# ===================== COLLECTOR =====================
class FundingCollectorV2:
    def __init__(self):
        self.trades = load_trades()
        self.active = get_active_positions(self.trades)
        self._recent_exits = {}  # symbol -> datetime of last exit

    def _save(self):
        save_trades(self.trades)

    def scan_funding_rates(self):
        resp = bybit_get("/v5/market/tickers", {"category": "linear"})
        if resp.get("retCode") != 0:
            print("  FAILED tickers:", resp.get("retMsg"))
            return []
        cands = []
        for t in resp["result"]["list"]:
            fr = float(t.get("fundingRate", 0) or 0) * 100
            pr = float(t.get("lastPrice", 0) or 0)
            if fr >= MIN_FUNDING_RATE_PCT and pr > 0.000001:
                cands.append({"symbol": t["symbol"], "funding_rate_pct": fr, "price": pr,
                    "mark_price": float(t.get("markPrice", pr) or pr)})
        cands.sort(key=lambda x: x["funding_rate_pct"], reverse=True)
        return cands

    def collect_funding_payments(self, sim_now=None):
        """Credit funding when settlement boundary crossed."""
        now = sim_now or datetime.now(timezone.utc)
        crossed = 0
        
        # Settlement hours: 00:00, 08:00, 16:00 UTC
        # Find all settlements between last_pay and now
        for sym in list(self.active.keys()):
            pos = self.active[sym]
            lp = pos.get("last_funding_payment_ts")
            if lp is None:
                try:
                    entry_dt = datetime.fromisoformat(pos["ts"].replace("Z","+00:00"))
                except:
                    entry_dt = now
                eh = entry_dt.hour
                bh = 0 if eh < 8 else (8 if eh < 16 else 16)
                s = entry_dt.replace(hour=bh, minute=0, second=0, microsecond=0)
                if s >= entry_dt:
                    s -= timedelta(hours=8)
                pos["last_funding_payment_ts"] = s.isoformat()
                continue
            
            lpd = datetime.fromisoformat(lp.replace("Z","+00:00"))
            print(f"  {sym}: last_pay={lpd.strftime('%m/%d %H:%M')}")
            
            # Find all settlement times between last_pay and now
            # Settlements: 00:00, 08:00, 16:00 UTC
            settlements = []
            scan_start = lpd + timedelta(seconds=1)  # exclusive of last_pay
            for h in [0, 8, 16]:
                candidate = scan_start.replace(hour=h, minute=0, second=0, microsecond=0)
                if candidate < scan_start:
                    candidate += timedelta(days=1)
                while candidate <= now:
                    settlements.append(candidate)
                    candidate += timedelta(days=1)
            
            for st in settlements:
                # Apply decay: funding rate decreases over hold time
                base_fr = pos.get("entry_funding_rate", 0)
                hh = (st - datetime.fromisoformat(pos["ts"].replace("Z","+00:00"))).total_seconds() / 3600
                decay = max(0.3, 1 - (hh / MAX_HOLD_HOURS) * 0.5)
                paid_rate = base_fr * decay
                pv = pos.get("value_usdt", POSITION_SIZE_USDT)
                gross = (paid_rate / 100) * pv
                fee_share = pv * LIMIT_FEE_RATE / 3
                net = gross - fee_share
                rec = {"type": "FUNDING", "symbol": sym, "ts": st.isoformat(),
                    "funding_rate_pct": round(paid_rate, 4),
                    "position_value": round(pv, 2), "gross_payment": round(gross, 4),
                    "fee": round(fee_share, 4), "net_payment": round(net, 4)}
                self.trades.append(rec)
                pos["last_funding_payment_ts"] = st.isoformat()
                pos["total_funding_collected"] = pos.get("total_funding_collected", 0) + net
                crossed += 1
                print(f"    FUNDING ${net:.4f} at {paid_rate:.4f}% | settlement {st.strftime('%H:%M')}")
        if crossed:
            print(f"  Total: {crossed} funding payments credited")
            self._save()
        else:
            print("  No settlements crossed")

    def check_positions(self, ticker_map, sim_now=None):
        closed = 0
        now = sim_now or datetime.now(timezone.utc)
        for sym in list(self.active.keys()):
            pos = self.active[sym]
            if sym not in ticker_map:
                continue
            t = ticker_map[sym]
            cur_fr = float(t.get("fundingRate", 0) or 0) * 100
            cur_pr = float(t.get("lastPrice", 0) or 0)
            entry_pr = pos["entry_price"]
            price_chg = ((cur_pr - entry_pr) / entry_pr) * 100
            try:
                entry_dt = datetime.fromisoformat(pos["ts"].replace("Z","+00:00"))
            except:
                entry_dt = now
            hours_held = (now - entry_dt).total_seconds() / 3600
            
            reason = None
            if cur_fr < EXIT_FUNDING_RATE_PCT and cur_fr >= 0:
                if hours_held >= MIN_HOLD_HOURS:
                    reason = f"fund drop {cur_fr:.4f}%"
                else:
                    print(f"  HOLD {sym}: funding dropped ({cur_fr:.4f}%) but only {hours_held:.1f}h old (min {MIN_HOLD_HOURS}h)")
            elif price_chg >= STOP_LOSS_PRICE_PCT:
                reason = f"stop-loss +{price_chg:.2f}%"
            elif hours_held >= MAX_HOLD_HOURS:
                reason = f"max-age {hours_held:.1f}h"
            
            if reason:
                fc = pos.get("total_funding_collected", 0)
                price_pnl = -price_chg / 100 * pos["value_usdt"]
                exit_fee = pos["value_usdt"] * FEE_RATE
                total = fc + price_pnl - exit_fee

                if LIVE_MODE:
                    qty = pos.get("qty", 0)
                    if qty > 0:
                        close = bybit_post("/v5/order/create", {
                            "category": "linear", "symbol": sym,
                            "side": "Buy", "orderType": "Market",
                            "qty": str(qty), "positionIdx": 0,
                            "reduceOnly": True,
                        })
                        if close.get("retCode") == 0:
                            print(f"  CLOSE ORDER {sym}: buy {qty} @ market | ID={close['result']['orderId']}")
                        else:
                            print(f"  FAILED close {sym}: {close.get('retMsg','?')}")

                rec = {"type": "EXIT", "symbol": sym, "ts": now_iso(),
                    "entry_ts": pos["ts"], "entry_price": entry_pr, "exit_price": cur_pr,
                    "entry_funding_rate": pos["entry_funding_rate"],
                    "exit_funding_rate": round(cur_fr, 4),
                    "price_change_pct": round(price_chg, 2), "hours_held": round(hours_held, 1),
                    "funding_collected": round(fc, 2), "price_pnl": round(price_pnl, 2),
                    "exit_fee": round(exit_fee, 2), "pnl_usdt": round(total, 2),
                    "exit_reason": reason}
                self.trades.append(rec)
                del self.active[sym]
                self._recent_exits[sym] = now
                closed += 1
                print(f"  CLOSE {sym}: fc=${fc:.2f} px=${price_pnl:.2f} fee=${exit_fee:.2f} = ${total:.2f} | {reason}")
        if not closed:
            print("  No exits triggered")

    def find_new_entries(self, candidates):
        slots = MAX_POSITIONS - len(self.active)
        if slots <= 0:
            print(f"  Max positions ({MAX_POSITIONS}) reached")
            return
        avail = [c for c in candidates if c["symbol"] not in self.active]
        # Filter out symbols on cooldown (recently exited)
        now = datetime.now(timezone.utc)
        avail = [c for c in avail if c["symbol"] not in self._recent_exits
                 or (now - self._recent_exits[c["symbol"]]).total_seconds() / 60 >= RE_ENTRY_COOLDOWN_MINUTES]
        print(f"  {slots} slots, {len(avail)} candidates ({len(candidates)-len(avail)} on cooldown)")
        for c in avail[:slots]:
            self._open_short(c)

    def _open_short(self, c):
        sym, fr, pr = c["symbol"], c["funding_rate_pct"], c["price"]
        qty = POSITION_SIZE_USDT / pr
        if sym == "BTCUSDT": qty = round(qty, 6)
        elif sym == "ETHUSDT": qty = round(qty, 5)
        else: qty = round(qty, 4)
        if qty <= 0: return
        val = qty * pr
        fee = val * LIMIT_FEE_RATE

        if LIVE_MODE:
            order = bybit_post("/v5/order/create", {
                "category": "linear", "symbol": sym,
                "side": "Sell", "orderType": "Market",
                "qty": str(qty), "positionIdx": 0,
            })
            if order.get("retCode") != 0:
                print(f"  FAILED {sym}: {order.get('retMsg','?')}")
                return
            oid = order["result"]["orderId"]
            print(f"  ORDER {sym}: short {qty} @ market | ID={oid}")
        else:
            print(f"  PAPER {sym}: short ${pr:.6f} rate={fr:.4f}% val=${val:.0f}")

        self.active[sym] = {"type": "ENTRY", "symbol": sym, "ts": now_iso(),
            "entry_price": pr, "entry_funding_rate": fr, "qty": qty,
            "value_usdt": round(val, 2), "entry_fee": round(fee, 4),
            "total_funding_collected": 0.0, "last_funding_payment_ts": None}
        self.trades.append(dict(self.active[sym]))
        self._save()

    def run_once(self, sim_now=None):
        self.paper_trade = not LIVE_MODE
        now = sim_now or datetime.now(timezone.utc)
        print(f"\n{'='*55}")
        print(f"  COLLECTOR @ {now.strftime('%Y-%m-%d %H:%M:%S')} UTC")
        print(f"  Active: {len(self.active)} positions")
        print(f"{'='*55}")

        print(f"\n--- Funding Settlement Check ---")
        self.collect_funding_payments(sim_now)

        print(f"\n--- Position Check ---")
        tr = bybit_get("/v5/market/tickers", {"category": "linear"})
        tm = {}
        if tr.get("retCode") == 0:
            for t in tr["result"]["list"]:
                tm[t["symbol"]] = t
        self.check_positions(tm)

        print(f"\n--- New Entries ---")
        candidates = self.scan_funding_rates()
        print(f"  Found {len(candidates)} >= {MIN_FUNDING_RATE_PCT}%")
        for c in candidates[:6]:
            m = " [ACTIVE]" if c["symbol"] in self.active else ""
            print(f"    {c['symbol']:16s} {c['funding_rate_pct']:.4f}%  ${c['price']:.6f}{m}")
        self.find_new_entries(candidates)

        tf = sum(t.get("net_payment",0) for t in self.trades if t["type"]=="FUNDING")
        rp = sum(t.get("pnl_usdt",0) for t in self.trades if t["type"]=="EXIT")
        oc = len(self.active)
        print(f"\n{'='*55}")
        print(f"  SUMMARY: Open={oc} | Funding=${tf:.4f} | Realized PnL=${rp:.2f}")
        cost = sum(t.get("entry_fee",0) for t in self.trades if t["type"]=="ENTRY")
        print(f"  Entry fees paid: ${cost:.4f}")
        if oc > 0:
            afr = sum(p["entry_funding_rate"] for p in self.active.values()) / oc
            daily = afr * 3 * oc / 100 * POSITION_SIZE_USDT
            print(f"  Avg rate: {afr:.4f}% | Est daily: ${daily:.2f} | Est monthly: ${daily*30:.2f}")
        print(f"{'='*55}")
        return {"status":"ok","active":oc,"funding_collected":round(tf,4),"realized_pnl":round(rp,2)}

def run_simulation():
    """Run 48h simulation: 6 funding cycles, positions enter, collect funding, exit."""
    import copy
    print(f"\n{'#'*60}")
    print(f"#  FUNDING COLLECTOR v2 — 48h SIMULATION")
    print(f"#  Capital: $100 (5 x $20 positions)")
    print(f"{'#'*60}\n")
    
    save_trades([])  # Reset
    fc = FundingCollectorV2()
    
    # Step 1: Open positions (scan real Bybit rates)
    print("--- STEP 1: OPEN POSITIONS ---")
    candidates = fc.scan_funding_rates()
    fc.find_new_entries(candidates)
    print(f"\n  {len(fc.active)} positions opened")
    
    # Step 2-7: Simulate 6 settlement cycles (48h)
    entry_rate_sum = sum(p["entry_funding_rate"] for p in fc.active.values())
    sample_rate = entry_rate_sum / len(fc.active) if fc.active else 0.15
    
    for cycle in range(1, 7):
        hours = cycle * 8
        sim_dt = datetime(2026, 5, 8, (cycle * 8) % 24, 0, 0, tzinfo=timezone.utc)
        print(f"\n--- CYCLE {cycle} (+{hours}h) @ {sim_dt.strftime('%H:%M')} UTC ---")
        
        # Apply funding payment for all active
        for sym in list(fc.active.keys()):
            pos = fc.active[sym]
            decay = max(0.3, 1 - (hours / MAX_HOLD_HOURS) * 0.5)
            decay = max(0.3, decay - cycle * 0.05)  # funding drops over time
            paid_rate = sample_rate * decay
            val = pos["value_usdt"]
            gross = (paid_rate / 100) * val
            fee_share = val * LIMIT_FEE_RATE / 3
            net = gross - fee_share
            
            rec = {"type":"FUNDING", "symbol":sym, "ts":sim_dt.isoformat(),
                "funding_rate_pct": round(paid_rate, 4),
                "position_value": round(val, 2), "gross_payment": round(gross, 4),
                "fee": round(fee_share, 4), "net_payment": round(net, 4)}
            fc.trades.append(rec)
            pos["total_funding_collected"] = pos.get("total_funding_collected", 0) + net
            pos["last_funding_payment_ts"] = sim_dt.isoformat()
            print(f"  {sym}: +${net:.4f} (rate={paid_rate:.4f}%)")
        
        # Check exits: after cycle 3, funding drops below exit threshold
        if cycle >= 4:
            for sym in list(fc.active.keys()):
                pos = fc.active[sym]
                fc_amt = pos.get("total_funding_collected", 0)
                # Simulate mild adverse price move (-1%)
                price_move = -1.0
                price_pnl = price_move / 100 * pos["value_usdt"]
                exit_fee = pos["value_usdt"] * FEE_RATE
                total = fc_amt + price_pnl - exit_fee
                
                rec = {"type":"EXIT", "symbol":sym, "ts":sim_dt.isoformat(),
                    "entry_ts": pos["ts"], "entry_price": pos["entry_price"],
                    "exit_price": pos["entry_price"] * 1.01,
                    "entry_funding_rate": pos["entry_funding_rate"],
                    "exit_funding_rate": 0.008,
                    "price_change_pct": 1.0, "hours_held": cycle * 8,
                    "funding_collected": round(fc_amt, 2),
                    "price_pnl": round(price_pnl, 2), "exit_fee": round(exit_fee, 2),
                    "pnl_usdt": round(total, 2),
                    "exit_reason": f"funding drop cycle {cycle}"}
                fc.trades.append(rec)
                del fc.active[sym]
                print(f"  CLOSE {sym}: fc=${fc_amt:.2f} px=${price_pnl:.2f} fee=${exit_fee:.2f} = ${total:.2f}")
    
    # Final summary
    tf = sum(t.get("net_payment",0) for t in fc.trades if t["type"]=="FUNDING")
    rp = sum(t.get("pnl_usdt",0) for t in fc.trades if t["type"]=="EXIT")
    entry_fees = sum(t.get("entry_fee",0) for t in fc.trades if t["type"]=="ENTRY")
    exit_fees = sum(t.get("exit_fee",0) for t in fc.trades if t["type"]=="EXIT")
    net = rp - entry_fees
    
    print(f"\n{'#'*60}")
    print(f"#  SIMULATION COMPLETE")
    print(f"#  Total funding collected: ${tf:.4f}")
    print(f"#  Entry fees:              ${entry_fees:.4f}")
    print(f"#  Exit fees:               ${exit_fees:.4f}")
    print(f"#  Realized PnL (exits):    ${rp:.2f}")
    print(f"#  Net profit (exits-fees): ${net:.2f}")
    print(f"#  ROI: {net:.2f}% on $100")
    print(f"#  Est monthly (30d):       ${net*15:.2f}")
    print(f"{'#'*60}")
    
    save_trades(fc.trades)
    result = {"funding": round(tf,4), "realized_pnl": round(rp,2), "net_profit": round(net,2),
              "entry_fees": round(entry_fees,4), "exit_fees": round(exit_fees,4)}
    with open(os.path.join(BASE_DIR, "funding_simulation.json"), "w") as f:
        json.dump(result, f, indent=2)
    print(f"\nResults saved to funding_simulation.json")
    return result

def main():
    global LIVE_MODE
    if "--live" in sys.argv:
        LIVE_MODE = True
        sys.argv.remove("--live")
    if not BYBIT_API_KEY:
        print(json.dumps({"status":"error","msg":"No API key"}))
        return 1
    if not os.path.exists(BYBIT_PRIV_KEY_PATH):
        print(json.dumps({"status":"error","msg":f"Key not found: {BYBIT_PRIV_KEY_PATH}"}))
        return 1
    
    mode = sys.argv[1] if len(sys.argv) > 1 else "once"
    
    if mode == "--simulate":
        save_trades([])  # Reset for clean sim
        result = run_simulation()
        print(f"\n{json.dumps(result, indent=2)}")
    elif mode == "--reset":
        save_trades([])
        print("Trade log reset")
    elif mode == "--once":
        fc = FundingCollectorV2()
        result = fc.run_once()
        print(f"\n{json.dumps(result, indent=2)}")
    elif mode == "--cron":
        fc = FundingCollectorV2()
        result = fc.run_once()
        # Cron-optimized output: JSON only
        print(f"\n{json.dumps(result, indent=2)}")
    else:
        fc = FundingCollectorV2()
        result = fc.run_once()
        print(f"\n{json.dumps(result, indent=2)}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
