# Windows MT5 demo deployment

## 1. Prepare MetaTrader 5

1. Install the broker's 64-bit MetaTrader 5 desktop terminal.
2. Log in to a **demo hedging account** in the terminal itself and leave the terminal running. The bot attaches to this
   already-logged-in terminal with `mt5.initialize()`; it never logs in, never asks for credentials and never changes the account.
3. Make sure `XAUUSD` is in Market Watch (the bot can select it, but the symbol name must be exactly `XAUUSD`).
4. Enable Algo Trading (AutoTrading button) and confirm the terminal is receiving live ticks.

## 2. Install Python and the bot

Open PowerShell in the extracted project directory:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[mt5,dev]"
Copy-Item .env.example .env
```

If PowerShell blocks activation, use `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` after reviewing your organisation's policy.

## 3. Configure integrations (no MT5 credentials)

Edit `.env` — all values optional except when several terminals are installed:

```dotenv
MT5_TERMINAL_PATH=C:\Program Files\Broker MT5\terminal64.exe
DISCORD_WEBHOOK=
FIREBASE_KEY_PATH=serviceAccountKey.json
```

There is no MT5 login, password or server setting anywhere in the bot. At start it validates: terminal connected, account is
DEMO, trading permitted, Algo Trading on, hedging (informational — scouts are disabled on netting accounts), XAUUSD present and
selectable, fresh tick, valid volume constraints. Any failure is written as `startup_failed` with `mt5.last_error()` and the
trading loop is not started. Keep `serviceAccountKey.json` private. Confirm in `config.yaml`:

- `allow_live_account: false`
- `require_hedging_for_scouts: true`
- `daily_max_loss_percent: 2.0`
- `max_consecutive_losses: 3`
- `emergency_scout_sl_price: 20.0`

### Broker server time is not UTC (v3.3.0)

MetaTrader 5 reports tick and bar timestamps as epoch seconds of the **broker server's own wall clock**. Most brokers
run their servers on UTC+2 / UTC+3 (some on UTC+1, UTC+5:30 and so on), so those numbers are *not* UTC.

The bot detects that offset automatically at startup and re-checks it at most once an hour (and immediately after any
reconnect): it compares the broker tick time with system UTC and rounds the difference to the nearest 30 minutes. That
rounded value is the broker's timezone; whatever is left over is treated as genuine clock skew.

- Every tick time, bar time, deal time and position open time is converted to true UTC at that one point, so sessions,
  freshness, sweep ages and every timestamp on a card are correct.
- Orders are blocked only when the **residual** skew exceeds `safety.max_clock_skew_seconds` (600 s). If you see
  `Scout orders blocked: broker clock skew …`, the broker timezone has *already* been removed — the PC clock is wrong.
  Fix it on Windows: Settings → Time & language → Date & time → *Set time automatically* on, then
  `w32tm /resync` in an elevated PowerShell. Task Scheduler and NSSM both inherit the corrected system clock.
- **You do not normally configure anything.** `safety.broker_utc_offset_hours` defaults to `null` (auto-detect). Set a
  float only to pin a known server:

  ```yaml
  safety:
    broker_utc_offset_hours: 3.0        # null = auto-detect (default)
    broker_offset_remeasure_seconds: 3600
    max_clock_skew_seconds: 600         # genuine skew AFTER the offset is removed
  ```

  A pinned value always wins over auto-detection and is never replaced, so a wrong pin shows up as skew and blocks
  orders rather than quietly shifting your timestamps.
- Confirm what was detected with the `!clock` command, the `broker_clock_offset` entry in `!events`, the
  `mt5_validated` line, or `broker_utc_offset_hours` in `data\heartbeat.json`.

Before each trading week, populate `sessions.market_holidays` and `sessions.market_early_closes` from the broker's published XAUUSD schedule. Dates are New York local ISO dates; early closes use `HH:MM`.

## 4. Smoke test

With the terminal logged in, run one cycle:

```powershell
xau-mt5-bot --config config.yaml
pytest
```

Confirm `mt5_validated` shows `is_demo: true`, `algo_trading: true`, a `broker_utc_offset_hours` that matches your broker's server time, the expected `is_hedging` value, prices and spread match MT5, and no `cycle_error` is written. The first cycle after every start or reconnect is a cold-start NO_TRADE cycle. Then run continuously:

```powershell
xau-mt5-bot --config config.yaml --loop
```

Observe one full Asia → London → New York lifecycle. Confirm equal scout volumes, exact-ticket closes, partial TP volumes, SL changes, Discord messages and Firestore documents.

Do not treat a live `GO` as funded-ready until the output also reports calibrated pace, at least 20 confirmed PA outcomes, and `funded_risk_ok: true`. Scouts are evidence-only and must not be mirrored into the manual account.

## 5. Automatic restart with Task Scheduler

`run.bat` starts `supervisor.py`, which restarts the bot with backoff and kills a child whose heartbeat becomes stale.

1. Open Task Scheduler → Create Task.
2. Trigger: At log on or At startup.
3. Action: Start a program → select the absolute path to `run.bat`.
4. Start in: the extracted project directory.
5. Enable restart on failure and run whether the user is logged on or not if permitted.

## 6. Alternative: NSSM service

After installing NSSM from its official distribution, run an elevated terminal:

```powershell
nssm install XAUUSD-Dacoit "C:\path\to\project\.venv\Scripts\python.exe" "C:\path\to\project\supervisor.py"
nssm set XAUUSD-Dacoit AppDirectory "C:\path\to\project"
nssm start XAUUSD-Dacoit
```

Use one startup method—Task Scheduler or NSSM—not both.

## 7. Logs and recovery files

- SQLite audit: `data\logs\trading.sqlite3`
- Daily JSONL analysis: `data\logs\analysis_YYYYMMDD.jsonl`
- Supervisor heartbeat: `data\heartbeat.json`
- Account-specific PA state: `data\positions_<account>_<server>_<symbol>.json`
- Account-specific scout state: `data\scouts_<account>_<server>_<symbol>.json`

Back up the `data` directory before moving installations. Never delete pending-finalization state while trades are unresolved.

The supervisor now anchors its child process, heartbeat, config and `PYTHONPATH` to the script directory, even when Task Scheduler or NSSM starts it from another working directory. NSSM `AppDirectory` should still be set for operator clarity.

Complete every item in `FORWARD_TEST_CHECKLIST.md` before relying on the system for manual funded decisions.

## Discord command bot (v3.1.0, commands extended in v3.3.0)

1. Discord Developer Portal → New Application → Bot → Reset Token → paste into `.env` as `DISCORD_BOT_TOKEN`.
2. Bot → Privileged Gateway Intents → enable **Message Content Intent**.
3. OAuth2 → URL Generator → scope `bot`, permissions `Send Messages`, `Read Message History` → invite to your server.
4. Optional: right-click the channel → Copy Channel ID → `.env` `DISCORD_COMMAND_CHANNEL_ID=`.
5. `pip install -e ".[discord]"` in the same venv, then run `run_discord_bot.bat` (second Task Scheduler entry, At log on).
6. Type `!help` in the channel. The bot only reads files; if `!status` says the snapshot is old, check `!heartbeat`.

Commands (all read-only — there are deliberately no order commands):

| Command | Answers |
| --- | --- |
| `!status` | GO / NO-GO card, including the **Blocked by** list of active router vetoes |
| `!why` | every veto currently blocking a trade **and what would flip each one** (v3.3.0) |
| `!clock` | system UTC, broker server time, detected offset, residual skew, guard status (v3.3.0) |
| `!detected` | patterns, structure events, sweeps (ROUND_1 collapsed) and zones with ATR distance |
| `!plan` `!silver` `!scouts` `!positions` `!day` | current plan, XAU/XAG evidence, scout pair, open trades, daily P/L and locks |
| `!trades [n]` `!go` `!events [n]` `!reports [n]` `!heartbeat` | history, session GO tallies, audit events, reports, process health |

`!why` is the fastest way to answer "why is it not trading?" — it names the veto (spread, confluence, zone, trigger,
SLOW, scouts, RR, target, session feasibility, clock, day lock) with the numbers behind it. `!clock` is the fastest way
to confirm the broker offset was detected correctly.
