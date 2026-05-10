#!/usr/bin/env python3
"""
Trade Journal — pulls full history from Bybit live
Run: source /root/.bybit_env && python3 trade_journal.py
"""
import os, sys, json, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from safety import SafeBybitAPI, make_safe_get

api = SafeBybitAPI('https://api.bybit.com', os.environ['BYBIT_API_KEY'], os.environ['BYBIT_API_PRIVATE_KEY_PATH'])
get = make_safe_get(api)

# Closed PnL - all history
r = get('/v5/position/closed-pnl', {'category': 'linear', 'limit': '50'})
trades = r.get('result', {}).get('list', [])

# Wallet
w = get('/v5/account/wallet-balance', {'accountType': 'UNIFIED'})
equity = float(w['result']['list'][0].get('totalEquity', 0))

print(f"{'='*72}")
print(f"  BYBIT TRADE JOURNAL — {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
print(f"{'='*72}")
print(f"  Current Equity : ${equity:.2f}")
print(f"  Initial Deposit: $150.00")
pnl = equity - 150
print(f"  Net P&L        : ${pnl:.2f} ({pnl/150*100:.1f}%)")
print(f"  Total Trades   : {len(trades)}")
print(f"{'='*72}")
print(f"  {'DATE':12s} {'SYMBOL':20s} {'QTY':12s} {'ENTRY':10s} {'EXIT':10s} {'PNL':>10s}")
print(f"  {'-'*12} {'-'*20} {'-'*12} {'-'*10} {'-'*10} {'-'*10}")

total_pnl = 0
wins, losses = 0, 0
by_symbol = {}

for t in trades:
    p = float(t.get('closedPnl', 0))
    total_pnl += p
    ts = t.get('updatedTime', '')
    dt = datetime.datetime.fromtimestamp(int(ts)/1000, tz=datetime.timezone.utc).strftime('%m/%d %H:%M') if ts else '?'
    sym = t.get('symbol', '?')
    qty = t.get('qty', '?')
    entry = float(t.get('avgEntryPrice', 0))
    exit_p = float(t.get('avgExitPrice', 0))
    flag = '✅' if p > 0 else '❌'
    if p > 0: wins += 1
    else: losses += 1
    by_symbol[sym] = by_symbol.get(sym, 0) + p
    print(f"  {dt:12s} {sym:20s} {str(qty):12s} {entry:10.6f} {exit_p:10.6f} {flag} ${p:+.4f}")

print(f"{'='*72}")
print(f"  Closed Perp PnL: ${total_pnl:+.4f}")
total = wins + losses
print(f"  Win Rate       : {wins}W / {losses}L = {wins/total*100:.0f}%" if total > 0 else "  No trades")
print()
print(f"  BY SYMBOL:")
for sym, spnl in sorted(by_symbol.items(), key=lambda x: x[1]):
    flag = '✅' if spnl > 0 else '❌'
    print(f"    {flag} {sym:20s}: ${spnl:+.4f}")
print(f"{'='*72}")
