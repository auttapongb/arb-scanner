#!/usr/bin/env python3
import json

with open('/root/arb-scanner/paper_trades.json') as f:
    trades = json.load(f)

print("=== RECENT TRADES (last 30) ===")
for t in reversed(trades[-30:]):
    sym = t['symbol']
    typ = t['type']
    if typ == 'ENTRY':
        print(f"  [{t['timestamp'][:19]}] ENTRY {sym}: spread={t['spread_pct']}%, target=${t['profit_target_usdt']}")
    elif typ == 'EXIT':
        ts = t.get('exit_timestamp', t.get('timestamp', '?'))
        print(f"  [{str(ts)[:19]}] EXIT  {sym}: PnL=${t.get('pnl_usdt', 0):.2f}, exit_spread={t.get('exit_spread_pct', 0)}%")

# Determine open positions
entries_by_sym = {}
exits_by_sym = {}
for t in trades:
    if t['type'] == 'ENTRY':
        s = t['symbol']
        if s not in entries_by_sym or t['timestamp'] > entries_by_sym[s]['timestamp']:
            entries_by_sym[s] = t
    elif t['type'] == 'EXIT':
        s = t['symbol']
        exit_ts = t.get('exit_timestamp', t.get('timestamp', ''))
        if s not in exits_by_sym or exit_ts > exits_by_sym[s].get('exit_timestamp', ''):
            exits_by_sym[s] = t

print()
print("=== OPEN POSITIONS ===")
for sym in sorted(entries_by_sym):
    entry = entries_by_sym[sym]
    exit_t = exits_by_sym.get(sym)
    if not exit_t or exit_t.get('exit_timestamp', '') < entry['timestamp']:
        print(f"  {sym}: entry spread={entry['spread_pct']}%, target=~${entry['profit_target_usdt']:.2f}, entered {entry['timestamp'][:19]}")

entries = len([t for t in trades if t['type']=='ENTRY'])
exits = len([t for t in trades if t['type']=='EXIT'])
print(f"\nTotal: {len(trades)} trades ({entries} entries, {exits} exits)")
