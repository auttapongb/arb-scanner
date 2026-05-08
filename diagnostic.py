#!/usr/bin/env python3
"""Diagnostic: fetch all spreads and paper trade history."""
import os, sys, json, urllib.request, urllib.parse, subprocess, time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_KEY = os.environ.get("BYBIT_API_KEY", "")
PRIV_KEY_PATH = os.environ.get("BYBIT_API_PRIVATE_KEY_PATH", "/root/.bybit/private.pem")

def bybit_get(path, params=None):
    q = urllib.parse.urlencode(params) if params else ""
    url = f"https://api.bybit.com{path}" + (f"?{q}" if q else "")
    ts = str(int(time.time() * 1000))
    param_str = f"{ts}{API_KEY}5000{q}"
    sig_proc = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", PRIV_KEY_PATH, "-binary"],
        input=param_str.encode(), capture_output=True, timeout=5
    )
    sig = subprocess.run(
        ["base64", "-w0"], input=sig_proc.stdout, capture_output=True, timeout=5
    ).stdout.decode().strip()
    headers = {
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-SIGN": sig,
        "X-BAPI-RECV-WINDOW": "5000",
        "X-BAPI-SIGN-TYPE": "2",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

# Fetch all tickers
print("=== Fetching tickers from Bybit ===")
spot = bybit_get("/v5/market/tickers", {"category": "spot"})
perp = bybit_get("/v5/market/tickers", {"category": "linear"})

if spot.get("retCode") != 0:
    print(f"ERROR spot: {spot}")
    sys.exit(1)
if perp.get("retCode") != 0:
    print(f"ERROR perp: {perp}")
    sys.exit(1)

spot_prices = {t["symbol"]: float(t["lastPrice"]) for t in spot["result"]["list"]}
perp_prices = {t["symbol"]: float(t["lastPrice"]) for t in perp["result"]["list"]}

# Symbols the engine monitors
symbols = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT",
    "DOGEUSDT", "DOTUSDT", "LINKUSDT", "AVAXUSDT",
    "ATOMUSDT", "UNIUSDT", "BNBUSDT",
    "NEARUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "SUIUSDT",
    "MOVRUSDT", "SOSOUSDT", "GMXUSDT", "CLOUDUSDT", "SQDUSDT",
    "TACUSDT", "FHEUSDT", "TRIAUSDT",
]

print(f"\n=== Spread Analysis ({len(symbols)} symbols) ===")
results = []
for sym in symbols:
    if sym not in spot_prices or sym not in perp_prices:
        print(f"  {sym}: MISSING (spot={sym in spot_prices}, perp={sym in perp_prices})")
        continue
    if spot_prices[sym] <= 0:
        print(f"  {sym}: spot price is 0")
        continue
    sp = (perp_prices[sym] - spot_prices[sym]) / spot_prices[sym] * 100
    qty = 100 / spot_prices[sym]
    profit = qty * (perp_prices[sym] - spot_prices[sym])
    results.append({
        "symbol": sym,
        "spot_price": spot_prices[sym],
        "perp_price": perp_prices[sym],
        "spread_pct": round(sp, 2),
        "profit_100usdt": round(profit, 4),
    })

results.sort(key=lambda x: x["spread_pct"], reverse=True)

print(f"{'Symbol':<12} {'Spread%':>8} {'Spot':>14} {'Perp':>14} {'Profit$100':>12} {'Eligible?':>10}")
print("-" * 70)
entry_count = 0
for r in results:
    eligible = r["spread_pct"] >= 0.3 and r["spread_pct"] <= 5.0
    if eligible and r["profit_100usdt"] >= 0.50:
        entry_count += 1
    flag = "✅" if (eligible and r["profit_100usdt"] >= 0.50) else ("⚠️" if (eligible and r["profit_100usdt"] < 0.50) else "")
    print(f"{r['symbol']:<12} {r['spread_pct']:>7.2f}% {r['spot_price']:>14.8f} {r['perp_price']:>14.8f} {r['profit_100usdt']:>11.4f} {flag:>10}")

print(f"\n=== Summary ===")
print(f"Total symbols with prices: {len(results)}")
print(f"Symbols above MIN_SPREAD (0.3%): {sum(1 for r in results if r['spread_pct'] >= 0.3 and r['spread_pct'] <= 5.0)}")
print(f"Symbols above profit threshold ($0.50): {entry_count}")
if results:
    top5 = results[:5]
    print(f"\nTop 5 opportunities:")
    for r in top5:
        print(f"  {r['symbol']}: {r['spread_pct']}% spread, ${r['profit_100usdt']:.2f} profit per $100")

# Check paper_trades.json
print(f"\n=== Paper Trade History ===")
pt_path = os.path.join(BASE_DIR, "paper_trades.json")
if os.path.exists(pt_path):
    with open(pt_path) as f:
        trades = json.load(f)
    entries = [t for t in trades if t["type"] == "ENTRY"]
    exits = [t for t in trades if t["type"] == "EXIT"]
    print(f"Total entries: {len(entries)}")
    print(f"Total exits: {len(exits)}")
    print(f"Active (unexited): {len(entries) - len(exits)}")
    if entries:
        print(f"\nRecent entries:")
        for t in entries[-10:]:
            print(f"  {t['timestamp'][:19]} | {t['symbol']} | spread={t['spread_pct']}% | value=${t['value_usdt']} | target=${t.get('profit_target_usdt',0)}")
    if exits:
        print(f"\nRecent exits:")
        for t in exits[-5:]:
            print(f"  {t.get('exit_timestamp','?')[:19]} | {t['symbol']} | exit_spread={t.get('exit_spread_pct','?')}% | pnl=${t.get('pnl_usdt',0)}")
else:
    print("No paper_trades.json found")
