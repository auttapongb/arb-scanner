#!/usr/bin/env python3
"""Quick diagnostic for all bots."""
import json
from collections import Counter

with open('/root/arb-scanner/paper_trades.json') as f:
    trades = json.load(f)
entries = [t for t in trades if t.get('type') == 'ENTRY']
exits = [t for t in trades if t.get('type') == 'EXIT']
print('=== SPOT-PERP ARB ===')
print(f'Total: {len(trades)} records ({len(entries)} entries, {len(exits)} exits)')

total_pnl = sum(t.get('pnl_usdt',0) or 0 for t in exits)
wins = sum(1 for t in exits if (t.get('pnl_usdt',0) or 0) > 0)
losses = sum(1 for t in exits if (t.get('pnl_usdt',0) or 0) <= 0)
print(f'Realized PnL: +${total_pnl:.2f} ({wins} wins, {losses} losses)')

reasons = {}
for t in exits:
    r = t.get('reason', 'convergence')
    pnl = t.get('pnl_usdt',0) or 0
    reasons.setdefault(r, []).append(pnl)
for r, pnls in reasons.items():
    print(f'  {r}: {len(pnls)}x, total=${sum(pnls):.2f}')

ec = Counter(t.get('symbol') for t in entries)
xc = Counter(t.get('symbol') for t in exits)
print()
for sym in sorted(set(list(ec.keys()) + list(xc.keys()))):
    e = ec.get(sym, 0)
    x = xc.get(sym, 0)
    pnls = [t.get('pnl_usdt',0) or 0 for t in exits if t.get('symbol') == sym]
    tp = sum(pnls)
    print(f'{sym:18}: {e:2} entries, {x:2} exits, pnl=${tp:.2f}, open={e-x}')

print()
print('=== FUNDING V3 ===')
with open('/root/arb-scanner/funding_v3_trades.json') as f:
    ft = json.load(f)
funding = sum(t.get('net_payment',0) for t in ft if t.get('type') == 'FUNDING')
print(f'Total funding collected: ${funding:.4f}')
print(f'Position: {ft[0].get("symbol","?")} (entered {ft[0].get("ts","?")[:19]})')

# Check for lingering bugs
print()
print('=== BUG CHECKS ===')
# Check that last_pay_ts persists
if ft[0].get('last_pay_ts'):
    print('[OK] last_pay_ts persists: ' + ft[0]['last_pay_ts'])
else:
    print('[BUG] last_pay_ts is null!')

# Check for stale open positions > 12h
from datetime import datetime, timezone
now = datetime.now(timezone.utc)
for t in entries:
    if t.get('symbol') not in {e.get('symbol') for e in exits}:
        ts = t.get('timestamp', '')
        try:
            et = datetime.fromisoformat(ts.replace('Z','+00:00'))
            age = (now - et).total_seconds() / 3600
            if age > 12:
                print(f'[WARN] Stale open: {t.get("symbol")} ({age:.0f}h old)')
        except:
            pass
