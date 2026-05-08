#!/usr/bin/env python3
import json, requests

WEBHOOK = 'https://discord.com/api/webhooks/1484948839550025749/H2TC6J0GCpNi4NaL8y5sRa04Cth4khFa9cNhztIhjIEB0ekfZCwmHOLfB-4_UWV9O9I2'

embed = {
    'title': '📊 Arbitrage Scan Summary — May 8, 2026 (11:12 UTC)',
    'color': 0x00d26a,
    'fields': [
        {
            'name': '🪙 Crypto Spot-Perp Arbitrage',
            'value': '```\nOpportunities: 0\nBest spread: N/A\nProfit USDT: N/A\nChange: ↔ No change (still 0)\nStatus: Switched to funding-only mode\n```',
            'inline': False
        },
        {
            'name': '🏛️ Polymarket Arbitrage',
            'value': '```\nOpportunities: 1\nBest: GTA VI series arb @ 56.7%\nProfit: $56.70 per $100\nChange: ⬆️ Spread improved from 55.2% to 56.7%\n800 markets scanned\n```',
            'inline': False
        },
        {
            'name': '💰 Funding Rate Positions',
            'value': '```\nActive: 5 positions (max capacity)\nTotal Realized PnL: -$2.37\nTop funding: RAVEUSDT (0.222%)\nOther high: ORCLUSDT (0.159%), TURTLEUSDT (0.141%)\nStatus: At capacity — no new entries\n```',
            'inline': False
        }
    ],
    'footer': {'text': 'Arbitrage Scanner • Pipeline executed at 11:12 UTC • Enriched analysis pushed to dashboard'}
}

r = requests.post(WEBHOOK, json={'embeds': [embed]})
print(f'Discord sent: {r.status_code}')
if r.status_code != 204:
    print(r.text)
