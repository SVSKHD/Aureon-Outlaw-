# XAUUSD MT5 Price-Action Scout Bot v1.4.0

## Delivered corrections

1. Partial-managed PA entries use structural SL and no broker TP1, preventing a full close at the first target.
2. TP1 and TP2 volumes are computed from original volume, rounded down to broker step, capped below remaining volume, and verified after execution.
3. TP1 confirmation unlocks break-even plus spread/offset; TP2 confirmation unlocks an SL lock at TP1. Failed modifications remain retryable.
4. TP3 remainder trails confirmed M5 higher lows/lower highs, with ATR fallback, and closes at TP3 when reached.
5. LONG management uses bid and SHORT management uses ask. Risk and RR are recalculated from confirmed MT5 fill price.
6. Partial fills are accepted as the actual plan volume; no blind top-up order is sent on hedging accounts.
7. M5 triggers receive engine pivots and require a pivot formed inside the current continuous zone visit, followed by a later breakout/retest.
8. Trigger records own setup ID, direction, zone kind/bounds/validity and confirmation time; mismatched or consumed triggers are vetoed.
9. Sweep deduplication includes level kind, level price, direction and candle time; multiple swing/equal/round levels remain available.
10. Broken, invalidated, fully mitigated and failed zones are excluded from evidence. Expired sweeps remain inactive. Failed breaks support the opposite side, and failed events cannot define premium/discount.
11. VWAP state uses closed M1 candles. Trendline retest starts strictly after the breakout candle.
12. Management operations validate exact ticket/symbol/magic, broker constraints and post-operation state. Scout legs must be equal volume.
13. Realized P/L aggregates every exit deal plus commission and swap. Unresolved finalization is persisted and retried without a six-cycle cutoff.
14. Session-end PA handling uses crossed session-boundary events. Scout closures persist until absence is confirmed before a next pair opens.
15. Existing trade databases gain missing columns with SQLite migrations; legacy position state migrates to account/server/symbol paths rooted at the project configuration.
16. Console/Discord/audit reporting describes staged targets, stop protection, active R/P&L, locked profit, remaining volume and trailing state.
17. Live accounts are rejected in executable code; the bot remains demo-only.

## Modified files

- `src/xau_mt5_bot/__init__.py`
- `src/xau_mt5_bot/config.py`
- `src/xau_mt5_bot/context.py`
- `src/xau_mt5_bot/decision_router.py`
- `src/xau_mt5_bot/engine.py`
- `src/xau_mt5_bot/execution.py`
- `src/xau_mt5_bot/liquidity.py`
- `src/xau_mt5_bot/logger.py`
- `src/xau_mt5_bot/models.py`
- `src/xau_mt5_bot/mt5_client.py`
- `src/xau_mt5_bot/notify.py`
- `src/xau_mt5_bot/position_manager.py`
- `src/xau_mt5_bot/report.py`
- `src/xau_mt5_bot/scouts.py`
- `src/xau_mt5_bot/trigger.py`
- `src/xau_mt5_bot/zones.py`
- `tests/conftest.py`
- `tests/test_triggers.py`
- `pyproject.toml`
- `README.md`
- `ARCHITECTURE.md`

New files: `tests/test_v14_regressions.py`, `EXAMPLE_REPORTS.md`, and this release note.

## Verification

- Python compilation: passed.
- Complete pytest suite: **38 passed**.
- Windows MT5 demo smoke test: not run because the build environment is Linux and has no MetaTrader5 terminal/package.

## Known limitations

- Profitability is neither guaranteed nor demonstrated by unit tests.
- A Windows demo forward test is required with the intended broker's XAUUSD symbol, digits, stop/freeze levels, filling mode, spread, commissions and slippage.
- Complex chart geometry remains conservative and trendline touches remain confirmed-pivot based.
- Broker deal history must remain available for result finalization; missing results remain pending rather than being guessed.
- No AI/ML, Vue work, funded-account locks, portfolio management, automatic lot escalation, or external-market signals were added in this pass.

See `EXAMPLE_REPORTS.md` for LONG, SHORT, WAIT and active-position examples.
