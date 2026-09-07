# XAUUSD MT5 Price-Action Scout Bot v2.3.0

Closes the 4 blocking and 5 additional findings of the independent v2.2.0 review. Trading and execution logic unchanged.

## Blocking

1. Trade identity is (account, symbol, ticket). `trades` now has an internal `id` primary key and
   `UNIQUE(account, symbol, ticket)`; writes use `INSERT … ON CONFLICT DO UPDATE`; account/symbol are never NULL
   ("unknown" for legacy). A pre-v2.3 database (ticket-keyed) is rebuilt in place on first open with every row kept
   (`_migrate_trade_identity`, tested).
2. GO tallies are keyed `<fingerprint>:<SESSION>@<start-iso>`; a parameter change mid-session starts a fresh tally, and a
   report is never labelled with a GO from another configuration.
3. Strategy fingerprint includes the funded-readiness settings (`manual_reference_lot`, `daily_target_price_move`,
   `daily_target_usd`, `monthly_target_usd`, `max_manual_risk_usd`); `discord_scout_pair_min_interval_seconds` stays out.
4. Scout statistics are idempotent and written first. `AuditLogger.event_once(type, key, payload)` inserts the key and the
   event in one transaction (`event_keys` table); `ScoutManager` calls it with `session_id` BEFORE clearing pending stats
   and incrementing calibration. A SQLite failure raises, keeps the pending summary, and the boundary retry completes it;
   a crash after the event but before the state write replays without a duplicate (both fault-injected in tests).

## Additional

5. `_session_go_report()` is read-only; the tally is consumed only after `report_once` returned (a failed report write keeps
   the GO evidence).
6. Live strategy-sample increments require `account == current account` exactly.
7. GO-store pruning is chronological (by instance start time).
8. The tally is persisted every cycle, so `cycles_observed` is exact after a crash.
9. `scout_pending_stats_dropped` and `shutdown_discord_pending` are Discord-eligible.

## Verification

- 154 tests pass on Linux (10 new in `tests/test_v23_review_fixes.py`); compilation passes.
- Database migration is automatic and one-way; back up `data/logs/trading.sqlite3` before the first v2.3 start.
