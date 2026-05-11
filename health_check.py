#!/usr/bin/env python3
"""Health check — reads .trade_state.json and runs a quick spread scan"""
import os, json, datetime, urllib.request

STATE_FILE = '/root/arb-scanner/.trade_state.json'
BASE_URL = 'https://api.bybit.com'

state = {}
if os.path.exists(STATE_FILE):
    with open(STATE_FILE) as f:
        state = json.load(f)

# Quick spread scan (no auth needed)
try:
    spot_r = json.loads(urllib.request.urlopen(f'{BASE_URL}/v5/market/tickers?category=spot', timeout=8).read())
    perp_r = json.loads(urllib.request.urlopen(f'{BASE_URL}/v5/market/tickers?category=linear', timeout=8).read())
    spot_map = {t['symbol']: float(t['lastPrice']) for t in spot_r['result']['list'] if float(t.get('lastPrice',0))>0}
    spreads = []
    for t in perp_r['result']['list']:
        sym = t['symbol']
        if sym in spot_map and spot_map[sym] > 0:
            pp = float(t.get('lastPrice',0))
            if pp > 0:
                s = (pp - spot_map[sym]) / spot_map[sym] * 100
                if s >= 0.5:
                    spreads.append((sym, round(s,3)))
    spreads.sort(key=lambda x: -x[1])
except Exception as e:
    spreads = []

result = {
    'timestamp': datetime.datetime.utcnow().isoformat(),
    'wallet_equity': state.get('total_equity', 'unknown'),
    'go_nogo': state.get('go_nogo', 'UNKNOWN'),
    'open_positions': state.get('open_count', 0),
    'top_spreads': spreads[:5],
    'spread_count_05': len([s for s in spreads if s[1] >= 0.5]),
    'spread_count_10': len([s for s in spreads if s[1] >= 1.0]),
    'spread_count_15': len([s for s in spreads if s[1] >= 1.5]),
    'last_dashboard': state.get('last_dashboard_run', 'never'),
    'alerts': [],
}

if state.get('total_equity', 200) < 100:
    result['alerts'].append('🔴 WALLET CRITICAL: below $100')

print(json.dumps(result, indent=2))
