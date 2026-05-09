#!/usr/bin/env python3
"""Pre-flight check for cron — run before bot executes.
Returns JSON with status and any issues found."""
import os, sys, json

BASE = '/root/arb-scanner'
sys.path.insert(0, BASE)

# Load env
env_path = os.path.expanduser('/root/.bybit_env')
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ[k.strip()] = v.strip()

BYBIT_API_KEY = os.environ.get('BYBIT_API_KEY', '')
BYBIT_PRIV_KEY_PATH = os.environ.get('BYBIT_API_PRIVATE_KEY_PATH',
    '/root/arb-scanner/bybit_private_key_rsa.pem')

from safety import validate_startup, atomic_read

check = validate_startup(BYBIT_API_KEY, BYBIT_PRIV_KEY_PATH, min_balance=1.0)

# Also check session tracker for circuit breaker
session_file = os.path.join(BASE, '.session_tracker.json')
session = atomic_read(session_file)
if session:
    daily_pnl = session.get("pnl", 0.0)
    target = 5.0  # DAILY_TARGET_PROFIT from bybit_execution.py
    max_loss = 3.0  # DAILY_MAX_LOSS from bybit_execution.py
    if daily_pnl >= target:
        check["circuit_breaker"] = f"🎯 Daily target hit: ${daily_pnl:.2f} >= ${target:.2f}"
    elif daily_pnl <= -max_loss:
        check["circuit_breaker"] = f"🛑 Max loss hit: ${daily_pnl:.2f} <= -${max_loss:.2f}"
    check["daily_pnl"] = daily_pnl

check["can_trade"] = (check.get("ok", False) and "circuit_breaker" not in check)

print(json.dumps(check, indent=2))
sys.exit(0 if check["can_trade"] else 1)
