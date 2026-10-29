@echo off
rem DisCortex launcher: uses DisCortex.exe if present, else the dev .venv.
rem Extra arguments are passed through (e.g. start.bat --profile dayz).
cd /d "%~dp0"

if exist "DisCortex.exe" (
    start "" "DisCortex.exe" %*
    exit /b 0
)

if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" -m app.main %*
    exit /b 0
)

echo DisCortex.exe was not found in this folder, and there is no .venv for dev mode.
echo  - On a normal install, DisCortex.exe should sit next to this file.
echo  - To run from source, run setup.bat first.
pause
exit /b 1
