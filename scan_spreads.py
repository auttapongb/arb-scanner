#!/usr/bin/env python3
"""Quick scan: show spreads for all symbols without executing trades."""
import os, sys, json, time, subprocess, urllib.request, urllib.parse
from datetime import datetime

base_url = "https://api.bybit.com"

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT",
    "DOGEUSDT", "DOTUSDT", "LINKUSDT", "AVAXUSDT", "MATICUSDT",
    "ATOMUSDT", "UNIUSDT", "BNBUSDT", "PEPEUSDT", "SHIBUSDT",
    "NEARUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "SUIUSDT",
]

def fetch_tickers(category):
    url = f"{base_url}/v5/market/tickers?category={category}"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())

spot_raw = fetch_tickers("spot")
perp_raw = fetch_tickers("linear")

spot_map = {}
if spot_raw.get("retCode") == 0:
    for t in spot_raw["result"]["list"]:
        try:
            spot_map[t["symbol"]] = float(t["lastPrice"])
        except:
            pass

perp_map = {}
if perp_raw.get("retCode") == 0:
    for t in perp_raw["result"]["list"]:
        try:
            perp_map[t["symbol"]] = float(t["lastPrice"])
        except:
            pass

results = []
for sym in SYMBOLS:
    sp = spot_map.get(sym)
    pp = perp_map.get(sym)
    if sp and pp and sp > 0:
        spread = ((pp - sp) / sp) * 100
        results.append({"symbol": sym, "spot": sp, "perp": pp, "spread_pct": round(spread, 4)})

results.sort(key=lambda x: abs(x["spread_pct"]), reverse=True)

print(f"=== Bybit Spot vs Perpetual Spreads @ {datetime.utcnow().isoformat()} ===")
print(f"{'Symbol':<12} {'Spot Price':<16} {'Perp Price':<16} {'Spread %':<10}")
print("-" * 56)
for r in results:
    flag = " <<< OPPORTUNITY" if abs(r["spread_pct"]) >= 0.3 else ""
    print(f"{r['symbol']:<12} {r['spot']:<16.8f} {r['perp']:<16.8f} {r['spread_pct']:<+10.4f}{flag}")

print(f"\nTotal symbols checked: {len(results)}")
print(f"Symbols with |spread| >= 0.3%: {sum(1 for r in results if abs(r['spread_pct']) >= 0.3)}")
