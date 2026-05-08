#!/usr/bin/env python3
"""Detailed Bybit arbitrage spread scanner."""
import os, json, time, subprocess, urllib.request, urllib.parse

BYBIT_API_KEY = os.environ.get('BYBIT_API_KEY', '')
BYBIT_PRIV_KEY_PATH = os.environ.get('BYBIT_API_PRIVATE_KEY_PATH', '/root/.bybit/private.pem')
BYBIT_BASE_URL = 'https://api.bybit.com'

def bybit_sign(method, path, query='', body=''):
    timestamp = str(int(time.time() * 1000))
    recv_window = '5000'
    param_str = f'{timestamp}{BYBIT_API_KEY}{recv_window}{query}'
    proc = subprocess.run(
        ['openssl', 'dgst', '-sha256', '-sign', BYBIT_PRIV_KEY_PATH, '-binary'],
        input=param_str.encode(), capture_output=True, timeout=5)
    sign = subprocess.run(
        ['base64', '-w0'], input=proc.stdout, capture_output=True, timeout=5
    ).stdout.decode().strip()
    headers = {
        'X-BAPI-API-KEY': BYBIT_API_KEY, 'X-BAPI-TIMESTAMP': timestamp,
        'X-BAPI-SIGN': sign, 'X-BAPI-RECV-WINDOW': recv_window,
        'X-BAPI-SIGN-TYPE': '2', 'User-Agent': 'bybit-arb-engine/1.0',
        'X-Referer': 'bybit-arb-engine',
    }
    return headers

def bybit_get(path, params=None):
    query = urllib.parse.urlencode(params) if params else ''
    url = f'{BYBIT_BASE_URL}{path}'
    if query:
        url = f'{url}?{query}'
    headers = bybit_sign('GET', path, query)
    req = urllib.request.Request(url, headers=headers, method='GET')
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {'retCode': -1, 'retMsg': str(e)}

# Fetch all tickers
spot = bybit_get('/v5/market/tickers', {'category': 'spot'})
perp = bybit_get('/v5/market/tickers', {'category': 'linear'})

spot_map = {}
if spot.get('retCode') == 0:
    for t in spot['result']['list']:
        try:
            spot_map[t['symbol']] = float(t['lastPrice'])
        except:
            pass

perp_map = {}
if perp.get('retCode') == 0:
    for t in perp['result']['list']:
        try:
            perp_map[t['symbol']] = float(t['lastPrice'])
        except:
            pass

SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'ADAUSDT',
           'DOGEUSDT', 'DOTUSDT', 'LINKUSDT', 'AVAXUSDT', 'MATICUSDT',
           'ATOMUSDT', 'UNIUSDT', 'BNBUSDT', 'PEPEUSDT', 'SHIBUSDT',
           'NEARUSDT', 'APTUSDT', 'ARBUSDT', 'OPUSDT', 'SUIUSDT']

results = []
for sym in SYMBOLS:
    sp = spot_map.get(sym)
    pp = perp_map.get(sym)
    if sp and pp and sp > 0:
        spread = ((pp - sp) / sp) * 100
        results.append({
            'symbol': sym, 'spot_price': sp, 'perp_price': pp,
            'spread_pct': round(spread, 4), 'abs_diff_usdt': round(pp - sp, 6)
        })
    else:
        results.append({
            'symbol': sym, 'spot_price': sp, 'perp_price': pp,
            'spread_pct': None, 'abs_diff_usdt': None
        })

# Sort by |spread| descending
results.sort(key=lambda r: abs(r['spread_pct']) if r['spread_pct'] is not None else 0, reverse=True)

print('=' * 78)
print('BYBIT SPOT vs PERPETUAL SPREAD REPORT')
print(f'Time: {time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())}')
print('=' * 78)
print(f"{'Symbol':<12} {'Spot Price':<18} {'Perp Price':<18} {'Spread %':<12} {'Abs Diff':<12}")
print('-' * 72)
for r in results:
    sp = f'{r["spot_price"]:<18.8f}' if r['spot_price'] else f'{"N/A":<18}'
    pp = f'{r["perp_price"]:<18.8f}' if r['perp_price'] else f'{"N/A":<18}'
    sprd = f'{r["spread_pct"]:+.4f}%' if r['spread_pct'] is not None else 'N/A'
    diff = f'{r["abs_diff_usdt"]:<12}' if r['abs_diff_usdt'] is not None else 'N/A'
    marker = ' <<<' if r['spread_pct'] is not None and r['spread_pct'] >= 0.3 else ''
    print(f'{r["symbol"]:<12} {sp} {pp} {sprd:<12} {diff}{marker}')

print()
print('--- ENTRY OPPORTUNITIES (spread >= 0.30%, $100 position) ---')
MIN_SPREAD = 0.30
entries = [r for r in results if r['spread_pct'] is not None and r['spread_pct'] >= MIN_SPREAD]
if entries:
    for e in entries:
        # Position sizing
        if e['symbol'] == 'BTCUSDT':
            qty = round(100 / e['spot_price'], 6)
        elif e['symbol'] == 'ETHUSDT':
            qty = round(100 / e['spot_price'], 5)
        else:
            qty = round(100 / e['spot_price'], 4)
        val = qty * e['spot_price']
        profit = (e['perp_price'] - e['spot_price']) * qty
        print(f'  {e["symbol"]:<12} Spread: {e["spread_pct"]:+.4f}% | '
              f'Qty: {qty:<10.6f} | Value: ${val:<6.2f} | Est Profit: ${profit:<.2f}')
else:
    print('  None — no pairs exceed the 0.30% minimum spread threshold.')

# Inverted spreads
neg = [r for r in results if r['spread_pct'] is not None and r['spread_pct'] < -0.30]
if neg:
    print()
    print('--- INVERTED SPREADS (perp below spot by >0.30%) ---')
    for n in neg:
        print(f'  {n["symbol"]:<12} Spread: {n["spread_pct"]:+.4f}% | '
              f'Perp: ${n["perp_price"]} vs Spot: ${n["spot_price"]}')

# Negative spreads (any direction)
neg_small = [r for r in results if r['spread_pct'] is not None and r['spread_pct'] < 0]
if neg_small and not neg:
    print()
    print('--- NEGATIVE SPREADS (perp below spot, below threshold) ---')
    for n in neg_small[:5]:
        print(f'  {n["symbol"]:<12} Spread: {n["spread_pct"]:+.4f}%')

print()
print(f'Scan complete. {len(entries)} entry opportunities found out of {len(results)} pairs monitored.')
