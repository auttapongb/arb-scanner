#!/usr/bin/env python3
import json, requests

WEBHOOK = 'https://discord.com/api/webhooks/1484948839550025749/H2TC6J0GCpNi4NaL8y5sRa04Cth4khFa9cNhztIhjIEB0ekfZCwmHOLfB-4_UWV9O9I2'

embed = {
    'title': '📊 Arbitrage Scan Summary — May 8, 2026',
    'color': 0x00d26a,
    'fields': [
        {
            'name': '🪙 Crypto Spot-Perp Arbitrage',
            'value': '```\nOpportunities: 13\nBest: FLOWUSDT @ 0.37% spread\nProfit: $36.87 per $10k\nChange: ↔ Same as last scan (13)\n```',
            'inline': False
        },
        {
            'name': '🏛️ Polymarket Arbitrage',
            'value': '```\nOpportunities: 1\nBest: GTA VI series arb @ 55.2%\nProfit: $55.20 per $100\nChange: ↔ Same as last scan (1)\n```',
            'inline': False
        },
        {
            'name': '💰 Funding Rate Positions',
            'value': '```\nActive: 5 positions (max)\nTotal Realized PnL: -$0.24\nTop Funding: RAVEUSDT (0.156%)\nOther high: MUUSDT (0.139%), SNTUSDT (0.122%)\nStatus: No new entries — max capacity reached\n```',
            'inline': False
        }
    ],
    'footer': {'text': 'Arbitrage Scanner • 2026-05-08 03:07 UTC'}
}

r = requests.post(WEBHOOK, json={'embeds': [embed]})
print(f'Discord sent: {r.status_code}')
if r.status_code != 204:
    print(r.text)
