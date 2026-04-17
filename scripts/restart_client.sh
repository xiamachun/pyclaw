#!/usr/bin/env bash
# PyClaw DingTalk Stream Client restart script.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_FILE="${PYCLAW_STATE_DIR:-$HOME/.pyclaw}/dingtalk.log"
PYTHON="${PYCLAW_PYTHON:-python3}"

pkill -f stream_client.py 2>/dev/null || true
sleep 2
cd "$PROJECT_DIR"
nohup "$PYTHON" pyclaw/channels/dingtalk/stream_client.py >> "$LOG_FILE" 2>&1 &
echo "[pyclaw] DingTalk Stream Client started (PID $!). Logs: $LOG_FILE"
