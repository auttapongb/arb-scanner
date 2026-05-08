# Lessons Learned

## May 8, 2026 — Funding V3 & Bybit Execution Engine Bug Fixes

### Bug: Set-based exit tracking in `_rebuild_active_positions()`

**Problem:** `bybit_execution.py` used `exits_by_symbol = set()` to track closed symbols. Once one OLUSDT position exited, ALL OLUSDT entries were considered closed, even if 12+ more entries existed without exits. This caused:
- 18 open positions silently hidden
- Engine kept re-entering same symbols (thought position was empty)
- No exits ever fired (exit checker only monitors `active_positions`)

**Checklist for similar bugs:**
1. Is the data structure tracking individual entries or just symbols?
2. Are entries stored as copies (dict()) or references?
3. After modifying active positions, are changes propagated back to the persistent trade list?
4. On reconstruct, are persisted fields (like `last_pay_ts`) preserved or overwritten with defaults?

### Bug: Active positions are copies, not references

`_reconstruct()` uses `p = dict(e)` — creates a COPY. Modifying the active position dict does NOT modify the original entry in `self.trades`. Any `_save()` on `self.trades` loses active position state.

**Fix:** Added `_save_active()` helper that explicitly propagates changed keys from active position back to the matching trade in `self.trades` before saving.

### Bug: Reconstruct always overrides persisted state

Even with `_save_active()` correctly writing `last_pay_ts` to disk, `_reconstruct()` ran on next init and ALWAYS rebuilt `last_pay_ts` from FUNDING records. If no funding records existed, it set `last_pay_ts = None` — wiping the persisted value.

**Fix:** `_reconstruct` now checks if the entry already has a valid `last_pay_ts` and preserves it.

### Key Principle: Persisted state should survive reconstruction

Any `_reconstruct()` / `_rebuild_*()` method that rebuilds state from a log file must:
1. Preserve fields that were set AFTER the initial entry (like `last_pay_ts`, `total_collected`)
2. Only override those fields with FUNDING records if they exist
3. Not default to `None` without checking persisted data first
