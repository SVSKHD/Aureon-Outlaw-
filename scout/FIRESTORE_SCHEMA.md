# Firestore contract for the Vue application

The Python bot is the only writer. The Vue application should authenticate with Firebase and use read-only listeners. Server timestamps are Firestore timestamps; numeric prices/P&L are numbers; optional values may be `null`.

## Collections

### `sessions/{tradingDate}_{SESSION}`

Updated every 300 seconds and on forced transitions.

| Field | Type | Meaning |
| --- | --- | --- |
| `date`, `session`, `symbol` | string | Broker trading date, session enum, symbol |
| `updated_ts` | timestamp | Latest server write |
| `price` | map | `bid`, `ask`, `spread` numbers and `freshness` string |
| `structure` | map<string,string> | D1/H4/H1/M15/M5 state |
| `pa` | map | Side, confluence, entry state, selected zone and trigger |
| `scouts` | map | Tickets, entries, raw P/L, MFE/MAE, leader, verdict, strength, unsigned rolling pace, separate displacement direction, calibration and SL-risk fields |
| `plan` | map/null | Requested/actual entry, SL, TPs, RR, volume and invalidation |
| `final` | map | `action`, one-word `go` (`GO`/`NO-GO`), reason and timestamp |
| `reporting` | map | Reference lot; `daily_target_price_move` ($5 XAUUSD move) and `daily_target_usd` ($500 at 1 lot) as separate fields; `monthly_target_usd` ($1,500); `manual_plan_stop_distance_price` / `manual_plan_risk_usd` / `manual_risk_limit_usd` ($1,500); `funded_translation` (broker order_calc_profit check); scaled scout/active-PA P/L; `config_fingerprint` (12-char strategy hash); `pattern_scan` map: `status` CURRENT/PENDING/STALE, `bar_time`, `bars`, `age_seconds`, `executor`, `last_scan_ms` |
| `patterns`, `sweeps`, `levels` | array/map | Recent deterministic evidence |

Subcollection `sessions/{id}/heavy/series` contains arrays: `t`, `bid`, `buy_pnl`, `sell_pnl`, `action`. Samples are taken every 30 seconds, capped to the latest 240 points, and pushed with the parent interval. The cap prevents unbounded document growth and repeated full-session rewrites.

### `days/{YYYY-MM-DD}`

Map `sessions.{SESSION}` contains `final`, `go`, `pa`, `confluence`, and `scout_leader`.

### `events/{autoId}`

| Field | Type |
| --- | --- |
| `ts` | timestamp |
| `kind` | string |
| `date` | string | Broker trading date captured at emission (never null since v3.0.0) |
| `payload` | map |
| `discord_eligible` | boolean | Whether this event kind is forwarded to Discord at all |
| `posted_discord` | boolean or null | `null` at write time (result not yet known, or event not Discord-eligible); set to `true`/`false` by the Discord worker once delivery is attempted |

Important kinds include `scout_session_open`, `scout_session_close`, `session_summary`, `weekly_report`, `next_week_open_report`, `scout_strength_fallback`, PA management changes and trade closure.

Historical report events carry the GO/NO-GO that applied during the reported period (v2.0.0): `session_summary.go` = GO if any live GO occurred in that session (`go_cycles`, `first_go_ts`, `last_go_ts`); `weekly_report.go` = GO if any session in the week had a GO (`go_sessions`); `next_week_open_report.go` = the live GO status at Friday close. Each carries `go_basis`. They also carry `report_status` (`COMPLETE` or `CATCH_UP`) and `actionability` (`HISTORICAL_SUMMARY` or `WAIT_FOR_LIVE_GO`). Only `sessions/{id}.final.go` is a live, actionable decision field.

### `telemetry/{YYYY-MM-DD}`

Contains `updated_ts`, process/uptime/cycle/error fields and `last`, including action, session, freshness, spread, confluence and market speed.

## Vue listener guidance

- Treat `final.go` as the compact manual-action flag; still display `final.action` and `final.reason`.
- Treat `scouts.velocity` as unsigned rolling range/minute. Use `velocity_direction` or `displacement` only when a directional display is required.
- Never infer direction from scouts. Display scouts as confirmation evidence only.
- Show scaled one-lot values as a manual translation, not actual account P/L.
- Handle missing optional fields for documents written by v1.4/v1.5.
- Use `onSnapshot` for the current session document and query `events` by descending `ts` with a bounded limit.

## Rules deployment

The included `firestore.rules` allows authenticated client reads and denies client writes. Firebase Admin service-account writes from the bot bypass Firestore Security Rules.

```powershell
firebase deploy --only firestore:rules
```

Review access requirements before production; do not make trading data publicly readable.

`FIRESTORE_SCHEMA.json` is the machine-readable contract checked by `tests/test_v17_hardening.py`.

## 3.1.0 additions

- `sessions/{date_SESSION}.analysis.intermarket` — `{status, symbol, correlation, regime (COUPLED|WEAK|DECOUPLED|INSUFFICIENT|UNAVAILABLE),
  smt (BULLISH|BEARISH|NONE), smt_timeframe, smt_detail {kind, xau[2], xag[2], timestamp}, relative_strength, silver_leading (UP|DOWN|NONE),
  long_points, short_points, reason}`. Evidence only; never a permission.
- `events` gains `smt_divergence` and `intermarket_unavailable`.

## 3.3.0 additions

- `sessions/{date_SESSION}.price.broker_utc_offset_hours` — the broker server's UTC offset in hours (e.g. `3.0`),
  detected at runtime and **already removed** from every timestamp in the document. MT5 reports tick and bar times in
  broker-server time; the bot converts them once, in `mt5_client.BrokerClock`, so `updated_ts` and every analysis
  timestamp are true UTC. `null` when no reading is available yet.
- `sessions/{date_SESSION}.analysis.broker_clock` —
  `{offset_hours, offset_seconds, residual_seconds, raw_delta_seconds, source (auto|manual|none), server, measured_at}`.
  `residual_seconds` is the genuine clock skew left after the offset is removed; it is the only value the order guard
  compares against `safety.max_clock_skew_seconds`.
- `sessions/{date_SESSION}.analysis.router_vetoes` and `.blocked_by` — the router's veto ladder in evaluation order as
  `{veto, active, detail, flips_when}`. `blocked_by` is the filtered, currently-active subset shown as "Blocked by" on
  the status card and by `!why`. Reporting only; the router itself is unchanged.
- `sessions/{date_SESSION}.analysis.atr` — M5 ATR, used to express zone distance on the Detected card.
- `events` gains `broker_clock_offset` (one per detected offset change) and `broker_clock_error`.
