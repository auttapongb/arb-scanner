#!/usr/bin/env python3
"""
Realistic Profit Simulation: 48h using ACTUAL current market data
Shows what each bot would earn with $100 capital.
"""
import os, sys, json, subprocess, urllib.request, urllib.parse, time
from datetime import datetime, timezone
from pathlib import Path

BASE = "/root/arb-scanner"

def bybit_api(path, params=None):
    ts = str(int(time.time()*1000))
    rw = "5000"
    api_key = os.environ.get("BYBIT_API_KEY", "")
    q = urllib.parse.urlencode(params) if params else ""
    ps = f"{ts}{api_key}{rw}{q}"
    priv_key = Path("/root/arb-scanner/bybit_private_key_rsa.pem")
    proc = subprocess.run(["openssl","dgst","-sha256","-sign",str(priv_key),"-binary"],
        input=ps.encode(), capture_output=True, timeout=5)
    sig = subprocess.run(["base64","-w0"], input=proc.stdout, capture_output=True, timeout=5).stdout.decode().strip()
    req = urllib.request.Request(f"https://api.bybit.com{path}?{q}" if q else f"https://api.bybit.com{path}",
        headers={"X-BAPI-API-KEY": api_key, "X-BAPI-TIMESTAMP": ts, "X-BAPI-SIGN": sig,
                 "X-BAPI-RECV-WINDOW": rw, "X-BAPI-SIGN-TYPE": "2"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

def simulate_funding_collector(capital=100, pos_size=20):
    """Use REAL funding rates to project profit."""
    resp = bybit_api("/v5/market/tickers", {"category": "linear"})
    if resp.get("retCode") != 0:
        return {"error": resp.get("retMsg")}
    
    rates = []
    for t in resp["result"]["list"]:
        fr = float(t.get("fundingRate", 0) or 0) * 100
        pr = float(t.get("lastPrice", 0) or 0)
        if fr >= 0.05 and pr > 0.000001:
            rates.append({"symbol": t["symbol"], "rate": fr, "price": pr})
    rates.sort(key=lambda x: x["rate"], reverse=True)
    
    max_pos = min(capital // pos_size, 5)
    top = rates[:max_pos]
    
    entry_fees = max_pos * pos_size * 0.0002  # 0.02% maker per entry
    results = []
    total_8h = 0.0
    
    print(f"\n{'='*65}")
    print(f"  FUNDING COLLECTOR — Real Market Data Simulation")
    print(f"  Capital: ${capital} ({max_pos} x ${pos_size})")
    print(f"  Entry fees total: ${entry_fees:.4f}")
    print(f"{'='*65}")
    print(f"\n  {'Symbol':20s} {'Rate%':8s} {'Per 8h':>10s} {'Per day':>10s} {'Per 30d':>10s}")
    print(f"  {'-'*58}")
    
    for c in top:
        per_8h = (c["rate"] / 100) * pos_size
        per_day = per_8h * 3
        per_30d = per_day * 30
        total_8h += per_8h
        results.append({"symbol": c["symbol"], "rate": round(c["rate"], 4), 
                        "price": c["price"], "per_8h": round(per_8h, 4),
                        "per_day": round(per_day, 2), "per_30d": round(per_30d, 2)})
        print(f"  {c['symbol']:20s} {c['rate']:.4f}%  ${per_8h:<8.4f} ${per_day:<7.2f} ${per_30d:<.2f}")
    
    total_day = total_8h * 3
    total_month = total_day * 30
    
    # Realistic: funding decays ~50% over 48h, then positions exit at funding drop
    # First 24h: collect full rate
    first_day = total_day * 0.9  # 90% of ideal (entry timing)
    # Next 24h: rates decay as more people short
    second_day = total_day * 0.6  # 60% of first day
    two_day_funding = first_day + second_day
    
    # Adverse price movement: ~1-2% over 48h (short = price goes up)
    price_loss_pct = 1.5  # moderate adverse move
    price_loss = -(price_loss_pct / 100) * capital
    
    # Exit fees (taker): 0.1% per position
    exit_fees = max_pos * pos_size * 0.001
    
    net_48h = two_day_funding + price_loss - exit_fees
    
    print(f"\n  {'-'*58}")
    print(f"  {'TOTAL':20s} {'':8s}  ${total_8h:<8.4f} ${total_day:<7.2f} ${total_month:<.2f}")
    print(f"\n{'='*65}")
    print(f"  REALISTIC 48h PROJECTION")
    print(f"  Funding collected (day 1):  ${first_day:.4f}")
    print(f"  Funding collected (day 2):  ${second_day:.4f}")
    print(f"  Total funding:              ${two_day_funding:.4f}")
    print(f"  Price loss ({price_loss_pct}%):         ${price_loss:.2f}")
    print(f"  Exit fees:                  ${exit_fees:.4f}")
    print(f"  NET (48h):                  ${net_48h:.2f}")
    if net_48h > 0:
        print(f"  ROI (48h):                  {net_48h/capital*100:.2f}% ✅ PROFIT")
    else:
        print(f"  ROI (48h):                  {net_48h/capital*100:.2f}% ❌ LOSS")
    print(f"{'='*65}")
    
    return {"type": "funding_collector", "capital": capital,
            "positions": results, "funding_8h": round(total_8h, 4),
            "funding_day": round(total_day, 2), "funding_month": round(total_month, 2),
            "net_48h": round(net_48h, 2)}

def simulate_spot_perp_arb(capital=100):
    """Use REAL spreads to project spot-perp arb profit."""
    resp = bybit_api("/v5/market/tickers", {"category": "linear"})
    if resp.get("retCode") != 0:
        return {"error": resp.get("retMsg")}
    
    perp_tickers = {}
    for t in resp["result"]["list"]:
        perp_tickers[t["symbol"]] = t
    
    # Get spot prices for same symbols
    resp_spot = bybit_api("/v5/market/tickers", {"category": "spot"})
    spot_tickers = {}
    if resp_spot.get("retCode") == 0:
        for t in resp_spot["result"]["list"]:
            base = t["symbol"].replace("USDT", "")
            spot_tickers[base] = t
    
    spreads = []
    for sym, pt in perp_tickers.items():
        base = sym.replace("USDT", "")
        # Match spot: bybit spot uses base+USDT format
        spot_sym = f"{base}USDT"
        st = spot_tickers.get(base) or spot_tickers.get(sym) or spot_tickers.get(spot_sym)
        if not st:
            continue
        
        try:
            perp_pr = float(pt.get("lastPrice", 0) or 0)
            spot_pr = float(st.get("lastPrice", 0) or 0)
            if perp_pr <= 0 or spot_pr <= 0:
                continue
            spread = (perp_pr - spot_pr) / spot_pr * 100
            fr = float(pt.get("fundingRate", 0) or 0) * 100
            if abs(spread) >= 0.3:  # minimum threshold
                spreads.append({"symbol": sym, "spread": round(spread, 2),
                               "perp_price": perp_pr, "spot_price": spot_pr,
                               "funding": round(fr, 4)})
        except:
            continue
    
    spreads.sort(key=lambda x: abs(x["spread"]), reverse=True)
    
    # Spot-perp arb: buy spot, short perp. Profit = spread - 2*entry_fees - exit_fees
    entry_fee = 0.001  # 0.1% taker for both legs
    exit_fee = 0.001   # 0.1% to close
    
    total_costs = 2 * entry_fee + exit_fee  # 0.3% total fees
    print(f"\n{'='*65}")
    print(f"  SPOT-PERP ARB — Real Market Data")
    print(f"  Capital: ${capital}")
    print(f"  Round-trip costs: {total_costs*100:.1f}%")
    print(f"{'='*65}")
    print(f"\n  {'Symbol':20s} {'Spread':8s} {'Net Profit':>12s} {'Funding':>8s}")
    print(f"  {'-'*48}")
    
    profitable = []
    for s in spreads:
        net = s["spread"] - total_costs * 100
        profit = net / 100 * capital
        status = "✅" if net > 0 else "❌"
        print(f"  {s['symbol']:20s} {s['spread']:>+.2f}%  {'${:>7.2f}'.format(profit):>12s}  {status}  f={s['funding']:.2f}%")
        if net > 0:
            profitable.append(s)
    
    print(f"\n  Profitable opportunities: {len(profitable)}")
    if not profitable:
        print(f"  ❌ Spot-perp arb is UNPROFITABLE on Bybit right now")
        print(f"     Fees ({total_costs*100:.1f}%) eat all spreads")
    
    return {"type": "spot_perp_arb", "capital": capital, "fee_cost_pct": total_costs * 100,
            "profitable_opps": len(profitable),
            "best_spread": spreads[0]["spread"] if spreads else 0,
            "best_symbol": spreads[0]["symbol"] if spreads else ""}

def main():
    print(f"\n{'#'*70}")
    print(f"#  BOT PROFITABILITY COMPARISON")
    print(f"#  Using REAL Bybit market data @ {datetime.now(timezone.utc).strftime('%H:%M UTC')}")
    print(f"#  Each bot starts with $100 paper capital")
    print(f"{'#'*70}")
    
    print(f"\n{'='*70}")
    print(f"  BOT 1: FUNDING COLLECTOR (short high-funding perps)")
    print(f"{'='*70}")
    
    fc = simulate_funding_collector(capital=100)
    
    print(f"\n{'='*70}")
    print(f"  BOT 2: SPOT-PERP ARBITRAGE (buy spot + short perp)")
    print(f"{'='*70}")
    
    sa = simulate_spot_perp_arb(capital=100)
    
    print(f"\n{'#'*70}")
    print(f"#  VERDICT")
    print(f"{'#'*70}")
    
    if fc.get("net_48h", -1) > 0 and sa.get("profitable_opps", 0) == 0:
        print(f"  ✅ **Funding Collector is winner**")
        print(f"     Est 48h profit: ${fc['net_48h']:.2f}")
        print(f"     Spot-perp arb has ZERO profitable opportunities")
    elif fc.get("net_48h", -1) <= 0 and sa.get("profitable_opps", 0) > 0:
        print(f"  ✅ **Spot-perp arb is winner**")
    elif fc.get("net_48h", -1) <= 0 and sa.get("profitable_opps", 0) == 0:
        print(f"  ❌ **NEITHER is profitable right now**")
    else:
        print(f"  Both have potential — funding collector projected ${fc.get('net_48h', 0):.2f}")
        print(f"                                     spot-perp: {sa.get('profitable_opps', 0)} opps")
    
    print(f"{'#'*70}")

if __name__ == "__main__":
    main()
