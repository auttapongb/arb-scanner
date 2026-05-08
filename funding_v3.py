#!/usr/bin/env python3
"""Funding Collector v3 — single best symbol, full $100 capital."""
import os, sys, json, subprocess, urllib.request, urllib.parse, time
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BYBIT_API_KEY = os.environ.get("BYBIT_API_KEY", "")
BYBIT_PRIV_KEY_PATH = "/root/arb-scanner/bybit_private_key_rsa.pem"

CAPITAL = 100.0
MIN_FUNDING_RATE_PCT = 0.20
EXIT_FUNDING_RATE_PCT = 0.01
STOP_LOSS_PRICE_PCT = 2.0
MAX_HOLD_HOURS = 24
LIMIT_FEE_RATE = 0.0002
FEE_RATE = 0.001
TRADE_LOG = os.path.join(BASE_DIR, "funding_v3_trades.json")

def bybit_get(path, params=None):
    q = urllib.parse.urlencode(params) if params else ""
    ts = str(int(time.time() * 1000))
    rw = "5000"
    ps = f"{ts}{BYBIT_API_KEY}{rw}{q}"
    proc = subprocess.run(["openssl","dgst","-sha256","-sign",BYBIT_PRIV_KEY_PATH,"-binary"],
        input=ps.encode(), capture_output=True, timeout=5)
    sig = subprocess.run(["base64","-w0"], input=proc.stdout, capture_output=True, timeout=5).stdout.decode().strip()
    url = f"https://api.bybit.com{path}" + (f"?{q}" if q else "")
    h = {"X-BAPI-API-KEY": BYBIT_API_KEY, "X-BAPI-TIMESTAMP": ts, "X-BAPI-SIGN": sig,
         "X-BAPI-RECV-WINDOW": rw, "X-BAPI-SIGN-TYPE": "2"}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"retCode": -1, "retMsg": str(e)}

def load_trades():
    if os.path.exists(TRADE_LOG):
        try:
            with open(TRADE_LOG) as f:
                return json.load(f)
        except:
            return []
    return []

def save_trades(t):
    with open(TRADE_LOG, "w") as f:
        json.dump(t, f, indent=2, default=str)

class FundingV3:
    def __init__(self):
        self.trades = load_trades()
        self.active = self._reconstruct()
    
    def _save_active(self):
        """Propagate active position changes (like last_pay_ts) back to self.trades and save."""
        for i, t in enumerate(self.trades):
            if t.get("type") == "ENTRY" and t.get("symbol") in self.active:
                sym = t["symbol"]
                for key in ("last_pay_ts", "total_collected"):
                    if key in self.active[sym]:
                        self.trades[i][key] = self.active[sym][key]
        self._save()

    def _reconstruct(self):
        entries, exits, fundings = {}, set(), {}
        for t in self.trades:
            tp = t.get("type", "")
            if tp == "EXIT":
                exits.add(t.get("symbol", ""))
            elif tp == "ENTRY":
                entries[t.get("symbol", "")] = t
            elif tp == "FUNDING":
                fundings.setdefault(t.get("symbol", ""), []).append(t)
        a = {}
        for s, e in entries.items():
            if s in exits:
                continue
            p = dict(e)
            fs = fundings.get(s, [])
            p["total_collected"] = sum(f.get("net_payment", 0) for f in fs)
            # Use entry's last_pay_ts if available, fall back to last funding record
            if "last_pay_ts" in e and e["last_pay_ts"] is not None:
                p["last_pay_ts"] = e["last_pay_ts"]
            else:
                sf = sorted(fs, key=lambda x: x.get("ts", ""))
                p["last_pay_ts"] = sf[-1]["ts"] if sf else None
            a[s] = p
        return a

    def _save(self):
        save_trades(self.trades)

    def get_best_symbol(self):
        resp = bybit_get("/v5/market/tickers", {"category": "linear"})
        if resp.get("retCode") != 0:
            return None
        best = None
        for t in resp["result"]["list"]:
            fr = float(t.get("fundingRate", 0) or 0) * 100
            pr = float(t.get("lastPrice", 0) or 0)
            sym = t["symbol"]
            if fr >= MIN_FUNDING_RATE_PCT and pr > 0.000001 and sym not in self.active:
                if not best or fr > best["rate"]:
                    best = {"symbol": sym, "rate": fr, "price": pr}
        return best

    def collect_funding(self):
        now = datetime.now(timezone.utc)
        count = 0
        for sym, pos in self.active.items():
            lp = pos.get("last_pay_ts")
            if lp is None:
                try:
                    ed = datetime.fromisoformat(pos["ts"].replace("Z", "+00:00"))
                except:
                    ed = now
                bh = 0 if ed.hour < 8 else (8 if ed.hour < 16 else 16)
                s = ed.replace(hour=bh, minute=0, second=0, microsecond=0)
                if s >= ed:
                    s -= timedelta(hours=8)
                pos["last_pay_ts"] = s.isoformat()
                # Save immediately so next run doesn't re-init
                self._save_active()
                continue

            lpd = datetime.fromisoformat(lp.replace("Z", "+00:00"))
            settlements = []
            for h in [0, 8, 16]:
                c = lpd.replace(hour=h, minute=0, second=0, microsecond=0)
                if c <= lpd:
                    c += timedelta(days=1)
                while c <= now:
                    settlements.append(c)
                    c += timedelta(days=1)
            settlements.sort()

            for st in settlements:
                fr = pos.get("entry_rate", 0)
                val = pos.get("value", CAPITAL)
                gross = (fr / 100) * val
                fee_share = val * LIMIT_FEE_RATE / 3
                net = gross - fee_share
                self.trades.append({"type":"FUNDING","symbol":sym,"ts":st.isoformat(),
                    "rate":round(fr,4),"gross":round(gross,4),"fee":round(fee_share,4),"net_payment":round(net,4)})
                pos["last_pay_ts"] = st.isoformat()
                pos["total_collected"] = pos.get("total_collected", 0) + net
                count += 1
        if count:
            print(f"  Funding: {count} payments credited")
            self._save_active()
        else:
            print("  No settlements crossed")

    def check_exits(self):
        resp = bybit_get("/v5/market/tickers", {"category": "linear"})
        tm = {}
        if resp.get("retCode") == 0:
            for t in resp["result"]["list"]:
                tm[t["symbol"]] = t
        closed = 0
        now = datetime.now(timezone.utc)
        for sym in list(self.active.keys()):
            pos = self.active[sym]
            if sym not in tm:
                continue
            t = tm[sym]
            cur_fr = float(t.get("fundingRate", 0) or 0) * 100
            cur_pr = float(t.get("lastPrice", 0) or 0)
            entry_pr = pos.get("entry_price", 0)
            price_chg = ((cur_pr - entry_pr) / entry_pr) * 100
            try:
                ed = datetime.fromisoformat(pos["ts"].replace("Z", "+00:00"))
            except:
                ed = now
            hours = (now - ed).total_seconds() / 3600
            reason = None
            if cur_fr < EXIT_FUNDING_RATE_PCT and cur_fr >= 0:
                reason = f"funding drop {cur_fr:.4f}%"
            elif price_chg >= STOP_LOSS_PRICE_PCT:
                reason = f"stop +{price_chg:.2f}%"
            elif hours >= MAX_HOLD_HOURS:
                reason = f"max-age {hours:.1f}h"
            if reason:
                fc = pos.get("total_collected", 0)
                price_pnl = -price_chg / 100 * pos.get("value", CAPITAL)
                exit_fee = pos.get("value", CAPITAL) * FEE_RATE
                total = fc + price_pnl - exit_fee
                entry_fee = pos.get("entry_fee", 0)
                net = total - entry_fee
                self.trades.append({"type":"EXIT","symbol":sym,"ts":datetime.now(timezone.utc).isoformat(),
                    "entry_price":entry_pr,"exit_price":cur_pr,"funding_collected":round(fc,2),
                    "price_pnl":round(price_pnl,2),"pnl_usdt":round(net,2),"reason":reason})
                del self.active[sym]
                closed += 1
                print(f"  CLOSE {sym}: pnl=${net:.2f} (fc=${fc:.2f} px=${price_pnl:.2f}) | {reason}")
        if closed:
            self._save_active()
        if not closed:
            print("  No exits triggered")

    def enter_position(self):
        if self.active:
            print(f"  Already in {list(self.active.keys())[0]}")
            return
        best = self.get_best_symbol()
        if not best:
            print(f"  No symbol >= {MIN_FUNDING_RATE_PCT}%")
            return
        sym, fr, pr = best["symbol"], best["rate"], best["price"]
        qty = CAPITAL / pr
        if sym == "BTCUSDT":
            qty = round(qty, 6)
        elif sym == "ETHUSDT":
            qty = round(qty, 5)
        else:
            qty = round(qty, 4)
        val = qty * pr
        fee = val * LIMIT_FEE_RATE
        self.active[sym] = {"type":"ENTRY","symbol":sym,"ts":datetime.now(timezone.utc).isoformat(),
            "entry_price":pr,"entry_rate":fr,"value":round(val,2),"qty":qty,
            "entry_fee":round(fee,4),"total_collected":0.0,"last_pay_ts":None}
        self.trades.append(dict(self.active[sym]))
        self._save()
        print(f"  ENTER {sym}: short ${pr:.6f} rate={fr:.4f}% val=${val:.0f}")

    def run(self):
        print(f"\n{'='*55}")
        print(f"  FUNDING V3 @ {datetime.now(timezone.utc).strftime('%H:%M UTC')}")
        print(f"{'='*55}")
        print(f"  Active: {list(self.active.keys()) if self.active else 'none'}")
        print("\n--- Funding ---")
        self.collect_funding()
        print("\n--- Check Exits ---")
        self.check_exits()
        print("\n--- Enter Position ---")
        self.enter_position()
        tf = sum(t.get("net_payment",0) for t in self.trades if t["type"]=="FUNDING")
        rp = sum(t.get("pnl_usdt",0) for t in self.trades if t["type"]=="EXIT")
        sym_in = list(self.active.keys())
        print(f"\n{'='*55}")
        print(f"  ACTIVE: {sym_in[0] if sym_in else 'none'}")
        print(f"  Funding collected: ${tf:.4f}")
        print(f"  Realized PnL: ${rp:.2f}")
        if sym_in:
            p = self.active[sym_in[0]]
            print(f"  Est 8h: ${p.get('entry_rate',0)/100*CAPITAL:.4f}")
        print(f"{'='*55}")
        return {"status":"ok","active":sym_in[0] if sym_in else None,
                "funding":round(tf,4),"pnl":round(rp,2)}

def main():
    if not BYBIT_API_KEY:
        print(json.dumps({"status":"error","msg":"No API key"}))
        return 1
    try:
        f3 = FundingV3()
        r = f3.run()
        print(f"\n{json.dumps(r, indent=2)}")
        return 0
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"CRASH: {e}")
        print(tb)
        crash_log = os.path.join(BASE_DIR, "funding_v3_crashes.log")
        with open(crash_log, "a") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} | {e}\n{tb}\n---\n")
        return 1

if __name__ == "__main__":
    sys.exit(main())
