# XAUUSD MT5 Price-Action Scout Bot v1.7.0

This release hardens v1.6.0 against missed boundaries, restarts, mixed account P/L, integration failures and premature funded interpretation.

## Delivered

- Effective 30-day M5 lookback with configuration invariant.
- Startup catch-up for weekly and Friday reports; durable SQLite deduplication.
- Separate startup, boundary and PA-session-end states.
- Immediate scout finalization before the next pair and account-wide PA+scout day locks.
- Historical report semantics separate from live GO/NO-GO.
- Unsigned rolling pace plus separate displacement direction.
- Configurable broker holidays/early closes and retry backoff.
- UTC-safe retention and SQLite WAL recovery check.
- Position-open-based deal-history queries.
- Bounded Firestore series and machine-readable schema contract.
- Discord chunking and HTTP 429 retry/backoff.
- Authoritative `.env` behavior and explicit integration warnings.
- Permanent netting-account incompatibility classification for paired scouts.
- Symbol-aware strength sources with unavailable-state veto.
- Twenty-session pace calibration and twenty-trade strategy-readiness states.
- One-lot plan-risk gate, scout cost reporting and evidence-only/no-mirroring labels.
- Absolute supervisor paths and atomic heartbeat writes.

## Validation

- 74 automated tests pass on Linux.
- Python compilation passes.
- Windows-only and strategy evidence remains governed by `FORWARD_TEST_CHECKLIST.md`.
