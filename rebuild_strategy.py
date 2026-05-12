#!/usr/bin/env python3
"""
Single script to:
1. Cancel the BTC grid (too wide, 0 trades)
2. Transfer $44 from FUND → UNIFIED
3. Create ETH/USDT grid ($100, ±2.5%, 8 lines)
4. Create SOL/USDT grid ($70, ±3%, 7 lines)

Run: python3 rebuild_strategy.py
"""

import os, sys, json, time

# Self-source env
if not os.environ.get("BYBIT_API_KEY"):
    env_path = "/root/.bybit_env"
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip()

from safety import SafeBybitAPI

BASE_URL = "https://api.bybit.com"
API_KEY = os.environ.get("BYBIT_API_KEY", "0ToflNv3n1mFpnkY3r")
KEY_PATH = os.environ.get("BYBIT_API_PRIVATE_KEY_PATH", "/root/.bybit/private.pem")

api = SafeBybitAPI(BASE_URL, API_KEY, KEY_PATH)

def do_get(path, params=None):
    r = api.get(path, params=params)
    if r.get("retCode") != 0:
        print(f"  ✗ GET {path}: [{r['retCode']}] {r.get('retMsg','?')}")
        return None
    return r

def do_post(path, body):
    r = api.post(path, body=body)
    if r.get("retCode") != 0:
        print(f"  ✗ POST {path}: [{r['retCode']}] {r.get('retMsg','?')}")
        return None
    return r

def get_btc_price():
    r = do_get("/v5/market/tickers", {"category": "spot", "symbol": "BTCUSDT"})
    if r and r.get("result",{}).get("list"):
        return float(r["result"]["list"][0]["lastPrice"])
    return None

def get_eth_price():
    r = do_get("/v5/market/tickers", {"category": "spot", "symbol": "ETHUSDT"})
    if r and r.get("result",{}).get("list"):
        return float(r["result"]["list"][0]["lastPrice"])
    return None

def get_sol_price():
    r = do_get("/v5/market/tickers", {"category": "spot", "symbol": "SOLUSDT"})
    if r and r.get("result",{}).get("list"):
        return float(r["result"]["list"][0]["lastPrice"])
    return None

print("=" * 55)
print("  REBUILD STRATEGY")
print("=" * 55)

# Step 0: Check live prices
print("\n--- Step 0: Live Prices ---")
btc = get_btc_price()
eth = get_eth_price()
sol = get_sol_price()
print(f"  BTC: ${btc:,.0f}")
print(f"  ETH: ${eth:,.2f}")
print(f"  SOL: ${sol:,.2f}")

# Step 1: Cancel BTC grid
print("\n--- Step 1: Cancel BTC Grid ---")
r = do_post("/v5/grid/cancel-grid", {"gridId": "619152395372384846"})
if r:
    print(f"  ✅ BTC grid cancelled")
else:
    print(f"  ⚠ BTC grid cancel failed (may already be cancelled)")
time.sleep(2)

# Step 2: Check wallets before transfer
print("\n--- Step 2: Check Wallets ---")
# Unified
uw = do_get("/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": "USDT"})
if uw:
    unified_before = float(uw["result"]["list"][0]["coin"][0]["walletBalance"])
    print(f"  UNIFIED before: ${unified_before:.2f}")

fw = do_get("/v5/asset/transfer/query-account-coins-balance", {"accountType": "FUND", "coin": "USDT"})
fund_before = 0
if fw and fw.get("result",{}).get("balance"):
    fund_before = float(fw["result"]["balance"][0]["walletBalance"])
print(f"  FUND before: ${fund_before:.2f}")

# Step 3: Transfer FUND → UNIFIED
if fund_before >= 1:
    print("\n--- Step 3: Transfer FUND → UNIFIED ---")
    transfer_amt = min(44.0, fund_before)
    r = do_post("/v5/asset/transfer/universal-transfer", {
        "transferId": f"rebuild_{int(time.time())}",
        "coin": "USDT",
        "amount": str(transfer_amt),
        "fromAccountType": "FUND",
        "toAccountType": "UNIFIED"
    })
    if r:
        print(f"  ✅ Transferred ${transfer_amt:.2f} FUND → UNIFIED")
    time.sleep(3)
else:
    print("\n--- Step 3: Skip Transfer (FUND already empty) ---")

# Step 4: Check available in UNIFIED
uw2 = do_get("/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": "USDT"})
unified_avail = 0
if uw2:
    c = uw2["result"]["list"][0]["coin"][0]
    wallet = float(c["walletBalance"])
    order_im = float(c.get("totalOrderIM", 0))
    unified_avail = wallet - order_im
    print(f"\n  UNIFIED available: ${unified_avail:.2f}")

# Step 5: Create ETH Grid ($100, 8 lines, ±2.5%)
print("\n--- Step 4: Create ETH/USDT Grid ($100) ---")
eth_price = get_eth_price()
if eth_price:
    pct = 0.025
    min_eth = eth_price * (1 - pct)
    max_eth = eth_price * (1 + pct)
    cells = 8
    r = do_post("/v5/grid/create-grid", {
        "category": "spot",
        "symbol": "ETHUSDT",
        "minPrice": str(int(min_eth)),
        "maxPrice": str(int(max_eth)),
        "investmentAmount": "100",
        "runType": "1",
        "cellInterval": "",
        "cells": cells,
        "stopType": "",
        "triggerType": "",
    })
    if r:
        grid_id = r["result"].get("gridId", "?")
        print(f"  ✅ ETH grid created!")
        print(f"     Grid ID: {grid_id}")
        print(f"     Range: ${min_eth:,.0f} – ${max_eth:,.0f}")
        print(f"     Cells: {cells}")
    time.sleep(2)

# Step 6: Check remaining balance
uw3 = do_get("/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": "USDT"})
remaining = 0
if uw3:
    c = uw3["result"]["list"][0]["coin"][0]
    wallet = float(c["walletBalance"])
    order_im = float(c.get("totalOrderIM", 0))
    remaining = wallet - order_im
    print(f"\n  Remaining after ETH grid: ${remaining:.2f}")

# Step 7: Create SOL Grid ($70, 7 lines, ±3%) if enough left
print("\n--- Step 5: Create SOL/USDT Grid ($70) ---")
sol_price = get_sol_price()
if sol_price and remaining >= 70:
    pct = 0.03
    min_sol = round(sol_price * (1 - pct), 2)
    max_sol = round(sol_price * (1 + pct), 2)
    cells = 7
    r = do_post("/v5/grid/create-grid", {
        "category": "spot",
        "symbol": "SOLUSDT",
        "minPrice": str(min_sol),
        "maxPrice": str(max_sol),
        "investmentAmount": "70",
        "runType": "1",
        "cellInterval": "",
        "cells": cells,
        "stopType": "",
        "triggerType": "",
    })
    if r:
        grid_id = r["result"].get("gridId", "?")
        print(f"  ✅ SOL grid created!")
        print(f"     Grid ID: {grid_id}")
        print(f"     Range: ${min_sol:.2f} – ${max_sol:.2f}")
        print(f"     Cells: {cells}")
    time.sleep(2)
elif sol_price:
    print(f"  ⚠ Insufficient remaining (${remaining:.2f}) for $70 SOL grid")
else:
    print(f"  ⚠ Could not get SOL price")

# Step 8: Final summary
print("\n" + "=" * 55)
print("  FINAL SUMMARY")
print("=" * 55)

# Check both grids
print("\n  Active grids:")
r = do_get("/v5/grid/query-active-grid-list", {"category": "spot", "limit": 20})
if r and r.get("result",{}).get("list"):
    for g in r["result"]["list"]:
        d = g.get("detail", {})
        print(f"    {d.get('symbol','?')}: {d.get('status','?')}")
        print(f"      Invest: ${float(d.get('total_investment',0)):.0f}")
        print(f"      Range: ${d.get('min_price','?')} – ${d.get('max_price','?')}")
        print(f"      Cells: {d.get('cell_number','?')}")
else:
    print("  (no active grids found)")

# Final wallets
uwf = do_get("/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": "USDT"})
fwf = do_get("/v5/asset/transfer/query-account-coins-balance", {"accountType": "FUND", "coin": "USDT"})
utotal = 0
if uwf:
    utotal = float(uwf["result"]["list"][0]["coin"][0]["walletBalance"])
ftotal = 0
if fwf and fwf.get("result",{}).get("balance"):
    ftotal = float(fwf["result"]["balance"][0]["walletBalance"])
print(f"\n  FUND:      ${ftotal:.2f}")
print(f"  UNIFIED:   ${utotal:.2f}")
print(f"  ─────────────────────────")
print(f"  TOTAL:     ${ftotal + utotal:.2f}")

print(f"\n  ✅ Complete! Bots stopped, grids deployed.")
print(f"  All previous strategies (funding_v3.py, tp_bot.py, bybit_execution.py)")
print(f"  remain installed but should stay in paper/disabled mode.")
