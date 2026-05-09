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
from bybit_execution import WALLET_SOFT_CIRCUIT_PCT, WALLET_HARD_CIRCUIT_PCT, DAILY_TARGET_PROFIT

check = validate_startup(BYBIT_API_KEY, BYBIT_PRIV_KEY_PATH, min_balance=1.0)
wallet_balance = check.get('wallet', 0.0)

# Also check session tracker for circuit breaker
session_file = os.path.join(BASE, '.session_tracker.json')
session = atomic_read(session_file)
if session:
    daily_pnl = session.get("pnl", 0.0)
    # Calculate actual dollar-based thresholds using wallet balance
    if wallet_balance > 0:
        soft_limit = wallet_balance * (WALLET_SOFT_CIRCUIT_PCT / 100)
        hard_limit = wallet_balance * (WALLET_HARD_CIRCUIT_PCT / 100)
    else:
        soft_limit = -999
        hard_limit = -999

    if daily_pnl >= DAILY_TARGET_PROFIT:
        check["target_alert"] = f"Daily target hit: ${daily_pnl:.2f} >= ${DAILY_TARGET_PROFIT:.2f} (alert-only, does NOT stop)"
    if daily_pnl <= hard_limit:
        check["circuit_breaker"] = f"HARD circuit: ${daily_pnl:.2f} <= ${hard_limit:.2f} ({WALLET_HARD_CIRCUIT_PCT:.0f}% of ${wallet_balance:.2f} wallet)"
    elif daily_pnl <= soft_limit:
        check["circuit_breaker"] = f"SOFT circuit: ${daily_pnl:.2f} <= ${soft_limit:.2f} ({WALLET_SOFT_CIRCUIT_PCT:.0f}% of ${wallet_balance:.2f} wallet)"
    check["daily_pnl"] = daily_pnl
    check["wallet_balance"] = wallet_balance

check["can_trade"] = (check.get("ok", False) and "circuit_breaker" not in check)

print(json.dumps(check, indent=2))
sys.exit(0 if check["can_trade"] else 1)
