"""
Polymarket Arbitrage Scanner (Polarlyst-style).
Scans Polymarket for asymmetric pricing opportunities and cross-references with Kalshi events.

This is where the 5-20% spreads come from — prediction markets where 
the same binary event trades at different prices across platforms.
"""

import sys
import os

# Add parent dir for imports  
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from alerts.discord_alerter import send_opportunity_alert, send_report, log_alert

import requests
import json
from datetime import datetime
from typing import Optional

# ==================== SETTINGS ====================

# Trading capital (hypothetical)
CAPITAL = 100  # USDC

# Alert thresholds
MIN_SPREAD_PCT = 3.0  # Alert when spread >= 3%
HIGH_SPREAD_PCT = 10.0  # High alert when spread >= 10%

# Polymarket API
POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB = "https://clob.polymarket.com"

# Kalshi API
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

# Interesting tags to scan (more profitable = Prediction/Election markets)
TARGET_TAGS = [
    "cryptocurrency", "election", "crypto", "prediction", 
    "bitcoin", "trump", "economy", "interest-rate"
]

# Series arb — same event, different expiry, same token
SERIES_KEYWORDS = [
    "bitcoin", "btc", "ethereum",
    "solana", "sol",
    "fed", "interest rate", "inflation", "recession",
    "trump", "election", "tariff", "trade war",
    "gta vi",
    "ceasefire",
    "openai", "anthropic", "ipo",
    "musk",
]


def fetch_polymarket_markets(tag: str = "", limit: int = 100) -> list:
    """Fetch active Polymarket markets from gamma-api."""
    url = f"{POLYMARKET_GAMMA}/markets"
    params = {
        "closed": "false",
        "active": "true",
        "limit": limit,
    }
    if tag:
        params["tag"] = tag
    
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        log_alert(f"Polymarket fetch error: {e}", "ERROR")
        return []


def parse_price(price_str) -> float:
    """Parse Polymarket price string to float."""
    try:
        if isinstance(price_str, str):
            prices = json.loads(price_str)
            return float(prices[0])  # YES price
        elif isinstance(price_str, list):
            return float(price_str[0])
        return 0.0
    except:
        return 0.0


def fetch_kalshi_markets(limit: int = 50) -> list:
    """Fetch open Kalshi markets (elections API)."""
    try:
        r = requests.get(
            f"{KALSHI_API}/markets",
            params={"limit": limit, "status": "open"},
            timeout=15
        )
        r.raise_for_status()
        data = r.json()
        return data.get("markets", [])
    except Exception as e:
        log_alert(f"Kalshi fetch error: {e}", "ERROR")
        return []


def normalize_title(title: str) -> str:
    """Normalize a market title for cross-platform matching."""
    t = title.lower().strip()
    # Remove common prefixes/suffixes
    words_to_remove = ["will ", "by ", "before ", "in ", "the ", "a ", "an "]
    for w in words_to_remove:
        if t.startswith(w):
            t = t[len(w):]
    return t.strip()


def find_series_arbitrage(markets: list) -> list:
    """
    Find series arbitrage: same topic, different expiry dates.
    Example: "BTC > $1m by June 2026" vs "BTC > $1m by Dec 2026"
    Later expiry should cost more — if it costs less, that's arb.
    """
    opportunities = []
    
    # Group markets by topic keyword
    groups = {}
    for m in markets:
        q = m.get("question", "")
        q_lower = q.lower()
        
        # Find which keyword it matches
        matched = None
        for kw in SERIES_KEYWORDS:
            if kw in q_lower:
                matched = kw
                break
        
        if not matched:
            continue
        
        price = parse_price(m.get("outcomePrices", "0"))
        # Skip resolved/zero-price markets
        if price == 0 or price == 1:
            continue
        
        if matched not in groups:
            groups[matched] = []
        groups[matched].append({
            "question": q,
            "price": price,
            "market": m,
        })
    
    # Within each group, find price discrepancies
    for keyword, items in groups.items():
        if len(items) < 2:
            continue
        
        # Sort by price
        items.sort(key=lambda x: x["price"])
        cheapest = items[0]
        most_expensive = items[-1]
        
        spread = (most_expensive["price"] - cheapest["price"]) * 100
        
        if spread >= 3.0:
            # Check: is the cheapest actually referring to a shorter timeframe?
            # If so, cheaper is expected — not an arb
            opportunities.append({
                "keyword": keyword,
                "type": "series_arb",
                "cheapest": cheapest["question"],
                "cheapest_price": cheapest["price"],
                "most_expensive": most_expensive["question"],
                "most_expensive_price": most_expensive["price"],
                "implied_spread": spread / 100,  # Convert to fractional
                "spread_pct": spread,
                "profit_per_100": spread,  # $ spread on $100 position
                "action": f"Buy YES on '{cheapest['question'][:40]}' @ {cheapest['price']*100:.2f}¢, " +
                          f"Buy NO on '{most_expensive['question'][:40]}' @ {1-most_expensive['price']:.4f} " +
                          f"(or short the expensive one)"
            })
    
    return opportunities


def find_mispriced_outcomes(markets: list) -> list:
    """
    Find markets where the outcome is mispriced (arb opportunity within the same market).
    For binary markets, the YES + NO price should equal ~1.0 (after fees).
    If YES + NO < 0.95, there's a purchase arb.
    """
    opportunities = []
    
    for m in markets:
        prices_str = m.get("outcomePrices", "")
        try:
            prices = json.loads(prices_str) if isinstance(prices_str, str) else prices_str
            if len(prices) >= 2:
                yes_price = float(prices[0])
                no_price = float(prices[1])
                total = yes_price + no_price
                
                # If total is significantly less than 1, buying both is profitable
                if total > 0 and total < 0.90:
                    profit_pct = (1.0 - total) * 100
                    if profit_pct >= MIN_SPREAD_PCT:
                        opportunities.append({
                            "type": "combined_outcome",
                            "question": m.get("question", "?"),
                            "yes_price": yes_price,
                            "no_price": no_price,
                            "total_price": total,
                            "spread_pct": profit_pct,
                            "profit_per_100": profit_pct,
                            "action": f"Buy both YES (${yes_price*100:.2f}) + NO (${no_price*100:.2f}) " +
                                      f"= ${total*100:.2f}, redeem at $1.00 = {profit_pct:.1f}% profit"
                        })
        except:
            pass
    
    return opportunities


def scan_kalshi_polymarket_cross(kalshi_markets: list, polymarkets: list) -> list:
    """Cross-reference Kalshi and Polymarket for the same event trading at different prices."""
    opportunities = []
    
    # Build Kalshi index by normalized title
    kalshi_index = {}
    for m in kalshi_markets:
        title = m.get("title", "")
        nt = normalize_title(title)
        # Kalshi prices are in cents (e.g., 50 = $0.50)
        yes_bid = m.get("yes_bid")
        yes_ask = m.get("yes_ask")
        last_price = m.get("last_price")
        
        # Best price is midpoint if available
        if yes_bid and yes_ask:
            try:
                yes_price = (float(yes_bid) + float(yes_ask)) / 2
            except:
                yes_price = 0
        elif yes_bid:
            try:
                yes_price = float(yes_bid)
            except:
                yes_price = 0
        elif last_price:
            try:
                yes_price = float(last_price)
            except:
                yes_price = 0
        else:
            continue
        
        if yes_price > 1:
            yes_price = yes_price / 100  # Convert cents to dollars
        
        if yes_price > 0:
            kalshi_index[nt] = {"price": yes_price, "title": title, "ticker": m.get("ticker","")}
    
    # Check each polymarket against Kalshi
    for pm in polymarkets:
        q = pm.get("question", "")
        nq = normalize_title(q)
        pm_price = parse_price(pm.get("outcomePrices", "0"))
        pm_price = pm_price * 100  # Convert to cents
        
        for kalshi_title, kalshi_data in kalshi_index.items():
            # Simple overlap: check if key words match
            pm_words = set(nq.split()[:5])
            kalshi_words = set(kalshi_title.split()[:5])
            overlap = pm_words & kalshi_words
            
            if len(overlap) >= 2:
                ks_price = kalshi_data["price"]
                spread = abs(pm_price - ks_price)
                if spread >= MIN_SPREAD_PCT:
                    opportunities.append({
                        "type": "cross_platform",
                        "question": q[:60],
                        "polymarket_price": pm_price,
                        "kalshi_price": ks_price,
                        "kalshi_title": kalshi_data["title"],
                        "spread_pct": spread,
                        "profit_per_100": spread,
                        "action": f"Buy cheaper market. Polymarket: {pm_price:.2f}¢ vs Kalshi: {ks_price:.2f}¢"
                    })
    
    return opportunities


def main():
    log_alert("Starting prediction market arbitrage scan...", "INFO")
    
    all_opportunities = []
    
    # 1. Scan Polymarket for all active markets
    all_markets = []
    for tag in TARGET_TAGS:
        markets = fetch_polymarket_markets(tag)
        log_alert(f"Fetched {len(markets)} markets for tag: {tag}", "INFO")
        all_markets.extend(markets)
    
    log_alert(f"Total unique Polymarket markets: {len(all_markets)}", "INFO")
    
    # 2. Find series arbitrage (same topic, different expiry)
    series_arb = find_series_arbitrage(all_markets)
    log_alert(f"Found {len(series_arb)} series arb opportunities", "INFO")
    all_opportunities.extend(series_arb)
    
    # 3. Find mispriced outcomes (YES+NO < 1.0)
    mispriced = find_mispriced_outcomes(all_markets)
    log_alert(f"Found {len(mispriced)} mispriced outcome opportunities", "INFO")
    all_opportunities.extend(mispriced)
    
    # 4. Kalshi cross-reference (if available)
    try:
        kalshi_markets = fetch_kalshi_markets(50)
        log_alert(f"Fetched {len(kalshi_markets)} Kalshi markets", "INFO")
        cross_arb = scan_kalshi_polymarket_cross(kalshi_markets, all_markets)
        log_alert(f"Found {len(cross_arb)} cross-platform opportunities", "INFO")
        all_opportunities.extend(cross_arb)
    except Exception as e:
        log_alert(f"Kalshi scan failed: {e}", "WARN")
    
    # 5. Sort by spread descending
    all_opportunities.sort(key=lambda x: x["spread_pct"], reverse=True)
    
    # 6. Send alerts for significant opportunities
    alerts_sent = 0
    for opp in all_opportunities:
        if opp["spread_pct"] >= MIN_SPREAD_PCT:
            # Determine high or normal alert
            is_high = opp["spread_pct"] >= HIGH_SPREAD_PCT
            level = "alert" if is_high else "warning"
            
            title_icon = "🔴" if is_high else "🎯"
            
            send_opportunity_alert({
                "symbol": opp.get("question", opp.get("cheapest", "Unknown")),
                "spread_pct": opp["spread_pct"],
                "profit_usdt": opp["profit_per_100"],
                "spot_price": 0,  # Not applicable
                "perp_price": opp.get("polymarket_price", opp.get("cheapest_price", opp.get("yes_price", 0))),
                "action": opp.get("action", "Arbitrage opportunity"),
            }, source="polymarket")
            alerts_sent += 1
    
    # 7. Send summary
    if all_opportunities:
        best = all_opportunities[0]
        avg_spread = sum(o["spread_pct"] for o in all_opportunities) / len(all_opportunities)
        total_value = sum(o["profit_per_100"] for o in all_opportunities)
        
        send_report({
            "total_scanned": len(all_markets),
            "opportunities": len(all_opportunities),
            "best_symbol": best.get("question", best.get("cheapest", "Unknown"))[:40],
            "best_spread": best["spread_pct"],
            "best_profit": best["profit_per_100"],
            "avg_spread": avg_spread,
            "total_profit": total_value,
        }, source="polymarket")
    
    # 8. Output JSON
    result = {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "scanned_polymarket": len(all_markets),
        "opportunities": len(all_opportunities),
        "alerts_sent": alerts_sent,
        "top_5": [
            {
                "type": o["type"],
                "description": o.get("question", o.get("keyword", "Unknown")),
                "spread_pct": round(o["spread_pct"], 2),
                "profit_per_100": round(o["profit_per_100"], 2),
                "action_preview": o.get("action", "")[:60]
            }
            for o in all_opportunities[:5]
        ]
    }
    print(json.dumps(result, indent=2))
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
