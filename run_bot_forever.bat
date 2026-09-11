@echo off
setlocal
title BOSS SMC BOT - Non-Stop Runner

echo ============================================
echo   BOSS SMC BOT v1.7 — AUTO-RECOVERY RUNNER
echo ============================================
echo.

:start
echo [%date% %time%] Starting bot...
call venv\Scripts\activate
python main.py

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
