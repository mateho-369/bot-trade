@echo off
setlocal
echo ============================================
echo   BOSS SMC BOT - Installer
echo ============================================
echo.

REM --- 1. Check Python ---
python --version >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python not found!
    echo Download from: https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)
for /f "delims=" %%v in ('python --version') do echo [OK] %%v
echo.

REM --- 2. Create virtual environment (skip if it already exists) ---
IF EXIST "venv\Scripts\activate.bat" (
    echo [OK] Virtual environment already exists.
) ELSE (
    echo Creating virtual environment...
    python -m venv venv
    IF %ERRORLEVEL% NEQ 0 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
)
call venv\Scripts\activate.bat
echo.

REM --- 3. Upgrade pip ---
echo Upgrading pip...
python -m pip install --upgrade pip --quiet

REM --- 4. Install everything from requirements.txt (single source of truth) ---
echo Installing packages from requirements.txt ...
pip install -r requirements.txt
IF %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Package installation failed. Check your internet connection
    echo and that requirements.txt is present, then run install.bat again.
    pause
    exit /b 1
)
echo [OK] Packages installed.
echo.

REM --- 5. Verify the key imports actually load ---
echo Verifying installation...
python -c "import MetaTrader5, pandas, numpy, sklearn, telegram, dotenv, matplotlib; print('[OK] All core packages import correctly')"
IF %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Some packages did not import. See the message above.
    pause
    exit /b 1
)
echo.

REM --- 6. Make sure a .env exists (secrets file) ---
IF EXIST ".env" (
    echo [OK] .env found.
) ELSE (
    IF EXIST ".env.example" (
        copy ".env.example" ".env" >nul
        echo [!] Created .env from template - EDIT IT with your real MT5 login,
        echo     password, server and Telegram token before running the bot.
    ) ELSE (
        echo [!] No .env found. The bot will use the fallback values in config.py.
    )
)
echo.

echo ============================================
echo   Done! Installation complete.
echo ============================================
echo.
echo Next steps:
echo   1. Edit .env with your MT5 + Telegram details (if not done).
echo   2. Start the bot with:  run_bot_forever.bat   (auto-restarts on crash)
echo.
pause
