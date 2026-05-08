#!/usr/bin/env python3
"""
Bybit Arbitrage Execution Engine (RSA auth)
Delta-neutral: Buy spot + Short perpetual on Bybit (single exchange)
Uses Bybit V5 API with RSA-SHA256 signing.
"""

import os
import sys
import json
import time
import subprocess
from datetime import datetime, timezone

# ==================== CONFIG ====================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

BYBIT_API_KEY = os.environ.get("BYBIT_API_KEY", "")
BYBIT_PRIV_KEY_PATH = os.environ.get("BYBIT_API_PRIVATE_KEY_PATH", "/root/arb-scanner/bybit_private_key_rsa.pem")
BYBIT_BASE_URL = "https://api.bybit.com"
BYBIT_ENV = os.environ.get("BYBIT_ENV", "mainnet")

# Trading settings
PAPER_TRADE = True  # Set to False for real trades
LIVE_MODE = False   # Set to True for live limit orders (overrides PAPER_TRADE concept)
POSITION_SIZE_USDT = 100  # Size per leg
MIN_SPREAD_PCT = 0.3  # Min spot-perpetual spread to enter (0.3%, cautious for tight market)
MAX_SPREAD_PCT = 5.0  # Sanity cap
MAX_OPEN_POSITIONS = 2  # Max simultaneous positions (2 = less risk, easier to manage)
CHECK_INTERVAL = 5
# Fee accounting
FEE_RATE = 0.001  # 0.1% per leg (worst-case taker fee, used for safety in PnL calc)
LIMIT_FEE_RATE = 0.0002  # 0.02% per leg when using PostOnly limit orders (maker)
TAKE_PROFIT_CONVERGENCE = 0.15  # Exit when spread narrows to ≤ this
SYMBOL_COOLDOWN_SECONDS = 3600  # Don't re-enter for 1 hour
MIN_PROFIT_AFTER_FEES = 0.30  # Minimum net profit after fees ($0.30, still safe with $0.08 PostOnly fees)
# Exit strategies
STOP_LOSS_SPREAD = -0.2  # Exit if spread goes deeply negative (actual loss)
TAKE_PROFIT_SPREAD_INCREASE = 1.0  # Unused — convergence exit handles this
MAX_POSITION_AGE_HOURS = 5    # Force-exit after 5 hours

# Symbols to monitor (spot + linear perpetual must exist on Bybit)
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT",
    "DOGEUSDT", "DOTUSDT", "LINKUSDT", "AVAXUSDT",
    "ATOMUSDT", "UNIUSDT", "BNBUSDT",
    "NEARUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "SUIUSDT",
    # Altcoins that historically show positive spreads when majors backwardate
    "MOVRUSDT", "SOSOUSDT", "GMXUSDT", "SQDUSDT",
    "TACUSDT", "FHEUSDT", "TRIAUSDT",
    # Extended altcoin coverage — frequently missed by core list
    "ALCHUSDT", "BOBAUSDT", "OLUSDT", "ROAMUSDT",
    "MAGICUSDT", "HPOS10IUSDT", "BICOUSDT", "UMAUSDT",
    "AIXBTUSDT", "WAVESUSDT", "FIGHTUSDT", "BRUSDT",
    "PUMPBTCUSDT", "COOKIEUSDT", "C98USDT", "BOMEUSDT",
    "SLPUSDT", "QTUMUSDT", "AIOZUSDT", "VELOUSDT",
    "MEWUSDT", "FLOWUSDT", "WHITEWHALEUSDT", "BSBUSDT",
]

# ==================== BYBIT API ====================

def bybit_sign(method: str, path: str, query: str = "", body: str = "") -> tuple:
    """Generate RSA-SHA256 signature for Bybit V5 API."""
    timestamp = str(int(time.time() * 1000))
    recv_window = "5000"

    if method == "GET":
        param_str = f"{timestamp}{BYBIT_API_KEY}{recv_window}{query}"
    else:
        param_str = f"{timestamp}{BYBIT_API_KEY}{recv_window}{body}"

    # RSA sign via openssl
    proc = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", BYBIT_PRIV_KEY_PATH, "-binary"],
        input=param_str.encode(),
        capture_output=True,
        timeout=5
    )
    sign = subprocess.run(
        ["base64", "-w0"],
        input=proc.stdout,
        capture_output=True,
        timeout=5
    ).stdout.decode().strip()

    headers = {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-SIGN": sign,
        "X-BAPI-RECV-WINDOW": recv_window,
        "X-BAPI-SIGN-TYPE": "2",
        "User-Agent": "bybit-arb-engine/1.0",
        "X-Referer": "bybit-arb-engine",
    }
    if method == "POST":
        headers["Content-Type"] = "application/json"

    return headers, timestamp


def bybit_get(path: str, params: dict = None) -> dict:
    """Authenticated GET request to Bybit."""
    import urllib.request, urllib.parse

    query = urllib.parse.urlencode(params) if params else ""
    url = f"{BYBIT_BASE_URL}{path}"
    if query:
        url = f"{url}?{query}"

    headers, _ = bybit_sign("GET", path, query)
    req = urllib.request.Request(url, headers=headers, method="GET")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"retCode": -1, "retMsg": str(e)}


def bybit_post(path: str, body: dict) -> dict:
    """Authenticated POST request to Bybit."""
    import urllib.request

    body_str = json.dumps(body, separators=(",", ":"))
    url = f"{BYBIT_BASE_URL}{path}"
    headers, _ = bybit_sign("POST", path, body=body_str)

    req = urllib.request.Request(url, data=body_str.encode(), headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"retCode": -1, "retMsg": str(e)}


# ==================== ENGINE ====================

class PaperTradeLogger:
    def __init__(self):
        self.log_file = os.path.join(BASE_DIR, "paper_trades.json")
        self.trades = []
        self._load()

    def _load(self):
        if os.path.exists(self.log_file):
            try:
                with open(self.log_file) as f:
                    self.trades = json.load(f)
            except:
                self.trades = []

    def _save(self):
        with open(self.log_file, "w") as f:
            json.dump(self.trades, f, indent=2, default=str)

    def log_entry(self, symbol, spread, spot_price, perp_price, qty, value, profit, fees=0, net_profit=0):
        trade = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": "ENTRY",
            "mode": "paper",  # Tag for rebuild filtering
            "symbol": symbol,
            "spread_pct": round(spread, 2),
            "spot_price": spot_price,
            "perp_price": perp_price,
            "qty": qty,
            "value_usdt": round(value, 2),
            "profit_target_usdt": round(profit, 2),
            "fees_usdt": round(fees, 2),
            "net_profit_usdt": round(net_profit, 2),
            "action": f"BUY {qty:.4f} {symbol} spot + SHORT {qty:.4f} {symbol} perpetual",
            "scan_count": 0,
        }
        self.trades.append(trade)
        self._save()
        return trade

    def log_exit(self, entry, exit_spread, pnl):
        exit_rec = {**entry, "type": "EXIT", "mode": "paper",
                     "exit_timestamp": datetime.now(timezone.utc).isoformat(),
                     "exit_spread_pct": round(exit_spread, 2),
                     "pnl_usdt": round(pnl, 2)}
        self.trades.append(exit_rec)
        self._save()
        return exit_rec


class BybitArbitrageEngine:
    def __init__(self, paper_trade=True):
        self.paper_trade = paper_trade
        self.logger = PaperTradeLogger()
        self.active_positions = {}  # symbol -> {entry details, scan_count: 0}
        self.recent_entries = {}  # symbol -> timestamp for cooldown
        self._rebuild_active_positions()
        self._clean_stale_positions()
        self.scan_count = 0
        self.spot_tickers = {}
        self.perp_tickers = {}
        self._precision_cache = {}  # symbol -> decimals for qty rounding
        self._cache_spot_perp_pairs()

    def _rebuild_active_positions(self):
        """Rebuild active_positions from paper_trades.json using entry-exit pairing.
        Tracks individual entries per symbol — one exit closes ONE entry.
        Positions persist across cron invocations.
        In live mode, only rebuilds from 'mode'='live' entries.
        """
        # Filter to current mode
        mode_filter = 'live' if not self.paper_trade else None  # None = all (paper log has no mode tag)
        
        # Build ordered lists of entries and exits by timestamp
        entries = []  # (symbol, timestamp, trade_dict)
        exits = []    # (symbol, timestamp)
        for trade in self.logger.trades:
            # Filter by mode: in live mode, only rebuild from 'mode':'live' entries
            if mode_filter is not None and trade.get('mode') != mode_filter:
                continue
            if trade["type"] == "ENTRY":
                entries.append((
                    trade["symbol"],
                    trade.get("timestamp", ""),
                    trade
                ))
            elif trade["type"] == "EXIT":
                exits.append((
                    trade["symbol"],
                    trade.get("exit_timestamp", trade.get("timestamp", ""))
                ))

        # Sort both by timestamp
        entries.sort(key=lambda x: x[1])
        exits.sort(key=lambda x: x[1])

        # Match exits to entries: for each exit, remove the oldest unmatched entry
        # We track which entry indices are still open
        entry_closed = [False] * len(entries)
        for ex_sym, ex_ts in exits:
            for i in range(len(entries)):
                if not entry_closed[i] and entries[i][0] == ex_sym:
                    entry_closed[i] = True
                    break

        # Rebuild active positions from open entries
        self.active_positions = {}
        for i, (sym, ts, trade) in enumerate(entries):
            if not entry_closed[i]:
                # If multiple open entries for same symbol, use the oldest one
                # (fundamentally same as funding_v3 approach — one active per symbol)
                if sym not in self.active_positions:
                    self.active_positions[sym] = {
                        "symbol": sym,
                        "spread": trade.get("spread_pct", trade.get("spread", 0)),
                        "spot_price": trade["spot_price"],
                        "perp_price": trade["perp_price"],
                        "qty": trade["qty"],
                        "value_usdt": trade["value_usdt"],
                        "profit_usdt": trade.get("profit_target_usdt", trade.get("profit_usdt", 0)),
                        "timestamp": trade.get("timestamp", ""),
                        "scan_count": trade.get("scan_count", 0),
                    }

    def _clean_stale_positions(self):
        """Auto-close positions > 24 hours old with no exit."""
        now = datetime.now(timezone.utc)
        to_remove = []
        for sym, pos in list(self.active_positions.items()):
            if "timestamp" in pos:
                try:
                    entry_time = datetime.fromisoformat(pos["timestamp"])
                    if (now - entry_time).total_seconds() > 86400:
                        print(f"🧹 Cleaning stale position: {sym} (entered {pos['timestamp']}, >24h old)")
                        to_remove.append(sym)
                except:
                    pass
        for sym in to_remove:
            del self.active_positions[sym]

    def _is_on_cooldown(self, symbol):
        """Check if symbol was entered in the last SYMBOL_COOLDOWN_SECONDS."""
        if symbol not in self.recent_entries:
            return False
        elapsed = (datetime.now(timezone.utc) - self.recent_entries[symbol]).total_seconds()
        return elapsed < SYMBOL_COOLDOWN_SECONDS

    def _has_open_position_on_exchange(self, symbol: str) -> bool:
        """Check if there's an existing open position on Bybit for this symbol."""
        try:
            # Check linear perpetual positions
            pos_resp = bybit_get("/v5/position/list", {"category": "linear", "symbol": symbol})
            if pos_resp.get("retCode") == 0:
                for pos in pos_resp["result"].get("list", []):
                    size = float(pos.get("size", 0) or 0)
                    if abs(size) > 0:
                        print(f"🔍 {symbol}: open position found on Bybit (size={size})")
                        return True

            # Check spot wallet balance
            wallet = bybit_get("/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": symbol.replace("USDT", "")})
            if wallet.get("retCode") == 0:
                for acct in wallet["result"].get("list", []):
                    for coin in acct.get("coin", []):
                        if float(coin.get("walletBalance", 0) or 0) > 0:
                            print(f"🔍 {symbol}: spot balance found ({coin.get('walletBalance')})")
                            return True

            return False
        except Exception as e:
            print(f"⚠️ Could not check Bybit positions for {symbol}: {e}")
            return False

    def _cache_spot_perp_pairs(self):
        """Pre-fetch all tickers."""
        spot = bybit_get("/v5/market/tickers", {"category": "spot"})
        perp = bybit_get("/v5/market/tickers", {"category": "linear"})

        if spot.get("retCode") == 0:
            for t in spot["result"]["list"]:
                self.spot_tickers[t["symbol"]] = {
                    "price": float(t["lastPrice"]),
                    "bid": float(t["bid1Price"]) if t.get("bid1Price") else 0,
                    "ask": float(t["ask1Price"]) if t.get("ask1Price") else 0,
                }

        if perp.get("retCode") == 0:
            for t in perp["result"]["list"]:
                self.perp_tickers[t["symbol"]] = {
                    "price": float(t["lastPrice"]),
                    "bid": float(t["bid1Price"]) if t.get("bid1Price") else 0,
                    "ask": float(t["ask1Price"]) if t.get("ask1Price") else 0,
                }

    def fetch_prices(self, symbol: str) -> dict:
        """Get spot + perpetual price for a symbol."""
        spot = self.spot_tickers.get(symbol)
        perp = self.perp_tickers.get(symbol)

        if not spot or not perp or spot["price"] <= 0:
            return None

        spread = ((perp["price"] - spot["price"]) / spot["price"]) * 100
        return {
            "symbol": symbol,
            "spot_price": spot["price"],
            "perp_price": perp["price"],
            "spot_bid": spot.get("bid", 0),
            "spot_ask": spot.get("ask", 0),
            "perp_bid": perp.get("bid", 0),
            "perp_ask": perp.get("ask", 0),
            "spread_pct": spread,
        }

    def _get_qty_precision(self, symbol: str) -> int:
        """Get lot size precision for a symbol, cached."""
        if symbol in self._precision_cache:
            return self._precision_cache[symbol]
        try:
            info_resp = bybit_get("/v5/market/instruments-info", {
                "category": "spot",
                "symbol": symbol,
            })
            lot = info_resp.get("result", {}).get("list", [{}])[0].get("lotSizeFilter", {})
            qty_step = lot.get("qtyStep", None)
            if qty_step and "." in qty_step:
                decimals = len(qty_step.split(".")[1])
            else:
                decimals = 0
        except Exception:
            # Fallback precision based on symbol
            if symbol == "BTCUSDT":
                decimals = 6
            elif symbol == "ETHUSDT":
                decimals = 5
            else:
                decimals = 4
        self._precision_cache[symbol] = decimals
        return decimals

    def calculate_position(self, prices: dict) -> dict:
        """Calculate position size based on spread."""
        spread = prices["spread_pct"]
        if spread < MIN_SPREAD_PCT:
            return None

        qty = POSITION_SIZE_USDT / prices["spot_price"]
        symbol = prices["symbol"]
        decimals = self._get_qty_precision(symbol)
        qty = round(qty, decimals)

        # In paper mode, use simple rounding
        if qty <= 0:
            return None

        value = qty * prices["spot_price"]
        gross_profit = (prices["perp_price"] - prices["spot_price"]) * qty
        fees = value * (FEE_RATE * 4)  # Entry + exit fees
        net_profit = gross_profit - fees

        if net_profit < MIN_PROFIT_AFTER_FEES:
            return None

        return {
            "symbol": symbol,
            "spread": spread,
            "spot_price": prices["spot_price"],
            "perp_price": prices["perp_price"],
            "qty": qty,
            "value_usdt": value,
            "profit_usdt": gross_profit,
            "fees_usdt": fees,
            "net_profit_usdt": net_profit,
            "entry_time": datetime.now(timezone.utc).isoformat(),
        }

    def execute_entry(self, pos: dict) -> bool:
        """Execute entry on Bybit."""
        if self.paper_trade:
            self.logger.log_entry(
                pos["symbol"], pos["spread"],
                pos["spot_price"], pos["perp_price"],
                pos["qty"], pos["value_usdt"], pos["profit_usdt"],
                pos.get("fees_usdt", 0), pos.get("net_profit_usdt", 0)
            )
            self.active_positions[pos["symbol"]] = pos
            self.recent_entries[pos["symbol"]] = datetime.now(timezone.utc)
            msg = (f"📝 PAPER TRADE ENTRY:\n"
                   f"  {pos['symbol']} | Spread: {pos['spread']:.2f}%\n"
                   f"  Value: ${pos['value_usdt']:.0f} | Target: ${pos['profit_usdt']:.2f}\n"
                   f"  Action: Buy {pos['qty']:.4f} spot + Short {pos['qty']:.4f} perpetual")
            print(msg)
            return True

        # REAL TRADE MODE — uses PostOnly limit orders (0.02% maker fees)
        try:
            # 1. Spot buy: PostOnly limit order at best bid → maker fee (0.02%)
            spot_price = str(round(pos.get("spot_bid", pos["spot_price"]), 6))
            spot_order = bybit_post("/v5/order/create", {
                "category": "spot",
                "symbol": pos["symbol"],
                "side": "Buy",
                "orderType": "Limit",
                "qty": str(pos["qty"]),
                "price": spot_price,
                "timeInForce": "PostOnly",
                "marketUnit": "baseCoin",
            })
            if spot_order.get("retCode") != 0:
                print(f"❌ Spot limit order failed: {spot_order.get('retMsg','?')} — falling back to market")
                # FALLBACK: try market order (taker, 0.1% fee)
                spot_order = bybit_post("/v5/order/create", {
                    "category": "spot",
                    "symbol": pos["symbol"],
                    "side": "Buy",
                    "orderType": "Market",
                    "qty": str(pos["qty"]),
                    "marketUnit": "baseCoin",
                })
                if spot_order.get("retCode") != 0:
                    print(f"❌ Spot market fallback also failed: {spot_order}")
                    return False
            spot_id = spot_order["result"]["orderId"]

            # 2. Perpetual sell (short): PostOnly limit order at best ask → maker fee (0.02%)
            perp_price = str(round(pos.get("perp_ask", pos["perp_price"]), 6))
            perp_order = bybit_post("/v5/order/create", {
                "category": "linear",
                "symbol": pos["symbol"],
                "side": "Sell",
                "orderType": "Limit",
                "qty": str(pos["qty"]),
                "price": perp_price,
                "timeInForce": "PostOnly",
                "positionIdx": 0,
            })
            if perp_order.get("retCode") != 0:
                print(f"❌ Perp limit order failed: {perp_order.get('retMsg','?')} — falling back to market")
                # FALLBACK: try market order
                perp_order = bybit_post("/v5/order/create", {
                    "category": "linear",
                    "symbol": pos["symbol"],
                    "side": "Sell",
                    "orderType": "Market",
                    "qty": str(pos["qty"]),
                    "positionIdx": 0,
                })
                if perp_order.get("retCode") != 0:
                    print(f"❌ Perp market fallback also failed: {perp_order}")
                    return False
            perp_id = perp_order["result"]["orderId"]

            pos["spot_order_id"] = spot_id
            pos["perp_order_id"] = perp_id
            pos["mode"] = "live"
            pos["spread_pct"] = pos.get("spread", 0)  # Normalize for rebuild
            self.active_positions[pos["symbol"]] = pos
            # Persist to log for cross-cron rebuild
            self.logger.trades.append({**pos, "type": "ENTRY", "mode": "live",
                "timestamp": datetime.now(timezone.utc).isoformat()})
            self.logger._save()
            msg = (f"REAL ENTRY (LIMIT):\n"
                   f"  {pos['symbol']} | Spread: {pos['spread']:.2f}%\\n"
                   f"  Spot Buy @ {spot_price} (PostOnly) | Perp Sell @ {perp_price} (PostOnly)\\n"
                   f"  Value: ${pos['value_usdt']:.0f} | Target: ${pos['profit_usdt']:.2f}")
            print(msg)
            return True
        except Exception as e:
            print(f"❌ Entry failed: {e}")
            return False

    def scan_and_trade(self):
        """Main scan loop."""
        # Refresh tickers
        self._cache_spot_perp_pairs()

        opportunities = []

        # Always check exits for active positions first (even without price data)
        for sym in list(self.active_positions.keys()):
            self._check_exit(sym)

        for symbol in SYMBOLS:
            prices = self.fetch_prices(symbol)
            if not prices:
                continue

            # Skip entry for active positions (already handled exit above)
            if symbol in self.active_positions:
                continue

            spread = prices["spread_pct"]

            # Cooldown check — don't re-enter recently exited symbols
            if self._is_on_cooldown(symbol):
                continue

            # Max positions cap — don't exceed limit
            if len(self.active_positions) >= MAX_OPEN_POSITIONS:
                continue

            # Exchange position check (live mode only — expensive API calls)
            # Only check if we need to: when position count suggests we might have missed one
            if not self.paper_trade and symbol in self.active_positions:
                # Verify the position actually exists on exchange
                if not self._has_open_position_on_exchange(symbol):
                    print(f"⚠️ {symbol}: tracked as active but no exchange position — removing")
                    del self.active_positions[symbol]

            # Check for entry
            pos = self.calculate_position(prices)
            if pos:
                opportunities.append(pos)

        # Sort by profit descending
        opportunities.sort(key=lambda x: x["profit_usdt"], reverse=True)

        if opportunities:
            best = opportunities[0]
            print(f"\n🎯 Best: {best['symbol']} @ {best['spread']:.2f}% | ${best['profit_usdt']:.2f}")
            if best["profit_usdt"] >= 0.50:
                self.execute_entry(best)

        return opportunities

    def _check_exit(self, symbol):
        """Check if position should be exited."""
        prices = self.fetch_prices(symbol)

        entry = self.active_positions.get(symbol)
        if not entry:
            return

        # EXIT 0 (pre-check): Max age — always check even if prices are stale
        entry_ts = entry.get("timestamp", "")
        hours_held = 0
        if entry_ts:
            try:
                entry_dt = datetime.fromisoformat(entry_ts.replace("Z", "+00:00"))
                hours_held = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600
            except:
                pass
        if hours_held >= MAX_POSITION_AGE_HOURS:
            # Estimate PnL: no price data, assume zero convergence
            fees = entry["value_usdt"] * (FEE_RATE * 4)
            print(f"⏰ MAX AGE: {symbol} held {hours_held:.1f}h, force-exiting (est fees=${fees:.2f})")
            self._execute_exit(symbol, entry.get("spread", 0), -fees)
            return

        if not prices:
            # No price data but too young for max-age — hold
            print(f"  ⏳ {symbol}: no price data, held {hours_held:.1f}h (waiting for max-age)")
            return

        current_spread = prices["spread_pct"]
        entry_spread = entry["spread"]

        # CORRECTED PnL: profit = convergence (entry_spread - current_spread)
        # When spread narrows, entry_spread - current_spread > 0 = profit
        gross_pnl = (entry_spread - current_spread) / 100 * entry["value_usdt"]
        fees = entry["value_usdt"] * (FEE_RATE * 4)  # 4 legs × 0.1% each (entry spot + perp, exit spot + perp)
        net_pnl = gross_pnl - fees

        # EXIT 1: Take profit — spread narrowed enough (convergence)
        # In contango, profit comes when spread contracts toward zero
        if current_spread <= TAKE_PROFIT_CONVERGENCE and current_spread >= -0.05:
            print(f"💰 TAKE PROFIT (CONVERGENCE): {symbol} spread {entry_spread:.2f}% → {current_spread:.2f}% (converged {(entry_spread - current_spread):.2f}%), Net PnL=${net_pnl:.2f}")
            self._execute_exit(symbol, current_spread, net_pnl)
            return

        # EXIT 2: Stop loss — spread went deeply negative (actual loss)
        if current_spread <= STOP_LOSS_SPREAD:
            print(f"🛑 STOP LOSS: {symbol} spread {entry_spread:.2f}% → {current_spread:.2f}% (negative), Net PnL=${net_pnl:.2f}")
            self._execute_exit(symbol, current_spread, net_pnl)
            return

    def _execute_exit(self, symbol, spread, pnl):
        """Exit position."""
        entry = self.active_positions.pop(symbol, None)
        if not entry:
            return

        if self.paper_trade:
            self.logger.log_exit(entry, spread, pnl)
            print(f"📝 PAPER TRADE EXIT: {symbol} | PnL: ${pnl:.2f}")
            return

        # REAL EXIT — PostOnly limit orders at the opposite side (maker fees)
        try:
            # Fetch fresh prices for bid/ask on exit side
            prices = self.fetch_prices(symbol)
            if not prices:
                print(f"❌ Can't exit {symbol}: no prices")
                return

            # Spot sell at best ask (maker)
            spot_price = str(round(prices["spot_ask"], 6))
            spot_exit = bybit_post("/v5/order/create", {
                "category": "spot",
                "symbol": symbol,
                "side": "Sell",
                "orderType": "Limit",
                "qty": str(entry["qty"]),
                "price": spot_price,
                "timeInForce": "PostOnly",
            })
            if spot_exit.get("retCode") != 0:
                print(f"❌ Spot limit exit failed: {spot_exit.get('retMsg','?')} — falling back to market")
                spot_exit = bybit_post("/v5/order/create", {
                    "category": "spot", "symbol": symbol,
                    "side": "Sell", "orderType": "Market",
                    "qty": str(entry["qty"]),
                })
                if spot_exit.get("retCode") != 0:
                    print(f"❌ Spot market exit fallback also failed: {spot_exit}")

            # Perp buy to close (maker) at best bid
            perp_price = str(round(prices["perp_bid"], 6))
            perp_exit = bybit_post("/v5/order/create", {
                "category": "linear",
                "symbol": symbol,
                "side": "Buy",
                "orderType": "Limit",
                "qty": str(entry["qty"]),
                "price": perp_price,
                "timeInForce": "PostOnly",
                "positionIdx": 0,
                "reduceOnly": True,
            })
            if perp_exit.get("retCode") != 0:
                print(f"❌ Perp limit exit failed: {perp_exit.get('retMsg','?')} — falling back to market")
                perp_exit = bybit_post("/v5/order/create", {
                    "category": "linear", "symbol": symbol,
                    "side": "Buy", "orderType": "Market",
                    "qty": str(entry["qty"]),
                    "positionIdx": 0, "reduceOnly": True,
                })
                if perp_exit.get("retCode") != 0:
                    print(f"❌ Perp market exit fallback also failed: {perp_exit}")

            print(f"REAL EXIT (LIMIT): {symbol} | Spot @ {spot_price} | Perp @ {perp_price} | PnL: ${pnl:.2f}")
            # Log live exit for cross-cron tracking
            self.logger.trades.append({**entry, "type": "EXIT", "mode": "live",
                "exit_timestamp": datetime.now(timezone.utc).isoformat(),
                "exit_spread_pct": round(spread, 2), "pnl_usdt": round(pnl, 2)})
            self.logger._save()
        except Exception as e:
            print(f"❌ Exit failed: {e}")


def main():
    """Single scan run (for cron)."""
    global LIVE_MODE
    if "--live" in sys.argv:
        LIVE_MODE = True
        sys.argv.remove("--live")
    if not BYBIT_API_KEY:
        print(json.dumps({"status": "error", "message": "No BYBIT_API_KEY set"}))
        return 1

    if not os.path.exists(BYBIT_PRIV_KEY_PATH):
        print(json.dumps({"status": "error", "message": f"Private key not found: {BYBIT_PRIV_KEY_PATH}"}))
        return 1

    try:
        engine = BybitArbitrageEngine(paper_trade=(not LIVE_MODE))
        opportunities = engine.scan_and_trade()

        result = {
            "status": "ok",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "paper_trade": engine.paper_trade,
            "exchange": "bybit",
            "opportunities_found": len(opportunities),
            "active_positions": list(engine.active_positions.keys()),
            "top_opps": [
                {"symbol": o["symbol"], "spread_pct": round(o["spread"], 2),
                 "profit_usdt": round(o["profit_usdt"], 2), "value_usdt": round(o["value_usdt"], 2)}
                for o in opportunities[:5]
            ],
        }
        print(json.dumps(result, indent=2))
        return 0
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"CRASH: {e}")
        print(tb)
        crash_log = os.path.join(BASE_DIR, "bybit_execution_crashes.log")
        with open(crash_log, "a") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} | {e}\n{tb}\n---\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
