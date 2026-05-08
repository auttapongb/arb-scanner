#!/usr/bin/env python3
"""
Funding Collector — short perpetuals with the HIGHEST positive funding rates on Bybit.
No spot hedge. Pure short, directional risk managed by stop-loss.
Collects funding payments every 8 hours, exits when funding drops below threshold.
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

PAPER_TRADE = True
LIVE_MODE = False

# Funding collection settings
MAX_POSITIONS = 5               # Max simultaneous shorts
POSITION_SIZE_USDT = 20         # $20 per position (5 x $20 = $100 total)
MIN_FUNDING_RATE_PCT = 0.05     # Minimum 0.05% per 8h to enter
EXIT_FUNDING_RATE_PCT = 0.01    # Exit when funding drops below 0.01%
STOP_LOSS_PRICE_PCT = 3.0       # Exit if price goes up 3% against us
MAX_HOLD_HOURS = 48             # Force-exit after 48 hours
CHECK_INTERVAL_MINUTES = 5      # Check positions every 5 min (cron)
FEE_RATE = 0.001                # Taker (if limit fails, fallback)
LIMIT_FEE_RATE = 0.0002         # Maker fee for PostOnly

# We ONLY short — no spot buy needed
# No hedge = simpler, less capital, just directional risk

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
        "User-Agent": "bybit-funding-collector/1.0",
        "X-Referer": "bybit-funding-collector",
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


# ==================== PAPER TRADE LOGGER ====================

class FundingPaperTradeLogger:
    def __init__(self):
        self.log_file = os.path.join(BASE_DIR, "funding_trades.json")
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

    def log_entry(self, symbol, funding_rate, entry_price, qty, value):
        trade = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": "ENTRY",
            "symbol": symbol,
            "funding_rate_pct": round(funding_rate, 4),
            "entry_price": entry_price,
            "qty": qty,
            "value_usdt": round(value, 2),
        }
        self.trades.append(trade)
        self._save()
        return trade

    def log_exit(self, entry, exit_price, exit_funding, pnl):
        exit_rec = {
            **entry,
            "type": "EXIT",
            "exit_timestamp": datetime.now(timezone.utc).isoformat(),
            "exit_price": exit_price,
            "exit_funding_rate_pct": round(exit_funding, 4),
            "pnl_usdt": round(pnl, 2),
        }
        self.trades.append(exit_rec)
        self._save()
        return exit_rec

    def get_total_pnl(self):
        return sum(t.get("pnl_usdt", 0) for t in self.trades if t.get("type") == "EXIT")


# ==================== FUNDING COLLECTOR ====================

class FundingCollector:
    def __init__(self, paper_trade=True):
        self.paper_trade = paper_trade
        self.logger = FundingPaperTradeLogger()
        self.active_shorts = {}  # symbol -> position details
        self._rebuild_active_positions()

    def _rebuild_active_positions(self):
        """Rebuild active_shorts from funding_trades.json so positions
        persist across cron invocations."""
        entries_by_symbol = {}
        exits_by_symbol = set()
        for trade in self.logger.trades:
            if trade["type"] == "EXIT":
                exits_by_symbol.add(trade["symbol"])
            elif trade["type"] == "ENTRY":
                sym = trade["symbol"]
                if sym not in entries_by_symbol or trade["timestamp"] > entries_by_symbol[sym]["timestamp"]:
                    entries_by_symbol[sym] = trade

        for sym, trade in entries_by_symbol.items():
            if sym not in exits_by_symbol:
                self.active_shorts[sym] = {
                    "symbol": sym,
                    "entry_price": trade.get("entry_price", 0),
                    "entry_funding_rate": trade.get("funding_rate_pct", 0),
                    "qty": trade.get("qty", 0),
                    "value_usdt": trade.get("value_usdt", 0),
                    "timestamp": trade.get("timestamp", datetime.now(timezone.utc).isoformat()),
                }

    def scan_funding_rates(self):
        """
        Fetch all linear perpetual funding rates from Bybit.
        Returns list of {symbol, fundingRate, price, markPrice, indexPrice}
        sorted by fundingRate descending (highest first).
        """
        resp = bybit_get("/v5/market/tickers", {"category": "linear"})
        if resp.get("retCode") != 0:
            print(f"❌ Failed to fetch tickers: {resp.get('retMsg')}")
            return []

        candidates = []
        for t in resp["result"]["list"]:
            fr = float(t.get("fundingRate", 0) or 0) * 100  # Convert to %
            price = float(t.get("lastPrice", 0) or 0)

            # Filter: must have meaningful funding and price
            if fr >= MIN_FUNDING_RATE_PCT and price > 0.000001:
                candidates.append({
                    "symbol": t["symbol"],
                    "funding_rate_pct": fr,
                    "price": price,
                    "mark_price": float(t.get("markPrice", price) or price),
                    "index_price": float(t.get("indexPrice", price) or price),
                })

        candidates.sort(key=lambda x: x["funding_rate_pct"], reverse=True)
        return candidates

    def check_positions(self):
        """Check all active shorts for exit conditions: funding drop, stop-loss, max age."""
        # Re-fetch current tickers
        resp = bybit_get("/v5/market/tickers", {"category": "linear"})
        tickers = {}
        if resp.get("retCode") == 0:
            for t in resp["result"]["list"]:
                tickers[t["symbol"]] = t

        for symbol in list(self.active_shorts.keys()):
            pos = self.active_shorts[symbol]

            if symbol not in tickers:
                print(f"⚠️ No ticker for {symbol}, skipping")
                continue

            t = tickers[symbol]
            current_fr = float(t.get("fundingRate", 0) or 0) * 100
            current_price = float(t.get("lastPrice", 0) or 0)
            entry_price = pos["entry_price"]

            # Price change (negative for shorts = good, positive = bad)
            price_change_pct = ((current_price - entry_price) / entry_price) * 100

            # Time held
            entry_time = datetime.fromisoformat(pos["timestamp"].replace("Z", "+00:00"))
            hours_held = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600

            # EXIT CONDITIONS
            exit_reason = None

            # 1. Funding rate dropped below exit threshold — take profit (funding collected)
            if current_fr < EXIT_FUNDING_RATE_PCT and current_fr >= 0:
                exit_reason = f"funding dropped to {current_fr:.4f}%"

            # 2. Stop loss — price moved against us
            if price_change_pct >= STOP_LOSS_PRICE_PCT:
                exit_reason = f"stop loss (price +{price_change_pct:.2f}%)"

            # 3. Max age
            if hours_held >= MAX_HOLD_HOURS:
                exit_reason = f"max age ({hours_held:.1f}h)"

            if exit_reason:
                # Calculate PnL: funding collected + price change
                funding_collected = hours_held / 8 * pos["entry_funding_rate"]  # ~ per 8h period
                price_pnl = -price_change_pct  # Short: price up = negative
                total_pnl_pct = funding_collected + price_pnl
                total_pnl = total_pnl_pct / 100 * pos["value_usdt"]

                self._close_position(symbol, current_price, current_fr, total_pnl, exit_reason)

    def find_new_entries(self, candidates):
        """Find best entries among candidates, respecting MAX_POSITIONS limit."""
        current_count = len(self.active_shorts)
        slots_available = MAX_POSITIONS - current_count

        if slots_available <= 0:
            print(f"📊 Max positions ({MAX_POSITIONS}) reached, no new entries")
            return

        # Filter: skip symbols we already have
        new_candidates = [c for c in candidates if c["symbol"] not in self.active_shorts]

        print(f"📊 {slots_available} slots available, {len(new_candidates)} candidates")

        for c in new_candidates[:slots_available]:
            self._open_short(c)

    def _open_short(self, candidate):
        """Open a short position."""
        symbol = candidate["symbol"]
        fr = candidate["funding_rate_pct"]
        price = candidate["price"]

        qty = POSITION_SIZE_USDT / price

        # Round to proper precision
        if symbol == "BTCUSDT":
            qty = round(qty, 6)
        elif symbol == "ETHUSDT":
            qty = round(qty, 5)
        else:
            qty = round(qty, 4)

        if qty <= 0:
            return

        value = qty * price

        if self.paper_trade:
            self.logger.log_entry(symbol, fr, price, qty, value)
            self.active_shorts[symbol] = {
                "symbol": symbol,
                "entry_price": price,
                "entry_funding_rate": fr,
                "qty": qty,
                "value_usdt": value,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            print(f"📝 PAPER SHORT: {symbol} @ ${price:.4f} | funding={fr:.4f}% | ${value:.0f}")
        else:
            # LIVE MODE — place PostOnly sell order
            self._execute_live_short(symbol, qty, price, fr)

    def _execute_live_short(self, symbol, qty, price, funding_rate):
        """Place real short on Bybit."""
        try:
            order = bybit_post("/v5/order/create", {
                "category": "linear",
                "symbol": symbol,
                "side": "Sell",
                "orderType": "Limit",
                "qty": str(qty),
                "price": str(round(price * 0.9995, 6)),  # Slightly below mid
                "timeInForce": "PostOnly",
                "positionIdx": 0,
            })
            if order.get("retCode") != 0:
                # Fallback to market
                order = bybit_post("/v5/order/create", {
                    "category": "linear",
                    "symbol": symbol,
                    "side": "Sell",
                    "orderType": "Market",
                    "qty": str(qty),
                    "positionIdx": 0,
                })
                if order.get("retCode") != 0:
                    print(f"❌ SHORT FAILED: {symbol} — {order.get('retMsg')}")
                    return

            print(f"✅ LIVE SHORT: {symbol} @ ${price:.4f} | Order: {order['result']['orderId']}")
            self.active_shorts[symbol] = {
                "symbol": symbol,
                "entry_price": price,
                "entry_funding_rate": funding_rate,
                "qty": qty,
                "value_usdt": qty * price,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "order_id": order["result"]["orderId"],
            }
        except Exception as e:
            print(f"❌ SHORT FAILED: {symbol} — {e}")

    def _close_position(self, symbol, current_price, current_fr, pnl, reason):
        """Close a short position."""
        entry = self.active_shorts.pop(symbol, None)
        if not entry:
            return

        if self.paper_trade:
            self.logger.log_exit(entry, current_price, current_fr, pnl)
            print(f"📝 PAPER CLOSE: {symbol} | ${entry['entry_price']:.4f}→${current_price:.4f} | PnL=${pnl:.2f} | Reason: {reason}")
        else:
            # LIVE — buy back to close
            try:
                order = bybit_post("/v5/order/create", {
                    "category": "linear",
                    "symbol": symbol,
                    "side": "Buy",
                    "orderType": "Limit",
                    "qty": str(entry["qty"]),
                    "price": str(round(current_price * 1.0005, 6)),
                    "timeInForce": "PostOnly",
                    "positionIdx": 0,
                    "reduceOnly": True,
                })
                if order.get("retCode") != 0:
                    order = bybit_post("/v5/order/create", {
                        "category": "linear",
                        "symbol": symbol,
                        "side": "Buy",
                        "orderType": "Market",
                        "qty": str(entry["qty"]),
                        "positionIdx": 0,
                        "reduceOnly": True,
                    })
                print(f"✅ CLOSE: {symbol} PnL=${pnl:.2f}")
            except Exception as e:
                print(f"❌ CLOSE FAILED: {symbol} — {e}")

    def run_once(self):
        """Single scan cycle (for cron)."""
        print(f"\n{'='*50}")
        print(f"🚀 Funding Collector Run @ {datetime.now(timezone.utc).isoformat()[:19]}")
        print(f"{'='*50}")

        # Step 1: Check existing positions
        if self.active_shorts:
            print(f"\n📊 Active positions: {len(self.active_shorts)}")
            self.check_positions()

        # Step 2: Scan for new entries
        print(f"\n🔍 Scanning funding rates...")
        candidates = self.scan_funding_rates()
        print(f"   Found {len(candidates)} symbols with funding >= {MIN_FUNDING_RATE_PCT}%")

        if candidates:
            print(f"\n   Top candidates:")
            for c in candidates[:10]:
                mark = " (active)" if c["symbol"] in self.active_shorts else ""
                print(f"     {c['symbol']:15s} funding={c['funding_rate_pct']:.4f}% price=${c['price']:.4f}{mark}")

        # Step 3: Enter new positions
        self.find_new_entries(candidates)

        # Summary
        total_pnl = sum(p.get("pnl_usdt", 0) for p in self.logger.trades if p.get("type") == "EXIT")
        open_count = len(self.active_shorts)
        print(f"\n{'='*50}")
        print(f"📈 Summary: {open_count} open | Total realized PnL: ${total_pnl:.2f}")
        if open_count > 0:
            avg_funding = sum(p["entry_funding_rate"] for p in self.active_shorts.values()) / open_count
            print(f"   Avg funding rate: {avg_funding:.4f}% | Est daily: ${avg_funding*3*20/100*open_count:.2f}")
        print(f"{'='*50}")

        return candidates


def main():
    if not BYBIT_API_KEY:
        print(json.dumps({"status": "error", "message": "No BYBIT_API_KEY set"}))
        return 1
    if not os.path.exists(BYBIT_PRIV_KEY_PATH):
        print(json.dumps({"status": "error", "message": f"Private key not found: {BYBIT_PRIV_KEY_PATH}"}))
        return 1

    collector = FundingCollector(paper_trade=(not LIVE_MODE))
    collector.run_once()

    result = {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "paper_trade": (not LIVE_MODE),
        "active_positions": len(collector.active_shorts),
        "total_pnl": collector.logger.get_total_pnl(),
    }
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
