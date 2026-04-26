"""Polymarket Bot Watchdog — 봇이 죽으면 자동 재시작.

단독 실행:
    python watchdog.py              # 1회 체크 후 종료
    python watchdog.py --loop       # 5분 간격 무한 루프

Task Scheduler 등록:
    python watchdog.py 를 5분마다 실행하거나,
    python watchdog.py --loop 을 시작 시 1회 실행.
"""

import subprocess
import sys
import time
import os
from datetime import datetime
from pathlib import Path

BOT_SCRIPT = "main.py"
BOT_ARGS = ["--mode", "live"]
CHECK_INTERVAL = 300  # 5분
LOG_FILE = Path(__file__).parent / "watchdog.log"
LOCK_FILE = Path(__file__).parent / ".bot.pid"


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", errors="replace").decode())
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def find_python() -> str:
    """Find Python executable."""
    for cmd in ["py", "python", "python3"]:
        try:
            r = subprocess.run([cmd, "--version"], capture_output=True, timeout=5)
            if r.returncode == 0:
                return cmd
        except Exception:
            continue
    # Fallback to known path
    fallback = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python", "Python310", "python.exe")
    if os.path.exists(fallback):
        return fallback
    return ""


def is_bot_running() -> bool:
    """Check if main.py --mode live is running via tasklist."""
    try:
        result = subprocess.run(
            ["powershell", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -like '*main.py*--mode*live*' } | "
             "Select-Object ProcessId"],
            capture_output=True, text=True, timeout=10
        )
        # If output contains a ProcessId number, bot is running
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if line.isdigit():
                return True
    except Exception as e:
        log(f"Check failed: {e}")
    return False


def start_bot(py_cmd: str):
    """Start the bot in a new detached process."""
    bot_dir = Path(__file__).parent
    bot_path = bot_dir / BOT_SCRIPT
    log_path = bot_dir / "bot.log"

    # Use CREATE_NEW_PROCESS_GROUP + DETACHED_PROCESS for Windows
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    DETACHED_PROCESS = 0x00000008

    with open(log_path, "a") as log_f:
        proc = subprocess.Popen(
            [py_cmd, str(bot_path)] + BOT_ARGS,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            cwd=str(bot_dir),
            creationflags=CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS,
        )

    # Save PID
    LOCK_FILE.write_text(str(proc.pid))
    log(f"Bot started (PID {proc.pid})")
    return proc.pid


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="Run in loop mode (every 5 min)")
    args = parser.parse_args()

    py_cmd = find_python()
    if not py_cmd:
        log("ERROR: Python not found")
        sys.exit(1)

    while True:
        if is_bot_running():
            if not args.loop:
                print("Bot is running.")
        else:
            log("Bot NOT running - restarting...")
            start_bot(py_cmd)

        if not args.loop:
            break
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
