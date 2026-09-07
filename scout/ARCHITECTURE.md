# Requirement Traceability

## Implemented

- Modular MT5 client and native timeframe history
- Broker-server clock offset detected once per hour and removed at a single conversion point (`mt5_client.BrokerClock`),
  so every tick, bar, deal and position timestamp downstream is true UTC; only the residual counts as clock skew
- Forming/closed candle separation
- DST-aware London/New York session boundaries
- Hedging-only paired scouts, magic separation, verified close-before-open lifecycle
- Scout P&L, MFE, MAE, leader, displacement, velocity and audit persistence
- Price action remains primary; scouts only confirm, remain neutral, or contradict
- Confirmed pivots, structure state, BOS/CHoCH, candle features and core patterns
- Time-valid liquidity/ORB levels, all sweep events, FVG/OB/S&R zones
- Current-zone-visit tracking and post-touch M1 or fresh M5 confirmation
- Actual bid/ask spread and RR, structural SL/TP and daily-range realism
- Exactly one decision router with hard veto priority
- Order result verification, duplicate checks and demo/live permission gates
- SQLite and JSONL analysis/event/order logs with in-place schema migration
- Confirmed-fill risk/RR, staged TP1/TP2 exits, protected TP3 runner and deal-ledger P/L
- Trigger ownership by setup, direction, zone bounds and validity timestamp
- Configurable scout pace/strength labels and hold guidance without transferring PA direction ownership
- Previous-year liquidity and restart-safe session/weekly/next-week reporting
- Boundary-only report scheduling, rolling scout pace, session warm-up and persisted samples
- Explicit GO/NO-GO plus display-only one-lot/manual-target translations
- Read-only Vue Firestore contract and mocked Windows MT5 adapter contract coverage

## Partially implemented

- Advanced named candlestick combinations: core families are implemented; remaining aliases and market-location qualification can be extended without changing the router.
- Chart geometry: double top/bottom and H&S candidates exist; triangle/wedge/flag/pennant/channel classifiers remain conservative extension points.
- Order blocks: require displacement and BOS/CHoCH, with basic mitigation/break status; institutional refinements are intentionally configurable work.
- Support/resistance: weighted pivot clustering is implemented; reaction-count decay and zone merging can be calibrated per broker.
- Target realism: daily ATR remaining-distance classification is deterministic but not statistically calibrated.

## Future modules

- VWAP/anchored VWAP, premium/discount, breaker/inversion FVG
- Relative/tick volume, trendline retests, advanced session sequences
- XAU/XAG SMT, DXY and US10Y context
- Portfolio-level risk controls
- ML feature store, training, calibration and inference (always behind hard safety rules)
