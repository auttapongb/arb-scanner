#!/usr/bin/env python3
"""Funding Collector v4-TP — same as v4 but with 1.5% take-profit + re-entry.
Short perps with highest funding rates, collects 8h settlement payments.
Auto-exits on funding drop, max-age, OR take-profit at 1.5%. Re-enters if rate still good.
"""
import os, sys, json, time
from datetime import datetime, timezone, timedelta
from safety import SafeBybitAPI, make_safe_get, make_safe_post, atomic_write, atomic_read

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BYBIT_API_KEY = os.environ.get("BYBIT_API_KEY", "")
BYBIT_PRIV_KEY_PATH = os.environ.get("BYBIT_API_PRIVATE_KEY_PATH",
    "/root/.bybit/private.pem")
BYBIT_BASE_URL = "https://api.bybit.com"
LIVE_MODE = False

# --- Strategy config ---
MAX_POSITIONS = 3
POSITION_SIZE = 25.0

MIN_FUNDING_RATE_PCT = 0.20
EXIT_FUNDING_RATE_PCT = 0.01
STOP_LOSS_PRICE_PCT = 3.0         # Hard SL — max -$0.75 on $25
TAKE_PROFIT_PCT = 1.5             # TP1 — if price slowly hits 1.5%, bank profit
TRAILING_TP_LOCK_PCT = 1.0        # Trailing: lock 1% below best price (catches flash crashes)
MIN_HOLD_HOURS = 4
MAX_HOLD_HOURS = 24
RE_ENTRY_COOLDOWN_MINUTES = 30
LIMIT_FEE_RATE = 0.0002
FEE_RATE = 0.001
TRADE_LOG = os.path.join(BASE_DIR, "funding_trades_tp.json")

STOCK_PERPS = {
    'MSTRUSDT','COINUSDT','CRCLUSDT','EWYUSDT','HOODUSDT','INTCUSDT',
    'NVDAUSDT','MUUSDT','GOOGLUSDT','TSLAUSDT','XAGUSDT','XAUUSDT',
    'SNDKUSDT','VINEUSDT','AAPLOUSDT','MSFTUSDT','AMZNUSDT','METAUSDT',
    'PLTRUSDT',
}

_lot_cache = {}

def _get_lot_info(sym):
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
    qty = math.floor(qty / step) * step
    qty = round(qty, dec)
    qty = max(qty, info["min_qty"])
    return qty

_bybit_api = SafeBybitAPI(BYBIT_BASE_URL, BYBIT_API_KEY, BYBIT_PRIV_KEY_PATH)
bybit_get = make_safe_get(_bybit_api)
bybit_post = make_safe_post(_bybit_api)

def load_trades():
    return atomic_read(TRADE_LOG) or []

def save_trades(t):
    atomic_write(TRADE_LOG, t)

class FundingBotTP:
    def __init__(self):
        self.trades = load_trades()
        self.active = {}
        self._recent_exits = {}
        self._rebuild()
        self._sync_from_server()

    def _sync_from_server(self):
        try:
            resp = bybit_get("/v5/position/list", {"category": "linear", "settleCoin": "USDT"})
            if resp.get("retCode") == 0:
                server_positions = {}
                for p in resp["result"]["list"]:
                    if float(p["size"]) > 0:
                        server_positions[p["symbol"]] = p
                removed = []
                for sym in list(self.active.keys()):
                    if sym not in server_positions:
                        removed.append(sym)
                        del self.active[sym]
                if removed:
                    print(f"  SERVER SYNC: removed stale {removed}")
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
        for t in self.trades:
            if "timestamp" in t and "ts" not in t:
                t["ts"] = t.pop("timestamp")
        all_entries = []
        all_exits = []
        fundings = {}
        for i, t in enumerate(self.trades):
            tp = t.get("type", "")
            if tp == "EXIT":
                all_exits.append((t.get("symbol", ""), i, t.get("ts", "")))
            elif tp == "ENTRY":
                all_entries.append((t.get("symbol", ""), i, t))
            elif tp == "FUNDING":
                fundings.setdefault(t.get("symbol", ""), []).append(t)
        entry_closed = [False] * len(all_entries)
        for ex_sym, ex_idx, ex_ts in all_exits:
            if ex_ts:
                try:
                    self._recent_exits[ex_sym] = datetime.fromisoformat(ex_ts.replace("Z", "+00:00"))
                except:
                    pass
            for ei in range(len(all_entries)):
                if not entry_closed[ei] and all_entries[ei][0] == ex_sym:
                    entry_closed[ei] = True
                    break
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
                if sym in self._recent_exits:
                    mins = (datetime.now(timezone.utc) - self._recent_exits[sym]).total_seconds() / 60
                    if mins < RE_ENTRY_COOLDOWN_MINUTES:
                        filtered_total += 1
                        continue
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
            trailing_updated = False

            if hours >= MAX_HOLD_HOURS:
                reason = f"max-age {hours:.1f}h"
            elif sym in tm:
                t = tm[sym]
                cur_fr = float(t.get("fundingRate", 0) or 0) * 100
                cur_pr = float(t.get("lastPrice", 0) or 0)
                price_chg = ((cur_pr - entry_pr) / entry_pr) * 100 if entry_pr > 0 else 0

                # 1. Funding drop exit
                if cur_fr < EXIT_FUNDING_RATE_PCT or cur_fr < 0:
                    if hours >= MIN_HOLD_HOURS:
                        reason = f"funding drop {cur_fr:.4f}%"
                    else:
                        print(f"  HOLD {sym}: funding dropped ({cur_fr:.4f}%) but only {hours:.1f}h old")
                # 2. Hard stop loss — price moved 3% against short
                elif price_chg >= STOP_LOSS_PRICE_PCT:
                    reason = f"sl +{price_chg:.2f}%"
                # 3. Hybrid: Fixed TP1 (1.5%) + Trailing TP
                elif price_chg < 0:
                    profit_pct = -price_chg
                    best_profit = pos.get("best_profit", 0)
                    if profit_pct > best_profit:
                        pos["best_profit"] = profit_pct
                        best_profit = profit_pct

                    # Track trailing TP on every new best price
                    # For shorts: as price drops, ratchet TP down (keep it 1% above current price)
                    if LIVE_MODE:
                        cur_tp_target = pos.get("trailing_tp_target", 0)
                        new_tp_target = round(cur_pr * (1 + TRAILING_TP_LOCK_PCT / 100), 6) if pos.get("tp1_hit", False) else 0
                        
                        if new_tp_target > 0 and (new_tp_target < cur_tp_target or cur_tp_target == 0) and pos.get("tp1_hit", False):
                            pos["trailing_tp_target"] = new_tp_target
                            tp_result = bybit_post("/v5/position/trading-stop", body={
                                "category": "linear", "symbol": sym,
                                "takeProfit": str(new_tp_target), "tpslMode": "Full", "positionIdx": 0,
                            })
                            if tp_result.get("retCode") == 0:
                                print(f"  TRAIL TP {sym}: best={best_profit:.2f}% → TP moved to ${new_tp_target:.6f}")
                            else:
                                print(f"  TRAIL TP FAIL {sym}: {tp_result.get('retMsg','?')}")

                    # TP1: if we hit 1.5% profit cleanly (price slowly moved there)
                    if best_profit >= TAKE_PROFIT_PCT and not pos.get("tp1_hit", False):
                        # Check if this is a smooth hit vs a flash crash
                        # Smooth hit: current price is near the best profit level
                        # Flash crash: current price is far below best profit (skipped past)
                        if profit_pct >= TAKE_PROFIT_PCT - 0.3:  # within 0.3% of best
                            pos["tp1_hit"] = True
                            # Move SL to breakeven
                            print(f"  TP1 HIT {sym}: profit {best_profit:.2f}% → banking profit")
                            if LIVE_MODE:
                                sl_result = bybit_post("/v5/position/trading-stop", body={
                                    "category": "linear", "symbol": sym,
                                    "stopLoss": str(entry_pr), "tpslMode": "Full", "positionIdx": 0,
                                })
                                if sl_result.get("retCode") == 0:
                                    print(f"  SL→BE {sym}: moved to breakeven ${entry_pr:.6f}")
                                else:
                                    print(f"  SL→BE FAIL {sym}: {sl_result.get('retMsg','?')}")
                                # Set initial trailing TP
                                tp_init = round(cur_pr * (1 + TRAILING_TP_LOCK_PCT / 100), 6)
                                init_tp = bybit_post("/v5/position/trading-stop", body={
                                    "category": "linear", "symbol": sym,
                                    "takeProfit": str(tp_init), "tpslMode": "Full", "positionIdx": 0,
                                })
                                if init_tp.get("retCode") == 0:
                                    pos["trailing_tp_target"] = tp_init
                                    print(f"  TRAIL INIT {sym}: trailing TP at ${tp_init:.6f}")
                                else:
                                    print(f"  TRAIL INIT FAIL {sym}: {init_tp.get('retMsg','?')}")
                            reason = f"tp1 {profit_pct:.2f}%"

                    # Flash crash detection: best_profit significantly > current profit
                    # This means price crashed past our targets quickly
                    if not pos.get("tp1_hit", False) and best_profit >= TAKE_PROFIT_PCT + 0.5:
                        # Flash crash — activate trailing immediately without TP1
                        pos["tp1_hit"] = True
                        print(f"  FLASH {sym}: price crashed {best_profit:.2f}% → activating trailing TP")
                        if LIVE_MODE:
                            # Move SL to breakeven
                            sl_result = bybit_post("/v5/position/trading-stop", body={
                                "category": "linear", "symbol": sym,
                                "stopLoss": str(entry_pr), "tpslMode": "Full", "positionIdx": 0,
                            })
                            if sl_result.get("retCode") == 0:
                                print(f"  SL→BE {sym}: moved to breakeven")
                            # Set trailing TP: best_price * (1 + lock_pct)
                            # For shorts: best_price is lowest. If price bounces back above best_price + 1%, TP fires.
                            best_price = entry_pr * (1 - best_profit / 100)
                            tp_init = round(best_price * (1 + TRAILING_TP_LOCK_PCT / 100), 6)
                            init_tp = bybit_post("/v5/position/trading-stop", body={
                                "category": "linear", "symbol": sym,
                                "takeProfit": str(tp_init), "tpslMode": "Full", "positionIdx": 0,
                            })
                            if init_tp.get("retCode") == 0:
                                pos["trailing_tp_target"] = tp_init
                                print(f"  FLASH TP {sym}: trailing at ${tp_init:.6f} (locks {best_profit - TRAILING_TP_LOCK_PCT:.1f}% profit)")
                            else:
                                print(f"  FLASH TP FAIL {sym}: {init_tp.get('retMsg','?')}")

                    # Trailing TP fire check: if TP target set and price bounced back to it
                    if pos.get("trailing_tp_target", 0) > 0 and cur_pr >= pos["trailing_tp_target"]:
                        reason = f"trailing-tp {profit_pct:.2f}%"

                if not trailing_updated and not reason:
                    print(f"  HOLD {sym}: price {price_chg:+.2f}% from entry, funding {cur_fr:.4f}%")

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
                
                # Don't add to cooldown on TP exit — allows re-entry on next cycle if rate still good
                self._recent_exits[sym] = now
                    
                del self.active[sym]
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
                # Set initial TP at 1.5% profit — first target locks in no-loss
                tp_initial = round(pr * (1 - TAKE_PROFIT_PCT / 100), 6)
                tp_result = bybit_post("/v5/position/trading-stop", body={
                    "category": "linear", "symbol": sym,
                    "takeProfit": str(tp_initial), "tpslMode": "Full", "positionIdx": 0,
                })
                if tp_result.get("retCode") == 0:
                    print(f"  TP INIT {sym}: initial target at {tp_initial}")
                else:
                    print(f"  TP FAILED {sym}: {tp_result.get('retMsg','?')}")
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
        print(f"  FUNDING BOT TP @ {datetime.now(timezone.utc).strftime('%H:%M UTC')}")
        print(f"  Max pos: {MAX_POSITIONS} | Size: ${POSITION_SIZE} | TP: {TAKE_PROFIT_PCT}%")
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
        return {"status":"ok","variant":"tp","active":sym_in,
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
        bot = FundingBotTP()
        r = bot.run()
        print(f"\n{json.dumps(r, indent=2)}")
        return 0
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"CRASH: {e}")
        print(tb)
        crash_log = os.path.join(BASE_DIR, "funding_bot_tp_crashes.log")
        with open(crash_log, "a") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} | {e}\n{tb}\n---\n")
        return 1

if __name__ == "__main__":
    sys.exit(main())
