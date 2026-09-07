# XAUUSD MT5 Price-Action Scout Bot v3.4.0 — the decision card (Sep 7 2026)

One card now answers "go or not" in a glance. Everything it shows is decided once per cycle, stored on the
snapshot as `analysis.decision_trace`, and rendered identically by the webhook, `!status` and `!why`.

## §1 `DecisionExplanation` — `decision_router.py`

`explain_decision(DecisionInput, Decision, ctx)` returns one structure. It never re-decides anything: the
`Decision` has already been made by `final_decision_router` plus the engine's later overrides, and `ctx`
carries only the measurements the router itself never sees (spreads, ages, the zone and its distance, the
confluence family breakdown, the placed ticket, the session tally). **No strategy weight or threshold changed.**

### The gate checklist
Fourteen gates in evaluation order, one row each: `✅ / ❌ / —` · name · measured value vs threshold.

`data` → `clock_account` → `spread` → `confluence` → `side_gap` → `zone` → `trigger` → `pace` → `scouts` →
`rr` → `target` → `session_target` → `risk_locks` → `session_time`

Exactly one row can carry `❌`: the **first** gate that fails. Everything before it shows `✅`; everything
after shows `—`, because the router never evaluated it. That is the difference between "these nine things are
wrong" and "this one thing is stopping you".

### Verdict and colour
| Verdict | Colour | When |
| --- | --- | --- |
| `PLACED` | green | an order was sent this cycle — title carries ticket and lot |
| `GO` | green | every gate clear but nothing sent (PA orders off, or the send failed) |
| `WAIT` | amber | the setup is alive and one gate is pending |
| `NO_TRADE` | red | the router refused |
| `CLOSED` | grey | no session is running |

Titles read `🟠 WAIT · LONG bias 58/100 · inside zone, trigger pending · ASIA · 05:00`.

### The rest of the card
- **Verdict sentence** — ≤ 30 words, plain English, with the numbers: the bias, the one thing blocking it,
  and where price sits relative to the zone. Clipped by whole sentences, never mid-number.
- **What flips it** — up to three numbered, concrete conditions: the price level to reach, the points still
  needed and which family has the headroom to supply them, the trigger required, the spread threshold.
- **Evidence** — FOR: the families backing the chosen side, biggest first (higher timeframes, structure,
  liquidity, location, momentum, candles, silver) plus discount/premium and VWAP bonuses. AGAINST: the
  penalties actually applied and the opposing side's families.
- **Levels** — current price, zone range and kind, the SL the plan would use, the TP1 candidate with its R.
- **Footer** — what GO means today (the session tally), the gates that would still block once price reaches
  the zone, and the `!why` / `!clock` hint.

## §2 The other cards follow the same skeleton
- **Order cards** — title with ticket and lot, a verdict sentence carrying entry, risk and every TP with its
  R multiple, the five gates the order cleared with their values, the management plan (TP1/TP2 percentages,
  break-even R, trail start, invalidation), and the silver line in the footer.
- **Detected card** is now a compact companion: a one-line structure strip (`D1 ▲ H4 ▲ H1 ▼ M15 ▲ M5 ▲`),
  the 3 newest patterns, the 3 newest sweeps **on the bias side**, and zones with their ATR distance.
  Everything else moved behind `!detected full`.

## §3 Commands
- `!status` renders the decision card (so does the webhook, pushed when the verdict **or the blocking gate**
  changes — not merely when the action does).
- `!why` prints the same fourteen gates as text plus what would flip the blocking one.
- `!detected` is compact; `!detected full` restores the long form.
- Snapshots written before v3.4.0 have no trace, so both fall back to the previous layout.

## Verification (Linux, Python 3.11)
- `pytest -q` from `scout/`: **269 passed, 0 failed.**
- New `tests/test_v34_decision_card.py` (16): the trace is on every snapshot with all fourteen gates in order;
  exactly one gate is marked blocking with `✅` before and `—` after; every gate is reachable as the first
  failure; the verdict sentence carries the score, the blocking reason and the threshold within 30 words;
  each verdict renders its own title and colour; the card, the order card, the compact and full detections
  cards, `!status`, `!why` and the webhook change-key all render from the same trace.
