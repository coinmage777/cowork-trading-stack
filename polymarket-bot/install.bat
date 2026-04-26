@echo off
chcp 65001 >nul
title Install Dependencies
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
    echo Install Python for Windows and enable the Python Launcher or add Python to PATH.
    pause
    exit /b 1
)
echo Installing Python dependencies...
"%PY_CMD%" -m pip install -r requirements.txt
echo.
echo Done! You can now run the bot.
pause
