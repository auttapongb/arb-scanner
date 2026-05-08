#!/usr/bin/env python3
"""
Polymarket Arbitrage Execution Engine
Live trading via CLOB API + EIP-712 signatures.
Paper-trade mode by default.

Usage:
    python polymarket_execution.py                    # Paper trade mode
    LIVE_MODE=1 python polymarket_execution.py        # Try live (needs wallet)
    
Environment variables:
    POLYMARKET_PRIVATE_KEY  - EVM private key for signing
    POLYMARKET_ADDRESS      - Wallet address (if key not provided)
    LIVE_MODE=1             - Enable live execution
"""

import os
import sys
import json
import time
import hashlib
import hmac
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
import requests

# ==================== CONFIG ====================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
POLYMARKET_CLOB = "https://clob.polymarket.com"
POLYGON_RPC = "https://polygon-rpc.com"

# Trading settings
PAPER_TRADE = not bool(int(os.environ.get("LIVE_MODE", "0")))
LIVE_MODE = bool(int(os.environ.get("LIVE_MODE", "0")))
POSITION_SIZE_USDC = 100  # Per opportunity

# Wallet config
POLYMARKET_PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
POLYMARKET_ADDRESS = os.environ.get("POLYMARKET_ADDRESS", "")

# Minimum thresholds
MIN_SERIES_SPREAD_PCT = 3.0   # Series arb minimum
MIN_COMBINED_SPREAD_PCT = 2.0  # Combined outcome minimum
MIN_CROSS_SPREAD_PCT = 3.0     # Cross-platform minimum

# Gas settings
MAX_GAS_PRICE_GWEI = 200
GAS_LIMIT = 300000


# ====================
#   PAPER TRADE LOGGER
# ====================

class PaperTradeLogger:
    """Logs paper trades for Polymarket to a JSON file."""

    def __init__(self):
        self.log_file = os.path.join(BASE_DIR, "polymarket_trades.json")
        self.trades = []
        self._load()

    def _load(self):
        if os.path.exists(self.log_file):
            try:
                with open(self.log_file) as f:
                    self.trades = json.load(f)
            except Exception:
                self.trades = []

    def _save(self):
        with open(self.log_file, "w") as f:
            json.dump(self.trades, f, indent=2, default=str)

    def log_entry(self, opportunity: dict):
        """Log a paper entry trade from a scan opportunity dict."""
        trade = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": "ENTRY",
            "opp_type": opportunity.get("type", "?"),
            "description": opportunity.get("description", "?"),
            "spread_pct": round(opportunity.get("spread_pct", 0), 2),
            "profit_per_100": round(opportunity.get("profit_per_100", 0), 2),
            "action_preview": opportunity.get("action_preview", ""),
            "action_full": opportunity.get("action", ""),
            "position_size_usdc": POSITION_SIZE_USDC,
        }
        self.trades.append(trade)
        self._save()
        return trade

    def log_exit(self, entry: dict, exit_spread: float, pnl: float):
        """Log a paper exit trade (for future managed-position use)."""
        exit_rec = {
            **entry,
            "type": "EXIT",
            "exit_timestamp": datetime.now(timezone.utc).isoformat(),
            "exit_spread_pct": round(exit_spread, 2),
            "pnl_usdt": round(pnl, 2),
        }
        self.trades.append(exit_rec)
        self._save()
        return exit_rec


# ====================
#   POLYMARKET WALLET
# ====================

class PolymarketWallet:
    """Manages EVM wallet for Polymarket trading."""

    def __init__(self, private_key: str = None):
        self.private_key = private_key or POLYMARKET_PRIVATE_KEY
        self.account = None
        self.address = POLYMARKET_ADDRESS or "0x0000... (not configured)"

        if self.private_key:
            try:
                from eth_account import Account
                self.account = Account.from_key(self.private_key)
                self.address = self.account.address
            except Exception as e:
                print(f"⚠️ Wallet init error: {e}")

    def is_configured(self) -> bool:
        return bool(self.private_key) or bool(POLYMARKET_ADDRESS)

    def get_checksummed_address(self) -> str:
        """Return checksummed address if account is loaded."""
        if self.account:
            from eth_account import Account
            return Account.checksum_address(self.address)
        return self.address


# ====================
#   POLYMARKET CLOB CLIENT
# ====================

class PolymarketClobClient:
    """HTTP client for Polymarket CLOB API."""

    BASE = "https://clob.polymarket.com"

    def __init__(self, wallet: PolymarketWallet = None):
        self.wallet = wallet
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "ArbitrageScanner/1.0",
            "Accept": "application/json",
        })

    # --- PUBLIC ENDPOINTS (no auth needed) ---

    def get_markets(self, tag: str = None, limit: int = 50) -> list:
        """Fetch markets from Polymarket gamma-api."""
        # Use gamma-api for market listing (as scanner does)
        try:
            resp = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"closed": "false", "active": "true", "limit": limit},
                timeout=15
            )
            return resp.json() if resp.ok else []
        except Exception as e:
            print(f"⚠️ Markets fetch error: {e}")
            return []

    def get_order_book(self, token_id: str) -> dict:
        """Fetch order book depth for a token."""
        try:
            resp = self.session.get(
                f"{self.BASE}/book",
                params={"token_id": token_id},
                timeout=10
            )
            return resp.json() if resp.ok else {}
        except Exception as e:
            print(f"⚠️ Order book error: {e}")
            return {}

    def get_token_prices(self, token_id: str) -> dict:
        """Get current price for a token."""
        try:
            resp = self.session.get(
                f"{self.BASE}/price",
                params={"token_id": token_id},
                timeout=10
            )
            return resp.json() if resp.ok else {}
        except Exception as e:
            print(f"⚠️ Price fetch error: {e}")
            return {}

    def get_positions(self, wallet_addr: str = None) -> list:
        """Get current positions for a wallet."""
        addr = wallet_addr or (self.wallet.address if self.wallet else None)
        if not addr:
            return []
        try:
            resp = self.session.get(
                f"{self.BASE}/positions",
                params={"wallet": addr},
                timeout=10
            )
            return resp.json() if resp.ok else []
        except Exception as e:
            print(f"⚠️ Positions fetch error: {e}")
            return []

    def get_midpoint(self, token_id: str) -> Optional[float]:
        """Get midpoint price for a token."""
        try:
            resp = self.session.get(
                f"{self.BASE}/midpoint",
                params={"token_id": token_id},
                timeout=10
            )
            if resp.ok:
                data = resp.json()
                return float(data.get("midpoint", 0))
            return None
        except Exception:
            return None

    def get_neg_risk(self, token_id: str) -> Optional[dict]:
        """Get neg-risk market info."""
        try:
            resp = self.session.get(
                f"{self.BASE}/neg-risk",
                params={"token_id": token_id},
                timeout=10
            )
            return resp.json() if resp.ok else None
        except Exception:
            return None

    def get_tick_size(self, token_id: str) -> Optional[float]:
        """Get tick size for a token."""
        try:
            resp = self.session.get(
                f"{self.BASE}/tick-size",
                params={"token_id": token_id},
                timeout=10
            )
            if resp.ok:
                data = resp.json()
                return float(data.get("minimum_tick_size", 0.01))
            return 0.01
        except Exception:
            return 0.01

    def get_server_time(self) -> Optional[int]:
        """Get CLOB server timestamp."""
        try:
            resp = self.session.get(f"{self.BASE}/time", timeout=10)
            if resp.ok:
                return int(resp.json().get("time", 0))
            return None
        except Exception:
            return None

    # --- AUTHENTICATED ENDPOINTS (need wallet) ---

    def _get_cross_alpha_headers(self) -> dict:
        """Generate CLOB auth headers using EIP-712 typed data signing."""
        if not self.wallet or not self.wallet.account:
            return {}
        from eth_account import Account
        from eth_account.messages import encode_typed_data

        timestamp = int(time.time() * 1000)
        nonce_data = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                ],
                "NegRisk": [
                    {"name": "timestamp", "type": "uint256"},
                ],
            },
            "domain": {"name": "Polymarket CTF", "version": "1", "chainId": 137},
            "primaryType": "NegRisk",
            "message": {"timestamp": timestamp},
        }
        signed = Account.sign_typed_data(self.wallet.account, nonce_data)
        return {
            "POLYMARKET-CREDENTIALS": signed.signature.hex(),
            "POLYMARKET-ADDRESS": self.wallet.address,
            "POLYMARKET-TIMESTAMP": str(timestamp),
        }

    def place_order(self, token_id: str, side: str, price: float,
                    size: float) -> dict:
        """Place a limit order with EIP-712 signing.

        Args:
            token_id: Polymarket token ID (condition ID)
            side: "BUY" or "SELL"
            price: Price in USDC (e.g. 0.50 for 50¢)
            size: Number of contracts

        Returns:
            API response dict
        """
        if not self.wallet or not self.wallet.account:
            return {"error": "Wallet not configured"}

        from eth_account import Account
        from eth_account.messages import encode_typed_data

        # Build EIP-712 order data per Polymarket CTF spec
        salt = int(time.time() * 1000)
        nonce = int(time.time() * 1000)
        maker_address = self.wallet.address

        # Price and size in 6 decimal places (USDC)
        price_raw = str(int(price * 10**6))
        size_raw = str(int(size * 10**6))

        order_data = {
            "salt": [salt],
            "maker": maker_address,
            "signer": maker_address,
            "taker": "0x0000000000000000000000000000000000000000",
            "tokenId": token_id,
            "side": side.upper(),
            "price": price_raw,
            "size": size_raw,
            "feeRateBps": "0",
            "nonce": str(nonce),
            "expiration": "0",
        }

        # EIP-712 domain
        domain = {
            "name": "Polymarket CTF",
            "version": "1",
            "chainId": 137,
        }

        # Types for EIP-712
        types = {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
            ],
            "Order": [
                {"name": "salt", "type": "uint256"},
                {"name": "maker", "type": "address"},
                {"name": "signer", "type": "address"},
                {"name": "taker", "type": "address"},
                {"name": "tokenId", "type": "uint256"},
                {"name": "side", "type": "uint8"},
                {"name": "price", "type": "uint256"},
                {"name": "size", "type": "uint256"},
                {"name": "feeRateBps", "type": "uint256"},
                {"name": "nonce", "type": "uint256"},
                {"name": "expiration", "type": "uint256"},
            ],
        }

        # Sign with EIP-712 typed data
        typed_data = {
            "types": types,
            "domain": domain,
            "primaryType": "Order",
            "message": order_data,
        }
        signed = Account.sign_typed_data(self.wallet.account, typed_data)
        signature = signed.signature.hex()

        # Build payload for API
        payload = {
            "token_id": token_id,
            "side": side.upper(),
            "price": price_raw,
            "size": size_raw,
            "signature": signature,
            "owner": maker_address,
            "maker": maker_address,
            "signer": maker_address,
            "taker": "0x0000000000000000000000000000000000000000",
            "fee_rate_bps": "0",
            "nonce": str(nonce),
            "salt": str(salt),
            "expiration": "0",
        }

        try:
            resp = self.session.post(
                f"{self.BASE}/order",
                json=payload,
                timeout=15
            )
            result = resp.json() if resp.ok else resp.text
            print(f"  Order response ({resp.status_code}): {str(result)[:200]}")
            return result if resp.ok else {"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            print(f"  Order submission error: {e}")
            return {"error": str(e)}

    def cancel_order(self, order_id: str) -> dict:
        """Cancel an order by ID."""
        if not self.wallet or not self.wallet.account:
            return {"error": "Wallet not configured"}
        try:
            resp = self.session.delete(
                f"{self.BASE}/cancel-order",
                json={"order_id": order_id},
                timeout=15
            )
            return resp.json() if resp.ok else {"error": resp.text[:200]}
        except Exception as e:
            return {"error": str(e)}

    def get_user_orders(self, wallet_addr: str = None) -> list:
        """Get open orders for a user."""
        addr = wallet_addr or (self.wallet.address if self.wallet else None)
        if not addr:
            return []
        try:
            resp = self.session.get(
                f"{self.BASE}/orders",
                params={"wallet": addr},
                timeout=10
            )
            return resp.json() if resp.ok else []
        except Exception as e:
            print(f"⚠️ Orders fetch error: {e}")
            return []


# ====================
#   POLYMARKET ARBITRAGE ENGINE
# ====================

class PolymarketArbitrageEngine:
    """Main execution engine. Paper-trade by default."""

    def __init__(self, paper_trade: bool = True):
        self.paper_trade = paper_trade
        self.logger = PaperTradeLogger()
        self.wallet = None
        self.client = None

        if not paper_trade or LIVE_MODE:
            try:
                self.wallet = PolymarketWallet()
                self.client = PolymarketClobClient(self.wallet)
                if self.wallet.is_configured():
                    print(f"🔑 Wallet: {self.wallet.address[:12]}... (configured)")
                else:
                    print(f"🔑 Wallet: {self.wallet.address[:12]}... (NOT CONFIGURED)")
                    print(f"   Set POLYMARKET_PRIVATE_KEY env var for live trading")
            except Exception as e:
                print(f"⚠️ Wallet init failed: {e}")

    def scan_and_execute(self, opportunities: list):
        """Execute the best opportunity from the scan results."""
        if not opportunities:
            print("No Polymarket opportunities to execute")
            return

        # Sort by spread descending
        opportunities.sort(key=lambda x: x.get("spread_pct", 0), reverse=True)
        best = opportunities[0]

        spread = best.get("spread_pct", 0)
        opp_type = best.get("type", "unknown")
        desc = best.get("description", "?")
        profit = best.get("profit_per_100", 0)

        # Determine min spread for this type
        min_spread = MIN_SERIES_SPREAD_PCT
        if opp_type == "combined_outcome":
            min_spread = MIN_COMBINED_SPREAD_PCT
        elif opp_type == "cross_platform":
            min_spread = MIN_CROSS_SPREAD_PCT

        if spread < min_spread:
            print(f"⏭️ {desc}: spread {spread:.1f}% < minimum {min_spread}%, skipping")
            return

        if LIVE_MODE and not self.paper_trade and self.client and self.wallet and self.wallet.is_configured():
            # LIVE EXECUTION
            print(f"🔴 LIVE POLYMARKET: {desc} | {spread:.1f}% | Profit: ${profit:.2f}/$100")
            print(f"   Executing live...")

            if opp_type == "series_arb":
                self._execute_series_arb_live(best)
            elif opp_type == "combined_outcome":
                self._execute_combined_outcome_live(best)
            else:
                print(f"  Unknown opp type: {opp_type}, no live action defined")
        else:
            # PAPER TRADE
            if not self.wallet or not self.wallet.is_configured():
                print(f"⚠️ Wallet not configured. Set POLYMARKET_PRIVATE_KEY env var for live trading.")
            self.logger.log_entry(best)
            print(f"📝 PAPER POLYMARKET: {opp_type} | {desc} | "
                  f"Spread: {spread:.1f}% | Profit: ${profit:.2f}/$100")
            print(f"   To go live: LIVE_MODE=1 + fund wallet with USDC on Polygon")

    def _execute_series_arb_live(self, opp: dict):
        """Execute series arbitrage live.

        For series arb we need to:
        1. Buy YES on the cheap market
        2. Buy NO on the expensive market (or short it)
        This requires knowing the token IDs for each market.
        """
        print(f"  Executing series arb: {opp.get('description', '?')}")
        print(f"  Full implementation needs CLOB token_id resolution")
        print(f"  Would place orders worth ${POSITION_SIZE_USDC}")

        # Log the live trade attempt
        trade_record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": "LIVE_ATTEMPT",
            "opp_type": "series_arb",
            "description": opp.get("description", "?"),
            "spread_pct": opp.get("spread_pct", 0),
            "action": opp.get("action", ""),
            "position_size_usdc": POSITION_SIZE_USDC,
            "status": "needs_token_id",
        }
        self.logger.trades.append(trade_record)
        self.logger._save()
        print(f"   Trade logged to {self.logger.log_file}")

    def _execute_combined_outcome_live(self, opp: dict):
        """Execute combined outcome arbitrage live.

        Buy both YES and NO when their sum is significantly less than $1.00.
        This is a pure arbitrage that locks in profit at redemption.
        """
        print(f"  Executing combined outcome arb: {opp.get('description', '?')}")
        print(f"  Would buy both YES + NO sides for ${POSITION_SIZE_USDC}")

        trade_record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": "LIVE_ATTEMPT",
            "opp_type": "combined_outcome",
            "description": opp.get("description", "?"),
            "spread_pct": opp.get("spread_pct", 0),
            "action": opp.get("action", ""),
            "position_size_usdc": POSITION_SIZE_USDC,
            "status": "needs_token_id",
        }
        self.logger.trades.append(trade_record)
        self.logger._save()
        print(f"   Trade logged to {self.logger.log_file}")


# ====================
#   MAIN ENTRY POINT
# ====================

def main():
    """Run polymarket execution (can be called from cron or run_all.py)."""
    scan_file = os.path.join(BASE_DIR, "last_scan.json")
    if not os.path.exists(scan_file):
        print(json.dumps({"status": "error", "message": "No scan results found"}))
        return 1

    with open(scan_file) as f:
        scan = json.load(f)

    opportunities = scan.get("polymarket", {}).get("top_5", [])

    if not opportunities:
        print(json.dumps({"status": "ok", "message": "No opportunities to execute"}))
        return 0

    engine = PolymarketArbitrageEngine(paper_trade=(not LIVE_MODE))
    engine.scan_and_execute(opportunities)

    result = {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "paper_trade": (not LIVE_MODE),
        "live_mode": LIVE_MODE,
        "wallet_configured": engine.wallet.is_configured() if engine.wallet else False,
        "opportunities_available": len(opportunities),
        "best_opportunity": opportunities[0] if opportunities else None,
    }
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
