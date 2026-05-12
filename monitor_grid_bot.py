#!/usr/bin/env python3
"""
Grid monitoring — check status, profit, and send alerts.
Uses query-grid-detail with saved grid_id from .grid_config.json
"""

import os, sys, json
from datetime import datetime, timezone
from safety import SafeBybitAPI

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BYBIT_API_KEY = os.environ.get("BYBIT_API_KEY", "")
BYBIT_PRIV_KEY_PATH = os.environ.get("BYBIT_API_PRIVATE_KEY_PATH",
                                      "/root/.bybit/private.pem")
BYBIT_BASE_URL = "https://api.bybit.com"

api = SafeBybitAPI(BYBIT_BASE_URL, BYBIT_API_KEY, BYBIT_PRIV_KEY_PATH)


def get_grid_id():
    """Get grid_id from saved config or prompt."""
    config_path = os.path.join(BASE_DIR, ".grid_config.json")
    if os.path.exists(config_path):
        with open(config_path) as f:
            cfg = json.load(f)
        grid_id = cfg.get("grid_id")
        if grid_id and grid_id != "dry_run":
            return grid_id
    return None


def check_grid(grid_id=None):
    """Query grid detail. If no grid_id, scan via wallet."""
    if not grid_id:
        grid_id = get_grid_id()
    
    if not grid_id:
        return {"status": "not_found", "msg": "No grid_id configured"}
    
    r = api.post("/v5/grid/query-grid-detail", body={"grid_id": grid_id})
    
    if r.get("retCode") != 0:
        return {"status": "error", "msg": r.get("retMsg", "?")}
    
    result = r.get("result", {})
    status_code = result.get("status_code", 0)
    
    if status_code != 200 and status_code != 0:
        return {"status": "error", "api_code": status_code,
                "msg": result.get("debug_msg", "?")}
    
    detail = result.get("detail", {})
    if not detail:
        return {"status": "not_found", "msg": f"No detail for grid {grid_id}"}
    
    return {
        "status": "ok" if detail.get("status") == "RUNNING" else detail.get("status", "?"),
        "grid_id": grid_id,
        "symbol": detail.get("symbol", "?"),
        "state": detail.get("status", "?"),
        "investment": detail.get("investment", "?"),
        "grid_profit": detail.get("grid_profit", "0"),
        "total_profit": detail.get("total_profit", "0"),
        "grid_apr": float(detail.get("grid_apr", 0)) * 100,
        "total_apr": float(detail.get("total_apr", 0)) * 100,
        "arbitrage_count": detail.get("arbitrage_count", 0),
        "min_price": detail.get("min_price", "?"),
        "max_price": detail.get("max_price", "?"),
        "cell_number": detail.get("cell_number", 0),
        "run_time": detail.get("run_time", 0),
        "current_profit": detail.get("current_profit", "0"),
        "current_per": detail.get("current_per", "0"),
    }


def print_report(st):
    print(f"\n{'='*50}")
    print(f"  SPOT GRID MONITOR @ {datetime.now().strftime('%H:%M UTC')}")
    print(f"{'='*50}")
    
    if st.get("status") == "error":
        print(f"  ✗ Error: {st.get('msg', '?')}")
        return
    if st.get("status") == "not_found":
        print(f"  ⊘ {st.get('msg', '?')}")
        return
    
    status_icon = "✓" if st.get("state") == "RUNNING" else "⚠"
    print(f"  {status_icon} Grid ID:   {st.get('grid_id', '?')}")
    print(f"  Status:    {st.get('state', '?')}")
    print(f"  Pair:      {st.get('symbol', '?')}")
    print(f"  Range:     ${st.get('min_price','?')} – ${st.get('max_price','?')}")
    print(f"  Grids:     {st.get('cell_number', '?')}")
    print(f"  Run time:  {st.get('run_time', 0)}h")
    print(f"  Invest:    ${st.get('investment', '?')}")
    print(f"  Trades:    {st.get('arbitrage_count', 0)}")
    print(f"  Grid PnL:  ${float(st.get('grid_profit', 0)):.4f}")
    
    # Current cycle profit (if running)
    current = st.get("current_profit", "0")
    if current and current != "0":
        print(f"  This cycle: ${float(current):.4f} ({st.get('current_per','0')}%)")
    
    print(f"  Total PnL: ${float(st.get('total_profit', 0)):.4f}")
    print(f"  Grid APR:  {st.get('grid_apr', 0):.2f}%")
    print(f"  Total APR: {st.get('total_apr', 0):.2f}%")
    print(f"{'='*50}")


if __name__ == "__main__":
    grid_id = sys.argv[1] if len(sys.argv) > 1 else None
    result = check_grid(grid_id)
    print_report(result)
    print(f"\n{json.dumps(result, indent=2)}")
