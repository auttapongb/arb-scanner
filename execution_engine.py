#!/usr/bin/env python3
"""
Binance Arbitrage Execution Engine
Delta-neutral strategy: Buy spot + Short perpetual on Binance
Uses CCXT. Paper-trade mode by default.
"""

import sys
import os
import json
import time
import logging
from datetime import datetime
from typing import Optional, Dict, Any

# Cross-platform import for discord alerter
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from alerts.discord_alerter import send_opportunity_alert, log_alert
except ImportError:
    # Fallback if alerter not available
    def log_alert(msg, level="INFO"):
        print(f"[{level}] {msg}")
    def send_opportunity_alert(*args, **kwargs):
        log_alert(f"Alert would be sent (no Discord): {kwargs}", "INFO")

# ==================== CONFIG ====================

# Load credentials
CRED_PATH = os.path.join(os.path.dirname(__file__), "credentials.json")
if os.path.exists(CRED_PATH):
    with open(CRED_PATH) as f:
        creds = json.load(f)
        BINANCE_API_KEY = creds["binance"]["api_key"]
        BINANCE_SECRET = creds["binance"]["secret"]
else:
    BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "")
    BINANCE_SECRET = os.environ.get("BINANCE_SECRET", "")

# Trading settings
PAPER_TRADE = True  # Set to False for real trades!
POSITION_SIZE_USDT = 100  # Size per leg (spot + futures = 200 USDT total)
MIN_SPREAD_PCT = 0.3  # Min spread % to enter
MAX_SPREAD_PCT = 5.0  # Max spread % (sanity check)
# Exit strategies
STOP_LOSS_SPREAD = 0.1  # Exit if spread narrows below this
TAKE_PROFIT_SPREAD_INCREASE = 1.0  # Exit if spread expands by this much from entry (%)
MAX_POSITION_AGE_SCANS = 10  # Force-exit after this many scan cycles (prevents permanent stacking)

# Symbols to monitor (spot and perpetual must exist on Binance)
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "DOTUSDT", "LINKUSDT", "AVAXUSDT",
    "MATICUSDT", "ATOMUSDT", "UNIUSDT",
]

# ==================== ENGINE ====================

class PaperTradeLogger:
    """Logs all trades without executing them."""
    
    def __init__(self):
        self.trades = []
        self.log_file = os.path.join(os.path.dirname(__file__), "paper_trades.json")
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
    
    def log_entry(self, symbol: str, spread: float, spot_price: float, perp_price: float,
                  spot_qty: float, perp_qty: float, value_usdt: float, profit_target: float):
        trade = {
            "timestamp": datetime.utcnow().isoformat(),
            "type": "ENTRY",
            "symbol": symbol,
            "spread_pct": round(spread, 2),
            "spot_price": spot_price,
            "perp_price": perp_price,
            "spot_qty": spot_qty,
            "perp_qty": perp_qty,
            "value_usdt": value_usdt,
            "profit_target_usdt": round(profit_target, 2),
            "action": f"BUY {spot_qty:.4f} {symbol} spot + SHORT {perp_qty:.4f} {symbol} perpetual",
        }
        self.trades.append(trade)
        self._save()
        log_alert(f"📝 PAPER TRADE ENTRY: {symbol} @ {spread:.2f}% spread | Value: ${value_usdt:.0f} | Target: ${profit_target:.2f}", "TRADE")
        return trade
    
    def log_exit(self, entry: dict, spread: float, pnl: float):
        exit_record = {
            **entry,
            "type": "EXIT",
            "exit_timestamp": datetime.utcnow().isoformat(),
            "exit_spread_pct": round(spread, 2),
            "pnl_usdt": round(pnl, 2),
        }
        self.trades.append(exit_record)
        self._save()
        log_alert(f"📝 PAPER TRADE EXIT: {entry['symbol']} | PnL: ${pnl:.2f}", "TRADE")


class BinanceArbitrageEngine:
    """Arbitrage execution engine for Binance spot + futures."""
    
    def __init__(self, paper_trade: bool = True):
        self.paper_trade = paper_trade
        self.exchange = None
        self.futures = None
        self.logger = PaperTradeLogger()
        self.active_positions = {}  # symbol -> entry details
        self.scan_count = 0
        self._init_exchange()
    
    def _init_exchange(self):
        """Initialize CCXT exchange connections."""
        try:
            import ccxt
            
            # Spot exchange
            self.exchange = ccxt.binance({
                'apiKey': BINANCE_API_KEY,
                'secret': BINANCE_SECRET,
                'enableRateLimit': True,
                'options': {'defaultType': 'spot'},
            })
            
            # Futures exchange (same keys, different endpoint)
            self.futures = ccxt.binance({
                'apiKey': BINANCE_API_KEY,
                'secret': BINANCE_SECRET,
                'enableRateLimit': True,
                'options': {'defaultType': 'future'},
            })
            
            # Load markets
            self.exchange.load_markets()
            self.futures.load_markets()
            
            log_alert(f"Binance engine initialized. Paper trade: {self.paper_trade}", "INFO")
            
        except Exception as e:
            log_alert(f"Failed to initialize exchange: {e}", "ERROR")
            raise

    def _has_open_position_on_exchange(self, symbol: str) -> bool:
        """Check if there's an existing open position on Binance futures for this symbol."""
        try:
            # Check futures positions
            positions = self.futures.fetch_positions([symbol])
            for pos in positions:
                if pos.get('symbol') == symbol and abs(float(pos.get('contracts', 0) or 0)) > 0:
                    log_alert(f"🔍 {symbol}: open position found on exchange ({pos.get('contracts')} contracts)", "INFO")
                    return True

            # Also check spot balance
            base_currency = symbol.replace('USDT', '')
            balance = self.exchange.fetch_balance()
            if base_currency in balance.get('free', {}) and balance['free'][base_currency] > 0:
                total_balance = balance['free'].get(base_currency, 0) + balance.get('used', {}).get(base_currency, 0)
                if total_balance > 0.01:  # Non-trivial balance
                    log_alert(f"🔍 {symbol}: spot balance found ({total_balance:.4f} {base_currency})", "INFO")
                    return True

            return False
        except Exception as e:
            log_alert(f"⚠️ Could not check exchange positions for {symbol}: {e}", "WARN")
            # If we can't check, assume no open position (fail open)
            return False

    def fetch_prices(self, symbol: str) -> Optional[Dict[str, float]]:
        """Fetch spot and perpetual prices for a symbol."""
        try:
            # Spot price
            spot_ticker = self.exchange.fetch_ticker(symbol)
            spot_price = spot_ticker['last']
            
            # Perpetual price
            perp_ticker = self.futures.fetch_ticker(symbol)
            perp_price = perp_ticker['last']
            
            return {
                'symbol': symbol,
                'spot_price': spot_price,
                'perp_price': perp_price,
                'spot_bid': spot_ticker['bid'],
                'spot_ask': spot_ticker['ask'],
                'perp_bid': perp_ticker['bid'],
                'perp_ask': perp_ticker['ask'],
                'spread_pct': ((perp_price - spot_price) / spot_price) * 100,
            }
        except Exception as e:
            log_alert(f"Error fetching {symbol}: {e}", "DEBUG")
            return None
    
    def calculate_position(self, prices: dict) -> Optional[Dict[str, Any]]:
        """Calculate position size and expected profit."""
        spot_price = prices['spot_price']
        perp_price = prices['perp_price']
        spread = prices['spread_pct']
        
        if spread < MIN_SPREAD_PCT:
            return None
        
        # Position size: we want $POSITION_SIZE_USDT exposure on each leg
        spot_qty = POSITION_SIZE_USDT / spot_price
        perp_qty = POSITION_SIZE_USDT / perp_price
        
        # Use the smaller qty to ensure both legs match
        trade_qty = min(spot_qty, perp_qty)
        trade_value = trade_qty * spot_price  # Actual USDT used
        
        # Expected profit = spread * position value
        profit = (perp_price - spot_price) * trade_qty
        
        return {
            'symbol': prices['symbol'],
            'spread': spread,
            'spot_price': spot_price,
            'perp_price': perp_price,
            'spot_qty': trade_qty,
            'perp_qty': trade_qty,
            'value_usdt': trade_value,
            'profit_usdt': profit,
        }
    
    def execute_entry(self, position: dict) -> bool:
        """Execute entry: BUY spot + SHORT perpetual."""
        symbol = position['symbol']
        spot_qty = position['spot_qty']
        perp_qty = position['perp_qty']
        
        if self.paper_trade:
            self.logger.log_entry(
                symbol, position['spread'],
                position['spot_price'], position['perp_price'],
                spot_qty, perp_qty,
                position['value_usdt'], position['profit_usdt']
            )
            self.active_positions[symbol] = {**position, 'scan_count': 0}
            return True
        
        # REAL TRADE MODE
        try:
            # 1. Buy on spot market
            spot_order = self.exchange.create_market_buy_order(symbol, spot_qty)
            log_alert(f"REAL: Spot BUY {symbol} qty={spot_qty:.4f} @ {position['spot_price']:.2f}", "TRADE")
            
            # 2. Short on futures market
            perp_order = self.futures.create_market_sell_order(symbol, perp_qty)
            log_alert(f"REAL: Futures SHORT {symbol} qty={perp_qty:.4f} @ {position['perp_price']:.2f}", "TRADE")
            
            self.active_positions[symbol] = {
                **position,
                'spot_order_id': spot_order.get('id', '?'),
                'perp_order_id': perp_order.get('id', '?'),
                'scan_count': 0,
            }
            return True
            
        except Exception as e:
            log_alert(f"Entry failed for {symbol}: {e}", "ERROR")
            # Close any partial positions
            return False
    
    def check_exit(self, symbol: str) -> bool:
        """Check if we should exit a position."""
        if symbol not in self.active_positions:
            return False
        
        entry = self.active_positions[symbol]
        prices = self.fetch_prices(symbol)
        if not prices:
            return False
        
        current_spread = prices['spread_pct']
        entry_spread = entry['spread']
        
        # Exit conditions:
        # 1. Spread collapsed (no longer profitable) — stop loss
        # 2. Spread widened enough for take-profit
        # 3. Spread went negative (position is losing)
        
        if current_spread <= STOP_LOSS_SPREAD:
            # Exit - spread collapsed
            pnl = (current_spread - entry_spread) / 100 * entry['value_usdt']
            self._execute_exit(symbol, current_spread, pnl)
            return True
        
        # Take-profit: exit if spread has expanded significantly from entry
        # (our short is paying us as the futures premium grows)
        spread_change = current_spread - entry_spread
        if spread_change >= TAKE_PROFIT_SPREAD_INCREASE and current_spread > MIN_SPREAD_PCT:
            # The funding rate is already paying us; lock in paper profit
            pnl = spread_change / 100 * entry['value_usdt']
            log_alert(f"💰 TAKE PROFIT: {symbol} spread {entry_spread:.2f}% → {current_spread:.2f}% (Δ={spread_change:.2f}%), PnL=${pnl:.2f}", "TRADE")
            self._execute_exit(symbol, current_spread, pnl)
            return True
        
        # Mean reversion: exit when spread comes back close to entry
        spread_drift = abs(current_spread - entry_spread)
        if spread_drift <= 0.05 and current_spread < entry_spread:
            pnl = (current_spread - entry_spread) / 100 * entry['value_usdt']
            log_alert(f"🔄 MEAN REVERSION: {symbol} spread {current_spread:.2f}% ≈ entry {entry_spread:.2f}%, PnL=${pnl:.2f}", "TRADE")
            self._execute_exit(symbol, current_spread, pnl)
            return True
        
        # Max age: force-exit after N scans so positions don't accumulate forever
        entry['scan_count'] = entry.get('scan_count', 0) + 1
        if entry['scan_count'] >= MAX_POSITION_AGE_SCANS:
            pnl = (current_spread - entry_spread) / 100 * entry['value_usdt']
            log_alert(f"⏰ MAX AGE: {symbol} held {entry['scan_count']} scans, force-exit (PnL=${pnl:.2f})", "TRADE")
            self._execute_exit(symbol, current_spread, pnl)
            return True
        
        return False
    
    def _execute_exit(self, symbol: str, spread: float, pnl: float):
        """Execute exit: SELL spot + BUY back perpetual."""
        entry = self.active_positions.pop(symbol, None)
        if not entry:
            return
        
        qty = entry['spot_qty']
        
        if self.paper_trade:
            self.logger.log_exit(entry, spread, pnl)
            return
        
        # REAL TRADE MODE
        try:
            spot_exit = self.exchange.create_market_sell_order(symbol, qty)
            perp_exit = self.futures.create_market_buy_order(symbol, qty)
            log_alert(f"REAL: Exited {symbol} | PnL: ${pnl:.2f}", "TRADE")
        except Exception as e:
            log_alert(f"Exit failed for {symbol}: {e}", "ERROR")
    
    def scan_and_trade(self, symbols: list = None):
        """Main loop: scan for opportunities and trade."""
        if symbols is None:
            symbols = SYMBOLS
        
        log_alert(f"Scanning {len(symbols)} symbols...", "INFO")
        
        opportunities = []
        
        for symbol in symbols:
            prices = self.fetch_prices(symbol)
            if not prices:
                continue
            
            spread = prices['spread_pct']
            
            # Check if we have an active position (need to check exit)
            if symbol in self.active_positions:
                self.check_exit(symbol)
                # Skip entry — don't stack positions on same symbol
                continue
            
            # Also check exchange for any existing positions on this symbol
            # (prevents stacking when engine restarts / memory is stale)
            if self._has_open_position_on_exchange(symbol):
                log_alert(f"⏭️ {symbol}: position exists on exchange, skipping", "INFO")
                continue
            
            # Check for entry
            if MIN_SPREAD_PCT <= spread <= MAX_SPREAD_PCT:
                position = self.calculate_position(prices)
                if position and position['profit_usdt'] > 0:
                    opportunities.append(position)
        
        # Sort by profit descending
        opportunities.sort(key=lambda x: x['profit_usdt'], reverse=True)
        
        # Execute best opportunity
        if opportunities:
            best = opportunities[0]
            if best['profit_usdt'] >= 1.0:  # Min $1 profit to bother
                log_alert(f"🎯 Best opportunity: {best['symbol']} @ {best['spread']:.2f}% | ${best['profit_usdt']:.2f}", "INFO")
                success = self.execute_entry(best)
                if success:
                    send_opportunity_alert(best, source="crypto")
        
        # Summary
        log_alert(f"Scan complete: {len(opportunities)} opportunities found", "INFO")
        
        return opportunities


def main():
    """Main entry point - single scan run (for cron jobs)."""
    
    # Check credentials
    if not BINANCE_API_KEY or not BINANCE_SECRET:
        log_alert("No Binance credentials found!", "ERROR")
        print(json.dumps({"status": "error", "message": "No credentials"}))
        return 1
    
    try:
        engine = BinanceArbitrageEngine(paper_trade=True)
        opportunities = engine.scan_and_trade()
        
        result = {
            "status": "ok",
            "timestamp": datetime.utcnow().isoformat(),
            "paper_trade": engine.paper_trade,
            "opportunities_found": len(opportunities),
            "active_positions": list(engine.active_positions.keys()),
            "top_opps": [
                {
                    "symbol": o['symbol'],
                    "spread_pct": round(o['spread'], 2),
                    "profit_usdt": round(o['profit_usdt'], 2),
                    "value_usdt": round(o['value_usdt'], 2),
                }
                for o in opportunities[:5]
            ],
        }
        print(json.dumps(result, indent=2))
        return 0
        
    except Exception as e:
        log_alert(f"Engine error: {e}", "ERROR")
        print(json.dumps({"status": "error", "message": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
