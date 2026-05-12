#!/usr/bin/env python3
"""
Consolidated status — wallet, grid, paper bots.
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


def get_wallet():
    """UNIFIED wallet only (UTA account has no separate FUND/SPOT wallets)."""
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


def get_grid(grid_id="619152395372384846"):
    """Grid bot detail. Returns the fund-within-unified breakdown."""
    r = api.post("/v5/grid/query-grid-detail", body={"grid_id": grid_id})
    if r.get("retCode") != 0:
        return {"error": r.get("retMsg", "?"), "code": r.get("retCode")}
    d = r["result"].get("detail", {})
    if not d:
        return {"error": "no detail"}
    return {
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
    }


def get_perp_positions():
    """Check for any open perp positions (should be 0)."""
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

    # Wallet
    w = get_wallet()
    if w.get("error"):
        print(f"  ✗ WALLET ERROR: {w['error']}")
    else:
        available_txt = f" (${w['available']:.2f} available)"
        print(f"  WALLET:   ${w['equity']:.2f}{available_txt}")
        print(f"  Locked:   ${w['locked']:.2f} | uPnL: ${w['upnl']:.4f}")

    # Grid
    g = get_grid()
    if g.get("error"):
        print(f"  ✗ GRID: {g.get('error', '?')} (code={g.get('code','?')})")
    else:
        status_icon = "✓" if g["status"] == "RUNNING" else "⚠"
        market_icon = "🟢" if g.get("market_status") == "ONLINE" else "🔴"
        print(f"  GRID:     {status_icon} {g['status']} {market_icon}")
        print(f"           ${g['min_price']} – ${g['max_price']} | {g['cells']} lines | {g['arbitrage_count']} trades")
        print(f"           Invested: ${g['investment']:.0f} | Equity: ${g['equity']:.2f}")
        print(f"           PnL: ${g['total_profit']:.4f} (cycle: ${g['current_profit']:.4f})")

    # Perp positions
    pp = get_perp_positions()
    if isinstance(pp, dict) and pp.get("error"):
        print(f"  ✗ PERP CHECK ERROR: {pp['error']}")
    elif len(pp) > 0:
        print(f"  ⚠ PERP POSITIONS OPEN: {len(pp)}")
        for p in pp:
            print(f"      {p['symbol']}: {p['side']} {p['size']} @ {p['entry']} uPnL=${p['upnl']:.2f}")
    else:
        print(f"  PERP:     ✅ 0 open")

    print(f"{'='*55}")

    # Summary
    grid_eq = g.get("equity", 0) if not g.get("error") else 0
    wallet_eq = w.get("equity", 0) if not w.get("error") else 0
    reserve = wallet_eq - grid_eq
    print(f"  SUMMARY: ${wallet_eq:.2f} total = Grid ${grid_eq:.2f} + Reserve ${reserve:.2f}")
    return {"wallet": w, "grid": g, "perp_positions": pp}


if __name__ == "__main__":
    s = print_status()
    print(f"\n{json.dumps(s, indent=2)}")
