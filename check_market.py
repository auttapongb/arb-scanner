#!/usr/bin/env python3
"""Check current spreads and funding rates for tracked symbols."""
import json, time, subprocess, urllib.request, os

BYBIT_API_KEY = os.environ.get('BYBIT_API_KEY','')
BYBIT_PRIV_KEY_PATH = '/root/arb-scanner/bybit_private_key_rsa.pem'

ts = str(int(time.time()*1000))
ps = f'{ts}{BYBIT_API_KEY}5000'
proc = subprocess.run(['openssl','dgst','-sha256','-sign',BYBIT_PRIV_KEY_PATH,'-binary'],
    input=ps.encode(), capture_output=True, timeout=5)
sig = subprocess.run(['base64','-w0'], input=proc.stdout, capture_output=True, timeout=5).stdout.decode().strip()
h = {'X-BAPI-API-KEY': BYBIT_API_KEY, 'X-BAPI-TIMESTAMP': ts, 'X-BAPI-SIGN': sig,
     'X-BAPI-RECV-WINDOW': '5000', 'X-BAPI-SIGN-TYPE': '2'}

req = urllib.request.Request('https://api.bybit.com/v5/market/tickers?category=linear', headers=h)
with urllib.request.urlopen(req, timeout=10) as r:
    perp = json.loads(r.read())

req = urllib.request.Request('https://api.bybit.com/v5/market/tickers?category=spot', headers=h)
with urllib.request.urlopen(req, timeout=10) as r:
    spot = json.loads(r.read())

perp_p = {t['symbol']: float(t['lastPrice']) for t in perp['result']['list']}
spot_p = {t['symbol']: float(t['lastPrice']) for t in spot['result']['list']}

# Also store funding rates
funding_r = {}
for t in perp['result']['list']:
    fr = t.get('fundingRate', '0')
    funding_r[t['symbol']] = float(fr)*100 if fr else 0

symbols = ['CLOUDUSDT','OLUSDT','WHITEWHALEUSDT','HPOS10IUSDT','1000BTTUSDT']
print(f'{"Symbol":15} {"Spot":>10} {"Perp":>10} {"Spread%":>8} {"Fund%":>8}')
print('-'*51)
for s in symbols:
    sp = spot_p.get(s,0)
    pp = perp_p.get(s,0)
    if sp and pp:
        spread = (pp - sp) / sp * 100
        fr = funding_r.get(s,0)
        print(f'{s:15} {sp:>10.6f} {pp:>10.6f} {spread:>7.2f}% {fr:>7.4f}%')

# Top 10 by funding rate
print()
print('=== TOP 10 BY FUNDING RATE ===')
all_fr = [(s, funding_r[s]) for s in funding_r if funding_r[s] > 0 and spot_p.get(s,0) > 0]
all_fr.sort(key=lambda x: x[1], reverse=True)
for sym, fr in all_fr[:10]:
    sp = spot_p.get(sym, 0)
    pp = perp_p.get(sym, 0)
    spread = ((pp-sp)/sp*100) if sp and pp else 0
    print(f'{sym:18} fund={fr:.4f}% spread={spread:+.2f}%')
