@echo off
rem RigWatch launcher: sets up its private Python environment on first run, then starts the app.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
  echo Setting up RigWatch for the first time. This takes about a minute...
  where py >nul 2>nul
  if errorlevel 1 ( python -m venv .venv ) else ( py -3 -m venv .venv )
  if errorlevel 1 (
    echo.
    echo Python 3.11 or newer is required. Install it from https://www.python.org/downloads/ and run this again.
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
  if errorlevel 1 (
    echo.
    echo Could not install the required packages. Check your internet connection and run this again.
    rmdir /s /q .venv
    pause
    exit /b 1
  )
)

rem CPU temperature needs the sensor engine (about 3 MB, checked against pinned hashes).
rem It is fetched once; RigWatch still starts without it and says the reading is unavailable.
if not exist "rigwatch\vendor\lhm\LibreHardwareMonitorLib.dll" (
  echo Fetching the sensor engine for CPU temperature...
  ".venv\Scripts\python.exe" tools\fetch-sensor-engine.py >nul 2>nul
)

start "" ".venv\Scripts\pythonw.exe" -m rigwatch %*
