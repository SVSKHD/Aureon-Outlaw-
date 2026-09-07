# XAUUSD MT5 Price-Action Scout Bot v2.0.0

Closes all 19 findings of the independent v1.9.0 review. Breaking config change in `reporting` (see item 10).

## Blocking defects fixed

1. MT5 filling mode. `SYMBOL_FILLING_FOK/IOC` are capability *flags* (1, 2); `ORDER_FILLING_FOK/IOC/RETURN` are enum *values*
   (0, 1, 2). `_filling_mode()` now maps flags → enum (IOC preferred, then FOK, else RETURN). The mocked MT5 uses the real
   constant values; parametrised test covers FOK-only, IOC-only, both, none.
2. Risk lock vs. realized losses. `positions.update()` runs before the gate; `orders_allowed()` first finalises anything that
   closed at the broker since the last check; the PA order path re-checks `orders_allowed` + `entry_allowed` at send time and
   audits `order_withheld`. Test: a SL hit between cycles locks the day before any order.
3. Scout repair uses the same `orders_allowed` gate, so a leg stopped between cycles cannot be reopened past the lock.
4. Calibration integrity. `scout_session_stats` and the calibration increment fire only after both legs are confirmed
   closed; failed closes (3 attempts) leave the counter at 0.
5. Calibration scope. State file stores `{account, symbol, fingerprint}`; a scope change resets to 0 with a
   `pace_calibration_reset` audit. SQLite fallback is `json_extract`-scoped to account+symbol+fingerprint; the unscoped
   `event_count` fallback at engine init is removed.
6. Trigger freshness. Detectors return `trigger_bar_time` (the confirming M1/M5 bar); detection delay, `confirmation_timestamp`
   and carry-grace are measured from it, not from the newest closed bar.
7. Cold start. The first cycle after start or MT5 reconnect (`startup_cycle`) can never place a PA order; LONG/SHORT is
   downgraded to NO_TRADE with reason and `order_withheld` audit. Scout pairs are unaffected.
8. Crash-safe state. `statefile.atomic_write_json`: temp file + fsync + `os.replace`, previous copy kept as `.bak`;
   `load_json_state` falls back to the backup and the manager audits `state_file_recovered`. Used by positions and scouts.
9. Early closes. `boundaries_for_utc_day` emits a CLOSE boundary at the configured early-close time for the session running
   at that moment, so scouts are closed by the boundary, not by a rejected order later.

## Reporting and readiness

10. Currency vs. price (breaking). `reporting` now has `daily_target_price_move: 5.0`, `daily_target_usd: 500.0`,
    `monthly_target_usd: 1500.0`, `max_manual_risk_usd: 1500.0`; `max_manual_risk_to_daily_target_ratio` is removed.
    The funded GO gate compares `order_calc_profit(plan SL, reference lot)` against `max_manual_risk_usd`. At startup
    `funded_translation` audits the P/L of the target move and of `max_sl_distance_price` at the reference lot and warns
    when the configured currency values disagree with broker arithmetic. Snapshot `reporting` carries
    `manual_plan_stop_distance_price` (price) and `manual_plan_risk_usd` (currency) as separate fields.
11. Open trades are bound to the fingerprint active when they were opened (`tracked[...].config_fingerprint`); the trade
    record keeps it through restarts and parameter changes.
12. `scout_performance_summary(account=, config_fingerprint=)` — weekly scout statistics are current-account,
    current-parameter only.
13. Historical GO preserved: `session_summary.go` (any live GO during the session, with `go_cycles`, `first_go_ts`,
    `last_go_ts`), `weekly_report.go` (any GO session in the week, `go_sessions`), `next_week_open_report.go` (live status
    at Friday close). Each carries `go_basis`. Discord headers show it.

## Firestore and notifications

14. `FirestoreSink.SCHEMA_VERSION` = `FIRESTORE_SCHEMA.json.version` = 2.0.0 (asserted by test).
15. Firestore events carry the broker trading date and the actual Discord delivery result (`posted_discord` true/false,
    or null when the event was not eligible for Discord).
16. Series continuity: on the first push of a session document the sink loads the existing `heavy/series` and appends.
17. Forced push: `first_cycle` is evaluated before `run_cycle()`; a push is also forced on session boundaries.
18. Discord forwards `cycle_slow`, `pattern_scan_failed`, `pattern_scan_executor`, `pace_calibration_reset`, `order_withheld`,
    `state_file_recovered`, `funded_translation`, `session_transition_delayed`.
19. Two independent delivery queues (Discord, Firestore); Discord back-off cannot delay Firestore; both drained (20 s) on
    graceful shutdown.

## Verification

- 120 tests pass on Linux (26 new in `tests/test_v20_review_fixes.py`), compilation passes.
- Timing unchanged from v1.9.0 on a 1-CPU box: warm cycle 0.97–1.05 s, bar-close 1.59 s, during background scan 1.06–1.14 s.
- Config migration: delete `max_manual_risk_to_daily_target_ratio` and set the four `reporting` currency/price fields; the old
  key now fails validation on load.

## Still Windows-only

Real demo/hedging detection, fill modes and retcodes on the broker's XAUUSD, partial closes, stops/freeze levels, Asia spread
calibration, Firestore Admin writes, Discord delivery — see `FORWARD_TEST_CHECKLIST.md`.
