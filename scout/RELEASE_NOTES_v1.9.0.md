# XAUUSD MT5 Price-Action Scout Bot v1.9.0

Closes the v1.8.0 review findings. Direction logic, router order, TP/SL lifecycle and demo-only enforcement are unchanged;
`structure.detect_pivots`/`analyze_structure` were rewritten for speed with output verified identical to v1.8.0 on 18
randomised cases (pivots, state, events).

## Changed

1. `poll_seconds: 3` (was 5).
2. Pattern scan runs out of process. `pattern_scan.compute_patterns` executes in a `ProcessPoolExecutor` (spawn, 1 worker,
   `BELOW_NORMAL` / nice 10) so the trading cycle no longer shares the GIL with the 30-day scan. First scan at startup is
   synchronous. Falls back to a thread if a process pool cannot be created or the worker dies.
   Build-box timings (1 CPU): warm cycle 0.96 s, bar-close cycle 1.54 s, cycles during a running scan 1.08–1.12 s, cold 12 s.
3. Pattern staleness is explicit. `reporting.pattern_scan = {status CURRENT|PENDING|STALE, bar_time, bars, age_seconds,
   executor, last_scan_ms}`. A new bar's scan is applied as soon as the worker finishes (typically 10–20 s after the bar
   close), not a bar later. Structure events in `patterns` still come from the live 1,200-bar window.
4. Scan failures are audited (`pattern_scan_failed`), the pending key is cleared, and the scan is retried after
   `analysis.pattern_scan_retry_seconds` (default 60). `pattern_scan_complete` is audited on success.
5. Pending key is reset on completion; a history reload with the same last bar but different bar count now triggers a rescan.
6. Strategy fingerprint now also covers `sessions`, `safety` and `magic`.
7. `performance_summary(config_fingerprint=...)`: session and weekly reports count only trades made under the current
   fingerprint; payload carries `config_fingerprint`.
8. Pivot detection vectorised (numpy sliding windows); BOS/CHoCH loop is O(n). ~3x faster per timeframe, output identical.
9. Pace calibration: the scout state file (per account+symbol) is authoritative on restore; the SQLite count is only a
   fallback when the file lacks the counter. `scout_analysis.reset_pace_calibration: true` zeroes the counter for one start
   and audits `pace_calibration_reset`.
10. `cycle_slow` audit event whenever a cycle exceeds `poll_seconds`, with the pattern-scan state attached.
11. `engine.shutdown()` stops the worker on exit; `main` calls it before `client.shutdown()`.
12. End-to-end tests: clean-trend synthetic history produces a directional read; every cycle in a 3-cycle run spanning a
    bar close and a running scan completes under `poll_seconds`.

## Verification

- 94 automated tests pass on Linux (12 new in `tests/test_v19_features.py`).
- Python compilation passes.
- Windows MT5 / Discord / Firestore validation remains governed by `FORWARD_TEST_CHECKLIST.md`.
  On Windows, confirm in the first hour that `pattern_scan_complete.executor == "process"` and that `cycle_slow` does not fire.
