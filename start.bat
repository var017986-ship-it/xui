@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment not found.
    echo Run setup.bat first.
    pause
    exit /b 1
)

if not exist ".env" (
    echo .env not found.
    echo Run setup.bat first, then add BOT_TOKEN to .env.
    pause
    exit /b 1
)

findstr /R /C:"^BOT_TOKEN=$" /C:"^BOT_TOKEN=123456789:replace_this_with_token_from_botfather$" /C:"^BOT_TOKEN=ваш_токен_бота$" ".env" >nul
if not errorlevel 1 (
    echo BOT_TOKEN is not configured in .env.
    echo Open .env and paste the token from @BotFather.
    pause
    exit /b 1
)

echo Starting Telegram bot...
echo Keep this window open while the bot is running.
echo Press Ctrl+C to stop it.
echo.
".venv\Scripts\python.exe" main.py

if errorlevel 1 (
    echo.
    echo Bot stopped with an error.
    pause
)
