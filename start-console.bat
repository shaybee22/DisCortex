@echo off
rem DisCortex troubleshooting launcher: same as start.bat, but this window shows the log live.
rem Close this window (or press Ctrl+C) to stop watching; DisCortex itself keeps running until you Quit it.
cd /d "%~dp0"
title DisCortex log

if exist "DisCortex.exe" (
    if not exist "logs" mkdir "logs"
    if not exist "logs\bot.log" type nul > "logs\bot.log"
    start "" "DisCortex.exe" %*
    echo DisCortex.exe started. Live log from logs\bot.log:
    echo.
    powershell -NoProfile -ExecutionPolicy Bypass -Command "[Console]::OutputEncoding = [Text.Encoding]::UTF8; Get-Content -LiteralPath 'logs\bot.log' -Tail 0 -Wait -Encoding UTF8"
    exit /b 0
)

if exist ".venv\Scripts\python.exe" (
    rem dev mode: python.exe (not pythonw) keeps this console, so the log prints here
    ".venv\Scripts\python.exe" -m app.main %*
    echo.
    echo DisCortex exited with code %errorlevel%.
    pause
    exit /b 0
)

echo DisCortex.exe was not found in this folder, and there is no .venv for dev mode.
echo Run setup.bat first to run from source.
pause
exit /b 1
