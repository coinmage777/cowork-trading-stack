@echo off
chcp 65001 >nul
title Polymarket Bot Watchdog
cd /d "%~dp0"

set "PY_CMD="
where py >nul 2>nul && set "PY_CMD=py"
if not defined PY_CMD (where python >nul 2>nul && set "PY_CMD=python")
if not defined PY_CMD (
    for %%P in ("%LocalAppData%\Programs\Python\Python310\python.exe") do (
        if exist %%~P set "PY_CMD=%%~P"
    )
)
if not defined PY_CMD (
    echo Python not found
    pause
    exit /b 1
)

echo Polymarket Bot Watchdog (5min interval)
echo Bot will auto-restart if it dies.
echo Close this window to stop watchdog.
echo.
"%PY_CMD%" watchdog.py --loop
pause
