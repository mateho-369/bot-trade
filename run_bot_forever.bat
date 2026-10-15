@echo off
setlocal
cd /d "%~dp0"
title BOSS SMC BOT v2.3 - Non-Stop Runner

echo ============================================
echo   BOSS SMC BOT v2.3 - AUTO-RECOVERY RUNNER
echo ============================================
echo.

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

REM --- Preflight: do not start a bot that is not ready --------------------
REM Checks packages, .env placeholders, broker settings, risk numbers and the
REM AI wiring. It trades nothing. See preflight.py if you want to read it.
venv\Scripts\python.exe preflight.py
if errorlevel 1 (
    echo.
    echo [ERROR] Preflight failed - fix every [FIX] line above, then run this again.
    echo         Missing packages: run install.bat.  Secrets/AI: edit .env.
    pause
    exit /b 1
)
echo.
echo [OK] Preflight passed - starting the bot. Press Ctrl+C to stop.
echo.

:start
echo [%date% %time%] Starting bot...
venv\Scripts\python.exe main.py
set EXITCODE=%ERRORLEVEL%

echo.
echo [%date% %time%] Bot exited with code %EXITCODE%. Last lines of logs\bot.log:
echo ----------------------------------------------------------------------
if exist "logs\bot.log" (
    powershell -NoProfile -Command "Get-Content -Tail 12 -Path 'logs\bot.log'" 2>nul
)
echo ----------------------------------------------------------------------

if "%EXITCODE%"=="0" (
    echo [%date% %time%] Clean exit. Restarting in 5 seconds... ^(Ctrl+C to stop^)
    timeout /t 5 /nobreak >nul
    goto start
)

echo [%date% %time%] Error exit - restarting in 20 seconds to avoid a fast crash loop.
echo [%date% %time%] Common causes: MT5 terminal closed, "Allow algorithmic trading"
echo [%date% %time%] unchecked, broker disconnected, or SYMBOLS suffix mismatch.
timeout /t 20 /nobreak >nul
goto start
