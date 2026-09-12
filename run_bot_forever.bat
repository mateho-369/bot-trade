@echo off
setlocal
cd /d "%~dp0"
title BOSS SMC BOT - Non-Stop Runner

echo ============================================
echo   BOSS SMC BOT v1.7 — AUTO-RECOVERY RUNNER
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

:start
echo [%date% %time%] Starting bot...
venv\Scripts\python.exe main.py

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [%date% %time%] Bot stopped with error code %ERRORLEVEL%. 
    echo [%date% %time%] Checking logs and restarting in 5 seconds...
    echo.
    timeout /t 5
    goto start
)

echo.
echo [%date% %time%] Bot stopped normally.
pause
