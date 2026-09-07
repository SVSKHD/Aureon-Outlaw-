# Forward-test and funded-readiness checklist

Code tests cannot establish broker compatibility or trading expectancy. Record evidence for every item below on the intended Windows demo terminal.

## Broker and adapter evidence

- Confirm `trade_mode` is detected as DEMO and `margin_mode` as RETAIL_HEDGING. A netting account must show `SCOUTS DISABLED` without repeated order attempts.
- Capture XAUUSD market-order retcodes and actual fill prices for BUY and SELL.
- Confirm partial close on the exact PA ticket, including `DONE_PARTIAL`, commission and remaining volume.
- Confirm SL modification outside the broker's stops/freeze levels and verify the modified position.
- Record the broker-supported filling mode (FOK, IOC or RETURN).
- Record Asia/London/New York spread distributions. Change `max_spread_price: 0.60` only from observed data.
- Compare the broker's D1 boundary with `broker_trading_date`, PDH/PDL and the terminal chart.
- Verify holiday and early-close dates in `config.yaml` each week.

## Integration and recovery evidence

- Receive a live Discord snapshot, scout-pair lifecycle line, long split message and a controlled rate-limit retry.
- Confirm Firestore Admin writes for `sessions`, `days`, `events` and `telemetry`; deploy and test `firestore.rules` using an authenticated read-only Vue user.
- Compare a written session document to `FIRESTORE_SCHEMA.json` and confirm series never exceed 240 points.
- Terminate the child process during an open PA trade. Confirm supervisor restart, SQLite `quick_check`, state adoption and no restart-driven session-end close.
- Restart after missing Monday Asia and after Friday NY close. Confirm one deduplicated `CATCH_UP` weekly/outlook report.
- Leave a mocked or controlled position record older than 90 days and confirm deal finalization searches from its open time.

## Calibration evidence

- Collect at least 20 complete scout sessions before `hold_when_slow` is trusted. Review 0.03/0.15 price-per-minute buckets against observed session behavior.
- Collect at least 20 confirmed PA outcomes before funded readiness can become `READY`.
- Review PA confluence components, the eight-point tie band, win/loss distribution and out-of-sample stability. Confluence is not a probability.
- Record the actual combined scout-pair net after both spreads and commissions. Never mirror scout pairs into the funded/manual account.
- Confirm every funded `GO` has translated one-lot SL risk at or below `reporting.max_manual_risk_usd` (default $1,500 ≈ a $15 XAUUSD stop at 1 lot).
- Treat the $5 move/day (≈ $500 P/L at 1 lot) and $1,500/month as reporting references, not expected returns.

## Final approval

Keep `allow_live_account: false`. This package controls only the demo terminal; it cannot enforce risk on a separate manual funded account. Approval for manual funded use requires the broker evidence, integration evidence and calibration sample above, plus an independent review of drawdown and transaction costs.

## v2.0.0 additions
- First hour: `funded_translation` event shows `daily_target_move_pnl_usd ≈ 500` and `max_sl_distance_risk_usd ≈ 1500` with no warnings; otherwise fix `reporting` before continuing.
- First order: `order` event `type_filling` matches the broker's advertised mode (IOC on most CFD brokers); no `10030 unsupported filling mode` retcodes.
- After the first session close: exactly one `scout_session_stats` and `calibration_sessions` increments by 1 only when both legs are gone.
- Kill the process once mid-session and restart: `state_file_recovered` must NOT appear; adopted PA position keeps its plan; Firestore `heavy/series` continues.
- Confirm Firestore `events.date` is the broker trading date and `posted_discord` is true for Discord-eligible events.

## v3.0.0 additions (demo terminal only)
- Terminal already logged in to the demo account; bot starts without any credential; `mt5_validated` shows is_demo=true, algo_trading=true, is_hedging as expected, tick_age_seconds small.
- Kill the terminal connection once: `restart` (mt5_reconnect) event, positions adopted, first cycle after reconnect is NO_TRADE.
- First confirmed setup: `setup_outcomes.registered` increments; within the session `setup_outcome_resolved` arrives with MFE/MAE and a classification.
- `session_target.target_verdict` is INSUFFICIENT_HISTORY with a structural read until 20 resolved comparable setups exist; ACHIEVABLE/STRETCHED/UNLIKELY afterwards.
- Discord shows one pair-level scout message per session, `STRONG SCOUT CONTRADICTION` when applicable, and the `$10 target` line in each status.
- Operator-maintained: `MT5_TERMINAL_PATH` (only if several terminals), Discord webhook, Firebase key path, `market_holidays`, `market_early_closes`, `max_spread_price` after observing the feed, pace thresholds after 20 sessions.
