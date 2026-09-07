# XAUUSD MT5 Price-Action Scout Bot v1.6.0

## Delivered

1. Rolling 20-minute range pace replaces net-since-session-open velocity for SLOW/NORMAL/FAST classification.
2. The first 15 session minutes and insufficient rolling history are `WARMUP`; slow-market holding is not applied during warm-up.
3. Rolling scout price samples persist across safe restarts.
4. Next-week outlook is generated at Friday New York close or the successful retry immediately after it.
5. Report queries execute only for session boundaries, Monday Asia weekly rollover, or Friday New York close.
6. Discord suppresses per-leg order/close JSON and emits throttled pair-level lifecycle messages.
7. Session reports list unresolved PA results as `PENDING FINALIZATION`.
8. Strength fallback emits `scout_strength_fallback` once and records its source.
9. Weekly reports include session scout leader counts, average BUY/SELL MFE/MAE and leader accuracy against confirmed PA outcomes in the same session window.
10. All live/session/weekly/next-week reports include display-only one-lot translations and $5/day / $1,500/month progress fields.
11. Discord and Firestore expose explicit `GO` or `NO-GO`; direction still belongs exclusively to the final router.
12. Added an explicit 30-day pattern lookback independent of history/cache bar limits.
13. Added Vue Firestore collection/type documentation and authenticated-read/client-write-deny security rules.
14. Added Windows venv, MT5 login, environment, Task Scheduler/NSSM, smoke-test and log-location instructions.
15. Added mocked MetaTrader5 contract coverage for retcodes, bid/ask entry/close prices, partial close, SL/TP modification, magic isolation and demo/hedging detection.
16. Enabled forward-test safety defaults: 2% daily maximum loss, three consecutive losses and $20 emergency scout SL.

## Verification

- Package version: `1.6.0`.
- Python compilation: passed.
- Complete automated suite: **58 passed**.
- ZIP integrity: passed.

## External validation still required

- Windows MT5 broker-demo fills, partial closes, stop/freeze rules and demo-mode detection.
- Discord webhook delivery and rate-limit behaviour.
- Firestore service-account authentication, Admin writes and Vue authenticated reads.
- Calibration of rolling pace and safety thresholds using broker-demo forward data.

Targets and one-lot figures are manual translations only. They do not guarantee returns and do not alter PA direction or order authorization.
