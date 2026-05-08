"""
Bybit Spot vs Perpetual Arbitrage Scanner with Discord alerts.
Quick snapshot mode — runs once, reports findings, exits.
"""

import sys
import os

# Add parent dir for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from alerts.discord_alerter import send_opportunity_alert, send_report, log_alert

import requests
import json
from datetime import datetime

# ==================== SETTINGS ====================
SYMBOLS = [
    'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'XRPUSDT', 'ADAUSDT', 'SOLUSDT', 'DOGEUSDT', 'DOTUSDT',
    'MATICUSDT', 'AVAXUSDT', 'LTCUSDT', 'LINKUSDT', 'BCHUSDT', 'XLMUSDT', 'ATOMUSDT', 'UNIUSDT',
    'ETCUSDT', 'TRXUSDT', 'FILUSDT', 'VETUSDT', 'ICPUSDT', 'NEOUSDT', 'EOSUSDT', 'XTZUSDT',
    'AAVEUSDT', 'ALGOUSDT', 'THETAUSDT', 'MKRUSDT', 'COMPUSDT', 'SNXUSDT', 'YFIUSDT', 'SUSHIUSDT',
    'CRVUSDT', '1INCHUSDT', 'LRCUSDT', 'BATUSDT', 'MANAUSDT', 'SANDUSDT', 'ENJUSDT', 'CHZUSDT',
    'AXSUSDT', 'GALAUSDT', 'FLOWUSDT', 'NEARUSDT', 'FTMUSDT', 'KSMUSDT', 'RUNEUSDT', 'IMXUSDT',
    'OMGUSDT', 'SCUSDT', 'IOTXUSDT', 'DASHUSDT', 'ONEUSDT', 'STXUSDT', 'ZECUSDT', 'ANKRUSDT',
    'GRTUSDT', 'DYDXUSDT', 'RSRUSDT', 'SRMUSDT', 'IOSTUSDT', 'CELRUSDT', 'STORJUSDT', 'AUDIOUSDT',
    'COTIUSDT', 'CHRUSDT', 'CVCUSDT', 'WOOUSDT', 'ALICEUSDT', 'ENSUSDT', 'C98USDT', 'YGGUSDT',
    'ILVUSDT', 'RNDRUSDT', 'KEEPUSDT', 'MASKUSDT', 'TLMUSDT', 'SLPUSDT', 'GTCUSDT', 'LITUSDT',
    'CTKUSDT', 'BICOUSDT', 'SXPUSDT', 'BTTUSDT', 'BSVUSDT', 'KLAYUSDT', 'QTUMUSDT', 'LPTUSDT',
    'SPELLUSDT', 'HBARUSDT', 'EGLDUSDT', 'ZENUSDT', 'REQUSDT', 'XEMUSDT', 'DENTUSDT', 'HOTUSDT',
    'OPUSDT', 'ARBUSDT', 'APTUSDT', 'SUIUSDT', 'INJUSDT', 'SEIUSDT', 'TIAUSDT', 'BLURUSDT',
    'JUPUSDT', 'DYMUSDT', 'WLDUSDT', 'ORDIUSDT', 'FETUSDT', 'AGIXUSDT', 'OCEANUSDT', 'TAOUSDT',
    'LDOUSDT', 'STRKUSDT', 'ARUSDT', 'ALTUSDT', 'PYTHUSDT', 'RONINUSDT', 'PIXELUSDT', 'ACEUSDT',
    'NFPUSDT', 'AIUSDT', 'XAIUSDT', 'MANTAUSDT', 'ONDOUSDT', 'LSKUSDT', 'PORTALUSDT', 'PDAUSDT',
    'PEPEUSDT', 'SHIBUSDT', 'FLOKIUSDT', 'BONKUSDT', 'WIFUSDT', 'BOMEUSDT', 'NOTUSDT', 'DOGSUSDT',
    'RENDERUSDT', 'ARKMUSDT', 'PHBUSDT', 'ARKUSDT', 'CVXUSDT', 'LDOUSDT', 'RDNTUSDT',
    'JASMYUSDT', 'HFTUSDT', 'HOOKUSDT', 'MAGICUSDT', 'HIGHUSDT', 'GMTUSDT', 'KDAUSDT', 'APEUSDT',
    'ROSEUSDT', 'DUSKUSDT', 'REEFUSDT', 'WINUSDT', 'KEYUSDT', 'STMXUSDT',
]

SPOT_CAPITAL = 10000
PERPETUAL_CAPITAL = 1000
LEVERAGE = 10
PERPETUAL_POSITION_SIZE = PERPETUAL_CAPITAL * LEVERAGE

# Thresholds
ALERT_THRESHOLD = 0.5  # Alert on spread >= 0.5%
ALERT_THRESHOLD_HIGH = 1.0  # Highlight alert on spread >= 1.0%

SPOT_API_URL = "https://api.bybit.com/v5/market/tickers"
PERPETUAL_API_URL = "https://api.bybit.com/v5/market/tickers"


def get_prices(category: str) -> dict:
    """Fetch prices from Bybit API for a given category (spot or linear)."""
    try:
        params = {'category': category}
        r = requests.get(SPOT_API_URL if category == 'spot' else PERPETUAL_API_URL,
                         params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        prices = {}
        if data.get('retCode') == 0:
            for ticker in data['result']['list']:
                symbol = ticker['symbol']
                if symbol in SYMBOLS:
                    prices[symbol] = {
                        'price': float(ticker['lastPrice']),
                        'bid': float(ticker['bid1Price']) if ticker['bid1Price'] else 0,
                        'ask': float(ticker['ask1Price']) if ticker['ask1Price'] else 0
                    }
        return prices
    except Exception as e:
        log_alert(f"Error fetching {category} prices: {e}", "ERROR")
        return {}


def calculate(spot_price, perp_price):
    """Calculate spread and profit."""
    if spot_price <= 0 or perp_price <= 0:
        return None, None, None
    spread_pct = ((perp_price - spot_price) / spot_price) * 100
    spot_qty = SPOT_CAPITAL / spot_price
    perp_qty = PERPETUAL_POSITION_SIZE / perp_price
    trade_qty = min(spot_qty, perp_qty)
    profit = (perp_price - spot_price) * trade_qty
    return spread_pct, profit, trade_qty


def main():
    log_alert("Starting crypto arbitrage scan...", "INFO")
    
    spot_prices = get_prices('spot')
    perp_prices = get_prices('linear')
    
    if not spot_prices or not perp_prices:
        log_alert("Failed to get price data", "ERROR")
        print(json.dumps({"status": "error", "message": "Failed to get price data"}))
        return 1

    # Build opportunities
    opportunities = []
    for symbol in SYMBOLS:
        if symbol in spot_prices and symbol in perp_prices:
            spot = spot_prices[symbol]['price']
            perp = perp_prices[symbol]['price']
            if spot < perp:
                spread, profit, qty = calculate(spot, perp)
                if spread is not None and spread > 0:
                    opportunities.append({
                        'symbol': symbol,
                        'spread': spread,
                        'profit': profit,
                        'spot_price': spot,
                        'perp_price': perp,
                        'quantity': qty
                    })
    
    # Sort by spread descending
    opportunities.sort(key=lambda x: x['spread'], reverse=True)
    
    total_opps = len(opportunities)
    log_alert(f"Found {total_opps} opportunities out of {len(SYMBOLS)} assets", "INFO")
    
    # Send alerts for significant opportunities
    alerts_sent = 0
    for opp in opportunities:
        if opp['spread'] >= ALERT_THRESHOLD:
            # Deduplicate: only alert on each symbol once
            already_alerted = False
            send_opportunity_alert(
                {
                    'symbol': opp['symbol'],
                    'spread_pct': opp['spread'],
                    'profit_usdt': opp['profit'],
                    'spot_price': opp['spot_price'],
                    'perp_price': opp['perp_price'],
                    'action': f"Buy spot + Short perpetual on Bybit. Long {SPOT_CAPITAL:,} USDT spot, short {PERPETUAL_CAPITAL:,} USDT x{LEVERAGE} perpetual."
                },
                source="crypto"
            )
            alerts_sent += 1
    
    # Send summary report
    if opportunities:
        best = opportunities[0]
        avg_spread = sum(o['spread'] for o in opportunities) / len(opportunities)
        total_profit = sum(o['profit'] for o in opportunities)
        
        send_report({
            'total_scanned': len(SYMBOLS),
            'opportunities': total_opps,
            'best_symbol': best['symbol'],
            'best_spread': best['spread'],
            'best_profit': best['profit'],
            'avg_spread': avg_spread,
            'total_profit': total_profit
        }, source="crypto")

    # Output JSON for cron/tooling
    result = {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "scanned": len(SYMBOLS),
        "opportunities": total_opps,
        "alerts_sent": alerts_sent,
        "top_5": [
            {
                "symbol": o['symbol'],
                "spread_pct": round(o['spread'], 2),
                "profit_usdt": round(o['profit'], 2)
            }
            for o in opportunities[:5]
        ]
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
