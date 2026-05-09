#!/usr/bin/env python3
"""Unified health check: wallet, rates, spreads, positions."""
import os, sys, json, time, urllib.request, urllib.parse
from datetime import datetime, timezone

BASE = '/root/arb-scanner'
sys.path.insert(0, BASE)
os.environ['BYBIT_API_KEY'] = os.environ.get('BYBIT_API_KEY', '')
os.environ['BYBIT_API_PRIVATE_KEY_PATH'] = os.environ.get('BYBIT_API_PRIVATE_KEY_PATH',
    '/root/.bybit/private.pem')

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
BYBIT_PRIV_KEY_PATH = os.environ.get('BYBIT_API_PRIVATE_KEY_PATH', '/root/.bybit/private.pem')

# ==================== SAFE BYBIT API ====================
from safety import SafeBybitAPI, make_safe_get, make_safe_post, atomic_read
_bybit_api = SafeBybitAPI('https://api.bybit.com', BYBIT_API_KEY, BYBIT_PRIV_KEY_PATH)
bybit_get = make_safe_get(_bybit_api)

results = {}

# === WALLET ===
try:
    r = bybit_get('/v5/account/wallet-balance', {'accountType': 'UNIFIED', 'coin': 'USDT'})
    if r.get('retCode') == 0:
        for c in r['result']['list'][0]['coin']:
            if c['coin'] == 'USDT':
                results['wallet'] = float(c['walletBalance'])
                results['equity'] = float(c['equity'])
                break
        else:
            results['wallet'] = 0.0
            results['equity'] = 0.0
    else:
        results['wallet_error'] = r.get('retMsg')
except Exception as e:
    results['wallet_error'] = str(e)

# === FUNDING RATES ===
results['funding_opportunities'] = 0
results['funding_top3'] = []
try:
    r = bybit_get('/v5/market/tickers', {'category': 'linear'})
    candidates = []
    for t in r['result']['list']:
        fr = float(t.get('fundingRate', 0) or 0) * 100
        pr = float(t.get('lastPrice', 0) or 0)
        prd = float(t.get('predictedFundingRate', 0) or 0) * 100
        if fr >= 0.15 and pr > 0.001:
            candidates.append({'sym': t['symbol'], 'rate': round(fr,4), 'pred': round(prd,4)})
    candidates.sort(key=lambda x: x['rate'], reverse=True)
    results['funding_opportunities'] = len(candidates)
    results['funding_top3'] = candidates[:3]
except Exception as e:
    results['funding_error'] = str(e)

# === SPREADS ===
results['spread_opportunities'] = 0
results['spread_top3'] = []
try:
    spot_r = bybit_get('/v5/market/tickers', {'category': 'spot'})
    spot_map = {t['symbol']: float(t.get('lastPrice',0)) for t in spot_r['result']['list']}
    linear_r = bybit_get('/v5/market/tickers', {'category': 'linear'})
    spreads = []
    for t in linear_r['result']['list']:
        sym = t['symbol']
        if sym in spot_map and spot_map[sym] > 0:
            pp = float(t.get('lastPrice', 0))
            sp = spot_map[sym]
            spread = ((pp - sp) / sp) * 100
            if abs(spread) >= 0.3:
                spreads.append((sym, round(spread,2)))
    spreads.sort(key=lambda x: -abs(x[1]))
    results['spread_opportunities'] = len(spreads)
    results['spread_top3'] = spreads[:3]
except Exception as e:
    results['spread_error'] = str(e)

# === POSITIONS ===
results['funding_positions'] = {}
results['arb_positions'] = {}
try:
    ft = atomic_read(os.path.join(BASE, 'funding_trades.json')) or []
    entries = set()
    exits = set()
    for t in ft:
        if t.get('type') == 'ENTRY':
            entries.add(t.get('symbol'))
        elif t.get('type') == 'EXIT':
            exits.add(t.get('symbol'))
    active_funding = entries - exits
    results['funding_positions'] = list(active_funding)
    results['funding_pnl'] = round(sum(t.get('pnl_usdt',0) for t in ft if t.get('type')=='EXIT'), 2)
except Exception as e:
    results['funding_positions_error'] = str(e)

try:
    pt = atomic_read(os.path.join(BASE, 'paper_trades.json')) or []
    # Separate mode tracking
    arb_open = [t for t in pt if t.get('type') == 'ENTRY']
    arb_closed = [t for t in pt if t.get('type') == 'EXIT']
    closed_symbols = {}  # symbol -> count of closes
    for t in arb_closed:
        sym = t.get('symbol')
        closed_symbols[sym] = closed_symbols.get(sym, 0) + 1
    open_symbols = []
    open_counts = {}
    for t in arb_open:
        sym = t.get('symbol')
        open_counts[sym] = open_counts.get(sym, 0) + 1
    for sym, count in open_counts.items():
        if count > closed_symbols.get(sym, 0):
            open_symbols.append(sym)
    results['arb_positions'] = open_symbols
    results['arb_pnl'] = round(sum(t.get('pnl_usdt', t.get('pnl', 0)) for t in pt if t.get('type')=='EXIT'), 2)
except Exception as e:
    results['arb_positions_error'] = str(e)

# === ALERTS ===
alerts = []
wallet = results.get('wallet', 0)
if wallet <= 0:
    alerts.append('🔴 WALLET EMPTY — no capital to trade')
elif wallet < 50:
    alerts.append(f'🟡 LOW WALLET — ${wallet:.2f}, may not cover position size')

if results.get('funding_opportunities', 0) == 0:
    alerts.append('🟡 NO FUNDING OPPORTUNITIES >= 0.15%')

if results.get('spread_opportunities', 0) == 0:
    alerts.append('🟡 NO SPREAD OPPORTUNITIES >= 0.3%')

results['alerts'] = alerts
results['timestamp'] = datetime.now(timezone.utc).isoformat()

# === SESSION STATUS ===
try:
    session_file = os.path.join(BASE, '.session_tracker.json')
    session_data = atomic_read(session_file)
    if session_data:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if session_data.get("date") == today:
            results['session'] = {
                "daily_pnl": session_data.get("pnl", 0.0),
                "target": 5.0,
                "max_loss": 3.0,
            }
        else:
            results['session'] = {"daily_pnl": 0.0, "target": 5.0, "max_loss": 3.0, "note": "new day"}
    else:
        results['session'] = {"daily_pnl": 0.0, "target": 5.0, "max_loss": 3.0}
except Exception as e:
    results['session_error'] = str(e)

print(json.dumps(results, indent=2))
