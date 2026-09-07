# XAUUSD MT5 Price-Action Scout Bot v3.3.0 — broker clock offset and self-explanatory cards (Sep 7 2026)

## §1 The bug: MT5 timestamps are broker-server time, not UTC — `mt5_client.py`

Live evidence, Asia session, Monday 7 Sep 2026:

> `Scout orders blocked: broker clock skew 10799s > 600s`

10 799 s ≈ 3 h. That is not clock skew — it is the broker server's UTC+3 timezone.

`symbol_info_tick().time`, `copy_rates_*()['time']`, `history_deals_get()` bounds and `position.time` are all epoch
seconds of the **broker server's wall clock**. v3.2.0 read them as UTC (`mt5_client.py:174` even carried the comment
*"MT5 Unix timestamps are UTC instants; never apply a broker-offset subtraction"*), so on any broker that is not on UTC:

- `engine._validate_clock` measured `tick.time − datetime.now(UTC)` = the timezone offset, and `orders_allowed()`
  refused **every** scout and PA order, permanently;
- every bar timestamp was N hours in the future, so `sessions.session_at()` returned the wrong session, freshness
  flipped between LIVE and STALE for the wrong reasons, sweep ages and pattern times were wrong on the cards;
- analysis timestamps written to SQLite, Firestore and Discord were wrong by the same N hours.

## §2 The fix: one conversion point — `mt5_client.BrokerClock`

- **Detection.** At `validate_terminal()` and once per hour thereafter, the raw
  `(broker tick time read as UTC) − system UTC` delta is rounded to the nearest **30 minutes** — the only offsets real
  brokers use, half-hour zones included. That rounded value is `broker_utc_offset`; what remains is `residual_seconds`.
- **Application.** `get_tick()` and `get_bars()` subtract the offset before anything downstream sees a timestamp;
  `history_deals_get()` bounds are converted the other way (`to_broker`); deal and position times come back as UTC via
  `epoch_to_utc()` / `position_open_time()`. No module outside `mt5_client.py` calls `datetime.fromtimestamp()` any more —
  there is a test that enforces this.
- **The real guard.** `engine._validate_clock` now reads `residual_seconds`. Orders are blocked only when genuine skew
  exceeds `safety.max_clock_skew_seconds`. A UTC+3 broker with a correct PC clock has a residual of ~0 s and trades.
  A PC clock 25 minutes out still blocks, with the offset named in the message.
- **Re-measurement.** At most once per hour, and immediately on `reconnect()` (which calls `clock.invalidate()`), so a
  broker that moves between summer and winter time is picked up without a restart. Inside that hour the offset is held
  fixed and only the residual is recomputed — that is what makes real drift visible instead of being absorbed.
- **Reporting.** One `broker_clock_offset` audit event per detected offset change (hours, residual, raw delta, source,
  server). The offset also appears in the `mt5_validated` event, `data/heartbeat.json`
  (`broker_clock`, `broker_utc_offset_hours`, `broker_clock_residual_seconds`), the snapshot
  (`analysis.broker_clock`) and Firestore `price.broker_utc_offset_hours`.

### Configuration — `config.yaml`
```yaml
safety:
  broker_utc_offset_hours: null      # null = auto-detect (default). Float pins a known server, e.g. 3.0
  broker_offset_remeasure_seconds: 3600
  max_clock_skew_seconds: 600        # genuine skew AFTER the offset is removed
```
A manual override always wins over auto-detection and is never replaced by a later measurement, so a wrong pin shows up
as skew and blocks orders rather than silently distorting timestamps. **Auto-detect needs no configuration change.**

## §3 Discord cards that tell the whole story — `cards.py`, `notify.py`, `discord_bot.py`

- **SCOUTS NOT PLACED** maps the *reason text*, not only MT5 retcodes: clock skew, spread, margin / not safe, netting,
  day lock, live account, market closed, duplicate pair, scouts disabled. New fields: `Detected offset`,
  `Spread` (current vs limit), `Margin` (free vs required), `Attempt N`, `Next retry` in `display_timezone`.
- **SCOUTS PLACED** is green and says *"after N failed attempts"* when it follows failures; it carries both tickets,
  both entry prices, the lot, the session open price and the emergency SL distance.
- **SCOUT LEG ROLLED BACK** (which leg, retcode, what was closed), **SCOUTS CLOSED** (both tickets, each P/L, pair P/L,
  leader, MFE/MAE, verdict strength) and **SCOUTS ADOPTED** on restart are all full cards. Colours unchanged:
  green / amber / grey / red.
- **Order lifecycle** cards carry ticket, entry, SL, every TP with its R multiple, volume, risk in currency, zone kind,
  trigger reason, confluence, scout verdict and the silver line. TP1 / break-even / TP2 / trail / close cards show
  realised P/L so far, remaining volume and the new SL.
- **Status card and `!status`** gain a **Blocked by** field listing every router veto currently active, in evaluation
  order: spread, confluence < 55, not inside zone, no trigger, SLOW, scouts contradict, RR, target, session feasibility,
  clock, day lock. A NO-GO now explains itself.
- **Detected card** shows at most 6 newest sweeps sorted by age, dedupes identical `level_type` + price, collapses
  ROUND_1 into one line (`ROUND_1 ×3 (4407.00–4409.00)`), shows the two newest structure events per timeframe and up to
  5 zones with their distance from price in ATR.

### New commands — read-only, still no order commands
- `!why` — the Blocked by list plus what would flip each veto.
- `!clock` — system UTC, broker server time, detected offset, residual skew, guard status.

## §4 Where the veto list comes from — `decision_router.py`
`router_vetoes(DecisionInput, gate)` enumerates the router's ladder in the same order the router evaluates it, with the
numbers behind each check and a `flips_when` sentence. `blocked_by()` filters it to the active ones. Nothing about the
decision itself changed: **no strategy weights or thresholds were touched**, and the demo-only guards are unchanged.
The snapshot gains `analysis.router_vetoes`, `analysis.blocked_by` and `analysis.broker_clock`.

## §5 Repository layout
The v3.2.0 upload landed every module, test and document in the repository root, so `pyproject.toml`'s src-layout
package discovery and `testpaths` never resolved and `pytest` could not collect. Package modules now live under
`src/xau_mt5_bot/`, tests and `conftest.py` under `tests/`; entry points, `config.yaml` and the documents stay at the
root. Two nested same-quote f-strings that only parse on Python 3.12+ were rewritten — the project targets 3.11.
`env.example` was restored to `.env.example`.

## Verification (Linux, Python 3.11)
- 241 tests collected, all pass (216 pre-existing + 25 new).
- New: `tests/test_v33_broker_clock.py` (14). Extended: `tests/test_v31_discord_bot.py` (+11).
- Covered: +3 h offset detected with residual < 5 s and scouts opening; a 25-minute genuine skew after the offset still
  blocking with the clock message; bar times converted so `session_at()` and freshness are right; a manual override
  winning over auto-detection and never being replaced; hourly / on-reconnect re-measurement; and a source audit that
  no module outside `mt5_client.py` converts a raw MT5 epoch.

## Upgrade
Nothing to change in `config.yaml` — `broker_utc_offset_hours` defaults to `null` (auto-detect). Restart the bot and
check the `!clock` reply or the `broker_clock_offset` event in `!events` to confirm the detected offset matches your
broker's server time.
