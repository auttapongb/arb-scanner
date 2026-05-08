#!/usr/bin/env python3
"""Check if funding_v3 should exit 1000BTTUSDT."""
import json, time, subprocess, urllib.request, os, sys
from datetime import datetime

BASE_DIR = '/root/arb-scanner'
BYBIT_API_KEY = os.environ.get('BYBIT_API_KEY', '')
BYBIT_PRIV_KEY_PATH = '/root/arb-scanner/bybit_private_key_rsa.pem'

ts = str(int(time.time() * 1000))
ps = f'{ts}{BYBIT_API_KEY}5000'
proc = subprocess.run(['openssl','dgst','-sha256','-sign',BYBIT_PRIV_KEY_PATH,'-binary'],
    input=ps.encode(), capture_output=True, timeout=5)
sig = subprocess.run(['base64','-w0'], input=proc.stdout, capture_output=True, timeout=5).stdout.decode().strip()
h = {'X-BAPI-API-KEY': BYBIT_API_KEY, 'X-BAPI-TIMESTAMP': ts, 'X-BAPI-SIGN': sig,
     'X-BAPI-RECV-WINDOW': '5000', 'X-BAPI-SIGN-TYPE': '2'}

req = urllib.request.Request('https://api.bybit.com/v5/market/tickers?category=linear&symbol=1000BTTUSDT', headers=h)
with urllib.request.urlopen(req, timeout=10) as r:
    data = json.loads(r.read())

if data['retCode'] == 0:
    t = data['result']['list'][0]
    fr = float(t.get('fundingRate', 0)) * 100
    pr = float(t.get('lastPrice', 0))
    print(f'1000BTTUSDT: price={pr:.8f}, funding_rate={fr:.4f}%')
    entry_price = 0.0003271
    price_chg = ((pr - entry_price) / entry_price) * 100
    print(f'Entry price: {entry_price:.8f}')
    print(f'Price change: {price_chg:.2f}%')
    print(f'Current funding: {fr:.4f}%/8h')
    
    # Check exit triggers
    reasons = []
    if fr < 0.01 and fr >= 0:
        reasons.append(f'Funding dropped to {fr:.4f}%')
    if price_chg >= 100.0:  # STOP_LOSS disabled — funding is delta-neutral
        reasons.append(f'Stop loss (price +{price_chg:.2f}%)')
    if reasons:
        print(f'>>> EXIT SIGNAL: {", ".join(reasons)}')
    else:
        print('>>> Position OK to hold')
        
    # Est daily funding
    nxt = 8 - (datetime.now().hour % 8)
    print(f'Next settlement in ~{nxt}h, est collected: ${fr/100*100:.4f}')
else:
    print(f'API Error: {data}')
