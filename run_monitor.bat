@echo off
setlocal
cd /d "%~dp0"

python -c "import psutil" 2>nul
if errorlevel 1 (
    echo Installing psutil...
    python -m pip install psutil
)

python hw_monitor.py %*
if errorlevel 1 pause
endlocal
