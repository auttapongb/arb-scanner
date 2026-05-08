# Arbitrage Scanner & Execution Engine

Multi-strategy crypto arbitrage system running on Bybit (paper mode).

## Ready for Live Trade ✅

All bots running in **PAPER_TRADE = True** mode. To go live:
1. Set `PAPER_TRADE = False` in `bybit_execution.py` or pass `LIVE_MODE = True`
2. Ensure Bybit API key has **Contract Trade + Spot Trade** permissions
3. Confirm wallet has sufficient USDT balance
4. The funding collector uses PostOnly limit orders (maker fees = 0.02%)

## Bots

| Bot | File | Strategy | Schedule |
|-----|------|----------|----------|
| Spot-Perp Arb | `bybit_execution.py` | Delta-neutral: buy spot + short perp, profit from spread convergence | every 15m (cron) |
| Funding V3 | `funding_v3.py` | Short high-funding perps, collect 8h settlements, single $100 position | every 15m (cron) |
| Polymarket Scanner | `polymarket_scanner.py` | Prediction market series arb (GTA VI, elections, etc.) | every 30m (cron) |
| Crypto Scanner | `crypto_scanner.py` | Scan all Bybit linear perps for funding rate + spread opportunities | every 30m (cron) |

## API

`api_server.py` runs on port 8080:

| Route | Description |
|-------|-------------|
| `/api/arb/health` | Comprehensive bot health summary |
| `/api/arb/latest` | Latest scan + trades + enriched data |
| `/api/arb/crypto` | Crypto opportunities from last scan |
| `/api/arb/polymarket` | Polymarket opportunities |
| `/api/arb/trades` | Spot-perp paper trade log |
| `/api/arb/funding-v3` | Funding v3 trade log |
| `/api/arb/history` | Historical scan data |
| `/api/arb/enriched` | Enriched analysis |
| `/health` | Simple health check |

## Bugs Fixed (May 8, 2026)

### 1. False Exit Tracking (bybit_execution.py)
`_rebuild_active_positions()` used a `set()` of exit symbols — one exit per symbol closed ALL entries. Fixed to proper entry-exit pairing (oldest unmatched entry gets closed by each exit).

### 2. Funding Init Not Persisting (funding_v3.py)
`collect_funding()` set `last_pay_ts` in memory but never saved to disk because `_reconstruct()` copies entry dicts. Added `_save_active()` to propagate changes back to `self.trades`.

### 3. Reconstruct Overwrites last_pay_ts (funding_v3.py)
`_reconstruct()` always rebuilt `last_pay_ts` from FUNDING records. With no FUNDING records yet, it set `last_pay_ts = None` every run. Fixed to preserve entry's own `last_pay_ts` when available.

### 4. Exit Not Saving (funding_v3.py)
`check_exits()` removed from `self.active` and appended to `self.trades` but never called `_save()`. Added `_save_active()` call after exits.

### 5. Crash Logging (both bots)
Both bots now wrap `main()` in try/except and write crash traces to `*_crashes.log`.

## Cron Jobs

- `arbitrage-full-scan` — every 30m
- `arbitrage-paper-trade` — every 15m
- `Bot comparison cycle` — every 15m
- `arb-bot-health-check` — every 2h
- `ginnie-trading-health-check` — daily at 10AM (Ginnie VPS)
