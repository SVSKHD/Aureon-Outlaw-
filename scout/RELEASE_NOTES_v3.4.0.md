# v3.4.0 — one decision card that answers "go or not" at a glance

v3.3.0 made the decision explainable. v3.4.0 makes it *readable*: a single card, in the order a
person actually needs the information, on the webhook, on `!status` and in `!why`.

## The card

**1. Headline** — emoji, state, bias, score, session and time:

```
🟢 LONG PLACED · #501 · 0.02 lot · LONDON 19:04
🟠 WAIT · LONG bias 58/100 · inside zone, trigger · ASIA 06:30
🔴 NO TRADE · LONG bias 49/100 · LONDON 14:12
⚪ CLOSED · no session
```

Green = an order was placed (or every gate passed), amber = the setup is alive and waiting, red =
no trade, grey = the session is closed.

**2. Verdict** — one plain sentence, at most 30 words, with the numbers in it: the bias and its
score, the gate that blocked it with its threshold, and where price is relative to the zone.

**3. Gate checklist** — all 14 router checks in evaluation order, one row each:

```
✅ data fresh        · M1 12s (LIVE) vs < 300s
✅ clock + account   · skew 0s · demo · hedging vs skew ≤ 600s, demo
✅ spread            · 0.21 vs ≤ 0.60
✅ confluence        · 58/100 LONG vs ≥ 55
✅ side gap          · 14 (58L vs 44S) vs ≥ 8
❌ inside zone       · APPROACHING · 0.8 ATR away vs inside FVG 4396.25–4402.88
—  trigger           · not reached
```

The router stops at its first failing check, so the checklist does too: **exactly one ❌**, and every
gate after it reads `not reached` instead of pretending to have passed. The gates that would still
have blocked it are named in the footer instead of competing for attention.

**4. What flips it** — up to three numbered conditions with real numbers: the price level to reach,
how many confluence points are missing, the trigger required, the spread threshold.

**5. Evidence** — FOR the bias: the confluence families with their points, plus discount/premium
location, VWAP and the scout adjustment. AGAINST: the penalties the score already paid
(counter-trend, exhausted daily range, opposing level within 1.5 ATR) and the opposing side's own
strongest families.

**6. Levels** — current price and spread, the zone with its distance in ATR, and the SL and TP1 the
plan would use.

**7. Footer** — what GO means today (a session GO tally, never a standing order), the vetoes that
would still stand once price reaches the zone, and the `!why` hint.

## Order cards reuse the same skeleton

`🟢 LONG PLACED · #501 · 0.02 lot`, a verdict sentence with entry, SL, risk and every take-profit
with its R multiple, the gates that let it through with their values, the management plan
(TP1 → break-even, TP2 → SL at TP1, then the M5 trail, plus the invalidation) and the silver footer.

## Detected becomes a compact companion

One structure strip (`D1 ▲ H4 ▲ H1 ▼ M15 ▲ M5 ▲`), the three newest relevant patterns, the three
newest sweeps on the bias side, and zones with their ATR distance. Everything else — all patterns,
two structure events per timeframe, six sweeps, five zones — moves to **`!detected full`**.

## Tests

275 passing. The four acceptance checks for this change: a decision trace in every snapshot
(including the ones written to SQLite), exactly one first-blocking gate marked, the verdict sentence
carrying both the score and the blocking reason, and a placed order producing the green card with
all its take-profits.

Nothing to configure. No strategy weights or thresholds changed. Demo-only guards unchanged, and the
Discord bot still has no order commands.
