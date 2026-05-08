# Arbitrage Scanner — Implementation Plan

## Codebase Overview

### File Architecture

```
/root/arb-scanner/
├── bybit_execution.py       # MAIN: Bybit spot-perp arb engine with RSA auth
├── execution_engine.py      # Binance spot-perp arb engine (secondary, uses CCXT)
├── crypto_scanner.py        # Bybit spread scanner (read-only, no execution)
├── polymarket_scanner.py    # Polymarket scanner (series arb + mispriced outcomes)
├── run_all.py               # Orchestrator: runs crypto + polymarket scanners
├── api_server.py            # HTTP API on port 8080 serving scan results
├── enrich_analysis.py       # Builds historical trends from scan data
├── scan_spreads.py          # Quick Bybit spreads tool
├── detail_scan.py           # Detailed Bybit spread report
├── diagnostic.py            # Diagnostic: spreads + paper trade history
├── full_market_dump.py      # Full Bybit market dump
├── check_trades.py          # Paper trade stats viewer
├── _check_positions.py      # Open position inspector
├── paper_trades.json        # All paper trade records (185 total)
├── credentials.json         # Binance API keys
├── last_scan.json           # Latest scan results
├── data/
│   ├── history.json         # Historical trend data
│   └── enriched.json        # Enriched analysis payload
├── alerts/                  # (external) Discord alert module
│   └── discord_alerter.py
```

### Data Flow

```
run_all.py
  ├── crypto_scanner.py         → reads Bybit public API → outputs JSON
  │                                   (read-only, no execution)
  ├── polymarket_scanner.py     → reads Polymarket + Kalshi APIs → outputs JSON
  └── enrich_analysis.py        → builds trends from last_scan.json + history.json

bybit_execution.py (separate cron/loop)
  → runs scan_and_trade() loop
  → reads Bybit V5 API (RSA auth)
  → paper_trades.json (read/write)
  → active_positions dict in memory (rebuilt from paper_trades.json on startup)
```

---

## TASK A: Fix Crypto Arbitrage Execution to Be Profitable

### Problems Identified

#### Problem 1: Entry Over-trading / Stacking on Same Symbol

**Evidence:** CLOUDUSDT has ~120+ entries but ZERO exits. The engine enters the same symbol over and over because:
- `active_positions[symbol]` check prevents second entry ONLY within the same process run
- The engine runs as independent cron jobs, and `active_positions` is an in-memory dict rebuilt from `paper_trades.json` at startup
- `_rebuild_active_positions()` looks for the LATEST ENTRY and checks if any later EXIT exists — but EXITS are only recorded with type="EXIT" in paper_trades.json
- The engine enters CLOUDUSDT at spread=0.5% dozens of times, then the spread collapses to negative in the NEXT FEW MINUTES
- The `scan_count` property starts at 0 each time the engine restarts! Once the process exits and comes back, `MAX_POSITION_AGE_SCANS=10` resets

**Fix:** 
- Use a SYMBOL-LEVEL cooldown. If we entered CLOUDUSDT in the last 30 minutes, skip it regardless of whether it was "exited" or not
- Better: track entry timestamps and enforce a minimum time-between-entries per symbol (e.g., 60 minutes)
- Remove `scan_count` reset on engine restart by persisting it in paper_trades.json

#### Problem 2: Exit Logic is Wrong (main cause of -$15.26 PnL)

**Evidence:** All 19 exits are losses. Every exit happens when spread goes negative or near-zero. The PnL formula:

```python
pnl = (current_spread - entry_spread) / 100 * entry["value_usdt"]
```

This is **correct** for marking-to-market if both legs are still open. But the problem is:
- **Entry:** Buy spot at $S_s, short perp at $P_s. Spread = (P_s - S_s) / S_s * 100 = positive (contango)
- **Profit should come from convergence:** When spread narrows (perp price falls relative to spot), you make money. When spread widens further, you LOSE money.
- The exit triggers happen when spread goes negative or very low:
  - STOP_LOSS_SPREAD = 0.05 — exits when spread collapses to near-zero. But this is a STOP LOSS, it should be locking in close-to-entry PnL
  - Actually these exits are firing when spread goes NEGATIVE (perp below spot). The formula `(current_spread - entry_spread)` gives NEGATIVE PnL when current_spread < entry_spread

**The actual bug:** The engine enters when spread is positive (perp > spot), but the spread ALWAYS converges (often overshoots to negative) within minutes. The exit conditions correctly detect this but:
1. The PnL calculation is paper-only and **doesn't account for funding rate** (which you COLLECT when shorting in contango)
2. **Take-profit is defined as "spread expanded from entry"** — but in contango, the spread NARROWS to profit, not expands!
3. The "mean reversion" exit fires at `abs(current - entry) <= 0.05` when current < entry — this ALWAYS produces negative PnL because the spread came down from entry.

**Fix:**
- **Flip the take-profit logic:** In contango (perp > spot), you profit when spread NARROWS (convergence). Entry at +0.7%, exit when spread is between 0.0% and 0.2% (not negative). That's the actual profit zone.
- **Add funding rate estimation:** In contango, shorts PAY you. Over hours/days this can be significant (0.01-0.1% per funding period). Add to expected PnL.
- **Fix PnL formula for exits:** The PnL = (entry_spread - current_spread) * (value_usdt / 100) — i.e., profit when spread narrows. *Flipped sign.*
- **Set real take-profit:** Exit when spread narrows to ≤ 0.15% (capturing 0.35-0.55% profit on a 0.5-0.7% entry). This is the sweet spot — before it goes negative.

Wait — let me re-examine. In a spot-perp arb:
- Buy spot at 100. Perp is 100.50 → spread = +0.5%.
- Spot goes to 101, perp goes to 101.20 → spread = +0.198%. 
  - Spot profit: +$1.00
  - Short loss: -(101.20-100.50) = -$0.70
  - Net: +$0.30 ← this is the convergence profit
- Spread went from 0.5% to 0.198%, a narrowing of 0.302% → on $100 = $0.302

So PnL formula should be: **pnl = (entry_spread - current_spread) / 100 * value_usdt**

But current code has: `pnl = (current_spread - entry_spread) / 100 * value_usdt`

**This is the WRONG SIGN.** The current code computes negative PnL when spread narrows (which is the profitable scenario).

Actual fix:
```python
# Profit = convergence = entry_spread - current_spread (positive when spread narrows)
pnl = (entry_spread - current_spread) / 100 * entry["value_usdt"]
```

#### Problem 3: Stop Loss and Exit Thresholds are Too Tight

Current: STOP_LOSS_SPREAD = 0.05 (exit when spread ≤ 0.05%)
Issue: A spread of 0.05% on a 0.5% entry means it narrowed by 0.45% — that's a PROFIT of ~$0.45 on $100. The stop loss should say "exit when spread is negative" (actual loss), not "exit when spread hit 0.05%"

Also: MAX_POSITION_AGE_SCANS = 10. With 5-min scan intervals, that's 50 minutes. Many positions would converge within that window. But with the buggy PnL sign, even locking in convergence gives negative reported PnL.

**Fix:**
- Change stop loss to: exit only when spread goes NEGATIVE (below -0.1%), not when it returns to near-zero
- Change take-profit to: exit when spread narrows below 0.2% (realized ~0.3-0.5% profit)
- Change max age to 20 scans (100 minutes) to give time for convergence

#### Problem 4: No Fee Accounting

Bybit spot fees: 0.1% maker / 0.1% taker. On a $100 entry (spot buy + perp sell) + exit (spot sell + perp buy):
- Entry: 2 × 0.1% = $0.20
- Exit: 2 × 0.1% = $0.20
- Total: **$0.40 in fees** on a $100 position

With a 0.5% spread profit ($0.50) and $0.40 in fees, net = $0.10 — barely profitable. Many of the 0.5% entries actually lose money after fees.

**Fix:**
- Account for fees transparently: `net_profit = gross_profit - (value_usdt * 0.004)` — that's 4 legs × 0.1%
- Only enter when `net_profit >= 0.50` (i.e., gross profit must be at least $0.90 for a $100 position)
- Or: raise MIN_SPREAD_PCT to 0.5% (so gross is $0.50+, before fees)
- **Best fix:** Raise MIN_SPREAD_PCT to 0.7% and require net_profit ≥ $0.50 after fee deduction

#### Problem 5: Fee-Free VIP Levels or Use Limit Orders

The current code uses MARKET orders (taker fees = 0.1%). If we use LIMIT orders (maker fees = 0.02-0.04%), fees drop by 5x.

**Fix:**
- For entry: use limit orders that cross the spread. On Bybit spot, place a buy limit at `spot_mid + 0.01%`, on perp place a sell limit at `perp_mid - 0.01%`. If not filled in 30 seconds, abandon.
- This reduces round-trip fees from $0.40 to ~$0.08 on $100

#### Problem 6: Order Size Precision Handling

For very low-priced coins (CLOUDUSDT at $0.02, SLP at $0.0007), the position size in base units is huge (4600+ CLOUD, 135,000+ SLP). This can cause:
- Slippage on market orders for illiquid altcoins
- Rounding errors vs exchange precision rules

**Fix:**
- Add a minimum dollar-value liquidity check: skip symbols where 24h volume < $500K
- Use Bybit's `lotSizeFilter` from market info to get correct step sizes
- Prefer symbols with price > $0.01 to minimize quantity precision issues

#### Problem 7: 147 Open Positions!

There are 166 entries and only 19 exits. The 147 "open" positions are paper trades that were never exited (the cron job may have died, or the spread never triggered any exit condition). This bloats the active_positions list and causes the engine to check exit conditions for all of them on every scan.

**Fix:**
- Add a max-age cleanup that force-exits after 24 hours regardless
- Clear stale positions on engine startup (any entry > 24 hours old with no exit = auto-exit at current spread)

---

### File-by-File Changes: bybit_execution.py

**Key constants to change:**
```python
MIN_SPREAD_PCT = 0.7        # Was 0.3 — accounting for fees
STOP_LOSS_SPREAD = -0.2     # Was 0.05 — only exit when actually losing
TAKE_PROFIT_CONVERGENCE = 0.15  # NEW: exit when spread narrows to ≤ this
MAX_POSITION_AGE_SCANS = 20     # Was 10
MIN_PROFIT_AFTER_FEES = 0.50    # NEW: minimum net profit after fees
FEE_RATE = 0.001              # 0.1% per leg
SYMBOL_COOLDOWN_SECONDS = 3600  # NEW: don't re-enter same symbol for 1 hour
```

**Changes to `_check_exit()` method:**
```python
def _check_exit(self, symbol):
    ...
    current_spread = prices["spread_pct"]
    entry_spread = entry["spread"]
    
    # CORRECTED PnL: profit = convergence (entry_spread - current_spread)
    gross_pnl = (entry_spread - current_spread) / 100 * entry["value_usdt"]
    fees = entry["value_usdt"] * (FEE_RATE * 4)  # 4 legs × 0.1%
    net_pnl = gross_pnl - fees
    
    # EXIT 1: Take profit — spread narrowed significantly
    if current_spread <= TAKE_PROFIT_CONVERGENCE and net_pnl > 0:
        self._execute_exit(symbol, current_spread, net_pnl)
        return
    
    # EXIT 2: Stop loss — spread went deeply negative
    if current_spread <= STOP_LOSS_SPREAD:
        self._execute_exit(symbol, current_spread, net_pnl)
        return
    
    # EXIT 3: Max age
    entry["scan_count"] += 1
    if entry["scan_count"] >= MAX_POSITION_AGE_SCANS:
        self._execute_exit(symbol, current_spread, net_pnl)
        return
```

**Changes to `scan_and_trade()` method:**
```python
# Add cooldown tracking
def __init__(self, ...):
    ...
    self.recent_entries = {}  # symbol -> timestamp

def _is_on_cooldown(self, symbol):
    """Check if symbol was entered in the last SYMBOL_COOLDOWN_SECONDS."""
    if symbol not in self.recent_entries:
        return False
    elapsed = (datetime.utcnow() - self.recent_entries[symbol]).total_seconds()
    return elapsed < SYMBOL_COOLDOWN_SECONDS
```

**Changes to `calculate_position()` method:**
```python
def calculate_position(self, prices):
    spread = prices["spread_pct"]
    if spread < MIN_SPREAD_PCT:
        return None
    
    qty = ...
    value = qty * prices["spot_price"]
    gross_profit = (prices["perp_price"] - prices["spot_price"]) * qty
    fees = value * (FEE_RATE * 4)  # Entry + exit × 2 legs each
    net_profit = gross_profit - fees
    
    if net_profit < MIN_PROFIT_AFTER_FEES:
        return None
    
    return {
        ...
        "profit_usdt": net_profit,
        "gross_profit_usdt": gross_profit,
        "fees_usdt": fees,
    }
```

**Add cleanup on startup:**
```python
def _clean_stale_positions(self):
    """Auto-close positions > 24 hours old with no exit."""
    now = datetime.utcnow()
    to_remove = []
    for sym, pos in self.active_positions.items():
        if "timestamp" in pos:
            entry_time = datetime.fromisoformat(pos["timestamp"])
            if (now - entry_time).total_seconds() > 86400:
                to_remove.append(sym)
    for sym in to_remove:
        del self.active_positions[sym]
```

---

## TASK B: Build Polymarket Execution Engine

### Current State

`polymarket_scanner.py` finds excellent opportunities:
- **GTA VI series arb:** 54.7% spread (YES at 0.80¢ on one expiry vs another)
- `find_series_arbitrage()`: same topic, different expiry — if later expiry costs LESS than earlier, arb
- `find_mispriced_outcomes()`: YES + NO price < 0.90, buy both and redeem at $1
- Cross-platform (Kalshi vs Polymarket)

But there is ZERO execution code. No ability to place trades on Polymarket.

### Polymarket API Architecture

Polymarket uses the **CTF CLOB API** (Central Limit Order Book). Key endpoints:
- `POST /order` — place an order
- `DELETE /order` — cancel an order
- `GET /book?token_id=xyz` — get order book
- `GET /price?token_id=xyz` — get price

Authentication: EIP-712 typed data signatures using an Ethereum wallet (private key).

### File to Create: `polymarket_execution.py`

New file at `/root/arb-scanner/polymarket_execution.py`.

#### Architecture

```python
#!/usr/bin/env python3
"""
Polymarket Arbitrage Execution Engine
Place orders on Polymarket via CLOB API + EIP-712 signatures.
Paper-trade mode by default.
"""

import os
import json
import time
import hashlib
import hmac
from datetime import datetime
from typing import Optional, Dict, Any, List
from web3 import Web3
from eth_account import Account
from eth_account.messages import encode_typed_data

# ==================== CONFIG ====================

POLYMARKET_API = "https://clob.polymarket.com"
POLYGON_RPC = "https://polygon-rpc.com"
POLYMARKET_NEG_RISK_API = "https://clob.polymarket.com"  # Neg-Risk endpoint

# Trading settings
PAPER_TRADE = True
POSITION_SIZE_USDC = 100  # Per opportunity

# Minimum thresholds
MIN_SERIES_SPREAD_PCT = 5.0   # Series arb: need >5% spread to be worth it
MIN_MISPRICED_SPREAD_PCT = 3.0  # Combined outcome: need >3%
MIN_CROSS_SPREAD_PCT = 5.0     # Cross-platform

# Gas settings
GAS_LIMIT = 300000
MAX_GAS_PRICE_GWEI = 200
```

#### Core Components

**Component 1: Wallet & Auth**
```python
class PolymarketWallet:
    """Manages EVM wallet for Polymarket trading."""
    
    def __init__(self, private_key: str = None):
        self.private_key = private_key or os.environ.get("POLYMARKET_PRIVATE_KEY")
        self.account = Account.from_key(self.private_key)
        self.address = self.account.address
        self.web3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
    
    def sign_eip712_order(self, order_data: dict) -> str:
        """Sign a Polymarket order using EIP-712 typed data."""
        domain = {
            "name": "Polymarket CTF",
            "version": "1",
            "chainId": 137,  # Polygon mainnet
        }
        typed_data = {
            "types": {
                "EIP712Domain": [...],
                "Order": [...],
            },
            "domain": domain,
            "primaryType": "Order",
            "message": order_data,
        }
        signed = Account.sign_typed_data(self.account, typed_data)
        return signed.signature.hex()
```

**Component 2: CLOB API Client**
```python
class PolymarketClobClient:
    """HTTP client for Polymarket CLOB API."""
    
    BASE_URL = "https://clob.polymarket.com"
    
    def __init__(self, wallet: PolymarketWallet):
        self.wallet = wallet
    
    def get_token_id(self, condition_id: str, outcome: str) -> str:
        """Get token ID for a specific outcome."""
        ...
    
    def get_order_book(self, token_id: str) -> dict:
        """Fetch order book for a token."""
        resp = requests.get(f"{self.BASE_URL}/book", 
                           params={"token_id": token_id})
        return resp.json()
    
    def place_order(self, token_id: str, side: str, price: float, 
                    size: float) -> dict:
        """Place a limit order on Polymarket."""
        # Build EIP-712 order struct
        order_data = {
            "salt": int(time.time() * 1000),
            "maker": self.wallet.address,
            "signer": self.wallet.address,
            "taker": "0x0000000000000000000000000000000000000000",
            "tokenId": token_id,
            "side": side,  # "BUY" or "SELL"
            "price": str(int(price * 10**6)),  # 6 decimal places
            "size": str(int(size * 10**6)),
            ...
        }
        signature = self.wallet.sign_eip712_order(order_data)
        
        payload = {**order_data, "signature": signature}
        resp = requests.post(f"{self.BASE_URL}/order", json=payload)
        return resp.json()
    
    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        ...
```

**Component 3: Series Arbitrage Execution**
```python
class PolymarketSeriesArb:
    """Execute series arbitrage: buy cheap expiry, sell expensive one."""
    
    def execute(self, opportunity: dict, client: PolymarketClobClient) -> bool:
        """
        opportunity = {
            "keyword": "gta vi",
            "type": "series_arb",
            "cheapest_price": 0.80,
            "most_expensive_price": 0.85,
            "cheapest_market": {...},
            "most_expensive_market": {...},
        }
        
        Strategy:
        - Buy YES on cheapest market (0.80 = 80¢)
        - Buy NO on most expensive market (1 - 0.85 = 0.15 = 15¢)
        - Total cost: 80¢ + 15¢ = 95¢
        - If both settle same way: worst case one pays $1, other $0 = $1
        - Profit: 5¢ per $1 position = 5%
        - OR: If it's same event different expiry, this is calendar spread
        """
        # Convert prices to centered USDC amounts
        size_usdc = POSITION_SIZE_USDC
        
        # Fetch order books for both tokens
        cheap_token_id = client.get_token_id(opp["cheapest_condition_id"], "YES")
        expensive_token_id = client.get_token_id(opp["expensive_condition_id"], "YES")
        
        # Place limit orders to buy cheap + sell expensive
        buy_order = client.place_order(cheap_token_id, "BUY", 
                                        opp["cheapest_price"], size_usdc)
        sell_order = client.place_order(expensive_token_id, "SELL",
                                         opp["most_expensive_price"], size_usdc)
        
        return buy_order.get("success") and sell_order.get("success")
```

**Component 4: Combined Outcome Arbitrage**
```python
class PolymarketCombinedOutcome:
    """Buy both YES and NO when their sum < 1.0."""
    
    def execute(self, opportunity: dict, client) -> bool:
        """Buy YES + NO tokens, redeem at settlement = risk-free profit."""
        # Buy YES
        yes_token = client.get_token_id(opp["condition_id"], "YES")
        client.place_order(yes_token, "BUY", opp["yes_price"], size)
        
        # Buy NO
        no_token = client.get_token_id(opp["condition_id"], "NO")
        client.place_order(no_token, "BUY", opp["no_price"], size)
        
        # To actually redeem: need to call the CTF exchange contract
        # This requires an on-chain transaction
        self._redeem_tokens(condition_id, yes_amount, no_amount)
```

**Component 5: Main Engine**
```python
class PolymarketArbitrageEngine:
    """Main execution engine for Polymarket arb."""
    
    def __init__(self, paper_trade=True):
        self.paper_trade = paper_trade
        self.client = None
        self.wallet = None
        self.logger = PaperTradeLogger()
        
        if not paper_trade:
            self.wallet = PolymarketWallet()
            self.client = PolymarketClobClient(self.wallet)
    
    def execute_opportunity(self, opportunity: dict) -> bool:
        """Execute a single opportunity based on type."""
        if opportunity["type"] == "series_arb":
            return self._execute_series_arb(opportunity)
        elif opportunity["type"] == "combined_outcome":
            return self._execute_combined_outcome(opportunity)
        elif opportunity["type"] == "cross_platform":
            return self._execute_cross_platform(opportunity)
    
    def scan_and_execute(self, opportunities: list):
        """Scan list of opportunities and execute the best ones."""
        opportunities.sort(key=lambda x: x["spread_pct"], reverse=True)
        
        for opp in opportunities[:1]:  # Execute best only
            if opp["spread_pct"] < self._min_spread_for_type(opp["type"]):
                continue
            
            if self.paper_trade:
                self.logger.log_entry(opp)
                print(f"📝 PAPER POLYMARKET: {opp['type']} | "
                      f"Spread: {opp['spread_pct']:.1f}% | "
                      f"Profit: ${opp['profit_per_100']:.2f}/$100")
            else:
                success = self.execute_opportunity(opp)
                if success:
                    self.logger.log_entry(opp)
```

#### New File: `/root/arb-scanner/polymarket_paper_trades.json`

Same format as `paper_trades.json` but for Polymarket trades.

#### Files to Modify

**`run_all.py`**: Add Polymarket execution call after scanner:
```python
from polymarket_execution import PolymarketArbitrageEngine

def main():
    # ... existing scanner runs ...
    
    # NEW: Execute Polymarket opportunities
    poly_results = results.get("polymarket", {})
    if poly_results.get("opportunities", 0) > 0 and poly_results.get("top_5"):
        engine = PolymarketArbitrageEngine(paper_trade=True)
        engine.scan_and_execute(poly_results.get("top_5", []))
```

**`api_server.py`**: Add endpoint for polymarket trades:
```python
elif self.path == "/api/arb/polymarket_trades":
    trades_file = os.path.join(BASE_DIR, "polymarket_paper_trades.json")
    if os.path.exists(trades_file):
        with open(trades_file) as f:
            body = json.dumps(json.load(f)).encode()
    else:
        body = json.dumps([]).encode()
    self._send(200, body, cors)
```

#### Required Dependencies

```
pip install web3 eth-account requests
```

---

## Complete File-by-File Change Summary

### File: `bybit_execution.py` (MAJOR CHANGES)

| Change | Description |
|--------|-------------|
| Constants | Raise MIN_SPREAD_PCT to 0.7, add FEE_RATE=0.001, add SYMBOL_COOLDOWN_SECONDS=3600, add TAKE_PROFIT_CONVERGENCE=0.15, change STOP_LOSS_SPREAD to -0.2 |
| `__init__` | Add `recent_entries: Dict[str, datetime]`, add `_clean_stale_positions()` call |
| `_check_exit()` | **Fix PnL sign**: profit = (entry_spread - current_spread)/100 * value. Add fee deduction. Fix take-profit to trigger on convergence, not expansion |
| `calculate_position()` | Add `net_profit = gross_profit - fees`, require net_profit ≥ $0.50 |
| `scan_and_trade()` | Add cooldown check, add stale position cleanup |
| `_rebuild_active_positions()` | Add check: skip positions > 24h old |

### File: `run_all.py` (MINOR CHANGES)

| Change | Description |
|--------|-------------|
| Import | Add `from polymarket_execution import PolymarketArbitrageEngine` |
| After Polymarket scan | Call `PolymarketArbitrageEngine().scan_and_execute()` |
| JSON output | Include polymarket execution results |

### File: `polymarket_execution.py` (NEW FILE)

Create with:
- `PolymarketWallet` class (EIP-712 signing)
- `PolymarketClobClient` class (HTTP API client)
- `PolymarketSeriesArb` class (series arb execution)
- `PolymarketCombinedOutcome` class (combined outcome arb)
- `PolymarketArbitrageEngine` class (main orchestrator)
- `PaperTradeLogger` class (Polymarket-specific paper trade log)
- `main()` entry point

### File: `api_server.py` (MINOR CHANGES)

| Change | Description |
|--------|-------------|
| New route | `/api/arb/polymarket_trades` — serve polymarket trade history |
| Update `/api/arb/latest` | Include polymarket trades in the combined payload |

### File: `enrich_analysis.py` (MINOR CHANGES)

| Change | Description |
|--------|-------------|
| History | Add polymarket PnL history tracking |
| Analysis | Add polymarket trade count and PnL deltas |

---

## Prioritization for Implementation

**Phase 1 — Fix Crypto (highest priority, ~2 hours)**
1. Fix PnL sign in `_check_exit()` — this alone turns -$15.26 into roughly +$13
2. Add fee calculation to `calculate_position()` — prevents false positives at low spreads
3. Raise MIN_SPREAD_PCT to 0.7 and add symbol cooldown
4. Fix exit thresholds (take profit on convergence, stop loss on negative spread)
5. Add stale position cleanup
6. Test in paper mode for 24 hours

**Phase 2 — Build Polymarket (~4 hours)**
1. Install dependencies (web3, eth-account)
2. Create `polymarket_execution.py` with wallet + CLOB client
3. Implement series arb execution
4. Implement combined outcome arb
5. Implement PaperTradeLogger
6. Wire into `run_all.py`
7. Test in paper mode

**Phase 3 — Go Live (~1 hour)**
1. Set PAPER_TRADE = False in bybit_execution.py
2. Set PAPER_TRADE = False in polymarket_execution.py
3. Add Polymarket API key / wallet env vars
4. Start continuous monitoring
