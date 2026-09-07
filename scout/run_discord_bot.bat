@echo off
cd /d %~dp0
call .venv\Scripts\activate.bat
:loop
py discord_bot.py
echo [discord_bot] exited, restart in 15s
timeout /t 15 /nobreak >nul
goto loop
