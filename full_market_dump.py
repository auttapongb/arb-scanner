#!/usr/bin/env python3
"""Full Bybit market dump — all pairs spread analysis."""
import json, subprocess, urllib.request, urllib.parse, os, time

key = os.environ["BYBIT_API_KEY"]
priv = os.environ["BYBIT_API_PRIVATE_KEY_PATH"]

def fetch(cat):
    params = urllib.parse.urlencode({"category": cat})
    ts = str(int(time.time() * 1000))
    ps = f"{ts}{key}5000{params}"
    sig = subprocess.run(
        ["base64", "-w0"],
        input=subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", priv],
            input=ps.encode(), capture_output=True, timeout=5
        ).stdout,
        capture_output=True, timeout=5
    ).stdout.decode().strip()
    req = urllib.request.Request(
        f"https://api.bybit.com/v5/market/tickers?{params}",
        headers={
            "X-BAPI-API-KEY": key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-SIGN": sig,
            "X-BAPI-RECV-WINDOW": "5000",
            "X-BAPI-SIGN-TYPE": "2",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
        result = {}
        for t in data["result"]["list"]:
            result[t["symbol"]] = float(t.get("lastPrice", 0))
        return result

spot = fetch("spot")
perp = fetch("linear")

monitored = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT",
    "DOTUSDT", "LINKUSDT", "AVAXUSDT", "ATOMUSDT", "UNIUSDT", "BNBUSDT",
    "NEARUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "SUIUSDT", "CLOUDUSDT",
    "FHEUSDT", "SOSOUSDT", "TRIAUSDT", "VELOUSDT", "MAGICUSDT",
    "HPOS10IUSDT", "FIGHTUSDT", "BRUSDT", "ALCHUSDT",
]

print("=== MONITORED SYMBOLS ===")
for sym in monitored:
    s = spot.get(sym)
    p = perp.get(sym)
    if s and p and s > 0:
        print(f"{sym:.<16} spot={s:<12.6f} perp={p:<12.6f} spread={((p-s)/s)*100:<+8.2f}%")
    else:
        print(f"{sym:.<16} MISSING")

print()
print("=== ALL POSITIVE SPREADS >= 0.3% ===")
found = 0
for sym in spot:
    if sym not in perp or spot[sym] <= 0 or perp[sym] <= 0:
        continue
    sp = ((perp[sym] - spot[sym]) / spot[sym]) * 100
    if sp >= 0.3:
        qty = 100 / spot[sym]
        profit = (perp[sym] - spot[sym]) * qty
        mon = "(monitored)" if sym in monitored else "(UNMONITORED!)"
        print(f"{sym:.<16} spread={sp:+.2f}% profit=${profit:.2f} {mon}")
        found += 1

if found == 0:
    print("None found — pure backwardation across all pairs.")
else:
    print(f"Total: {found} opportunities >= 0.3%")

print()
print("=== MAJORS SUMMARY ===")
for sym in ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT"]:
    s = spot.get(sym)
    p = perp.get(sym)
    if s and p and s > 0:
        print(f"{sym:.<12} spread={((p-s)/s)*100:+.3f}%  spot={s:.2f} perp={p:.2f}")
    else:
        print(f"{sym:.<12} MISSING")
