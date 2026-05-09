#!/usr/bin/env python3
"""Cron: run both bots + report status."""
import os, sys, json, subprocess
from datetime import datetime, timezone
from safety import atomic_read, atomic_write

BASE = '/root/arb-scanner'
LOG = os.path.join(BASE, 'bot_cycle.log')
os.chdir(BASE)

def run(cmd, timeout=60):
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return proc.stdout.strip(), proc.stderr.strip(), proc.returncode

results = {}
ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

# 1. Health check
out, err, rc = run('python3 health_check.py')
health = json.loads(out) if rc == 0 else {'error': err[:200]}
results['health'] = health

# 2. Funding bot
out, err, rc = run('python3 funding_v3.py 2>&1', timeout=30)
funding_output = out + ('\n' + err if err else '')
# Extract the JSON summary from the last line
funding_json = None
for line in reversed(out.split('\n')):
    if line.strip().startswith('{'):
        try:
            funding_json = json.loads(line)
            break
        except:
            pass
        
results['funding'] = funding_json or {'status': 'error', 'raw': funding_output[:500]}
results['funding_raw'] = funding_output[:1000]

# 3. Arb bot
out, err, rc = run('python3 bybit_execution.py 2>&1', timeout=30)
arb_output = out + ('\n' + err if err else '')
# Extract JSON
arb_json = None
for line in reversed(out.split('\n')):
    if line.strip().startswith('{'):
        try:
            arb_json = json.loads(line)
            break
        except:
            pass
results['arb'] = arb_json or {'status': 'error', 'raw': arb_output[:500]}
results['arb_raw'] = arb_output[:1000]

# === STRATEGY ANALYSIS ===
alerts = []
recs = []

wallet = health.get('wallet', 0)
funding_opps = health.get('funding_opportunities', 0)
spread_opps = health.get('spread_opportunities', 0)
funding_pnl = health.get('funding_pnl', 0)
arb_pnl = health.get('arb_pnl', 0)

# Alert: wallet issues
if wallet <= 0:
    alerts.append('🔴 WALLET EMPTY - cannot trade live')
elif wallet < 100:
    alerts.append(f'🟡 LOW CAPITAL: ${wallet:.2f} - may not cover 3×$50 positions')

# Alert: sustained losses
if funding_pnl < -10:
    alerts.append(f'🔴 FUNDING BOT LOST ${abs(funding_pnl):.2f} - consider disabling')
elif funding_pnl < -5:
    alerts.append(f'🟡 FUNDING BOT DOWN ${abs(funding_pnl):.2f} - monitor')

# Alert: too few opportunities
if funding_opps < 3:
    alerts.append(f'🟡 Only {funding_opps} funding opps >= 0.15% - low rate environment')
if spread_opps < 2:
    alerts.append(f'🟡 Only {spread_opps} spread opps >= 0.3% - tight market')

# Alert: stale positions
fund_pos = health.get('funding_positions', [])
arb_pos = health.get('arb_positions', [])
if len(fund_pos) > 0:
    # Check ages by scanning funding_trades.json
    pass  # TODO: load and check

# Recommendations
if funding_pnl < -5 and arb_pnl >= 2:
    recs.append('✅ REC: Reduce funding bot capital to $50, move $100 to arb bot')
elif funding_pnl < -3 and spread_opps >= 2:
    recs.append('✅ REC: Arb bot has opportunities - keep both running')
elif funding_opps < 2 and spread_opps < 2:
    recs.append('⚠️ REC: Neither strategy has opportunities - consider pausing both')

results['alerts'] = alerts
results['recommendations'] = recs

# Summary line for quick scan
summary_parts = [f"${wallet:.0f}"]
summary_parts.append(f"fund={funding_pnl:+.2f}")
summary_parts.append(f"arb={arb_pnl:+.2f}")
summary_parts.append(f"f_opps={funding_opps}")
summary_parts.append(f"s_opps={spread_opps}")
summary_parts.append(f"f_pos={len(fund_pos)}")
summary_parts.append(f"a_pos={len(arb_pos)}")
results['summary'] = ' | '.join(summary_parts)

# Write log
log_entry = f"\n=== {ts} ===\n"
log_entry += f"SUMMARY: {results['summary']}\n"
if alerts:
    log_entry += "ALERTS:\n  " + "\n  ".join(alerts) + "\n"
if recs:
    log_entry += "RECS:\n  " + "\n  ".join(recs) + "\n"

with open(LOG, 'a') as f:
    f.write(log_entry)

print(json.dumps(results, indent=2))
