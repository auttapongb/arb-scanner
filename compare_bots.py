#!/usr/bin/env python3
"""
Compare bots head-to-head for 48h paper trade.
Resets all logs, runs each bot, tracks PnL.
"""
import os, sys, json, time, subprocess
from datetime import datetime, timezone, timedelta

BASE_DIR = "/root/arb-scanner"
CAPITAL = 100

BOTS = {
    "funding-collector": {
        "script": os.path.join(BASE_DIR, "funding_collector_v2.py"),
        "log": os.path.join(BASE_DIR, "funding_trades.json"),
        "status": "ready",
    },
    "spot-perp-arb": {
        "script": os.path.join(BASE_DIR, "bybit_execution.py"),
        "log": os.path.join(BASE_DIR, "paper_trades.json"),
        "status": "ready",
    },
}

def get_pnl(log_path):
    try:
        with open(log_path) as f:
            trades = json.load(f)
        if not trades:
            return 0
        # Sum realized PnL
        pnl = sum(t.get("pnl_usdt", 0) for t in trades if t.get("type") == "EXIT")
        # Also check for profit_usdt on entries
        entry_profit = sum(t.get("profit_usdt", 0) for t in trades if t.get("type") == "ENTRY")
        return pnl + entry_profit
    except:
        return 0

def get_positions(log_path):
    """Count active (open) positions."""
    try:
        with open(log_path) as f:
            trades = json.load(f)
        entries, exits = {}, set()
        for t in trades:
            if t.get("type") == "EXIT":
                exits.add(t.get("symbol", ""))
            elif t.get("type") == "ENTRY":
                s = t.get("symbol", "")
                entries[s] = t
        return len([s for s in entries if s not in exits])
    except:
        return 0

def reset_all():
    for name, bot in BOTS.items():
        log = bot["log"]
        if os.path.exists(log):
            # Backup
            backup = log + ".bak"
            os.rename(log, backup)
        with open(log, "w") as f:
            json.dump([], f)
        print(f"  Reset {name} logs")

def run_bot(name, script):
    print(f"\n  Running {name}...")
    env = os.environ.copy()
    env["BYBIT_API_KEY"] = os.environ.get("BYBIT_API_KEY", "")
    env["BYBIT_API_SECRET"] = os.environ.get("BYBIT_API_SECRET", "")
    env["BYBIT_API_PRIVATE_KEY_PATH"] = "/root/arb-scanner/bybit_private_key_rsa.pem"
    env["BYBIT_USE_RSA"] = "true"
    
    try:
        result = subprocess.run(
            ["python3", script],
            capture_output=True, text=True, timeout=60,
            cwd=BASE_DIR, env=env
        )
        out = result.stdout.strip()
        err = result.stderr.strip()
        
        # Extract JSON result from last line
        lines = out.split("\n")
        last_json = None
        for line in reversed(lines):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    last_json = json.loads(line)
                    break
                except:
                    pass
        
        print(f"  Exit code: {result.returncode}")
        if last_json:
            print(f"  Result: {json.dumps(last_json, indent=2)[:300]}")
        if err and "error" in err.lower():
            print(f"  Stderr: {err[:200]}")
        return last_json or {"status": "error", "msg": "no json in output"}
    except subprocess.TimeoutExpired:
        print(f"  TIMEOUT (60s)")
        return {"status": "error", "msg": "timeout"}
    except Exception as e:
        print(f"  ERROR: {e}")
        return {"status": "error", "msg": str(e)}

def main():
    reset_all()
    
    print(f"\n{'='*60}")
    print(f"  BOT COMPARISON TEST")
    print(f"  Capital: ${CAPITAL} each")
    print(f"  Time: {datetime.now(timezone.utc).strftime('%H:%M UTC')}")
    print(f"{'='*60}")
    
    for name, bot in BOTS.items():
        result = run_bot(name, bot["script"])
        if result and result.get("status") == "ok":
            bot["status"] = "running"
        else:
            bot["status"] = "failed"
    
    # Show initial state
    print(f"\n{'='*60}")
    print(f"  INITIAL STATE")
    print(f"{'='*60}")
    for name, bot in BOTS.items():
        pnl = get_pnl(bot["log"])
        pos = get_positions(bot["log"])
        print(f"  {name:25s}: {pos} positions | PnL=${pnl:.2f}")
    
    print(f"\nBots started. Run 'python3 compare_bots.py --status' to check progress.")
    print(f"Run 'python3 compare_bots.py --run' to execute another cycle.")

if __name__ == "__main__":
    if "--status" in sys.argv:
        print(f"\n{'='*60}")
        print(f"  BOT STATUS @ {datetime.now(timezone.utc).strftime('%H:%M UTC')}")
        print(f"{'='*60}")
        for name, bot in BOTS.items():
            pnl = get_pnl(bot["log"])
            pos = get_positions(bot["log"])
            status = "RUNNING" if pos > 0 else "IDLE"
            print(f"  {name:25s}: {pos} pos | PnL=${pnl:>6.2f} | {status}")
    elif "--run" in sys.argv:
        for name, bot in BOTS.items():
            run_bot(name, bot["script"])
        # Show status
        print(f"\n{'='*60}")
        print(f"  AFTER RUN @ {datetime.now(timezone.utc).strftime('%H:%M UTC')}")
        for name, bot in BOTS.items():
            pnl = get_pnl(bot["log"])
            pos = get_positions(bot["log"])
            print(f"  {name:25s}: {pos} pos | PnL=${pnl:>6.2f}")
    else:
        main()
