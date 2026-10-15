@echo off
setlocal
cd /d "%~dp0"
echo ============================================
echo   BOSS SMC BOT v2.3 - Starting (single run)
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

REM The secrets file lives OUTSIDE the repo since v2.2:
REM   %BOT_ENV_FILE%  or  %USERPROFILE%\.boss-bot\.env  (legacy: .\.env)
set "SECRETS_OK="
if exist ".env" set "SECRETS_OK=1"
if exist "%USERPROFILE%\.boss-bot\.env" set "SECRETS_OK=1"
if defined BOT_ENV_FILE set "SECRETS_OK=1"
if not defined SECRETS_OK (
    echo [!] No secrets file found - the bot would run on the fallback values in
    echo     config.py ^(no MT5 login, AI disabled^). Create
    echo     %USERPROFILE%\.boss-bot\.env  from .env.example and fill it in.
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
