# Example bot reports

These are output-format examples, not trade promises or performance claims.

## LONG — entry confirmed

```text
XAUUSD LONG — ENTRY CONFIRMED
Requested entry: 3512.40
Actual entry: 3512.46
Initial SL: 3507.40
Risk: $5.06
Volume: 0.25 lot
TP1: 3517.52 — 1R — close 50%
TP2: 3522.58 — 2R — close 25% of original volume
TP3: 3530.20 — runner target
Profit protection: after confirmed TP1 move SL to entry + spread/offset; after confirmed TP2 lock SL at TP1; then trail confirmed M5 higher lows with ATR fallback.
PA confluence: 72/100 (not a win probability)
Scout evidence: BUY leading — CONFIRMS
Market speed: NORMAL — follow the confirmed PA plan
Invalidation: M5 close below 3508.10
```

## SHORT — entry confirmed

```text
XAUUSD SHORT — ENTRY CONFIRMED
Requested entry: 3508.20
Actual entry: 3508.12
Initial SL: 3513.20
TP1: 3503.04 — close 50%
TP2: 3497.96 — close 25% of original volume
TP3: 3490.34 — runner target
Profit protection: after confirmed TP1 move SL below entry with spread/offset; after confirmed TP2 lock SL at TP1; then trail confirmed M5 lower highs with ATR fallback.
Scout evidence: SELL leading — CONFIRMS
Market speed: FAST — require normal PA confirmation; do not chase entry
Invalidation: M5 close above 3512.60
```

## WAIT

```text
XAUUSD WAIT
PA bias: LONG
Entry zone: 3509.80–3511.10 SUPPORT
Status: APPROACHING ENTRY
Trigger: WAITING — no confirmed M5 pivot break and later retest in the current visit
Scout evidence: BUY leading — NEUTRAL
Market speed: SLOW
Guidance: HOLD — wait for the next session unless velocity improves
GO status: NO-GO
No PA order authorized by the final router.
```

## Active position

```text
XAUUSD LONG — ACTIVE
Bid / Ask: 3518.30 / 3518.48
Current P/L: +$145.20 | Current R: +1.16R
Current SL: 3512.66
Locked profit: 0.20 price / +$5.00 account currency
TP1: COMPLETE | TP2: PENDING
Remaining volume: 0.12 lot
Next target: 3522.58
Trailing: WAITING FOR TP2
Scout verdict: CONFIRMS
Invalidation: NOT TRIGGERED
```
