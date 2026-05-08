#!/usr/bin/env python3
"""
Enriched analysis generator for arbitrage scans.
Builds historical trend data and analysis payload for the dashboard.
Called after each scan completes.
"""

import json
import os
import urllib.request
from datetime import datetime

BASE_DIR = "/root/arb-scanner"
HISTORY_FILE = os.path.join(BASE_DIR, "data", "history.json")
SCAN_FILE = os.path.join(BASE_DIR, "last_scan.json")
ENRICHED_FILE = os.path.join(BASE_DIR, "data", "enriched.json")

os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)


def load_scan():
    """Load the latest scan results."""
    if not os.path.exists(SCAN_FILE):
        return None
    with open(SCAN_FILE) as f:
        return json.load(f)


def load_history():
    """Load historical trend data."""
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE) as f:
            return json.load(f)
    return {"crypto": [], "polymarket": [], "timestamps": []}


def save_history(history):
    """Save updated history."""
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f)


def build_enriched(scan, history):
    """Build the enriched analysis payload."""
    now = datetime.utcnow().isoformat()
    
    # Append current scan data to history
    history["timestamps"].append(now)
    
    crypto_opps = scan.get("crypto", {}).get("opportunities", 0) if scan else 0
    poly_opps = scan.get("polymarket", {}).get("opportunities", 0) if scan else 0
    
    history["crypto"].append(crypto_opps)
    history["polymarket"].append(poly_opps)
    
    # Keep last 96 entries (48h at 30min intervals)
    for key in ["crypto", "polymarket"]:
        history[key] = history[key][-96:]
    history["timestamps"] = history["timestamps"][-96:]
    
    # Build analysis
    analysis = {
        "total_crypto_opps": crypto_opps,
        "total_polymarket_opps": poly_opps,
        "best_crypto": None,
        "best_polymarket": None,
    }
    
    if scan:
        crypto = scan.get("crypto", {})
        polymarket = scan.get("polymarket", {})
        
        if crypto.get("top_5"):
            analysis["best_crypto"] = crypto["top_5"][0]
        
        if polymarket.get("top_5"):
            analysis["best_polymarket"] = polymarket["top_5"][0]
    
    # Calculate change from previous scan
    crypto_delta = 0
    poly_delta = 0
    if len(history["crypto"]) >= 2:
        crypto_delta = history["crypto"][-1] - history["crypto"][-2]
        analysis["crypto_delta"] = crypto_delta
    if len(history["polymarket"]) >= 2:
        poly_delta = history["polymarket"][-1] - history["polymarket"][-2]
        analysis["polymarket_delta"] = poly_delta
    
    enriched = {
        "status": "ok",
        "timestamp": now,
        "trends": {
            "crypto_history": history["crypto"],
            "polymarket_history": history["polymarket"],
            "timestamps": history["timestamps"],
        },
        "analysis": analysis,
    }
    
    return enriched, history


def push_to_api(enriched):
    """POST enriched data to the API server."""
    try:
        payload = json.dumps(enriched).encode()
        req = urllib.request.Request(
            "http://localhost:8080/api/arb/enriched",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=5)
        print(f"✅ Enriched analysis pushed to API (status={resp.status})")
    except Exception as e:
        print(f"❌ Failed to push: {e}")


def save_enriched(enriched):
    """Save enriched data locally."""
    with open(ENRICHED_FILE, "w") as f:
        json.dump(enriched, f, indent=2)
    print(f"✅ Enriched saved to {ENRICHED_FILE}")


def main():
    scan = load_scan()
    if not scan:
        print("❌ No scan data found")
        return 1
    
    history = load_history()
    enriched, updated_history = build_enriched(scan, history)
    save_history(updated_history)
    save_enriched(enriched)
    push_to_api(enriched)
    
    # Print summary
    a = enriched["analysis"]
    print(f"📊 Analysis generated:")
    print(f"  Crypto opportunities: {a['total_crypto_opps']}")
    print(f"  Polymarket opportunities: {a['total_polymarket_opps']}")
    if a.get("crypto_delta"):
        print(f"  Crypto change: {'+' if a['crypto_delta'] > 0 else ''}{a['crypto_delta']}")
    if a.get("best_crypto"):
        bc = a["best_crypto"]
        print(f"  Best crypto: {bc['symbol']} @ {bc['spread_pct']}% = ${bc['profit_usdt']}")
    if a.get("best_polymarket"):
        bp = a["best_polymarket"]
        print(f"  Best polymarket: {bp['spread_pct']}% = ${bp.get('profit_per_100','?')}/$100")
    
    return 0


if __name__ == "__main__":
    exit(main())
