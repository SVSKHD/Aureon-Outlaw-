@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto install
py -3.11 -c "import struct; assert struct.calcsize('P') * 8 == 64" >nul 2>&1
if errorlevel 1 (
  echo Install 64-bit Python 3.11 with the Python Launcher, then run setup.bat again.
  goto fail
)
py -3.11 -m venv .venv
if errorlevel 1 goto fail
:install
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install -e ".[mt5,discord]"
if errorlevel 1 goto fail
if /I not "%~1"=="firebase" goto check
".venv\Scripts\python.exe" -m pip install "firebase-admin>=6.5"
if errorlevel 1 goto fail
:check
".venv\Scripts\python.exe" -c "import MetaTrader5, discord, xau_mt5_bot, pandas, yaml, pydantic"
if errorlevel 1 goto fail
if not exist ".env" copy /y ".env.example" ".env" >nul
if errorlevel 1 goto fail
echo Setup complete. Edit .env, check config.yaml, open MT5 demo, then run run.bat and run_discord_bot.bat.
echo Existing .env and config.yaml were preserved. No trades or Discord messages were sent by setup.
pause
exit /b 0
:fail
echo Setup failed. Read the error above and README_RUN.md. Do not start the launchers yet.
pause
exit /b 1
