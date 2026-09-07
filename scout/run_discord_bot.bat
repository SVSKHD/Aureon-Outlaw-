@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" goto missing
".venv\Scripts\python.exe" -c "import discord, xau_mt5_bot" >nul 2>&1
if errorlevel 1 goto missing
if not exist ".env" goto missing
:loop
".venv\Scripts\python.exe" discord_bot.py
echo [discord_bot] exited, restart in 15s. Close this window to stop.
timeout /t 15 /nobreak >nul
goto loop
exit /b %errorlevel%
:missing
echo Setup is incomplete. Run setup.bat in this folder, edit .env, then try again.
echo See README_RUN.md for the installation and run order.
pause
exit /b 1
