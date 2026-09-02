@echo off
setlocal
cd /d "%~dp0"

echo Creating virtual environment...
py -m venv .venv
if errorlevel 1 (
    echo Failed to create .venv. Make sure Python 3.11+ is installed.
    pause
    exit /b 1
)

echo Installing dependencies...
call ".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 (
    echo Failed to upgrade pip.
    pause
    exit /b 1
)
call ".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo Failed to install dependencies.
    pause
    exit /b 1
)

if not exist ".env" (
    copy /Y ".env.example" ".env" >nul
    echo Created .env from .env.example.
    echo Add your Telegram BOT_TOKEN to .env before starting the bot.
) else (
    echo .env already exists. It was not changed.
)

echo.
echo Setup complete.
pause
