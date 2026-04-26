#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_PORT=38742
FRONTEND_PORT=35173
BACKEND_HEALTH_URL="http://localhost:$BACKEND_PORT/api/health"
FRONTEND_URL="http://localhost:$FRONTEND_PORT"
STARTUP_WAIT_SECONDS=90

kill_listening_port() {
    local port="$1"
    local pids=""

    if command -v lsof >/dev/null 2>&1; then
        pids="$(lsof -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
    elif command -v fuser >/dev/null 2>&1; then
        pids="$(fuser "$port"/tcp 2>/dev/null || true)"
    fi

    if [ -n "$pids" ]; then
        kill $pids 2>/dev/null || true
        sleep 1
    fi
}

echo "============================================"
echo "  Bithumb Arbitrage Dashboard"
echo "============================================"
echo ""
echo "  Backend:  http://localhost:$BACKEND_PORT"
echo "  Frontend: http://localhost:$FRONTEND_PORT"
echo ""
echo "============================================"
echo ""

echo "[0/2] Cleaning stale processes on fixed ports..."
kill_listening_port "$BACKEND_PORT"
kill_listening_port "$FRONTEND_PORT"

echo "[1/2] Starting backend server..."
cd "$SCRIPT_DIR"
backend/venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port "$BACKEND_PORT" &
BACKEND_PID=$!

echo "[2/2] Starting frontend server..."
cd "$SCRIPT_DIR/frontend"
npm run dev -- --strictPort &
FRONTEND_PID=$!

trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; exit" INT TERM

echo ""
echo "Waiting for servers to start..."
backend_ready=0
frontend_ready=0
for i in $(seq 1 "$STARTUP_WAIT_SECONDS"); do
    if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
        echo "Backend failed to start. Check logs above."
        kill "$FRONTEND_PID" 2>/dev/null
        exit 1
    fi

    if ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
        echo "Frontend failed to start. Check logs above."
        kill "$BACKEND_PID" 2>/dev/null
        exit 1
    fi

    if [ "$backend_ready" -eq 0 ] && curl -sf "$BACKEND_HEALTH_URL" > /dev/null 2>&1; then
        backend_ready=1
    fi

    if [ "$frontend_ready" -eq 0 ] && curl -sf "$FRONTEND_URL" > /dev/null 2>&1; then
        frontend_ready=1
    fi

    if [ "$backend_ready" -eq 1 ] && [ "$frontend_ready" -eq 1 ]; then
        echo "Opening browser..."
        cmd.exe /c start "" "$FRONTEND_URL" 2>/dev/null
        break
    fi
    sleep 1
done

if [ "$backend_ready" -ne 1 ]; then
    echo "Backend health check failed. Check logs above."
    kill "$FRONTEND_PID" 2>/dev/null
    exit 1
fi

if [ "$frontend_ready" -ne 1 ]; then
    echo "Frontend failed to become ready. Check logs above."
    kill "$BACKEND_PID" 2>/dev/null
    exit 1
fi

echo ""
echo "Servers started. Press Ctrl+C to stop all servers."
echo ""

wait
