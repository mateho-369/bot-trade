@echo off
setlocal
cd /d "%~dp0"
title BOSS SMC BOT v2.1 - Non-Stop Runner

echo ============================================
echo   BOSS SMC BOT v2.1 - AUTO-RECOVERY RUNNER
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

if not exist ".env" (
    echo [!] No .env file - the bot will run on the fallback values in config.py
    echo     ^(no MT5 login, AI disabled^). Copy .env.example to .env and fill it in.
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
