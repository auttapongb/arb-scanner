#!/usr/bin/env python3
"""
Trade Dashboard — queries Bybit live for wallet, positions, trade history
"""
import os, sys, json, time, subprocess, urllib.request, urllib.parse, urllib.error, datetime

API_KEY  = os.environ.get('BYBIT_API_KEY', '')
PRIV_KEY = os.environ.get('BYBIT_API_PRIVATE_KEY_PATH', '/root/.bybit/private.pem')
BASE_URL = 'https://api.bybit.com'
STATE_FILE  = '/root/arb-scanner/.bot_state.json'
TRADES_FILE = '/root/arb-scanner/paper_trades.json'


def rsa_sign(message):
    r = subprocess.run(['openssl','dgst','-sha256','-sign',PRIV_KEY],
        input=message.encode(), capture_output=True, timeout=5)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode())
    b = subprocess.run(['base64','-w0'], input=r.stdout, capture_output=True, timeout=5)
    return b.stdout.decode().strip()


def api(method, path, params=None, body=None):
    ts = str(int(time.time()*1000))
    rw = '5000'
    if method == 'GET':
        q = urllib.parse.urlencode(params or {})
        ps = f'{ts}{API_KEY}{rw}{q}'
        url = f'{BASE_URL}{path}?{q}' if q else f'{BASE_URL}{path}'
        data = None
    else:
        bs = json.dumps(body or {}, separators=(',',':'))
        ps = f'{ts}{API_KEY}{rw}{bs}'
        url = f'{BASE_URL}{path}'
        data = bs.encode()
    sig = rsa_sign(ps)
    hdrs = {'X-BAPI-API-KEY':API_KEY,'X-BAPI-TIMESTAMP':ts,'X-BAPI-SIGN':sig,
            'X-BAPI-RECV-WINDOW':rw,'X-BAPI-SIGN-TYPE':'2','Content-Type':'application/json'}
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {'retCode':-1,'retMsg':str(e)}


def main():
    now = datetime.datetime.now(datetime.timezone.utc)

    # Wallet
    w = api('GET', '/v5/account/wallet-balance', {'accountType':'UNIFIED'})
    wallet = w['result']['list'][0] if w.get('retCode')==0 else {}
    total_equity = float(wallet.get('totalEquity', 0))
    usdt_bal = 0.0
    spot_tokens = []
    for c in wallet.get('coin', []):
        if c['coin'] == 'USDT':
            usdt_bal = float(c.get('walletBalance', 0))
        elif float(c.get('usdValue', 0)) > 0.5:
            spot_tokens.append({'coin': c['coin'], 'balance': float(c.get('walletBalance',0)), 'usd': float(c.get('usdValue',0))})

    # Open positions
    p = api('GET', '/v5/position/list', {'category':'linear','settleCoin':'USDT'})
    positions = []
    if p.get('retCode') == 0:
        for pos in p['result']['list']:
            if float(pos.get('size',0)) > 0:
                positions.append({
                    'symbol': pos['symbol'],
                    'side': pos['side'],
                    'size': float(pos['size']),
                    'entry_price': float(pos['avgPrice']),
                    'mark_price': float(pos.get('markPrice', pos['avgPrice'])),
                    'upnl': float(pos.get('unrealisedPnl', 0)),
                    'liq_price': float(pos.get('liqPrice', 0)),
                    'created_time': pos.get('createdTime'),
                    'open_time': pos.get('updatedTime'),
                })

    # Closed PnL
    cp = api('GET', '/v5/position/closed-pnl', {'category':'linear','limit':'20'})
    trade_history = []
    if cp.get('retCode') == 0:
        for t in cp['result']['list']:
            trade_history.append({
                'symbol': t['symbol'],
                'pnl': float(t.get('closedPnl', 0)),
                'qty': float(t.get('qty', 0)),
                'entry_price': float(t.get('avgEntryPrice', 0)),
                'exit_price': float(t.get('avgExitPrice', 0)),
                'close_time': t.get('updatedTime'),
            })

    # Load local state
    local_state = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                local_state = json.load(f)
        except Exception:
            pass

    # Bot state
    session = local_state.get('session', {})
    active = local_state.get('active_positions', {})
    wallet_start = session.get('wallet_start', total_equity)

    # Compute PnL
    total_upnl = sum(p['upnl'] for p in positions)
    net_from_start = total_equity - 150.0  # hardcoded initial deposit

    # Spread scan (public)
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
                    if s >= 0.3:
                        spreads.append((sym, round(s,3)))
        spreads.sort(key=lambda x: -x[1])
    except Exception:
        spreads = []

    # Position age
    for pos in positions:
        ct = pos.get('created_time')
        if ct:
            try:
                age_ms = now.timestamp()*1000 - int(ct)
                pos['age_h'] = round(age_ms/3600000, 2)
            except Exception:
                pos['age_h'] = 0

    # Go/No-Go
    consecutive = session.get('consecutive_losses', 0)
    pause_until = session.get('pause_until')
    paused = False
    if pause_until:
        try:
            pu = datetime.datetime.fromisoformat(pause_until)
            paused = now < pu
        except Exception:
            pass

    go_nogo = 'NO-GO' if (paused or consecutive >= 2) else 'GO'

    result = {
        'timestamp': now.isoformat(),
        'total_equity': round(total_equity, 2),
        'usdt_balance': round(usdt_bal, 2),
        'spot_tokens': spot_tokens,
        'total_upnl': round(total_upnl, 4),
        'net_from_deposit': round(net_from_start, 2),
        'net_from_deposit_pct': round(net_from_start/150.0*100, 2),
        'open_positions': positions,
        'open_count': len(positions),
        'trade_history': trade_history[:10],
        'spread_opportunities': len(spreads),
        'top_spreads': spreads[:5],
        'session': {
            'consecutive_losses': consecutive,
            'pause_until': pause_until,
            'paused': paused,
            'daily_pnl': round(session.get('daily_pnl', 0), 4),
            'wins': session.get('wins', 0),
            'losses': session.get('losses', 0),
            'wallet_start': wallet_start,
            'soft_circuit': round(wallet_start * 0.90, 2),
            'hard_circuit': round(wallet_start * 0.80, 2),
        },
        'go_nogo': go_nogo,
    }

    # Human readable
    print(f"{'='*50}")
    print(f"TRADE DASHBOARD — {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*50}")
    print(f"💰 Equity:    ${total_equity:.2f}  (USDT: ${usdt_bal:.2f})")
    print(f"📉 Net P&L:   ${net_from_start:.2f} ({net_from_start/150*100:.1f}% from $150 deposit)")
    print(f"📊 Positions: {len(positions)} open")
    for pos in positions:
        print(f"   {pos['symbol']} {pos['side']} {pos['size']} @ {pos['entry_price']} | uPnL=${pos['upnl']:.4f} | age={pos.get('age_h','?')}h")
    if spot_tokens:
        print(f"🪙 Spot tokens: {[(t['coin'], t['usd']) for t in spot_tokens]}")
    print(f"📈 Spreads:   {len(spreads)} ≥0.3% | Top: {spreads[0] if spreads else 'none'}")
    print(f"⛔ Circuit:   Soft=${result['session']['soft_circuit']:.2f} Hard=${result['session']['hard_circuit']:.2f}")
    print(f"🔴 ConsLoss:  {consecutive} (pause={'YES until '+pause_until[:16] if paused else 'NO'})")
    print(f"🎯 GO/NO-GO:  {go_nogo}")
    if trade_history:
        t = trade_history[0]
        print(f"📜 Last trade: {t['symbol']} PnL=${t['pnl']:.4f}")
    print(f"{'='*50}")

    print('\n---BEGINJSON---')
    print(json.dumps(result, default=str))
    print('---ENDJSON---')

    # Save state file for health check
    state_out = {
        'last_dashboard_run': now.isoformat(),
        'total_equity': result['total_equity'],
        'go_nogo': go_nogo,
        'open_count': len(positions),
        'top_spreads': spreads[:3],
    }
    with open('/root/arb-scanner/.trade_state.json', 'w') as f:
        json.dump(state_out, f, indent=2)


if __name__ == '__main__':
    main()
