# XAUUSD MT5 Price-Action Scout Bot v1.5.0

## Delivered

1. Added configurable `SLOW`, `NORMAL`, and `FAST` scout market-speed labels.
2. Added explicit `HOLD — wait for the next session unless velocity improves` guidance and a configurable slow-market router wait.
3. Made scout price-strength step, minimum verdict strength, and strong contradiction threshold configurable.
4. Uses broker profit calculation to convert scout P/L divergence to a lot-neutral price-equivalent strength when available.
5. Added previous-year high and low (`PYH` and `PYL`) from native D1 history with next-year validity.
6. Added session summaries combining confirmed PA performance and closed scout statistics.
7. Added completed-week performance reports with trades, wins, losses, net/average/best/worst P/L and duration.
8. Added Friday next-week-open reports with yearly/monthly/weekly reference levels, current PA bias, scout leader and pace guidance.
9. Added a persistent report registry so restart cycles do not duplicate the same report.
10. Added missing Discord scout lifecycle events: open, close, rollback, repair, adoption, stale-pair close and close retries.
11. Replaced the hardcoded Discord `IST` suffix with the abbreviation produced by the configured display timezone.
12. Added market speed and guidance to console, Discord, Firestore and telemetry output.
13. Preserved the v1.4 demo-only, single-router, trigger-ownership and TP/SL execution lifecycle.

## Verification

- Python compilation: passed.
- Complete automated suite: **44 passed**.
- Windows MT5/Discord/Firestore smoke test: not available in the Linux build environment.

## Remaining external validation

- Confirm broker-specific MT5 demo fills, partial closes, stop/freeze levels and account-mode detection on Windows.
- Confirm the configured Discord webhook receives lifecycle and scheduled report messages.
- Confirm the Firestore service account can write snapshots, series, events and telemetry.
- Forward-test the pace thresholds on the intended broker; they are configurable starting values, not profitability claims.
