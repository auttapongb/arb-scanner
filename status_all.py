#!/usr/bin/env python3
"""
Consolidated status — UNIFIED wallet + FUND wallet + grid + perp.
Queries Bybit API directly. Self-sources env vars.

Use: python3 status_all.py
"""

import os, sys, json
from datetime import datetime, timezone

# Self-source env vars if not set
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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BYBIT_API_KEY = os.environ.get("BYBIT_API_KEY", "")
BYBIT_PRIV_KEY_PATH = os.environ.get("BYBIT_API_PRIVATE_KEY_PATH",
                                      "/root/.bybit/private.pem")
BYBIT_BASE_URL = "https://api.bybit.com"

api = SafeBybitAPI(BYBIT_BASE_URL, BYBIT_API_KEY, BYBIT_PRIV_KEY_PATH)


def get_funding_wallet():
    """FUND wallet via /v5/asset/transfer/query-account-coins-balance."""
    r = api.get("/v5/asset/transfer/query-account-coins-balance",
                {"accountType": "FUND", "coin": "USDT"})
    if r.get("retCode") != 0:
        return {"error": r.get("retMsg", "?")}
    balances = {}
    for b in r.get("result", {}).get("balance", []):
        bal = float(b.get("walletBalance", 0))
        if bal > 0:
            balances[b["coin"]] = bal
    return balances


def get_unified_wallet():
    """UNIFIED trading wallet."""
    r = api.get("/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": "USDT"})
    if r.get("retCode") != 0:
        return {"error": r.get("retMsg", "?")}
    c = r["result"]["list"][0]["coin"][0]
    return {
        "wallet_balance": float(c["walletBalance"]),
        "equity": float(c["equity"]),
        "position_im": float(c.get("totalPositionIM", 0)),
        "order_im": float(c.get("totalOrderIM", 0)),
        "locked": float(c.get("locked", 0)),
        "upnl": float(c.get("unrealisedPnl", 0)),
        "available": float(c["equity"]) - float(c.get("totalPositionIM", 0)) - float(c.get("totalOrderIM", 0)),
    }


def get_grid(grid_id=None):
    """List all active spot grids from the exchange (returns list)."""
    # Try query-active-grid-list first (GET)
    r = api.get("/v5/grid/query-active-grid-list", {"category": "spot", "limit": 20})
    if r.get("retCode") == 0:
        grids = r.get("result", {}).get("list", [])
        return grids  # list of grid detail dicts
    
    # Fallback: if a specific grid_id is provided, try the detail endpoint
    if grid_id:
        r = api.post("/v5/grid/query-grid-detail", body={"gridId": grid_id})
        if r.get("retCode") == 0 and r.get("result", {}).get("detail"):
            d = r["result"]["detail"]
            return [{
                "grid_id": grid_id,
                "status": d.get("status", "?"),
                "market_status": d.get("spot_symbol_status", "?"),
                "investment": float(d.get("total_investment", 0)),
                "equity": float(d.get("equity", 0)),
                "grid_profit": float(d.get("grid_profit", 0)),
                "total_profit": float(d.get("total_profit", 0)),
                "arbitrage_count": int(d.get("arbitrage_num", 0)),
                "min_price": d.get("min_price", "?"),
                "max_price": d.get("max_price", "?"),
                "cells": int(d.get("cell_number", 0)),
                "run_time_h": int(d.get("run_time", 0)),
                "current_profit": float(d.get("current_profit", 0)),
                "current_per": d.get("current_per", "0"),
            }]
    
    return []  # no active grids


def get_perp_positions():
    """Open perp positions (should be 0)."""
    r = api.get("/v5/position/list", {"category": "linear", "settleCoin": "USDT"})
    if r.get("retCode") != 0:
        return {"error": r.get("retMsg", "?")}
    positions = []
    for p in r["result"]["list"]:
        if float(p.get("size", 0)) > 0:
            positions.append({
                "symbol": p["symbol"],
                "side": p["side"],
                "size": float(p["size"]),
                "entry": p["avgPrice"],
                "mark": p["markPrice"],
                "upnl": float(p.get("unrealisedPnl", 0)),
                "sl": p.get("stopLoss", "none"),
                "tp": p.get("takeProfit", "none"),
            })
    return positions


def print_status():
    now = datetime.now(timezone.utc)
    print(f"\n{'='*55}")
    print(f"  STATUS @ {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*55}")

    # ── Funding Wallet ──
    fw = get_funding_wallet()
    funding_total = 0.0
    if fw.get("error"):
        print(f"  FUND:     ✗ {fw['error']}")
    else:
        for coin, bal in fw.items():
            print(f"  FUND:     ${bal:.2f} {coin}")
            funding_total += bal

    # ── Unified Wallet ──
    uw = get_unified_wallet()
    if uw.get("error"):
        print(f"  UNIFIED:  ✗ {uw['error']}")
        unified_total = 0.0
    else:
        avail = uw.get("available", 0)
        print(f"  UNIFIED:  ${uw['equity']:.2f} (${avail:.2f} available)")
        print(f"            uPnL=${uw['upnl']:.4f} | IM=${uw['position_im']:.2f}")
        unified_total = uw['equity']

    # ── Grid Bots ──
    all_grids = get_grid()
    grid_eq = 0.0
    if all_grids:
        for g in all_grids:
            ge = float(g.get("equity", 0))
            grid_eq += ge
            icon = "✓" if g.get("status") == "RUNNING" else "⚠"
            print(f"  GRID:     {icon} {g.get('status','?')} {g.get('symbol','?')}")
            print(f"            ${g.get('min_price','?')} – ${g.get('max_price','?')} | {g.get('cell_number',0)} lines | {g.get('arbitrage_num',0)} trades")
            print(f"            Invested: ${float(g.get('total_investment',0)):.0f} | Equity: ${ge:.2f}")
            print(f"            PnL: ${float(g.get('total_profit',0)):.4f}")
    else:
        print(f"  GRID:     none")

    # ── Perp Positions ──
    pp = get_perp_positions()
    if isinstance(pp, dict) and pp.get("error"):
        print(f"  PERP:     ✗ {pp['error']}")
    elif pp:
        print(f"  PERP:     ⚠ {len(pp)} OPEN ———")
        for p in pp:
            print(f"            {p['symbol']}: {p['side']} {p['size']} @ {p['entry']} uPnL=${p['upnl']:.2f}")
    else:
        print(f"  PERP:     ✅ 0 open")

    # ── Summary ──
    print(f"{'='*55}")
    reserve = unified_total - grid_eq
    grand_total = funding_total + unified_total
    print(f"  FUND:      ${funding_total:.2f}")
    print(f"  UNIFIED:   ${unified_total:.2f}")
    if grid_eq > 0:
        print(f"    Grid:    ${grid_eq:.2f}")
        print(f"    Reserve: ${reserve:.2f}")
    print(f"  ─────────────────────────────")
    print(f"  TOTAL:     ${grand_total:.2f}")
    print(f"{'='*55}")

    return {"fund_wallet": fw, "unified_wallet": uw, "grids": all_grids, "perp_positions": pp,
            "totals": {"fund": round(funding_total,2), "unified": round(unified_total,2),
                       "grid": round(grid_eq,2), "grand": round(grand_total,2)}}


if __name__ == "__main__":
    s = print_status()
    print(f"\n{json.dumps(s, indent=2)}")
