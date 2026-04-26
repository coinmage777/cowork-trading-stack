@echo off
chcp 65001 >nul
title Polymarket Web Dashboard
cd /d "%~dp0"
set "PY_CMD="
where py >nul 2>nul && set "PY_CMD=py"
if not defined PY_CMD (
    where python >nul 2>nul && set "PY_CMD=python"
)
if not defined PY_CMD (
    for %%P in ("%LocalAppData%\Programs\Python\Python310\python.exe" "%LocalAppData%\Python\bin\python.exe" "%LocalAppData%\Python\pythoncore-3.14-64\python.exe") do (
        if exist %%~P set "PY_CMD=%%~P"
    )
)
if not defined PY_CMD (
    echo Python was not found.
    pause
    exit /b 1
)
echo Web dashboard URL will be printed below.
"%PY_CMD%" web_dashboard.py
pause

