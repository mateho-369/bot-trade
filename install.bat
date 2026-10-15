@echo off
setlocal
cd /d "%~dp0"
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
    echo Use Python 3.10 - 3.12 ^(64-bit^): MetaTrader5 and numpy wheels for
    echo newer versions may not exist, which breaks this install step.
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
echo.

REM --- 3. Upgrade pip ---
echo Upgrading pip...
venv\Scripts\python.exe -m pip install --upgrade pip --quiet

REM --- 4. Install everything from requirements.txt (single source of truth) ---
echo Installing packages from requirements.txt ...
venv\Scripts\python.exe -m pip install -r requirements.txt
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
venv\Scripts\python.exe -c "import MetaTrader5, pandas, numpy, sklearn, telegram, dotenv, matplotlib; print('[OK] All core packages import correctly')"
IF %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Some packages did not import. See the message above.
    pause
    exit /b 1
)
echo.

REM --- 6. Secrets file: keep it OUTSIDE the repo so git can never see it ---
REM     The bot reads, in order: %BOT_ENV_FILE%, %USERPROFILE%\.boss-bot\.env,
REM     then .\.env (legacy, git-ignored).
IF EXIST "%USERPROFILE%\.boss-bot\.env" (
    echo [OK] Secrets file found: %USERPROFILE%\.boss-bot\.env
) ELSE (
    IF EXIST ".env" (
        echo [OK] .env found in the repo folder ^(git-ignored^).
        echo [!] Safer: move it to %USERPROFILE%\.boss-bot\.env
    ) ELSE (
        IF EXIST ".env.example" (
            mkdir "%USERPROFILE%\.boss-bot" >nul 2>&1
            copy ".env.example" "%USERPROFILE%\.boss-bot\.env" >nul
            echo [!] Created %USERPROFILE%\.boss-bot\.env from the template - EDIT IT
            echo     with your MT5 login, password, server, Telegram token, AI key.
            echo     It lives outside the repo, so it cannot be committed by accident.
        ) ELSE (
            echo [!] No .env found. Create %USERPROFILE%\.boss-bot\.env
        )
    )
)
echo.

REM --- 7. Turn on the commit-time secret scanner (blocks a commit with a key) ---
git rev-parse --git-dir >nul 2>&1
IF %ERRORLEVEL% EQU 0 (
    git config core.hooksPath .githooks
    echo [OK] Secret scanner enabled for git commits ^(.githooks/pre-commit^).
)
echo.

echo ============================================
echo   Done! Installation complete.
echo ============================================
echo.
echo Next steps:
echo   1. Edit .env with your MT5 (and Telegram / AI key) details.
echo   2. Check you are ready:   venv\Scripts\python.exe preflight.py
echo   3. Start the bot with:    run_bot_forever.bat   (auto-restarts on crash)
echo      Open the MetaTrader 5 terminal first and enable
echo      Tools - Options - Expert Advisors - Allow algorithmic trading.
echo.
pause
