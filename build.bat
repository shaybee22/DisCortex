@echo off
rem Builds DisCortex.exe and a clean, ready-to-copy portable folder in dist\DisCortex\.
rem Never copies data\memory.db, logs or the bot token (the token lives in Credential Manager, not in files).
setlocal
cd /d "%~dp0"
set "ROOT=%~dp0"
set "PY=.venv\Scripts\python.exe"
set "OUT=dist\DisCortex"

if not exist "%PY%" (
    echo No .venv found. Run setup.bat first.
    pause
    exit /b 1
)

if exist "%OUT%\data\memory.db" (
    echo %OUT% contains data\memory.db, so that copy has been used and holds memory.
    echo build.bat never deletes memory: move or rename "%OUT%" first, then build again.
    pause
    exit /b 1
)

echo [1/5] Installing build tools...
"%PY%" -m pip install --disable-pip-version-check -q -r requirements.txt -r requirements-build.txt
if errorlevel 1 goto :fail

echo [2/5] Generating assets\discortex.ico...
"%PY%" -m app.branding
if errorlevel 1 if not exist "assets\discortex.ico" goto :fail

echo [3/5] Building DisCortex.exe (takes a minute or two)...
"%PY%" -m PyInstaller --noconfirm --clean --log-level WARN ^
    --onefile --windowed --name DisCortex ^
    --icon "%ROOT%assets\discortex.ico" ^
    --add-data "%ROOT%assets\discortex.png;assets" ^
    --add-data "%ROOT%assets\discortex.ico;assets" ^
    --paths "%ROOT%." ^
    --hidden-import keyring.backends.Windows ^
    --hidden-import pystray._win32 ^
    --hidden-import pynput.keyboard._win32 ^
    --hidden-import pynput.mouse._win32 ^
    --copy-metadata keyring ^
    --distpath build\exe --workpath build\work --specpath build ^
    app\main.py
if errorlevel 1 goto :fail

echo [4/5] Assembling %OUT%...
if exist "%OUT%" rmdir /s /q "%OUT%"
mkdir "%OUT%\assets" "%OUT%\bot_profiles" "%OUT%\data" "%OUT%\logs"
copy /y "build\exe\DisCortex.exe" "%OUT%\" >nul || goto :fail
for %%F in (start.bat start-console.bat README.md) do copy /y "%%F" "%OUT%\" >nul || goto :fail
copy /y "assets\discortex.png" "%OUT%\assets\" >nul
copy /y "assets\discortex.ico" "%OUT%\assets\" >nul
rem ship only the examples; the app copies them to config.yaml / <name>.yaml on first run
if exist "config.example.yaml" copy /y "config.example.yaml" "%OUT%\" >nul
if exist "bot_profiles\*.example.yaml" copy /y "bot_profiles\*.example.yaml" "%OUT%\bot_profiles\" >nul

echo [5/5] Checking...
"%PY%" -c "import glob, yaml; bad = [f for f in glob.glob(r'%OUT%\bot_profiles\*.yaml') if (yaml.safe_load(open(f, encoding='utf-8')) or {}).get('ollama_api_key')]; print('NOTE: these profiles contain an Ollama API key in plain text:', ', '.join(bad)) if bad else None"
dir /a /b "%OUT%\data" 2>nul | findstr "." >nul && echo WARNING: %OUT%\data is not empty.
for %%A in ("%OUT%\DisCortex.exe") do echo Built %%~fA (%%~zA bytes)
echo.
echo Done. Copy the whole "%OUT%" folder anywhere (USB stick, another PC) and run start.bat.
echo It contains no memory and no token: the first run on a PC asks for the bot token once.
endlocal
exit /b 0

:fail
echo.
echo BUILD FAILED (see the messages above).
endlocal
exit /b 1
