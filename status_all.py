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
    """FUND wallet - ALL coins for total equity."""
    r = api.get("/v5/asset/transfer/query-account-coins-balance",
                {"accountType": "FUND"})
    if r.get("retCode") != 0:
        return {"error": r.get("retMsg", "?")}
    balances = {}
    for b in r.get("result", {}).get("balance", []):
        bal = float(b.get("walletBalance", 0))
        if bal > 0:
            coin = b["coin"]
            if coin == "USDT":
                balances[coin] = bal
            else:
                # Get USD value for non-USDT coins
                ticker = api.get("/v5/market/tickers", {"category": "spot", "symbol": f"{coin}USDT"})
                price = float(ticker["result"]["list"][0]["lastPrice"])
                balances[coin] = bal * price  # store as USD value
    return balances


def get_unified_wallet():
    """UNIFIED trading wallet - queries ALL coins for total equity."""
    r = api.get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
    if r.get("retCode") != 0:
        return {"error": r.get("retMsg", "?")}
    lst = r["result"]["list"][0]
    total_equity = float(lst.get("totalEquity", 0))
    # Get USDT-specific data for margin info
    usdt_coin = {"walletBalance": 0, "equity": 0, "totalPositionIM": 0, "totalOrderIM": 0, "locked": 0, "unrealisedPnl": 0}
    for c in lst.get("coin", []):
        if c.get("coin") == "USDT":
            usdt_coin = c
            break
    return {
        "wallet_balance": float(usdt_coin.get("walletBalance", 0)),
        "equity": total_equity,
        "position_im": float(usdt_coin.get("totalPositionIM", 0)),
        "order_im": float(usdt_coin.get("totalOrderIM", 0)),
        "locked": float(usdt_coin.get("locked", 0)),
        "upnl": float(usdt_coin.get("unrealisedPnl", 0)),
        "available": total_equity,
    }


def get_grids():
    """List all active grids by querying known grid IDs."""
    known_grids = [
        "619152395372384846",  # BTC grid
        "619197277327646447",  # SOL grid
    ]
    active = []
    for gid in known_grids:
        r = api.post("/v5/grid/query-grid-detail", body={"grid_id": gid})
        d = r.get("result", {}).get("detail")
        if d and d.get("status") in ("RUNNING",):
            active.append({
                "grid_id": gid,
                "symbol": d.get("symbol", "?"),
                "status": d.get("status", "?"),
                "total_investment": float(d.get("total_investment", 0)),
                "equity": float(d.get("equity", 0)),
                "grid_profit": float(d.get("grid_profit", 0)),
                "arbitrage_num": int(d.get("arbitrage_num", 0)),
                "min_price": d.get("min_price", "?"),
                "max_price": d.get("max_price", "?"),
                "cell_number": int(d.get("cell_number", 0)),
            })
    return active


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
        for coin, bal_usd in fw.items():
            print(f"  FUND:     ${bal_usd:.2f} {coin}" if coin == "USDT" else f"  FUND:     ${bal_usd:.2f} ({coin})")
            funding_total += bal_usd

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
    all_grids = get_grids()
    if all_grids:
        total_grid_eq = 0
        for g in all_grids:
            ge = float(g.get("equity", 0))
            total_grid_eq += ge
            icon = "✓" if g.get("status") == "RUNNING" else "⚠"
            print(f"  GRID:     {icon} {g['status']} {g['symbol']}")
            print(f"            ${g['min_price']} – ${g['max_price']} | {g['cell_number']} lines | {g['arbitrage_num']} trades")
            print(f"            Invested: ${float(g.get('total_investment',0)):.0f} | Equity: ${ge:.2f}")
        grid_eq = total_grid_eq
    else:
        grid_eq = 0.0

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
    grand_total = funding_total + unified_total + grid_eq
    print(f"{'='*55}")
    print(f"  FUNDING:     ${funding_total:.2f}")
    print(f"  UNIFIED:     ${unified_total:.2f}")
    print(f"  TRADING BOT: ${grid_eq:.2f}   ({len(all_grids)} active grids)")
    print(f"  ─────────────────────────────")
    print(f"  GRAND TOTAL: ${grand_total:.2f}")
    print(f"{'='*55}")

    return {"fund_wallet": fw, "unified_wallet": uw, "grids": all_grids, "perp_positions": pp,
            "totals": {"fund": round(funding_total,2), "unified": round(unified_total,2),
                       "grid": round(grid_eq,2), "grand": round(grand_total,2)}}


if __name__ == "__main__":
    s = print_status()
    print(f"\n{json.dumps(s, indent=2)}")
