# XAUUSD MT5 Price-Action Scout Bot v3.0.0 — demo-only analysis & validation build

Scope: demo-account XAUUSD analysis bot. Price action decides direction; paired scouts are secondary evidence; historical
outcomes give context; the bot estimates whether a $10 XAUUSD PRICE move (not $10 profit) is still achievable this session.
The bot never trades a live account, never controls or copies to a funded account, never logs in to MT5.

## §4 MT5 connection (breaking)
- `MT5Client(terminal_path)` attaches with `mt5.initialize()` / `mt5.initialize(path=…)` to the already-logged-in terminal.
  No `mt5.login()`, no `MT5_LOGIN/PASSWORD/SERVER` anywhere (AST-tested). `.env.example` = `MT5_TERMINAL_PATH`, `DISCORD_WEBHOOK`, `FIREBASE_KEY_PATH`.
- Validation after attach: terminal connected, DEMO account, trading permitted, Algo Trading on, hedging flag, XAUUSD selectable,
  tick freshness, volume constraints → `mt5_validated` audit. Failure → `mt5.last_error()` in `startup_failed`, no loop, no order.
- `reconnect()`: shutdown → bounded back-off → initialize → validation; engine then adopts positions and runs one cold-start NO_TRADE cycle.

## §5 v2.4 production defects
- 5.1 `_restore_pending_reports()` now runs after `_handle_sessions()` in `run_cycle()`; tested through the real cycle (exactly once, cleared after storage, kept on storage failure, no duplicate on second restart).
- 5.2 Scout-stat idempotency key = `account:symbol:fingerprint:session_id`.
- 5.3 Event-key backfill and duplicate cleanup grouped by that scope; other scopes preserved.
- 5.4 Trade migration inspects columns and unique indexes; handles pre-v2.3, authentic v2.3 (id PK, nullable identity) and current schemas; `NOT NULL DEFAULT 'unknown'`; rollback tested at create/copy/drop.
- 5.5 Discarded pending reports (fingerprint mismatch, invalid session/timestamp, malformed) are persisted immediately (`pending_report_dropped`).
- 5.6 Routing: `session_summary_recovered` and `scout_pending_stats_dropped` → SQLite, Firestore, Discord, console; `shutdown_discord_pending` → SQLite, Firestore (own queue), console — it is emitted after the Discord worker stopped, so it is not claimed for Discord.

## §6 demo-signal semantics (breaking config)
- `reporting` = `research_reference_lot`, `research_daily_price_move`, `research_daily_usd`, `research_monthly_usd` (normalised research values only).
  Removed: `manual_reference_lot`, `daily_target_*`, `monthly_target_usd`, `max_manual_risk_usd`, all `funded_*` / `manual_*` output fields.
- `signal_go` = current demo PA setup valid and executable (PA, scouts, market health, remaining session, demo risk controls). Calibration is shown separately as `calibration_status`.
- `analysis.decision_summary`: `signal_go`, `trade_action`, `scout_verdict`, `target_verdict`, `calibration_status`, `reason`.

## §7 $10 session feasibility — `session_target.py`, config `session_target`
All required outputs; verdicts ACHIEVABLE / STRETCHED / UNLIKELY / INSUFFICIENT_HISTORY (with structural read). Uses remaining time,
rolling pace, required pace, session range vs historical p50/p75/p90 (background scan), ATR, MTF alignment, opposing liquidity,
spread, scout verdict, historical +10 rate/fakeout rate. UNLIKELY → NO_TRADE (`block_when_unlikely`), HIGHER_TF_CONFLICT + STRETCHED → WAIT.

## §8–10 outcome intelligence — `outcomes.py`, config `outcomes`
`setup_outcomes` table with the specified features; `SetupTracker` follows every confirmed setup on closed M1 bars (persisted, restart-safe)
to +3/+5/+10, invalidation or the tracking limit; deterministic `classify_path` (CONTINUATION, REVERSAL, FAILED_BREAKOUT, SWEEP_RECLAIM,
STOP_HUNT_THEN_CONTINUATION, RANGE_REVERSION, INCONCLUSIVE — structure-based, not P/L); `reliability()` with the 5-level fallback,
sample size, comparison level, Wilson intervals; only records decided before the decision timestamp are used.

## §11 multi-timeframe — `mtf.py`
Alignment FULL/PARTIAL/LOWER_TF_COUNTERTREND/HIGHER_TF_CONFLICT/RANGE_CONDITION per D1/H4/H1/M15/M5 with roles; treatment names
(TREND_CONTINUATION … NO_VALID_STRUCTURE) with reasons. Shown in console, Discord and Firestore `analysis`.

## §13–14 reporting
Live snapshot carries MTF, treatment, reliability, fakeout risk, target verdict, remaining minutes, `signal_go`. Session summaries add
setup outcomes, target-verdict tallies, pattern/scout agreement, spread evidence; weekly reports add reliability by session, +10 rate,
fakeout rate, scout agreement, calibration sample counts. Firestore schema 3.0.0 (`analysis` block); Discord messages for strong
scout contradiction, setup resolved, recovery, MT5 attach.

## Verification (Linux)
- 195 tests collected, all pass (34 new in `tests/test_v30_demo_strategy.py`); `python -m compileall` clean; `pip check` clean.
- Timings, 1-CPU container: cold 9.9 s, warm 0.72 s, M5 close 1.25 s, during background scan 0.90–1.00 s.
