# XAUUSD MT5 Price-Action Scout Bot v3.1.0 — silver correlation and Discord command bot (Sep 6 2026)

## §1 XAU/XAG intermarket evidence — `intermarket.py`, config `intermarket`
- Silver (`XAGUSD`, M5 + M15) is loaded through the same cached history path as gold (`load_native_history(symbol=…)`),
  closed bars only. Failure (symbol missing, stale feed) → `status: UNAVAILABLE`, one `intermarket_unavailable` audit,
  cycle continues unchanged.
- Rolling Pearson r of M5 log returns over `correlation_bars` (60): COUPLED ≥ 0.50, WEAK ≥ 0.20, else DECOUPLED.
- Relative strength: XAG% − XAU% over `relative_strength_bars` (12); |rs| ≥ 0.15 pp → `silver_leading` UP/DOWN.
- SMT divergence on the last two confirmed `smt_timeframe` (M15) pivots, matched within 3 bars, newest pivot ≤ 24 bars old:
  gold HH + silver LH = BEARISH; gold LL + silver HL = BULLISH. `smt_divergence` audit + Discord on change.
- Confluence: new family `intermarket`, cap `max_weight` (8): SMT +6, leading +3, **only while COUPLED**. Silver never
  authorises, vetoes or sizes a trade. Patterns list gains `Bullish/Bearish SMT XAU/XAG` and `XAU/XAG decoupled`.
- Reported in `analysis.intermarket` (snapshot, SQLite, Firestore 3.1.0), Discord status line `Silver: …`.
- `intermarket` is part of the strategy fingerprint: enabling it starts a new sample scope for calibration counts.

## §2 read-only Discord command bot — `discord_bot.py`, `run_discord_bot.bat`
- Separate process; reads `data/heartbeat.json`, `data/logs/trading.sqlite3` (read-only URI), `data/positions_*.json`,
  `data/scouts_*.json`. Never imports MetaTrader5, never writes.
- Commands: `!status` `!plan` `!silver` `!scouts` `!positions` `!day` `!trades [n]` `!go` `!events [n]` `!reports [n]`
  `!heartbeat` `!help`. `!buy/!sell/!close` answer with a refusal by design.
- Env: `DISCORD_BOT_TOKEN`, optional `DISCORD_COMMAND_CHANNEL_ID`, `DISCORD_COMMAND_PREFIX`. Install `pip install -e ".[discord]"`.
  Discord Developer Portal: create a bot, enable **Message Content Intent**, invite with `Send Messages` + `Read Message History`.
- Messages are split at 1,900 characters. Formatters are pure functions covered by tests without a Discord connection.

## Verification (Linux)
- 212 tests collected, all pass (17 new: `tests/test_v31_intermarket.py`, `tests/test_v31_discord_bot.py`); `compileall` clean.

## v3.1.1 — quiet Discord (Sep 7 2026)
- `integrations.discord_status_mode` (default `events`): the periodic status block is no longer pushed; get it on demand with
  `!status`. `changes` pushes only when action/entry-state/PA side/session changes; `interval` restores v3.0 behaviour.
- `integrations.discord_event_level` (default `trade`): only startup/shutdown/errors, session transitions and summaries,
  scout pair open/close, orders, TP/BE/trail/close, strong scout contradiction and SMT divergence are pushed. `all` restores
  the full v3.0 event set (cycle_slow, clock_check, mt5_validated, pattern-scan, calibration notices…).

## v3.2.0 — Discord cards (Sep 7 2026)
- `cards.py`: embed builders shared by the webhook and the command bot — GO/NO-GO status card (green GO+order, amber WAIT,
  grey GO/no trade, red NO-GO), Detected card (patterns, structure events, newest sweeps, best zones), SCOUTS PLACED /
  SCOUTS NOT PLACED / SCOUTS CLOSED cards, order / TP / BE / trail / close cards, session transition and summary cards,
  SMT card, error cards.
- Detected card is pushed by the webhook only when a new pattern, structure event or sweep appears (≥ 60 s apart);
  status card obeys `discord_status_mode` (`events` = only via `!status`).
- New audit event `scout_open_failed` (session, message, MT5 retcode, retryable, attempt) emitted once per distinct failure
  on bootstrap and on every session START retry; the card includes the fix hint for retcodes 10004/10006/10013/10014/
  10016/10018/10019/10021/10027/10030/10031 and the netting/margin/clock reasons. A failed START no longer also posts the
  generic SESSION FAILED text.
- Command bot: `!status` and `!detected` reply with cards; `!text` returns the old plain block. 216 tests passing.
