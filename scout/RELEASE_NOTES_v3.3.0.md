# v3.3.0 — broker clock offset, and a decision you can read

Two problems, one theme: the bot knew things it was not telling you, and one of them stopped it trading.

## 1. Scouts were never placed — broker clock offset (root cause)

Live evidence, Asia session, Monday 7 Sep 2026:

> `Scout orders blocked: broker clock skew 10799s > 600s`

10 799 s ≈ 3 h. The MT5 server runs on **UTC+3**, and `symbol_info_tick().time` and the `time`
column of `copy_rates_*()` are epoch seconds in the **broker server timezone**, not UTC. The bot
read them as UTC (`mt5_client.py` `get_tick` / `get_bars`), so:

* `engine._validate_clock` measured a permanent 3-hour "skew" and `orders_allowed()` refused every
  scout and PA order, for ever;
* every bar stamp was 3 h ahead, so `sessions.session_at()`, freshness (LIVE/WARNING/STALE), sweep
  ages and the pattern times on the cards were all wrong by one session;
* analysis timestamps written to SQLite, Firestore and Discord were wrong by the same 3 h.

### The fix — one conversion point

`mt5_client.BrokerClock` converts broker-server time to true UTC, and everything downstream sees
only true UTC:

* the offset is measured at startup/`validate_terminal` from one tick and rounded to the nearest
  30 minutes (server timezones are whole or half hours). It is adopted **only** if the raw difference
  is within 120 s of that grid — an arbitrary difference is a broken PC clock, never a timezone;
* it is re-measured at most once an hour, and on every reconnect. After the first detection the
  offset only moves for a **DST step** — a whole number of hours that leaves under a minute behind.
  Any other difference stays visible as residual skew instead of being absorbed into a new
  "timezone";
* it is applied to every tick, every bar `time`, deal times and deal-history query windows.
  `broker_epoch_to_utc()` routes the last raw epoch reads (`position.time` in `position_manager`,
  scout adoption in `scouts.py`) through the same clock;
* **the clock guard now acts on the residual skew only** — that is the real fault. A wrong PC clock
  still blocks orders, and the block message names the detected offset.

### Configuration

```yaml
safety:
  broker_utc_offset_hours: null   # null = auto-detect (recommended); 3, 2.5, -5 … to pin it
  max_clock_skew_seconds: 600     # residual tolerated AFTER the offset is applied
```

A manual value must be a whole or half hour; it wins over auto-detection and any difference from
the real server time then shows up as residual skew (so a wrong override is loud, not silent).

### Where the offset is visible

`broker_clock_offset` audit event (once per detected offset) · the `mt5_validated` event ·
`data/heartbeat.json` · `snapshot.analysis.broker_clock` · the status card ("Broker clock") ·
`!clock` in Discord · Firestore `price.broker_utc_offset_hours`.

## 2. The decision now explains itself

`decision_router.explain_decision()` renders the same `DecisionInput` the router just judged:

* **verdict** — one sentence: *"NO TRADE — LONG bias 46/100; blocked by confluence (46) and inside
  zone (APPROACHING)"*, or *"LONG authorised — FVG 4396.25–4402.88, M1 trigger 13:41, confluence
  72/100, RR 1.18"*;
* **gates** — all 17 in router order (data fresh, account safe, clock, spread, setup exists,
  confluence, PA side gap, inside zone, trigger fresh, market pace, scouts, RR, target realism,
  $ session feasibility, day lock, cooldown, one-position rule), each with value, threshold and a
  note. Gates an earlier failure made unreachable say `not reached` rather than pretending to pass;
* **next** — what would flip each failed gate ("9 more confluence points", "price back inside
  FVG 4396.25–4402.88", "spread at or below 0.60");
* **evidence** — the three strongest confluence families per side with points, plus the silver line.

It is carried in `snapshot.analysis["decision_trace"]`, printed as a `DECISION TRACE` block every
cycle, shown on the status card as **Verdict / Blocked by / Next**, and stored in SQLite and
Firestore. GO/NO-GO now also states in one line what GO means, so it is never read as an order.

## 3. Cards tell the whole story

* **SCOUTS NOT PLACED** maps the reason *text*, not just retcodes: clock skew (with the detected
  offset and what to do about the PC clock), spread vs limit, free margin vs the pair, netting
  accounts, the day lock, live accounts, a closed market, a stale tick. Plus **Detected offset**,
  **Attempt N** and **Next retry** in `display_timezone`.
* **SCOUTS PLACED** — both tickets, entries, lot, session open price, emergency SL distance, and
  "after N failed attempts" when it followed failures.
* **SCOUT LEG ROLLED BACK** — which leg failed, its retcode, what was closed.
* **SCOUTS CLOSED** — both tickets, each P/L, pair P/L, leader, MFE/MAE, verdict strength, pace.
* **SCOUTS ADOPTED ON RESTART** — what the restart took over.
* **ORDER PLACED** — this card previously could not appear: `notify.py` listed the `order` event but
  nothing ever emitted it. The engine now does, with ticket, entry, SL and reason, all TPs with
  their R multiples, volume, risk in price and currency, zone kind, trigger reason, confluence,
  scout verdict and the silver line.
* **TP1 / break-even / TP2 lock / trail / close** — realised P/L so far, remaining volume, new SL
  (and what it was).
* **Detected** — six newest sweeps by age, deduped on level+price, `ROUND_1 ×3 (4407–4409)`
  collapsed into one line, two newest structure events per timeframe, zones with their distance
  from price in ATR.

## 4. New read-only Discord commands

* `!why` (alias `!decide`) — the full gate table, what would flip each failed gate, and the evidence.
* `!clock` — system UTC, broker time, detected offset, residual skew, guard status.

There are still **no order commands**, by design: the trading loop's router is the only
authorisation point.

## 5. Merged with the manual-offset fix (PR #2)

PR #2 landed on `master` while this work was in flight, attacking the same failure with a **manual**
`safety.broker_timestamp_offset_seconds` plus stricter guards. Both are kept:

* an offset is adopted automatically **only** when the broker-minus-system difference sits within
  120 s of the half-hour grid every MT5 server timezone lives on. A broken PC clock produces an
  arbitrary difference and is never absorbed into an invented offset — it stays as residual skew and
  blocks orders, which is exactly what PR #2 set out to protect;
* a tick still in the future after the offset is refused at startup, and the clock window is
  asymmetric (a lag is normal, a future tick is not);
* `broker_timestamp_offset_seconds` still works as the manual form and is honoured whenever
  `broker_utc_offset_hours` is null; the hours key wins when both are set;
* PR #2's manual-readiness status card (READY / WAIT title, manual entry-SL-targets, next pattern to
  watch, fakeout assessment, validity note), hourly/off Discord status modes, the `allow_pa_orders`
  execution gate and `tools/diagnose_clock.py` are all retained, with the decision trace added on top.

One test changed meaning deliberately: `test_future_tick_rejected_without_guessing_offset` asserted
that even an exact +3 h server must be refused, which would keep the reported failure alive. It is now
`test_off_grid_future_tick_is_rejected_not_guessed_as_a_timezone` — a 25-minute future tick is still
refused, an exact timezone is detected.

## 6. Also in this release

* **Python 3.11 fix**: `cards.py` and `discord_bot.py` used nested same-type quotes inside an
  f-string, which only parses on 3.12+. On the supported 3.11 runtime the whole package — and the
  entire test suite — failed to import. Fixed.
* `.gitignore` added; `__pycache__/` and `.pytest_cache/` were being committed and are now untracked.
* Firestore `SCHEMA_VERSION` 3.3.0, `FIRESTORE_SCHEMA.json` adds `analysis.decision_trace`,
  `analysis.broker_clock` and `price.broker_utc_offset_hours`.
* Version 3.3.0 in `pyproject.toml` and `__init__.py`.
* Tests: 216 → 275, all passing (`test_v33_broker_clock.py`, `test_v33_decision_and_cards.py`, plus
  PR #2's `test_clock_decision_cards.py`).

## Upgrading

Nothing to configure. `safety.broker_utc_offset_hours` defaults to `null` (auto-detect) and
`max_clock_skew_seconds` keeps its 600 s default. Set the offset by hand only if your broker's
server time cannot be detected from its own ticks.

Demo-only guards are unchanged. Strategy weights and thresholds are unchanged.
