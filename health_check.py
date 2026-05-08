#!/usr/bin/env python3
"""Unified health check: wallet, rates, spreads, positions."""
import os, sys, json, time, subprocess, urllib.request, urllib.parse
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

def bybit_get(path, params=None):
    query = urllib.parse.urlencode(params) if params else ''
    ts = str(int(time.time() * 1000))
    rw = '5000'
    ps = f'{ts}{BYBIT_API_KEY}{rw}{query}'
    proc = subprocess.run(['openssl','dgst','-sha256','-sign',BYBIT_PRIV_KEY_PATH,'-binary'],
        input=ps.encode(), capture_output=True, timeout=5)
    sig = subprocess.run(['base64', '-w0'], input=proc.stdout, capture_output=True, timeout=5).stdout.decode().strip()
    url = f'https://api.bybit.com{path}'
    full_url = f'{url}?{query}' if query else url
    req = urllib.request.Request(full_url, headers={
        'X-BAPI-API-KEY': BYBIT_API_KEY, 'X-BAPI-TIMESTAMP': ts,
        'X-BAPI-SIGN': sig, 'X-BAPI-RECV-WINDOW': rw,
        'X-BAPI-SIGN-TYPE': '2', 'User-Agent': 'health-check/1.0'
    })
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

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
            if abs(spread) >= 0.5:
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
    with open(os.path.join(BASE, 'funding_trades.json')) as f:
        ft = json.load(f)
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
    with open(os.path.join(BASE, 'paper_trades.json')) as f:
        pt = json.load(f)
    arb_open = [t for t in pt if t.get('type') == 'ENTRY']
    arb_closed = [t for t in pt if t.get('type') == 'EXIT']
    closed_symbols = {t.get('symbol') for t in arb_closed}
    open_symbols = []
    for t in arb_open:
        if t.get('symbol') not in closed_symbols:
            open_symbols.append(t.get('symbol'))
    results['arb_positions'] = open_symbols
    results['arb_pnl'] = round(sum(t.get('pnl',0) for t in pt if t.get('type')=='EXIT'), 2)
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
    alerts.append('🟡 NO SPREAD OPPORTUNITIES >= 0.5%')

results['alerts'] = alerts
results['timestamp'] = datetime.now(timezone.utc).isoformat()

print(json.dumps(results, indent=2))
