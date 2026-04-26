@echo off
chcp 65001 >nul
cd /d "%~dp0"

:: Polymarket Bot Watchdog
:: Task Scheduler에서 5분마다 실행.
:: 프로세스가 없거나 bot.log가 5분+ 업데이트 안 되면 kill 후 재시작.

set "PY_CMD="
where py >nul 2>nul && set "PY_CMD=py"
if not defined PY_CMD (where python >nul 2>nul && set "PY_CMD=python")
if not defined PY_CMD (
    for %%P in ("%LocalAppData%\Programs\Python\Python310\python.exe") do (
        if exist %%~P set "PY_CMD=%%~P"
    )
)
if not defined PY_CMD exit /b 1

:: 헬스체크 + 재시작을 PowerShell에 위임 (bot.log 최근 업데이트 확인 포함)
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ErrorActionPreference='SilentlyContinue';" ^
    "$botDir='%~dp0'.TrimEnd('\');" ^
    "$logFile=Join-Path $botDir 'bot.log';" ^
    "$wdLog=Join-Path $botDir 'watchdog.log';" ^
    "$now=Get-Date;" ^
    "$procs=Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'main\.py.*--mode live' };" ^
    "$needRestart=$false;" ^
    "$reason='';" ^
    "if (-not $procs) { $needRestart=$true; $reason='process not running' }" ^
    "elseif (Test-Path $logFile) {" ^
    "  $lastWrite=(Get-Item $logFile).LastWriteTime;" ^
    "  $stale=($now - $lastWrite).TotalMinutes;" ^
    "  if ($stale -gt 5) { $needRestart=$true; $reason=('log stale '+[math]::Round($stale,1)+'min') }" ^
    "}" ^
    "if ($needRestart) {" ^
    "  if ($procs) { foreach ($p in $procs) { try { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue } catch {} } }" ^
    "  Start-Sleep -Seconds 3;" ^
    "  Add-Content -Path $wdLog -Value (\"[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Restarting (\"+$reason+\")\");" ^
    "  Start-Process -FilePath '%PY_CMD%' -ArgumentList 'main.py','--mode','live' -WorkingDirectory $botDir -WindowStyle Hidden;" ^
    "  Add-Content -Path $wdLog -Value (\"[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Bot started\");" ^
    "}"
