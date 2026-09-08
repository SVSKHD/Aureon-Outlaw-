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

Before each trading week, populate `sessions.market_holidays` and `sessions.market_early_closes` from the broker's published XAUUSD schedule. Dates are New York local ISO dates; early closes use `HH:MM`.

## 4. Smoke test

With the terminal logged in, run one cycle:

```powershell
xau-mt5-bot --config config.yaml
pytest
```

Confirm `mt5_validated` shows `is_demo: true`, `algo_trading: true`, the expected `is_hedging` value, prices and spread match MT5, and no `cycle_error` is written. The first cycle after every start or reconnect is a cold-start NO_TRADE cycle. Then run continuously:

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

## Broker server time (v3.3.0)

MetaTrader 5 reports tick and bar times in **its server's** timezone, which is usually not UTC —
UTC+2/UTC+3 is the common broker setting. The bot detects that offset from the terminal itself at
startup, re-measures it at most once an hour and on every reconnect, and converts every tick, bar and
deal time to UTC before anything else looks at them.

* **Nothing to configure.** `safety.broker_utc_offset_hours: null` means auto-detect.
* Pin it only if you have a reason to: `broker_utc_offset_hours: 3` for a UTC+3 server (whole or half
  hours). A pinned value that disagrees with the server shows up as residual skew and blocks orders,
  so a wrong value is never silent. The older `broker_timestamp_offset_seconds` still works and is
  used whenever `broker_utc_offset_hours` is null.
* Detection is deliberately narrow: the difference must land within 120 seconds of a whole or half
  hour. A PC clock that is simply wrong does not look like a timezone, so it is never absorbed —
  it blocks orders until you fix it. `tools\diagnose_clock.py` prints the raw evidence.
* A tick that is still in the future after the offset is refused at startup, and the guard window is
  asymmetric: a small lag is normal, a tick ahead of your clock is not.
* `safety.max_clock_skew_seconds` (default 600) applies to the **residual** skew after the offset —
  i.e. to a genuinely wrong Windows clock. If orders are blocked with
  `broker clock skew … (residual after broker offset …)`, fix the PC clock:
  **Settings → Time & language → Date & time → Set time automatically**, then **Sync now**.
* Check it any time from Discord with `!clock` (system UTC, broker time, detected offset, residual
  skew, guard status), or in `data/heartbeat.json` (`broker_utc_offset_hours`).

## Reading a NO-GO (v3.3.0, one card in v3.4.0)

* `!why` (or `!decide`) prints every router gate for the latest cycle — PASS/FAIL, the value, the
  threshold — followed by what would flip each failed gate and the strongest evidence per side.
* The same trace appears in the console under `DECISION TRACE`, and on the `!status` card as
  **Verdict**, **Blocked by** and **Next**.
* GO means the cycle's demo setup passed every gate. It is a signal and a session GO tally, never a
  standing instruction to place an order.

## The decision card (v3.4.0)

`!status` (and the webhook, on every change or hourly) answers "go or not" in one screen:

1. the headline — 🟢 placed / 🟠 waiting / 🔴 no trade / ⚪ session closed, with the bias, the score,
   the session and the time;
2. a one-sentence verdict with the numbers in it;
3. the 14-gate checklist — ✅ passed, ❌ the one gate that blocked it, — for the gates the router
   never reached;
4. "what flips it": up to three concrete conditions;
5. evidence for and against the bias;
6. price, zone (with its distance in ATR), SL and TP1;
7. what GO means, and which vetoes still stand behind the blocker.

`!why` prints the same gate table as text, `!detected` is the compact market companion and
`!detected full` shows everything the engine sees.
