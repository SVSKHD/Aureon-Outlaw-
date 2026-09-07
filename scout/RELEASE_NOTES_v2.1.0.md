# XAUUSD MT5 Price-Action Scout Bot v2.1.0

Closes the 13 findings of the independent v2.0.0 review. No change to PA direction logic, router order, TP/SL lifecycle,
pattern scanning, or demo-only enforcement.

## Blocking flaws fixed

1. `scout_session_stats` now carries `symbol` alongside `account` and `config_fingerprint`; the scoped SQLite recovery
   (`scoped_event_count`) works with real events (tested end-to-end through `ScoutManager.close_session`).
2. Weekly scout P/L (`kind='SCOUT'` trades) is filtered by account, symbol and fingerprint like the session statistics.
3. Delivery independence. `fanout()` writes the Firestore event through the Firestore queue at emission time and, only if the
   event is Discord-eligible, queues the Discord send separately; the Discord worker reports its result back through the
   Firestore queue via `sink.mark_discord(event_id, posted)`. A blocked or full Discord queue cannot delay or drop a
   Firestore write (tested with a 3-second Discord stall).
4. Shutdown drains both queues unconditionally (`drained_discord`, `drained_firestore` evaluated separately).
5. Realized P/L is charged to the broker trading date of the *close* (`SessionEngine.broker_trading_date(close_time)`);
   the record carries `close_trading_date`. An overnight PA loss now locks the day it lands on.

## Other flaws fixed

6. GO tallies live in the persisted positions state (`meta.session_go`, saved on every GO cycle) and the Friday NY-close GO is
   recorded at the boundary (`meta.friday_close_go`). The outlook uses the recorded value; when absent it says so
   (`go_basis` starts with `CATCH-UP`).
7. `performance_summary` accepts `account`/`symbol`; every report query and every `generated_reports` key is prefixed
   `account:symbol:fingerprint`; report payloads carry `account` and `config_fingerprint`; weekly GO aggregation reads only
   same-scope session summaries.
8. Firestore event metadata: `date` is captured at emission; `discord_eligible` (bool) says whether a Discord result is
   expected; `posted_discord` is null until the worker reports true/false.
9. Pending scout-close summary: a one-leg retry never overwrites the original pair summary; `pending_stats` is persisted in
   the scout state file and restored after a crash.
10. Live `strategy_samples` increments only when the closed trade's fingerprint equals the current one.
11. Calibration scope must match exactly (account, symbol, fingerprint); a legacy/unscoped v1.9 file resets to 0 with audit.
12. All config models use `extra="forbid"`; unknown or removed keys (e.g. `max_manual_risk_to_daily_target_ratio`) fail to load.
13. Documentation corrected: FORWARD_TEST_CHECKLIST ($1,500 limit, $5 move ≈ $500), FIRESTORE_SCHEMA.md (currency/price
    fields, version), README (test count).

## Verification

- 134 tests pass on Linux (14 new in `tests/test_v21_review_fixes.py`); compilation passes.
- Config migration: none beyond v2.0.0; any leftover unknown key now stops the bot at startup with a validation error.

## Still Windows-only

Broker filling modes, hedging/demo detection, partial closes, freeze/stops levels, spread calibration, Discord delivery,
Firestore Admin writes — `FORWARD_TEST_CHECKLIST.md`.
