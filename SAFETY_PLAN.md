# Safety Layer Implementation Plan

## Goal
Add structural safety to the arb bot so edge cases (network failure, corrupt file, rate limit, API change, concurrent runs) are handled automatically without human intervention.

## Files to Create/Modify

### 1. `safety.py` — Shared safety module
- `safe_api_call()` — wrapped bybit_get/bybit_post with retry + exponential backoff (3 retries, 1s/2s/4s)
- `atomic_write()` — write to .tmp then rename (prevents corrupt file on crash)
- `openssl_signer()` — timeout+retry wrapper for openssl subprocess
- `RateLimiter` — simple token bucket: max 10 calls/second
- `validate_config()` — startup check: API key present, key file exists, balance > 0, position sync

### 2. Modify `bybit_execution.py`
- Replace direct `bybit_get()`/`bybit_post()` with `safe_api_call()` wrapper
- Add startup validation before any trade
- Add file write atomicity in PaperTradeLogger._save()
- Add openssl fallback in case of sign failure

### 3. Modify `health_check.py`
- Add startup validation
- Add atomic file reads

### 4. Integration test
- Run all bots with safety layer
- Verify no regressions

## Implementation Order
1. Create `safety.py` module
2. Integrate into `bybit_execution.py`
3. Integrate into `health_check.py`
4. Full integration test
5. Commit and push
