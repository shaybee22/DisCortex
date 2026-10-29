@echo off
rem Run-from-source setup: creates .venv and installs requirements.txt. Safe to rerun.
setlocal
cd /d "%~dp0"

rem --- find Python 3.11+ on PATH (python, then the py launcher) ---
set "PY="
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && set "PY=python"
if not defined PY (
    py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
    echo.
    echo ERROR: Python 3.11 or newer was not found on PATH.
    echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH",
    echo then run setup.bat again.
    echo.
    pause
    exit /b 1
)
for /f "delims=" %%V in ('%PY% -c "import sys; print(sys.version.split()[0])"') do set "PYVER=%%V"
echo Using Python %PYVER% ^(%PY%^)

rem --- a .venv copied from another PC points at that PC's Python; rebuild it ---
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
    if errorlevel 1 (
        echo Existing .venv is broken or too old ^(copied from another PC?^). Rebuilding it...
        rmdir /s /q ".venv"
    )
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo ERROR: could not create .venv
        pause
        exit /b 1
    )
)

echo Installing requirements ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo.
    echo ERROR: pip install failed. Check your internet connection and the messages above.
    pause
    exit /b 1
)

echo.
echo Setup complete. Run the bot with:
echo     .venv\Scripts\python.exe -m app.main
echo.
pause
exit /b 0
