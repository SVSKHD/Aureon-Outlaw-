# XAUUSD MT5 Price-Action Scout Bot v2.4.0

Closes the 5 remaining findings of the independent v2.3.0 review (conditional GO). Trading and execution logic unchanged.

1. Friday-close GO is stored under `<fingerprint>:<close-iso>`; a configuration change after Friday yields a `CATCH-UP`
   basis instead of the old configuration's status.
2. Legacy migration converts NULL `account`/`symbol` to `'unknown'` (`COALESCE`); both columns are `NOT NULL DEFAULT 'unknown'`
   in the new and migrated schema.
3. Migration is restart-safe: runs in one `BEGIN IMMEDIATE … COMMIT` transaction with rollback on failure, drops a leftover
   `trades_v3` from an interrupted attempt, and is a no-op on an already-migrated database (all three fault-injected in tests).
4. Session summaries are durable across a crash inside the boundary cycle: when a session ends, a `pending_reports` record
   (`meta`, positions state file) is written before the report; it is cleared only after `report_once` succeeds; at startup
   same-fingerprint records are re-queued into `closed_sessions` (`session_summary_recovered` audit), other-fingerprint
   records are discarded.
5. `event_keys` is backfilled from existing `scout_session_stats.session_id` on first open and pre-existing duplicate rows
   are collapsed (earliest kept); `scout_performance_summary` also de-duplicates by `session_id` at query time.

## Verification

- 161 tests pass on Linux (7 new in `tests/test_v24_review_fixes.py`); compilation passes.
- Migration remains one-way; back up `data/logs/trading.sqlite3` before the first start, or use a fresh database for the
  forward test as the review recommends.
