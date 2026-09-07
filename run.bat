@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" goto missing
".venv\Scripts\python.exe" -c "import MetaTrader5, xau_mt5_bot" >nul 2>&1
if errorlevel 1 goto missing
if not exist ".env" goto missing
".venv\Scripts\python.exe" supervisor.py
exit /b %errorlevel%
:missing
echo Setup is incomplete. Run setup.bat in this folder, edit .env, then try again.
echo See README_RUN.md for the installation and run order.
pause
exit /b 1
