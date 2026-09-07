# XAUUSD MT5 Price-Action Scout Bot v1.8.0

Closes the v1.7.0 review findings. No change to PA direction logic, router order, TP/SL lifecycle or demo-only enforcement.

## Fixed

1. Cycle time (blocking). `analysis_window_bars.M5` is back to 1,200 bars for structure, zones, sweeps and triggers. The 30-day
   pattern scan now runs on its own window (`history.pattern_window`, 8,640 M5 bars) and is recomputed only when a new M5 bar
   closes — synchronously once at startup, then in a background thread while the previous bar's result is served.
   Measured on the build machine: per-cycle path 2.2–2.9 s (was 12–18 s), bar-close cycle 2.9 s, cold start 17 s.
   The `history_bars.M5 >= lookback` validator is unchanged.
2. Trigger detection delay (`max_trigger_detection_delay_seconds`) is applied only to the first detection of a trigger.
   Carried triggers keep `trigger_grace_m1_bars`; the two settings no longer conflict, and a carried trigger cannot be
   mis-measured against a newer bar.
3. Funded GO gate: `max_manual_risk_to_daily_target_ratio` default 3.0 (1-lot SL risk ≤ $15, aligned with
   `max_sl_distance_price: 15`). At 1.0 nearly every valid PA plan reported NO-GO.
4. Scout pace calibration counter is persisted in the scout state file (in addition to the SQLite `scout_session_stats`
   count), so it survives restarts and 30-day event retention.
5. Strategy-readiness sample is scoped to a strategy fingerprint (`fingerprint.py`: SHA-1 of symbol, analysis, management,
   risk, scout_analysis and analysis windows). Every trade record carries `config_fingerprint` (SQLite migration adds the
   column); `confirmed_trade_count(fingerprint)` counts only trades made under the current parameters, so a parameter
   change restarts the 20-trade sample.

## Not changed (operational, by design)

- `market_holidays` / `market_early_closes` ship empty; fill them before each forward-test week (FORWARD_TEST_CHECKLIST.md).

## Verification

- 82 automated tests pass on Linux (8 new in `tests/test_v18_features.py`, including a full `run_cycle` end-to-end timing test
  with synthetic 30-day history).
- Python compilation passes.
- Windows MT5 / Discord / Firestore validation remains governed by `FORWARD_TEST_CHECKLIST.md`.

## Known residual

- On the bar-close cycle the background pattern scan shares the interpreter with the trading cycle; one cycle every 5 minutes
  may run ~5 s (observed 5.2 s on the build machine), i.e. at `poll_seconds`, but well under the 15 s detection limit.
