#!/usr/bin/env python3
import json
from collections import Counter

with open('/root/arb-scanner/paper_trades.json') as f:
    trades = json.load(f)

entries = [t for t in trades if t.get('type') == 'ENTRY']
exits = [t for t in trades if t.get('type') == 'EXIT']
total_pnl = sum(t.get('pnl_usdt', 0) for t in exits)

print(f"Total trades: {len(trades)}")
print(f"Entries: {len(entries)}")
print(f"Exits: {len(exits)}")
print(f"Total realized PnL: ${total_pnl:.2f}")
print(f"Open positions (un-exited): {len(entries) - len(exits)}")

sym_counts = Counter(t.get('symbol', '?') for t in entries)
print()
print("Entries by symbol (top 10):")
for sym, cnt in sym_counts.most_common(10):
    sym_trades = [t for t in entries if t.get('symbol') == sym]
    spreads = [t.get('spread_pct', 0) for t in sym_trades]
    avg_spread = sum(spreads) / len(spreads) if spreads else 0
    print(f"  {sym}: {cnt} entries, avg spread {avg_spread:.2f}%")

print()
print("Last 5 entries:")
for t in entries[-5:]:
    print(f"  {t.get('timestamp','?')} | {t.get('symbol','?')} | spread={t.get('spread_pct',0):.2f}% | ${t.get('profit_target_usdt',0):.2f}")

print()
print("Last 5 trades (any type):")
for t in trades[-5:]:
    print(f"  {t.get('timestamp','?')} | {t.get('type','?')} | {t.get('symbol','?')} | spread={t.get('spread_pct',0):.2f}% | pnl={t.get('pnl_usdt','?')}")
