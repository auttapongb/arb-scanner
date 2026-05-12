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
from datetime import datetime, timezone

# Safety module — SafeBybitAPI, atomic file ops, startup validation
from safety import SafeBybitAPI, make_safe_get, make_safe_post, atomic_write, atomic_read, validate_startup, openssl_sign

# ==================== CONFIG ====================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

BYBIT_API_KEY = os.environ.get("BYBIT_API_KEY", "")
BYBIT_PRIV_KEY_PATH = os.environ.get("BYBIT_API_PRIVATE_KEY_PATH", "/root/arb-scanner/bybit_private_key_rsa.pem")
BYBIT_BASE_URL = "https://api.bybit.com"
BYBIT_ENV = os.environ.get("BYBIT_ENV", "mainnet")

# Trading settings
PAPER_TRADE = True  # Set to False for real trades
LIVE_MODE = False   # Set to True for live limit orders (overrides PAPER_TRADE concept)
POSITION_SIZE_USDT = 35  # Size per leg (reduced to match available capital ~$40)
MIN_SPREAD_PCT = 0.3  # Min spot-perpetual spread to enter (0.3%, cautious for tight market)
MAX_SPREAD_PCT = 5.0  # Sanity cap
MAX_OPEN_POSITIONS = 2  # Max simultaneous positions (2 = less risk, easier to manage)
CHECK_INTERVAL = 5
# Fee accounting
FEE_RATE = 0.001  # 0.1% per leg (worst-case taker fee, used for safety in PnL calc)
LIMIT_FEE_RATE = 0.0002  # 0.02% per leg when using PostOnly limit orders (maker)
TAKE_PROFIT_CONVERGENCE = 0.15  # Exit when spread narrows to ≤ this
SYMBOL_COOLDOWN_SECONDS = 3600  # Don't re-enter for 1 hour
MAX_POSTONLY_WAIT_SECONDS = 60  # Max time to wait for PostOnly spot fill before falling back to market
MIN_PROFIT_AFTER_FEES = 0.30  # Minimum net profit after fees ($0.30, still safe with $0.08 PostOnly fees)
# Exit strategies
STOP_LOSS_SPREAD = -0.2  # Exit if spread goes deeply negative (actual loss)
TAKE_PROFIT_SPREAD_INCREASE = 1.0  # Unused — convergence exit handles this
MAX_POSITION_AGE_HOURS = 5    # Force-exit after 5 hours

# Wallet-based risk management (auto-scaled)
WALLET_PCT = 0.0             # Will be set from API on startup — %-based thresholds below
# Per-position stop loss (applied to position value, not wallet)
PER_TRADE_SOFT_SL_PCT = -5.0   # Soft warning: -5% of position value → alert, tighten monitoring
PER_TRADE_HARD_SL_PCT = -15.0  # Hard stop: -15% of position value → force exit
# Wallet circuit breakers (% of total wallet, not position)
WALLET_SOFT_CIRCUIT_PCT = -10.0  # Daily -10% of wallet → stop new entries, keep existing
WALLET_HARD_CIRCUIT_PCT = -20.0  # Daily -20% of wallet → liquidate ALL positions, stop bot
# Daily target (alert only, never stops trading)
DAILY_TARGET_PROFIT = 5.0    # Alert when reached (does NOT stop trading)

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

# ==================== SAFE BYBIT API ====================
# Initialize safe API wrapper once
_bybit_api = SafeBybitAPI(BYBIT_BASE_URL, BYBIT_API_KEY, BYBIT_PRIV_KEY_PATH)
bybit_get = make_safe_get(_bybit_api)
bybit_post = make_safe_post(_bybit_api)


# ==================== ENGINE ====================

class SessionTracker:
    """Track daily profit/loss and enforce circuit breakers.
    Uses %-of-wallet thresholds that auto-scale with balance."""
    def __init__(self, logger, wallet_balance=0.0):
        self.logger = logger
        self._session_file = os.path.join(BASE_DIR, ".session_tracker.json")
        self.daily_pnl = 0.0
        self.wallet_balance = wallet_balance or WALLET_PCT  # fallback
        self._load()
    
    @property
    def soft_circuit_limit(self):
        """Daily PnL threshold for stopping new entries (%-based)."""
        if self.wallet_balance <= 0:
            return -999  # disabled
        return self.wallet_balance * (WALLET_SOFT_CIRCUIT_PCT / 100)
    
    @property
    def hard_circuit_limit(self):
        """Daily PnL threshold for liquidating everything (%-based)."""
        if self.wallet_balance <= 0:
            return -999
        return self.wallet_balance * (WALLET_HARD_CIRCUIT_PCT / 100)
    
    def _load(self):
        data = atomic_read(self._session_file)
        if data:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if data.get("date") == today:
                self.daily_pnl = data.get("pnl", 0.0)
                self.wallet_balance = data.get("wallet_balance", self.wallet_balance)
                return
        # New day or no data
        self.daily_pnl = 0.0
        self._save()
    
    def _save(self):
        atomic_write(self._session_file, {
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "pnl": self.daily_pnl,
            "wallet_balance": self.wallet_balance,
        })
    
    def record_exit(self, pnl_usdt):
        """Called after a successful exit.
        Returns (keep_trading: bool, action: str)
          - action='ok' → keep going
          - action='soft_circuit' → stop new entries only (daily PnL < -10% wallet)
          - action='hard_circuit' → liquidate everything (daily PnL < -20% wallet)
        """
        self.daily_pnl += pnl_usdt
        self._save()

        if self.daily_pnl >= DAILY_TARGET_PROFIT:
            print(f"🎯 DAILY TARGET HIT: ${self.daily_pnl:.2f} >= ${DAILY_TARGET_PROFIT} (alert only, keep trading)")

        if self.daily_pnl <= self.hard_circuit_limit:
            print(f"🛑 HARD CIRCUIT BREAKER: ${self.daily_pnl:.2f} <= {self.hard_circuit_limit:.2f} "
                  f"({WALLET_HARD_CIRCUIT_PCT:.0f}% of ${self.wallet_balance:.2f} wallet) — "
                  f"LIQUIDATING ALL POSITIONS")
            return False, 'hard_circuit'

        if self.daily_pnl <= self.soft_circuit_limit:
            print(f"⛔ SOFT CIRCUIT BREAKER: ${self.daily_pnl:.2f} <= {self.soft_circuit_limit:.2f} "
                  f"({WALLET_SOFT_CIRCUIT_PCT:.0f}% of ${self.wallet_balance:.2f} wallet) — "
                  f"stop new entries, keep existing positions")
            return False, 'soft_circuit'

        return True, 'ok'
    
    def can_trade(self):
        """Check if we're allowed to enter new trades today."""
        return self.daily_pnl > self.soft_circuit_limit
    
    def get_status(self) -> dict:
        wallet_pct = 0.0
        if self.wallet_balance > 0:
            wallet_pct = round((self.daily_pnl / self.wallet_balance) * 100, 2)
        return {
            "daily_pnl": round(self.daily_pnl, 2),
            "wallet_balance": round(self.wallet_balance, 2),
            "daily_pnl_pct": wallet_pct,
            "target_alert": DAILY_TARGET_PROFIT,
            "soft_circuit": round(self.soft_circuit_limit, 2),
            "hard_circuit": round(self.hard_circuit_limit, 2),
        }


class PaperTradeLogger:
    def __init__(self):
        self.log_file = os.path.join(BASE_DIR, "paper_trades.json")
        self.trades = []
        self._load()

    def _load(self):
        data = atomic_read(self.log_file)
        self.trades = data if isinstance(data, list) else []

    def _save(self):
        atomic_write(self.log_file, self.trades)

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
    def __init__(self, paper_trade=True, wallet_balance=0.0):
        self.paper_trade = paper_trade
        self.wallet_balance = wallet_balance
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
        self.session = SessionTracker(self.logger, wallet_balance)

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
            tp = trade.get("type", "")
            if tp == "ENTRY":
                entries.append((
                    trade["symbol"],
                    trade.get("timestamp", ""),
                    trade
                ))
            elif tp == "EXIT":
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

    def _place_limit_or_market(self, category, symbol, side, qty, price, pos_idx=None):
        """Place PostOnly limit order, wait for fill, fallback to market on timeout.
        Returns (order_id, fill_price) or (None, None) on failure.
        """
        price_str = str(round(price, 6))
        payload = {
            "category": category,
            "symbol": symbol,
            "side": side,
            "orderType": "Limit",
            "qty": str(qty),
            "price": price_str,
            "timeInForce": "PostOnly",
        }
        if pos_idx is not None:
            payload["positionIdx"] = pos_idx
        if category == "spot":
            payload["marketUnit"] = "baseCoin"

        order = bybit_post("/v5/order/create", payload)
        if order.get("retCode") != 0:
            print(f"  ⚠️ {category} {side} limit failed ({order.get('retMsg','?')}) → market fallback")
            payload.pop("price", None)
            payload.pop("timeInForce", None)
            payload["orderType"] = "Market"
            order = bybit_post("/v5/order/create", payload)
            if order.get("retCode") != 0:
                print(f"  ❌ {category} {side} market also failed: {order.get('retMsg','?')}")
                return None, None
            print(f"  ✅ {category} {side} MARKET @ market price")
            return order["result"]["orderId"], price_str

        oid = order["result"]["orderId"]
        print(f"  🕐 {category} {side} LIMIT @ {price_str} (PostOnly) — waiting for fill...")

        # Poll for fill up to MAX_POSTONLY_WAIT_SECONDS
        deadline = time.time() + MAX_POSTONLY_WAIT_SECONDS
        while time.time() < deadline:
            time.sleep(3)
            order_status = bybit_get("/v5/order/realtime", {
                "category": category,
                "symbol": symbol,
                "orderId": oid,
            })
            orders = order_status.get("result", {}).get("list", [])
            if orders:
                status = orders[0]["orderStatus"]
                cum_qty = float(orders[0].get("cumExecQty", "0"))
                if status == "Filled":
                    avg_px = orders[0].get("avgPrice", price_str)
                    print(f"  ✅ {category} {side} FILLED @ {avg_px}")
                    return oid, avg_px
                elif status == "PartiallyFilled":
                    print(f"  ⏳ {category} {side} partial fill ({cum_qty}/{qty}) — waiting...")
                elif status in ("Cancelled", "Rejected"):
                    print(f"  ⚠️ {category} {side} {status} → market fallback")
                    break
            print(f"  ⏳ {category} {side} still open... (3s elapsed)")

        # Timeout or cancelled → cancel remaining order and use market
        print(f"  ⏰ {category} {side} PostOnly timeout → cancelling...")
        try:
            bybit_post("/v5/order/cancel", {"category": category, "symbol": symbol, "orderId": oid})
        except:
            pass
        time.sleep(1)

        payload.pop("price", None)
        payload.pop("timeInForce", None)
        payload["orderType"] = "Market"
        order = bybit_post("/v5/order/create", payload)
        if order.get("retCode") != 0:
            print(f"  ❌ {category} {side} market fallback failed: {order.get('retMsg','?')}")
            return None, None
        print(f"  ✅ {category} {side} MARKET (after limit timeout)")
        return order["result"]["orderId"], price_str

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

        # REAL TRADE MODE — uses PostOnly limit orders with fill verification
        try:
            symbol = pos["symbol"]
            qty = pos["qty"]
            
            # STEP 1: Spot buy first (must fill before we short perp)
            spot_bid = pos.get("spot_bid", pos["spot_price"])
            spot_id, spot_fill_px = self._place_limit_or_market("spot", symbol, "Buy", qty, spot_bid)
            if spot_id is None:
                print(f"❌ Spot buy failed — aborting entry for {symbol}")
                return False
            
            # STEP 2: Only now place perp short (we have the spot hedge)
            perp_ask = pos.get("perp_ask", pos["perp_price"])
            perp_id, perp_fill_px = self._place_limit_or_market("linear", symbol, "Sell", qty, perp_ask, pos_idx=0)
            if perp_id is None:
                # Perp failed — revert spot position
                print(f"⚠️ Perp short failed — selling spot to unwind...")
                bybit_post("/v5/order/create", {
                    "category": "spot", "symbol": symbol,
                    "side": "Sell", "orderType": "Market",
                    "qty": str(qty), "marketUnit": "baseCoin",
                })
                print(f"❌ Entry aborted for {symbol} — spot reverted")
                return False

            pos["spot_order_id"] = spot_id
            pos["perp_order_id"] = perp_id
            pos["spot_fill_px"] = spot_fill_px
            pos["perp_fill_px"] = perp_fill_px
            pos["mode"] = "live"
            pos["spread_pct"] = pos.get("spread", 0)  # Normalize for rebuild
            self.active_positions[pos["symbol"]] = pos
            # Persist to log for cross-cron rebuild
            self.logger.trades.append({**pos, "type": "ENTRY", "mode": "live",
                "timestamp": datetime.now(timezone.utc).isoformat()})
            self.logger._save()
            msg = (f"REAL ENTRY (LIMIT):\n"
                   f"  {pos['symbol']} | Spread: {pos['spread']:.2f}%\\n"
                   f"  Spot Buy @ {spot_fill_px} | Perp Sell @ {perp_fill_px}\\n"
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

        # Circuit breaker: check daily PnL before processing new entries
        if not self.session.can_trade():
            print(f"⛔ Circuit breaker active — daily PnL ${self.session.daily_pnl:.2f} (soft circuit at ${self.session.soft_circuit_limit:.2f}, hard circuit at ${self.session.hard_circuit_limit:.2f})")
            # Still need to check exits for active positions
            for sym in list(self.active_positions.keys()):
                self._check_exit(sym)
            return opportunities  # Skip entering new positions

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
            if best["net_profit_usdt"] >= MIN_PROFIT_AFTER_FEES:
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
        pos_value = entry["value_usdt"]
        pnl_pct = (net_pnl / pos_value * 100) if pos_value > 0 else 0.0

        # EXIT 1: Take profit — spread narrowed enough (convergence)
        # In contango, profit comes when spread contracts toward zero
        if current_spread <= TAKE_PROFIT_CONVERGENCE and current_spread >= -0.05:
            print(f"💰 TAKE PROFIT (CONVERGENCE): {symbol} spread {entry_spread:.2f}% → {current_spread:.2f}% (converged {(entry_spread - current_spread):.2f}%), Net PnL=${net_pnl:.2f} ({pnl_pct:+.1f}% of pos)")
            self._execute_exit(symbol, current_spread, net_pnl)
            return

        # EXIT 2: Soft SL warning — PnL dropping, monitor closely
        if pnl_pct <= PER_TRADE_SOFT_SL_PCT:
            print(f"⚠️ SOFT SL WARNING: {symbol} PnL=${net_pnl:.2f} ({pnl_pct:+.1f}% of ${pos_value:.0f}) — spread {entry_spread:.2f}%→{current_spread:.2f}%, monitoring")
            # Do NOT exit — just warn and continue monitoring

        # EXIT 3: Stop loss — spread went deeply negative (actual loss)
        if current_spread <= STOP_LOSS_SPREAD:
            print(f"🛑 STOP LOSS (SPREAD): {symbol} spread {entry_spread:.2f}% → {current_spread:.2f}% (negative), Net PnL=${net_pnl:.2f} ({pnl_pct:+.1f}% of pos)")
            self._execute_exit(symbol, current_spread, net_pnl)
            return

        # EXIT 4: Per-trade hard stop loss — PnL % exceeded threshold
        if pnl_pct <= PER_TRADE_HARD_SL_PCT:
            print(f"🛑 HARD STOP LOSS: {symbol} Net PnL=${net_pnl:.2f} ({pnl_pct:+.1f}% of ${pos_value:.0f}) <= {PER_TRADE_HARD_SL_PCT:.0f}%, exiting")
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
            # Record exit PnL in session tracker (returns (keep_trading, action))
            keep_trading, action = self.session.record_exit(pnl)
            if not keep_trading:
                print(f"⛔ Session circuit breaker active ({action}) — stopping new entries")
            return

        # REAL EXIT — uses same fill-verified limit order approach
        try:
            prices = self.fetch_prices(symbol)
            if not prices:
                print(f"❌ Can't exit {symbol}: no prices")
                return

            # Spot sell first (remove the hedge)
            spot_id, _ = self._place_limit_or_market("spot", symbol, "Sell", entry.get("qty", 0), prices["spot_ask"])
            if spot_id is None:
                print(f"⚠️ Spot sell failed for {symbol}")
                return

            # Perp buy to close (reduceOnly)
            perp_id, _ = self._place_limit_or_market("linear", symbol, "Buy", entry.get("qty", 0), prices["perp_bid"], pos_idx=0)
            if perp_id is None:
                print(f"⚠️ Perp buy-to-close failed for {symbol}")

            print(f"REAL EXIT: {symbol} | PnL: ${pnl:.2f}")
            # Log live exit for cross-cron tracking
            self.logger.trades.append({**entry, "type": "EXIT", "mode": "live",
                "exit_timestamp": datetime.now(timezone.utc).isoformat(),
                "exit_spread_pct": round(spread, 2), "pnl_usdt": round(pnl, 2)})
            self.logger._save()
            # Record exit PnL in session tracker (returns (keep_trading, action))
            keep_trading, action = self.session.record_exit(pnl)
            if not keep_trading:
                print(f"⛔ Session circuit breaker active ({action}) — stopping new entries")
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

    # Startup validation — get wallet balance for %-based risk management
    startup = validate_startup(BYBIT_API_KEY, BYBIT_PRIV_KEY_PATH, min_balance=1.0)
    wallet_balance = startup.get('wallet', 0.0)
    if not startup["ok"]:
        for check, status in startup["checks"].items():
            if not status:
                print(f"⚠️ STARTUP CHECK FAILED: {check}")
        if wallet_balance > 0:
            print(f"  Wallet balance: ${wallet_balance:.2f}")
        for err in startup.get("errors", []):
            print(f"  Error: {err}")

    try:
        engine = BybitArbitrageEngine(paper_trade=(not LIVE_MODE), wallet_balance=wallet_balance)
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
            "session": engine.session.get_status(),
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
