#!/usr/bin/env python3
"""Funding Collector — consolidated (replaces v2 + v3).
Short perps with highest funding rates, collects 8h settlement payments.
Auto-exits on funding drop or max-age only — no price exits.
"""
import os, sys, json, time
if not os.environ.get('BYBIT_API_KEY'):
    env_path = '/root/.bybit_env'
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and '=' in line and not line.startswith('#'):
                    k, v = line.split('=', 1)
                    os.environ[k.strip()] = v.strip()
from datetime import datetime, timezone, timedelta
from safety import SafeBybitAPI, make_safe_get, make_safe_post, atomic_write, atomic_read

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BYBIT_API_KEY = os.environ.get("BYBIT_API_KEY", "")
BYBIT_PRIV_KEY_PATH = os.environ.get("BYBIT_API_PRIVATE_KEY_PATH",
    "/root/.bybit/private.pem")
BYBIT_BASE_URL = "https://api.bybit.com"
LIVE_MODE = False  # Set True for real orders

# --- Strategy config ---
CAPITAL = 150.0          # Total capital
MAX_POSITIONS = 3        # Small positions, more diversification
POSITION_SIZE = 25.0     # $25 each — small enough to ride through price noise

MIN_FUNDING_RATE_PCT = 0.30   # Only enter rates >= 0.30% (was 0.20 — fewer, stronger entries)
EXIT_FUNDING_RATE_PCT = 0.01  # Exit only when funding dies
STOP_LOSS_PRICE_PCT = 3.0     # Hard SL — max -$0.75 on $25
MIN_HOLD_HOURS = 6             # Wait for at least one settlement
MAX_HOLD_HOURS = 120           # Hold up to 5 days — let funding compound
RE_ENTRY_COOLDOWN_MINUTES = 120
LIMIT_FEE_RATE = 0.0002
FEE_RATE = 0.001
TRADE_LOG = os.path.join(BASE_DIR, "funding_trades.json")

# Stock/commodity tokenized perps — require T&C acceptance on Bybit, skip these
STOCK_PERPS = {
    'MSTRUSDT','COINUSDT','CRCLUSDT','EWYUSDT','HOODUSDT','INTCUSDT',
    'NVDAUSDT','MUUSDT','GOOGLUSDT','TSLAUSDT','XAGUSDT','XAUUSDT',
    'SNDKUSDT','VINEUSDT','AAPLOUSDT','MSFTUSDT','AMZNUSDT','METAUSDT',
    'PLTRUSDT',  # Also requires T&C
}

# Cache for lot size filters (symbol -> {"qty_step": N, "min_qty": N})
_lot_cache = {}

def _get_lot_info(sym):
    """Fetch and cache lot size info from Bybit for a symbol."""
    if sym in _lot_cache:
        return _lot_cache[sym]
    try:
        resp = bybit_get("/v5/market/instruments-info", {"category": "linear", "symbol": sym})
        if resp.get("retCode") == 0 and resp["result"].get("list"):
            ls = resp["result"]["list"][0].get("lotSizeFilter", {})
            info = {
                "qty_step": float(ls.get("qtyStep", "0.0001") or "0.0001"),
                "min_qty": float(ls.get("minOrderQty", "0.001") or "0.001"),
            }
            _lot_cache[sym] = info
            return info
    except:
        pass
    _lot_cache[sym] = {"qty_step": 0.0001, "min_qty": 0.001}
    return _lot_cache[sym]

def _round_qty(qty, sym):
    """Round qty DOWN to the lot size step for the symbol."""
    import math
    info = _get_lot_info(sym)
    step = info["qty_step"]
    if step <= 0:
        step = 1
    step_str = f"{step:.10f}".rstrip("0")
    if "." in step_str:
        dec = len(step_str.split(".")[1])
    else:
        dec = 0
    # Floor to step (never round up — avoids "Qty invalid")
    qty = math.floor(qty / step) * step
    qty = round(qty, dec)
    qty = max(qty, info["min_qty"])
    return qty

# ==================== SAFE BYBIT API ====================
_bybit_api = SafeBybitAPI(BYBIT_BASE_URL, BYBIT_API_KEY, BYBIT_PRIV_KEY_PATH)
bybit_get = make_safe_get(_bybit_api)
bybit_post = make_safe_post(_bybit_api)

# ==================== TRADE STORE ====================
def load_trades():
    return atomic_read(TRADE_LOG) or []

def save_trades(t):
    atomic_write(TRADE_LOG, t)

# ==================== BOT ====================
class FundingBot:
    def __init__(self):
        self.trades = load_trades()
        self.active = {}
        self._recent_exits = {}
        self._rebuild()
        self._sync_from_server()  # Don't trust local logs — verify against server

    def _sync_from_server(self):
        """Query Bybit server for actual open positions and sync local state.
        Removes any active entries that don't exist on the server.
        This is the SOURCE OF TRUTH — never trust local logs over server data.
        """
        try:
            resp = bybit_get("/v5/position/list", {"category": "linear", "settleCoin": "USDT"})
            if resp.get("retCode") == 0:
                server_positions = {}
                for p in resp["result"]["list"]:
                    if float(p["size"]) > 0:
                        server_positions[p["symbol"]] = p
                # Remove any active entries not on server
                removed = []
                for sym in list(self.active.keys()):
                    if sym not in server_positions:
                        removed.append(sym)
                        del self.active[sym]
                if removed:
                    print(f"  SERVER SYNC: removed stale {removed} (not on server)")
                # Remove any trade log entries for symbols no longer on server
                for i in range(len(self.trades) - 1, -1, -1):
                    t = self.trades[i]
                    if t.get("type") == "ENTRY" and t.get("symbol") not in self.active and t.get("symbol") not in server_positions:
                        self.trades.pop(i)
                if removed:
                    self._save()
            return resp
        except Exception as e:
            print(f"  SERVER SYNC FAILED: {e}")
            return None

    def _rebuild(self):
        """Rebuild active positions from trade log, normalizing ts field.
        Uses FIFO entry-exit pairing: each EXIT closes the oldest open ENTRY.
        Then syncs with server to remove any stale entries.
        """
        for t in self.trades:
            if "timestamp" in t and "ts" not in t:
                t["ts"] = t.pop("timestamp")
        # Build ordered lists of entries and exits by symbol
        all_entries = []  # (symbol, ts_idx, trade_dict)
        all_exits = []    # (symbol, ts_idx)
        fundings = {}     # symbol -> [funding_events]
        for i, t in enumerate(self.trades):
            tp = t.get("type", "")
            if tp == "EXIT":
                all_exits.append((t.get("symbol", ""), i, t.get("ts", "")))
            elif tp == "ENTRY":
                all_entries.append((t.get("symbol", ""), i, t))
            elif tp == "FUNDING":
                fundings.setdefault(t.get("symbol", ""), []).append(t)
        # Track which entries are closed (FIFO per symbol)
        entry_closed = [False] * len(all_entries)
        for ex_sym, ex_idx, ex_ts in all_exits:
            # Record recent exit for cooldown
            if ex_ts:
                try:
                    self._recent_exits[ex_sym] = datetime.fromisoformat(ex_ts.replace("Z", "+00:00"))
                except:
                    pass
            # Close the oldest unmatched entry for this symbol
            for ei in range(len(all_entries)):
                if not entry_closed[ei] and all_entries[ei][0] == ex_sym:
                    entry_closed[ei] = True
                    break
        # Rebuild active from open entries (only the FIRST open per symbol)
        seen_symbols = set()
        for i, (sym, ei, t) in enumerate(all_entries):
            if not entry_closed[i] and sym not in seen_symbols:
                seen_symbols.add(sym)
                p = dict(t)
                fs = fundings.get(sym, [])
                p["total_collected"] = sum(f.get("net_payment", 0) for f in fs)
                if "last_pay_ts" in t and t["last_pay_ts"] is not None:
                    p["last_pay_ts"] = t["last_pay_ts"]
                else:
                    sf = sorted(fs, key=lambda x: x.get("ts", ""))
                    p["last_pay_ts"] = sf[-1]["ts"] if sf else None
                self.active[sym] = p

    def _save(self):
        save_trades(self.trades)

    def _save_active(self):
        for i, t in enumerate(self.trades):
            if t.get("type") == "ENTRY" and t.get("symbol") in self.active:
                sym = t["symbol"]
                for key in ("last_pay_ts", "total_collected"):
                    if key in self.active[sym]:
                        self.trades[i][key] = self.active[sym][key]
        self._save()

    def get_top_symbols(self):
        resp = bybit_get("/v5/market/tickers", {"category": "linear"})
        if resp.get("retCode") != 0:
            return [], 0
        candidates = []
        filtered_total, filtered_predicted = 0, 0
        for t in resp["result"]["list"]:
            fr = float(t.get("fundingRate", 0) or 0) * 100
            pr = float(t.get("lastPrice", 0) or 0)
            sym = t["symbol"]
            if fr >= MIN_FUNDING_RATE_PCT and pr > 0.000001 and sym not in self.active and sym not in STOCK_PERPS:
                # Check cooldown
                if sym in self._recent_exits:
                    mins = (datetime.now(timezone.utc) - self._recent_exits[sym]).total_seconds() / 60
                    if mins < RE_ENTRY_COOLDOWN_MINUTES:
                        filtered_total += 1
                        continue
                # Rate stability check: predicted funding rate should also be positive
                predicted = float(t.get("predictedFundingRate", 0) or 0) * 100
                if predicted < 0 and fr < 0.3:
                    filtered_predicted += 1
                    continue
                candidates.append({"symbol": sym, "rate": fr, "price": pr, "predicted": predicted})
        candidates.sort(key=lambda x: x["rate"], reverse=True)
        return candidates[:MAX_POSITIONS], filtered_predicted, filtered_total

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
                val = pos.get("value", POSITION_SIZE)
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
            entry_pr = pos.get("entry_price", 0)
            try:
                ed = datetime.fromisoformat(pos["ts"].replace("Z", "+00:00"))
            except:
                ed = now
            hours = (now - ed).total_seconds() / 3600
            reason = None

            # Max-age always checked first
            if hours >= MAX_HOLD_HOURS:
                reason = f"max-age {hours:.1f}h"
            elif sym in tm:
                t = tm[sym]
                cur_fr = float(t.get("fundingRate", 0) or 0) * 100
                cur_pr = float(t.get("lastPrice", 0) or 0)
                price_chg = ((cur_pr - entry_pr) / entry_pr) * 100 if entry_pr > 0 else 0
                if cur_fr < EXIT_FUNDING_RATE_PCT or cur_fr < 0:
                    if hours >= MIN_HOLD_HOURS:
                        reason = f"funding drop {cur_fr:.4f}%"
                    else:
                        print(f"  HOLD {sym}: funding dropped ({cur_fr:.4f}%) but only {hours:.1f}h old")
                elif price_chg >= STOP_LOSS_PRICE_PCT:
                    reason = f"sl +{price_chg:.2f}%"

            if reason:
                fc = pos.get("total_collected", 0)
                cur_pr_val = float(tm.get(sym, {}).get("lastPrice", entry_pr)) if sym in tm else entry_pr
                price_chg_val = ((cur_pr_val - entry_pr) / entry_pr) * 100 if entry_pr > 0 else 0
                price_pnl = -price_chg_val / 100 * pos.get("value", POSITION_SIZE)
                exit_fee = pos.get("value", POSITION_SIZE) * FEE_RATE
                total = fc + price_pnl - exit_fee
                entry_fee = pos.get("entry_fee", 0)
                net = total - entry_fee

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
                            continue  # Skip marking as exited — position still on exchange

                self.trades.append({"type":"EXIT","symbol":sym,"ts":datetime.now(timezone.utc).isoformat(),
                    "entry_price":entry_pr,"exit_price":cur_pr_val,"funding_collected":round(fc,2),
                    "price_pnl":round(price_pnl,2),"pnl_usdt":round(net,2),"reason":reason})
                del self.active[sym]
                self._recent_exits[sym] = now
                closed += 1
                print(f"  CLOSE {sym}: pnl=${net:.2f} (fc=${fc:.2f} px=${price_pnl:.2f}) | {reason}")
        if closed:
            self._save_active()
        if not closed:
            print("  No exits triggered")

    def enter_positions(self):
        slots = MAX_POSITIONS - len(self.active)
        if slots <= 0:
            print(f"  Max positions reached ({len(self.active)}/{MAX_POSITIONS})")
            return
        candidates, filtered_predicted, filtered_cooldown = self.get_top_symbols()
        if not candidates:
            print(f"  Top symbols >= {MIN_FUNDING_RATE_PCT}%: none (filtered {filtered_predicted}+{filtered_cooldown})")
            return
        print(f"  Top symbols >= {MIN_FUNDING_RATE_PCT}%:")
        for c in candidates[:6]:
            print(f"    {c['symbol']:18s} {c['rate']:.4f}% (pred={c.get('predicted',0):.4f}%)")
        print(f"  (filtered {filtered_predicted} predicted-negative, {filtered_cooldown} cooldown)")
        entered = 0
        # Deduplicate: skip symbols already in active positions
        candidate_pool = [c for c in candidates if c['symbol'] not in self.active]
        for best in candidate_pool[:slots]:
            sym, fr, pr = best["symbol"], best["rate"], best["price"]
            qty = _round_qty(POSITION_SIZE / pr, sym)
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
                    continue
                print(f"  ORDER {sym}: short {qty} @ market | ID={order['result']['orderId']}")
                # Set exchange-native SL at 3% immediately
                sl_price = round(pr * (1 + STOP_LOSS_PRICE_PCT / 100), 6)
                sl_result = bybit_post("/v5/position/trading-stop", body={
                    "category": "linear", "symbol": sym,
                    "stopLoss": str(sl_price), "tpslMode": "Full", "positionIdx": 0,
                })
                if sl_result.get("retCode") == 0:
                    print(f"  SL SET {sym}: stop at {sl_price}")
                else:
                    print(f"  SL FAILED {sym}: {sl_result.get('retMsg','?')}")
            else:
                print(f"  PAPER {sym}: short ${pr:.6f} rate={fr:.4f}% qty={qty} val=${val:.0f}")

            self.active[sym] = {"type":"ENTRY","symbol":sym,"ts":datetime.now(timezone.utc).isoformat(),
                "entry_price":pr,"entry_rate":fr,"value":round(val,2),"qty":qty,
                "entry_fee":round(fee,4),"total_collected":0.0,"last_pay_ts":None}
            self.trades.append(dict(self.active[sym]))
            entered += 1
        if entered:
            self._save()
            print(f"  Entered {entered} new position(s), active={len(self.active)}/{MAX_POSITIONS}")

    def run(self):
        print(f"\n{'='*55}")
        print(f"  FUNDING BOT @ {datetime.now(timezone.utc).strftime('%H:%M UTC')}")
        print(f"  Capital: ${CAPITAL} | Max pos: {MAX_POSITIONS}")
        print(f"{'='*55}")
        print(f"  Active: {list(self.active.keys()) if self.active else 'none'}")

        print("\n--- Funding ---")
        self.collect_funding()
        print("\n--- Server Sync ---")
        self._sync_from_server()
        print("\n--- Check Exits ---")
        self.check_exits()
        print("\n--- Enter Positions ---")
        self.enter_positions()

        tf = sum(t.get("net_payment",0) for t in self.trades if t["type"]=="FUNDING")
        rp = sum(t.get("pnl_usdt",0) for t in self.trades if t["type"]=="EXIT")
        sym_in = list(self.active.keys())
        print(f"\n{'='*55}")
        print(f"  ACTIVE: {', '.join(sym_in) if sym_in else 'none'}")
        print(f"  Funding collected: ${tf:.4f}")
        print(f"  Realized PnL: ${rp:.2f}")
        for s in sym_in:
            p = self.active[s]
            print(f"  {s}: est 8h=${p.get('entry_rate',0)/100*POSITION_SIZE:.4f} collected=${p.get('total_collected',0):.4f}")
        print(f"{'='*55}")
        return {"status":"ok","active":sym_in,
                "funding":round(tf,4),"pnl":round(rp,2)}

def main():
    global LIVE_MODE
    if "--live" in sys.argv:
        LIVE_MODE = True
        sys.argv.remove("--live")
    if not BYBIT_API_KEY:
        print(json.dumps({"status":"error","msg":"No API key"}))
        return 1
    try:
        bot = FundingBot()
        r = bot.run()
        print(f"\n{json.dumps(r, indent=2)}")
        return 0
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"CRASH: {e}")
        print(tb)
        crash_log = os.path.join(BASE_DIR, "funding_bot_crashes.log")
        with open(crash_log, "a") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} | {e}\n{tb}\n---\n")
        return 1

if __name__ == "__main__":
    sys.exit(main())
