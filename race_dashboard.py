#!/usr/bin/env python3
"""
BOT RACE Dashboard — track all three bots in real-time.
"""
import os, sys, json
from datetime import datetime, timezone

BASE = "/root/arb-scanner"

def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return []

def analyze_funding():
    trades = load_json(os.path.join(BASE, "funding_trades.json"))
    entries = [t for t in trades if t.get("type") == "ENTRY"]
    exits = [t for t in trades if t.get("type") == "EXIT"]
    fundings = [t for t in trades if t.get("type") == "FUNDING"]
    active_symbols = set()
    for e in entries:
        s = e.get("symbol", "")
        if not any(x.get("symbol") == s for x in exits):
            active_symbols.add(s)
    total_funding = sum(t.get("net_payment", 0) for t in fundings)
    realized_pnl = sum(t.get("pnl_usdt", 0) for t in exits)
    total_fees = sum(t.get("entry_fee", 0) for t in entries) + sum(t.get("exit_fee", 0) for t in exits)
    active_info = []
    for sym in active_symbols:
        es = [t for t in entries if t.get("symbol") == sym]
        if es:
            e = es[-1]
            active_info.append({"symbol": sym,
                "entry_rate": e.get("entry_funding_rate", 0),
                "value": e.get("value_usdt", 0),
                "collected": sum(t.get("net_payment", 0) for t in fundings if t.get("symbol") == sym)})
    return {"active": len(active_symbols), "entries": len(entries), "exits": len(exits),
            "funding_payments": len(fundings), "total_funding": round(total_funding, 4),
            "realized_pnl": round(realized_pnl, 2), "total_fees": round(total_fees, 4),
            "net_pnl": round(realized_pnl, 2), "active_positions": active_info}

def analyze_spotperp():
    trades = load_json(os.path.join(BASE, "paper_trades.json"))
    entries = [t for t in trades if t.get("type") == "ENTRY"]
    exits = [t for t in trades if t.get("type") == "EXIT"]
    active_symbols = set()
    for e in entries:
        s = e.get("symbol", "")
        if not any(x.get("symbol", "") == s for x in exits):
            active_symbols.add(s)
    entry_pnl = sum(t.get("profit_target_usdt", t.get("net_profit_usdt", t.get("profit_usdt", 0))) for t in entries)
    exit_pnl = sum(t.get("pnl_usdt", 0) for t in exits)
    return {"active": len(active_symbols), "entries": len(entries), "exits": len(exits),
            "total_entry_profit_target": round(entry_pnl, 2), "realized_pnl": round(exit_pnl, 2),
            "net_pnl": round(exit_pnl, 2), "active_symbols": list(active_symbols)}

def analyze_v3():
    trades = load_json(os.path.join(BASE, "funding_v3_trades.json"))
    entries = [t for t in trades if t.get("type") == "ENTRY"]
    exits = [t for t in trades if t.get("type") == "EXIT"]
    fundings = [t for t in trades if t.get("type") == "FUNDING"]
    active = None
    for e in entries:
        if not any(x.get("symbol") == e.get("symbol") for x in exits):
            active = e
    tf = sum(t.get("net_payment", 0) for t in fundings)
    rp = sum(t.get("pnl_usdt", 0) for t in exits)
    return {"active": active.get("symbol", "") if active else None,
            "symbol": active.get("symbol", "") if active else "",
            "entry_rate": active.get("entry_rate", 0) if active else 0,
            "value": active.get("value", 0) if active else 0,
            "funding": round(tf, 4), "pnl": round(rp, 2)}

def main():
    print(f"\n{'='*60}")
    print(f"  BOT RACE DASHBOARD")
    print(f"  Time: {datetime.now(timezone.utc).strftime('%H:%M UTC')}")
    print(f"  Capital: $100 each")
    print(f"{'='*60}")

    f2 = analyze_funding()
    sa = analyze_spotperp()
    f3 = analyze_v3()

    print(f"\n{'─'*60}")
    print(f"  BOT 1: FUNDING v2 (5 x $20)")
    print(f"{'─'*60}")
    print(f"  Active:  {f2['active']} positions")
    print(f"  Funding: ${f2['total_funding']:.4f}")
    print(f"  PnL:     ${f2['net_pnl']:.2f}")
    for p in f2.get('active_positions', []):
        print(f"    {p['symbol']:20s} r={p['entry_rate']:.4f}% coll=${p['collected']:.4f}")

    print(f"\n{'─'*60}")
    print(f"  BOT 2: SPOT-PERP ARB")
    print(f"{'─'*60}")
    print(f"  Active:  {sa['active']} positions")
    print(f"  PnL:     ${sa['net_pnl']:.2f}")
    if sa['active_symbols']:
        print(f"  In: {', '.join(sa['active_symbols'])}")

    print(f"\n{'─'*60}")
    print(f"  BOT 3: FUNDING v3 (1 x $100)")
    print(f"{'─'*60}")
    print(f"  Active:  {f3['active'] or 'none'}")
    if f3['active']:
        print(f"  Symbol:  {f3['symbol']}")
        print(f"  Rate:    {f3['entry_rate']:.4f}%")
        print(f"  Value:   ${f3['value']:.2f}")
        print(f"  Est 8h:  ${f3['entry_rate']/100*100:.4f}")
    print(f"  Funding: ${f3['funding']:.4f}")
    print(f"  PnL:     ${f3['pnl']:.2f}")

    print(f"\n{'='*60}")
    leaders = []
    if f2['net_pnl'] > 0: leaders.append(("Funding v2", f2['net_pnl']))
    if sa['net_pnl'] > 0: leaders.append(("Spot-perp", sa['net_pnl']))
    if f3['pnl'] > 0: leaders.append(("Funding v3", f3['pnl']))
    if leaders:
        winner = max(leaders, key=lambda x: x[1])
        print(f"  WINNER: {winner[0]} (${winner[1]:.2f})")
    else:
        print(f"  WAITING: No profit yet (next funding ~00:00 UTC)")
        print(f"           V3 est 8h: ${f3['entry_rate']/100*100:.2f}" if f3['active'] else "")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()
