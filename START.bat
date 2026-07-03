@echo off
title DentalMotion Monitor - Starting...
echo.
echo  ==========================================
echo   DentalMotion Monitor - Starting System
echo  ==========================================
echo.
echo  [1/2] Starting Gateway (network bridge)...
start "NHOS Gateway" cmd /k "cd /d "%~dp0gateway" && python scripts/start.py && pause"

timeout /t 4 /nobreak >nul

echo  [2/2] Starting IMU Viewer (the web dashboard)...
start "IMU Viewer" cmd /k "cd /d "%~dp0imu_viewer" && python app.py && pause"

timeout /t 5 /nobreak >nul

echo.
echo  Opening dashboard in your browser...
start http://127.0.0.1:8765

echo.
echo  ==========================================
echo   System is running!
echo   Dashboard: http://127.0.0.1:8765
echo   Close the two black windows to stop.
echo  ==========================================
echo.
pause
