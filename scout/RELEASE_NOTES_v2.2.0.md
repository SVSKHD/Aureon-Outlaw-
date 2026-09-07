# XAUUSD MT5 Price-Action Scout Bot v2.2.0

Closes the 5 blocking and 5 minor findings of the independent v2.1.0 review. Strategy logic, execution lifecycle,
pattern scanning and demo-only enforcement unchanged.

## Blocking

1. Strategy readiness scoped by account+symbol+fingerprint: `confirmed_trade_count(fp, account=, symbol=)`; the engine loads
   the count once the account key is known (not at construction) and the live increment also requires a matching account.
2. GO tallies keyed by session instance (`LONDON@<start-iso>`), not session name. A GO from a session whose close was missed
   can no longer leak into a later session; instances that never reported are aged out (last 12 kept).
   `session_summary` carries `session_instance`.
3. Pending scout summary is restored only when account, symbol AND fingerprint match; otherwise dropped with
   `scout_pending_stats_dropped` audit.
4. Firestore event `date` comes from `engine.cycle_tdate`, set at the top of `run_cycle` before any event of that cycle;
   startup/pre-cycle events use the broker trading date of now — never null.
5. Bounded shutdown: worker polls with a stop flag; `drain()` uses `put_nowait` and cannot block on a full queue. Discord is
   drained first, then Firestore, so `mark_discord` results are included; if Discord does not finish in 20 s a
   `shutdown_discord_pending` audit is written and late results remain `posted_discord: null` by design.

## Minor

6. Scout state is persisted before `scout_session_stats` is emitted; each stat carries a `session_id`
   (`<SESSION>@<open-iso>`) and SQLite recovery counts DISTINCT session ids — a duplicate emission after a crash counts once.
7. Weekly GO aggregation requires exact account/symbol/fingerprint on session summaries (legacy reports excluded).
8. `FIRESTORE_SCHEMA.json` requires `discord_eligible`; `posted_discord_values: [true, false, null]`.
9. `FIRESTORE_SCHEMA.md` documents `posted_discord` as boolean-or-null and `date` as never null.
10. `Discord.event()` uses `self.ELIGIBLE_EVENTS` directly.

## Verification

- 144 tests pass on Linux (10 new in `tests/test_v22_review_fixes.py`); compilation passes.
- Versions: package, `SCHEMA_VERSION`, `FIRESTORE_SCHEMA.json` = 2.2.0.
