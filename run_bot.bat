@echo off
setlocal
cd /d "%~dp0"
echo ============================================
echo   BOSS SMC BOT v2.1 - Starting (single run)
echo ============================================

if not exist "venv\Scripts\python.exe" (
    echo [ERROR] The project virtual environment is missing.
    echo Run install.bat first, then run this file again.
    pause
    exit /b 1
)

venv\Scripts\python.exe -c "import MetaTrader5" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] MetaTrader5 is not installed in the project virtual environment.
    echo Run install.bat first, then run this file again.
    pause
    exit /b 1
)

if not exist ".env" (
    echo [!] No .env file - the bot will run on the fallback values in config.py.
    echo     Copy .env.example to .env and fill in your MT5 account for AI + symbols.
    echo.
)

venv\Scripts\python.exe preflight.py
if errorlevel 1 (
    echo.
    echo [ERROR] Preflight failed - fix every [FIX] line above first.
    pause
    exit /b 1
)

venv\Scripts\python.exe main.py
set RC=%ERRORLEVEL%
echo.
echo Bot stopped, exit code %RC%.   Full log: logs\bot.log
pause
