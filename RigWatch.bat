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

start "" ".venv\Scripts\pythonw.exe" -m rigwatch %*
