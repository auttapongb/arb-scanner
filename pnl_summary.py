#!/usr/bin/env python3
"""Compute total PnL across all bots."""
import json

total = 0.0

# 1. Spot-perp arb
with open('/root/arb-scanner/paper_trades.json') as f:
    trades = json.load(f)
entries = [t for t in trades if t.get('type') == 'ENTRY']
exits = [t for t in trades if t.get('type') == 'EXIT']
spot_pnl = sum(t.get('pnl_usdt',0) or 0 for t in exits)
total += spot_pnl
print(f'Spot-perp arb:')
print(f'  {len(entries)} entries, {len(exits)} exits')
print(f'  Realized PnL: +${spot_pnl:.2f}')

# 2. Funding V3
with open('/root/arb-scanner/funding_v3_trades.json') as f:
    ft = json.load(f)
funding = sum(t.get('net_payment',0) or 0 for t in ft if t.get('type') == 'FUNDING')
total += funding
exits_v3 = [t for t in ft if t.get('type') == 'EXIT']
exit_pnl_v3 = sum(t.get('pnl_usdt',0) or 0 for t in exits_v3)
total += exit_pnl_v3
print(f'Funding V3:')
print(f'  Funding collected: +${funding:.4f}')
print(f'  Exit PnL: ${exit_pnl_v3:.2f}')

# 3. Polymarket paper trades
try:
    with open('/root/arb-scanner/polymarket_paper_trades.json') as f:
        pt = json.load(f)
    if pt:
        pnl_poly = sum(t.get('profit_per_100',0) or 0 for t in pt if t.get('type') == 'EXIT')
        total += pnl_poly
        print(f'Polymarket: +${pnl_poly:.2f}')
    else:
        print(f'Polymarket: no trades')
except:
    print(f'Polymarket: no file')

# 4. Funding collector v2
try:
    with open('/root/arb-scanner/funding_trades.json') as f:
        ft2 = json.load(f)
    if ft2:
        f2_funding = sum(t.get('net_payment',0) or 0 for t in ft2 if t.get('type') == 'FUNDING')
        f2_exits = sum(t.get('pnl_usdt',0) or 0 for t in ft2 if t.get('type') == 'EXIT')
        total += f2_funding + f2_exits
        if f2_funding or f2_exits:
            print(f'Funding V2: fund=+${f2_funding:.2f} exit=+${f2_exits:.2f}')
except:
    pass

# 5. Polymarket main
try:
    with open('/root/arb-scanner/polymarket_trades.json') as f:
        pm = json.load(f)
    if pm and isinstance(pm, list):
        pm_pnl = sum(t.get('pnl_usdt',0) or 0 for t in pm if t.get('type')=='EXIT')
        total += pm_pnl
        if pm_pnl:
            print(f'Polymarket main: +${pm_pnl:.2f}')
except:
    pass

print()
print(f'=== TOTAL REALIZED PnL ($100/bot) ===')
print(f'  +${total:.2f}')
