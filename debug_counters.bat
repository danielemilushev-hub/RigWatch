@echo off
setlocal
cd /d "%~dp0"
echo Dumping raw GPU performance-counter instances. Close the window to stop.
python hw_monitor.py --debug
pause
endlocal
