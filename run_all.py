#!/usr/bin/env python3
"""Run all three strategies and output combined results"""
import sys, os, json
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Auto-source env if available
env_files = ["/root/.bybit_env", os.path.join(BASE_DIR, ".env")]
for ef in env_files:
    if os.path.exists(ef):
        print(f"Loading env from {ef}")
        # Simple env parser
        with open(ef) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip().strip("'\"")

results = {}

# 1. Funding collector
print("\n=== FUNDING COLLECTOR ===")
try:
    from funding_collector import FundingCollector
    col = FundingCollector(paper_trade=(os.environ.get("LIVE_MODE", "0") != "1"))
    col.run_once()
    results["funding"] = {
        "status": "ok",
        "active_positions": len(col.active_shorts),
        "total_pnl": col.logger.get_total_pnl(),
    }
except Exception as e:
    print(f"Funding collector error: {e}")
    results["funding"] = {"status": "error", "message": str(e)}

# 2. Crypto arb (if env available)
print("\n=== CRYPTO SPOT-PERP ARB ===")
try:
    from bybit_execution import BybitArbitrageEngine
    engine = BybitArbitrageEngine(paper_trade=True)
    opps = engine.scan_and_trade()
    results["crypto_arb"] = {
        "status": "ok",
        "opportunities_found": len(opps),
        "active_positions": len(engine.active_positions),
    }
except Exception as e:
    print(f"Crypto arb error: {e}")
    results["crypto_arb"] = {"status": "error", "message": str(e)}

# 3. Polymarket scanner + execution
print("\n=== POLYMARKET ===")
try:
    # Scanner
    from polymarket_scanner import main as poly_scan
    poly_scan()
    
    # Execution
    from polymarket_execution import PolymarketArbitrageEngine
    scan_file = os.path.join(BASE_DIR, "last_scan.json")
    if os.path.exists(scan_file):
        with open(scan_file) as f:
            scan = json.load(f)
        opps_poly = scan.get("polymarket", {}).get("top_5", [])
        if opps_poly:
            pm = PolymarketArbitrageEngine(paper_trade=True)
            pm.scan_and_execute(opps_poly)
            results["polymarket"] = {"status": "ok", "opportunities": len(opps_poly), "executed": True}
        else:
            results["polymarket"] = {"status": "ok", "opportunities": 0, "executed": False}
except Exception as e:
    print(f"Polymarket error: {e}")
    results["polymarket"] = {"status": "error", "message": str(e)}

# Output
results["timestamp"] = datetime.now(timezone.utc).isoformat()
print(f"\n{'='*50}")
print("COMBINED RESULTS:")
print(json.dumps(results, indent=2, default=str))
print(f"{'='*50}")
