@echo off
title Stopping Hanoi Glove Monitor...
echo.
echo  Stopping all running components...
echo.

REM Stop gateway
cd /d "%~dp0gateway"
python scripts/stop.py 2>nul

REM Stop viewer (find Python process on port 8765 and kill it)
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8765 "') do (
    taskkill /PID %%a /F >nul 2>&1
)

echo  All components stopped.
echo.
pause
