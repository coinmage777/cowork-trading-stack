@echo off
setlocal
cd /d "%~dp0"
echo ============================================
echo   Bithumb Arbitrage Dashboard (Windows)
echo ============================================
echo Backend:  http://localhost:38742
echo Frontend: http://localhost:35173
echo ============================================

REM 로그 디렉토리 생성
if not exist logs mkdir logs

REM Backend (hidden bg, 로그 파일로 저장)
start "DH-backend" /MIN cmd /c "backend\venv\Scripts\uvicorn.exe backend.main:app --host 0.0.0.0 --port 38742 >> logs\backend.log 2>&1"

REM Frontend (hidden bg, 로그 파일로 저장)
start "DH-frontend" /MIN cmd /c "cd frontend && npm run dev -- --port 35173 --strictPort >> ..\logs\frontend.log 2>&1"

echo.
echo Both servers launched (minimized).
echo Close the backend/frontend windows to stop.
endlocal
