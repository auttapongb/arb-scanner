#!/usr/bin/env python3
"""
Bybit Spot Grid Bot Setup — deploy one grid on BTC/USDT.
Runs PRE-validation then creation with full error handling.

Usage:
  source /root/.bybit_env && python3 setup_grid_bot.py --live

Safe to run in dry-run mode (without --live) to validate parameters.
"""

import os, sys, json, requests, time
from datetime import datetime
from safety import SafeBybitAPI, make_safe_get, make_safe_post

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ─── Config ────────────────────────────────────────────────────────────────
SYMBOL = "BTCUSDT"
MIN_PRICE = "76000"      # Lower bound: -6% from ~$81K
MAX_PRICE = "86000"      # Upper bound: +6% from ~$81K
CELL_NUMBER = 10         # Grid lines (minimum 2, max 54 per validate)
INVESTMENT_USDT = 100    # Total capital to commit (leave $26.54 reserve)
INVEST_MODE = 0          # 0 = quote only (USDT)

# ─── API Setup ─────────────────────────────────────────────────────────────
BYBIT_API_KEY = os.environ.get("BYBIT_API_KEY", "")
BYBIT_PRIV_KEY_PATH = os.environ.get("BYBIT_API_PRIVATE_KEY_PATH",
                                      "/root/.bybit/private.pem")
BYBIT_BASE_URL = "https://api.bybit.com"

if not BYBIT_API_KEY:
    print(json.dumps({"status": "error", "msg": "BYBIT_API_KEY not set"}))
    sys.exit(1)

api = SafeBybitAPI(BYBIT_BASE_URL, BYBIT_API_KEY, BYBIT_PRIV_KEY_PATH)
bybit_get = make_safe_get(api)
bybit_post = make_safe_post(api)

LIVE_MODE = "--live" in sys.argv


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def validate_grid():
    """Run pre-validation via /v5/grid/validate-input."""
    log(f"Validating grid: {SYMBOL} ${MIN_PRICE}–${MAX_PRICE}, "
        f"{CELL_NUMBER} grids, ${INVESTMENT_USDT}")
    
    r = api.post("/v5/grid/validate-input", body={
        "symbol": SYMBOL,
        "min_price": MIN_PRICE,
        "max_price": MAX_PRICE,
        "cell_number": CELL_NUMBER,
    })
    
    if r.get("retCode") != 0:
        log(f"✗ Validation failed: {r.get('retMsg', '?')}")
        return False
    
    result = r.get("result", {})
    check_code = result.get("check_code", "UNKNOWN")
    
    if "SUCCESS" in check_code:
        log(f"✓ Validation passed: {check_code}")
        
        # Print grid economics
        profit_range = result.get("profit", {})
        investment_range = result.get("investment", {})
        cell_dist = result.get("cell_distance", "?")
        log(f"  Cell distance: ${cell_dist}")
        log(f"  Investment range: ${investment_range.get('from','?')} – "
            f"${investment_range.get('to','?')}")
        
        if profit_range:
            per_trade = float(profit_range.get("from", 0))
            log(f"  Est profit/trade: ${per_trade:.4f} ({profit_range.get('from','?')}% – "
                f"{profit_range.get('to','?')}%)")
        
        # Check if our investment is in range
        inv_from = float(investment_range.get("from", 0))
        inv_to = float(investment_range.get("to", 0))
        if inv_from <= INVESTMENT_USDT <= inv_to:
            log(f"✓ Investment ${INVESTMENT_USDT} is within "
                f"valid range (${inv_from:.0f}–${inv_to:.0f})")
        else:
            log(f"✗ Investment ${INVESTMENT_USDT} OUTSIDE "
                f"valid range! Need ${inv_from:.0f}–${inv_to:.0f}")
            return False
        
        # Cell number check
        cn = result.get("cell_number", {})
        if cn.get("from") and cn.get("to"):
            c_min = int(cn["from"])
            c_max = int(cn["to"])
            if c_min <= CELL_NUMBER <= c_max:
                log(f"✓ Grid count {CELL_NUMBER} within valid "
                    f"range ({c_min}–{c_max})")
            else:
                log(f"✗ Grid count {CELL_NUMBER} OUTSIDE range "
                    f"({c_min}–{c_max})")
                return False
        
        return True
    else:
        log(f"✗ Validation failed: {check_code}")
        log(f"  Full response: {json.dumps(result, indent=2)}")
        return False


def check_balance():
    """Verify sufficient USDT in spot wallet."""
    r = bybit_get("/v5/account/wallet-balance", {
        "accountType": "UNIFIED", "coin": "USDT"
    })
    
    if r.get("retCode") != 0:
        log(f"✗ Balance check failed: {r.get('retMsg', '?')}")
        return False
    
    equity = float(r["result"]["list"][0]["coin"][0]["equity"])
    wallet = float(r["result"]["list"][0]["coin"][0]["walletBalance"])
    order_im = float(r["result"]["list"][0]["coin"][0].get("totalOrderIM", 0))
    pos_im = float(r["result"]["list"][0]["coin"][0].get("totalPositionIM", 0))
    available = equity - order_im - pos_im
    
    log(f"Wallet: ${wallet:.2f} | Equity: ${equity:.2f} | "
        f"Available: ${available:.2f}")
    
    if available < INVESTMENT_USDT:
        log(f"✗ Insufficient funds! Need ${INVESTMENT_USDT}, "
            f"have ${available:.2f}")
        return False
    
    # Leave 20% reserve for safety
    reserve_needed = INVESTMENT_USDT * 0.20
    if available < INVESTMENT_USDT + reserve_needed:
        log(f"⚠ Low reserve: using ${INVESTMENT_USDT} of ${available:.2f} "
            f"(only ${available - INVESTMENT_USDT:.2f} left)")
    else:
        log(f"✓ Reserve after grid: ${available - INVESTMENT_USDT:.2f}")
    
    return True


def check_existing_grids():
    """Check that no active grid bot already exists on this symbol."""
    r = api.post("/v5/grid/query-active-grid", body={
        "symbol": SYMBOL, "category": "spot", "limit": 10
    })
    
    if r.get("retCode") == 0:
        result = r.get("result", {})
        all_grids = result.get("all_grids", [])
        active = [g for g in all_grids 
                  if g.get("status") in ("NEW", "RUNNING")]
        
        if active:
            log(f"✗ Active grid already exists on {SYMBOL}: "
                f"grid_id={active[0].get('grid_id', '?')}")
            return False
        log("✓ No existing active grid on BTCUSDT")
        
        # Handle completed/cancelled grids
        completed = [g for g in all_grids 
                     if g.get("status") in ("COMPLETED", "CANCELLED")]
        if completed:
            log(f"  ({len(completed)} previous completed grid(s) found)")
    
    return True


def check_market_hours():
    """Check that spot market is open for BTCUSDT."""
    r = bybit_get("/v5/market/instruments-info", {
        "category": "spot", "symbol": SYMBOL
    })
    
    if r.get("retCode") == 0:
        status = r["result"]["list"][0].get("status", "")
        if status != "Trading":
            log(f"✗ Market {SYMBOL} not trading (status={status})")
            return False
        log(f"✓ {SYMBOL} market is open (status={status})")
    return True


def create_grid():
    """Create the spot grid bot."""
    if not LIVE_MODE:
        log("⚠ DRY RUN — pass --live to actually create")
        log(f"\nWould create grid:")
        log(f"  Symbol:     {SYMBOL}")
        log(f"  Range:      ${MIN_PRICE} – ${MAX_PRICE}")
        log(f"  Grids:      {CELL_NUMBER}")
        log(f"  Investment: ${INVESTMENT_USDT} USDT")
        log(f"  Mode:       quote_only")
        return {"status": "dry_run"}
    
    log(f"\nCreating grid...")
    r = api.post("/v5/grid/create-grid", body={
        "symbol": SYMBOL,
        "min_price": MIN_PRICE,
        "max_price": MAX_PRICE,
        "cell_number": CELL_NUMBER,
        "invest_mode": INVEST_MODE,
        "total_investment": str(INVESTMENT_USDT),
    })
    
    if r.get("retCode") != 0:
        log(f"✗ Grid creation FAILED: {r.get('retMsg', '?')}")
        log(f"  Full: {json.dumps(r, indent=2)[:1000]}")
        return {"status": "error", "detail": r}
    
    grid_id = r.get("result", {}).get("grid_id", "?")
    log(f"✓ Grid created! ID: {grid_id}")
    
    # Optionally query detail to confirm
    time.sleep(2)
    dr = api.post("/v5/grid/query-grid-detail", body={"grid_id": grid_id})
    if dr.get("retCode") == 0:
        detail = dr.get("result", {}).get("detail", {})
        log(f"  Status: {detail.get('status', '?')}")
        log(f"  Investment: ${detail.get('investment', '?')}")
        log(f"  Grid APR: {float(detail.get('grid_apr', 0)) * 100:.2f}%")
        log(f"  Total APR: {float(detail.get('total_apr', 0)) * 100:.2f}%")
    
    return {"status": "ok", "grid_id": grid_id}


def save_grid_config(grid_id=None):
    """Save grid config for later monitoring/reference."""
    config = {
        "type": "spot_grid",
        "symbol": SYMBOL,
        "min_price": MIN_PRICE,
        "max_price": MAX_PRICE,
        "cell_number": CELL_NUMBER,
        "investment": INVESTMENT_USDT,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "grid_id": grid_id,
    }
    config_path = os.path.join(BASE_DIR, ".grid_config.json")
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
    log(f"✓ Config saved to {config_path}")


def main():
    log(f"{'='*50}")
    log(f"  BYBIT SPOT GRID BOT — SETUP")
    log(f"  {SYMBOL} | ${INVESTMENT_USDT} | {CELL_NUMBER} grids")
    log(f"{'='*50}")
    
    # 1. Check market is open
    if not check_market_hours():
        return 1
    
    # 2. Check balance
    if not check_balance():
        return 1
    
    # 3. Check no duplicate grid
    if not check_existing_grids():
        return 1
    
    # 4. Validate parameters
    if not validate_grid():
        return 1
    
    # 5. Create grid
    result = create_grid()
    if result.get("status") == "error":
        return 1
    
    grid_id = result.get("grid_id")
    save_grid_config(grid_id)
    
    log(f"\n{'='*50}")
    log(f"  SETUP COMPLETE")
    log(f"  Grid ID: {grid_id or 'dry_run'}")
    log(f"  API: POST /v5/grid/query-grid-detail (grid_id={grid_id})")
    log(f"  Monitor: POST /v5/grid/query-active-grid")
    log(f"  Close:   POST /v5/grid/close-grid (close_mode=3 for USDT)")
    log(f"{'='*50}")
    
    return 0


if __name__ == "__main__":
    from datetime import timezone
    sys.exit(main())
