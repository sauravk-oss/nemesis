#!/bin/bash
# Start Nemesis v2 services
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$ROOT_DIR"

echo "Starting Nemesis v2..."
echo "  FastAPI Event Bus  → :8000"
echo "  Flask UI           → :5555"
echo ""

uvicorn scripts.api_server:app --port 8000 --reload &
PID_API=$!

python3 scripts/rubick_ui.py --port 5555 &
PID_FLASK=$!

trap "kill $PID_API $PID_FLASK 2>/dev/null" EXIT INT TERM

wait
