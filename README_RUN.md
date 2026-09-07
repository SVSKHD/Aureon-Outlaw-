# Windows setup: run.bat and run_discord_bot.bat

Use this guide for the MT5 scout bot and its Discord reporting. cTrader is not connected in this build.

## Which script does what?

| Script | Purpose | Requirements |
| --- | --- | --- |
| `setup.bat` | One-time virtual environment and dependency installation; creates `.env` only if missing | Internet, Windows, 64-bit Python 3.11 with Python Launcher |
| `run.bat` | Runs the MT5 analysis/scout engine under a restarting supervisor; pushes configured Discord webhook cards | MT5 desktop, logged-in demo account, valid `config.yaml` and `.env` |
| `run_discord_bot.bat` | Separate read-only Discord command bot (`!status`, `!scouts`, etc.) reading the engine's local output | Discord bot token and access to the same folder/data as the engine |

`run.bat` can send automatic cards without `run_discord_bot.bat`. The second script is needed to answer commands, and does not place trades itself.

## 1. Pick ONE working folder

The repository has launchers/configs at the root and inside `scout/`. Use **all three scripts from the same folder**. Do not run one engine at the root and another inside `scout/`; their `.env`, `.venv`, configuration and reports are separate.

The examples below use the repository root. Open PowerShell in that folder. If you already run everything inside `scout/`, use that folder instead and keep its existing settings/data.

## 2. Install the prerequisites

1. Install **64-bit Python 3.11**, including its Python Launcher. Check with `py -3.11 --version`. The project supports Python 3.11+, but the setup script chooses 3.11 when creating a new environment for a consistent installation.
2. Install your broker's **MetaTrader 5 desktop terminal** on Windows. The web/mobile terminal is insufficient for this Python connection.
3. Log into a **demo hedging account** in MT5. Opposing BUY/SELL scouts require hedging. Enable Algo Trading and ensure the terminal permits external Python trading (the “Disable automated trading via external Python API” option must not be checked).
4. Confirm the configured symbol appears in Market Watch. Change `symbol` in `config.yaml` if your broker uses a suffix.
5. Synchronize Windows Date & time. Leave MT5 open and connected when running the engine. No MT5 username/password is required in `.env`; this code uses `mt5.initialize()` to attach.

## 3. Install once

Double-click `setup.bat`, or run:

```powershell
.\setup.bat
```

It creates `.venv` if absent, installs the project with **both MT5 and Discord extras**, checks imports, and copies `.env.example` only when `.env` does not already exist. It preserves your existing `.env` and `config.yaml`. It does not start trading or contact Discord.

Included: numpy, pandas, pydantic, PyYAML, tzdata, MetaTrader5, and discord.py. You do not need to install these individually. The old `requirements.txt` alone does not install the Discord command bot dependency.

For optional Firebase/Firestore reporting:

```powershell
.\setup.bat firebase
```

This additionally installs `firebase-admin`. Without a configured Firebase key, MT5 and Discord can still run; a Firebase-disabled warning is expected.

Manual installation equivalent (if you prefer PowerShell):

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[mt5,discord]"
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

No PowerShell activation script or execution-policy change is needed. Both launchers use the environment's Python executable directly. Re-running setup after a code update is fine; it uses the existing environment.

## 4. Configure Discord in .env

Use plain values without surrounding quotes. Do not upload `.env`, webhook URLs, bot tokens or Firebase key files to GitHub.

```dotenv
MT5_TERMINAL_PATH=
DISCORD_WEBHOOK=YOUR_CHANNEL_WEBHOOK_URL
DISCORD_BOT_TOKEN=YOUR_DISCORD_BOT_TOKEN
DISCORD_COMMAND_CHANNEL_ID=YOUR_CHANNEL_ID
DISCORD_ALLOWED_USER_IDS=YOUR_USER_ID
DISCORD_COMMAND_PREFIX=!
FIREBASE_KEY_PATH=serviceAccountKey.json
```

- **Webhook:** create an incoming webhook for your Discord reporting channel and put its URL in `DISCORD_WEBHOOK`. This sends automatic cards from the engine.
- **Bot token:** create a Discord application/bot, enable **[Message Content Intent](https://docs.discord.com/developers/events/gateway#message-content-intent)**, invite the bot to your server with the `bot` scope and permission to View Channel, Send Messages and Embed Links in the command channel. Put its token in `DISCORD_BOT_TOKEN`. A webhook URL is not a bot token.
- **IDs:** enable Discord Developer Mode to copy channel/user IDs. `DISCORD_ALLOWED_USER_IDS` accepts comma-separated IDs. Set both restrictions for a private command channel; leaving them blank allows broader access to reports.
- **Terminal path:** leave blank for automatic terminal discovery. If multiple MT5 installations exist, set the exact `terminal64.exe` path for the intended demo terminal.
- **Firebase:** only if using Firestore, place the service-account JSON at the configured path in this working folder. This is optional for Discord.

## 5. Review the active config.yaml

Merge these values into the existing sections; **do not replace the whole file** with this excerpt:

```yaml
safety:
  allow_scout_orders: true
  allow_pa_orders: false
  allow_live_account: false
  require_hedging_for_scouts: true
  broker_timestamp_offset_seconds: 0
integrations:
  discord_status_mode: hourly
```

This enables automatic **demo scout pairs**, while leaving directional entries manual. Review `risk.scout_lot` and the emergency scout SL before starting. The default scout lot is 0.01 per leg. Closing the programs does not automatically close open positions; monitor remaining positions in MT5.

## 6. Start both processes

1. Open MT5, connected to the intended demo hedging account.
2. Double-click `run.bat`. It checks setup and starts the supervised engine.
3. Double-click `run_discord_bot.bat` **in the same folder**, in a separate window.
4. In your authorized Discord channel, type `!heartbeat`, then `!status` and `!scouts`.

Expected: either a scout BUY/SELL pair with tickets, or a specific placement blocker. Hourly decision cards are produced on startup and the first successful snapshot of each display-timezone hour; decision/scout-confirmation changes also cause updates. They show READY/WAIT, entry/SL/TP when available, next conditional pattern, and historical fakeout evidence. No history means no fakeout score yet.

Useful commands: `!help`, `!status`, `!plan`, `!scouts`, `!positions`, `!detected`, `!silver`, `!events 10`, `!heartbeat`. Read-only commands never place a manual directional order.

Keep both windows open. The launchers restart exited processes; when intentionally stopping, stop both windows and check Task Manager for remaining engine/supervisor processes. MT5 positions remain at the broker until closed or their broker-side stops/targets execute.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| `py` not recognized / Python 3.11 missing | Install 64-bit Python 3.11 with the launcher, reopen PowerShell, rerun setup. |
| Missing `.venv` / `No module named discord` or `MetaTrader5` | Run `setup.bat` in the same folder as both launchers. |
| Dependency installation fails | Read the pip error; check internet/proxy access and Python architecture. Setup stops without starting the bot. |
| `DISCORD_BOT_TOKEN is not set` | Edit `.env` beside the launcher; webhook and bot token are different settings. |
| Automatic cards work but commands do not | Run the second launcher; check Message Content Intent, channel permissions and allowed IDs. |
| Command bot reports no snapshot | Start the engine first; use the same working folder for both programs. |
| Scouts blocked by netting/live account | Use a demo hedging account; this build refuses live accounts. |
| Scouts blocked by clock skew / future tick | Sync Windows UTC, then run the clock diagnostic below. Do not widen the allowed skew or guess an offset. |
| Firebase unavailable | Install optional Firebase support and supply the key only if you need Firestore; it is not required for Discord. |
| Relaunch loop | Read the first error in the window. Stop the launcher, fix that error, then restart. |

Clock diagnostic from the repository root:

```powershell
.\.venv\Scripts\python.exe scout\tools\diagnose_clock.py --symbol XAUUSD
```

From inside `scout/`, use `tools\diagnose_clock.py` instead. Share the printed tick/system timestamps if the mismatch persists. Leave `broker_timestamp_offset_seconds: 0` for standard UTC feeds. A verified non-standard feed may require a configured offset; a broker chart showing UTC+3 alone is not evidence to set 10800.

For development tests only:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
```
