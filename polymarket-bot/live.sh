#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

PY_CMD=""
if command -v python3 >/dev/null 2>&1; then
  PY_CMD="python3"
elif command -v python >/dev/null 2>&1; then
  PY_CMD="python"
fi

if [ -z "$PY_CMD" ]; then
  echo "Python was not found. Install Python 3 and ensure python3 or python is on PATH."
  exit 1
fi

read -r -p "Continue in live mode? (y/n): " confirm
if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
  exit 0
fi

"$PY_CMD" main.py --mode live
