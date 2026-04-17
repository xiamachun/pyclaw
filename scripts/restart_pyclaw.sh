#!/usr/bin/env bash
#
# PyClaw Gateway restart script.
# Usage:
#   ./scripts/restart_pyclaw.sh              # restart with defaults
#   ./scripts/restart_pyclaw.sh --port 8080  # override port
#   ./scripts/restart_pyclaw.sh --stop       # stop only
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PID_FILE="${PYCLAW_STATE_DIR:-$HOME/.pyclaw}/pyclaw.pid"
LOG_FILE="${PYCLAW_STATE_DIR:-$HOME/.pyclaw}/gateway.log"

# Ensure state directory exists
mkdir -p "$(dirname "$PID_FILE")"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log_info()  { echo "[pyclaw] $(date '+%H:%M:%S') $*"; }
log_error() { echo "[pyclaw] $(date '+%H:%M:%S') ERROR: $*" >&2; }

stop_gateway() {
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            log_info "Stopping PyClaw Gateway (PID $pid)..."
            kill "$pid"
            # Wait up to 10 seconds for graceful shutdown
            local waited=0
            while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt 10 ]; do
                sleep 1
                waited=$((waited + 1))
            done
            if kill -0 "$pid" 2>/dev/null; then
                log_info "Force-killing PyClaw Gateway (PID $pid)..."
                kill -9 "$pid" 2>/dev/null || true
            fi
            log_info "PyClaw Gateway stopped."
        else
            log_info "PID $pid is not running (stale PID file)."
        fi
        rm -f "$PID_FILE"
    else
        log_info "No PID file found — PyClaw Gateway is not running."
    fi
}

start_gateway() {
    log_info "Starting PyClaw Gateway..."
    cd "$PROJECT_DIR"

    # Build the command — uses the zero-arg create_app factory
    # Use environment Python or PYCLAW_PYTHON override
    local PYTHON="${PYCLAW_PYTHON:-python3}"
    local cmd="$PYTHON -m uvicorn pyclaw.gateway.server:create_app --factory"
    cmd="$cmd --host ${PYCLAW_HOST:-127.0.0.1}"
    cmd="$cmd --port ${PYCLAW_PORT:-18789}"

    # Launch in background, redirect output to log file
    nohup $cmd >> "$LOG_FILE" 2>&1 &
    local new_pid=$!
    echo "$new_pid" > "$PID_FILE"

    # Brief wait to check it didn't crash immediately
    sleep 1
    if kill -0 "$new_pid" 2>/dev/null; then
        log_info "PyClaw Gateway started (PID $new_pid)."
        log_info "Logs: $LOG_FILE"
        log_info "PID file: $PID_FILE"
    else
        log_error "PyClaw Gateway failed to start. Check $LOG_FILE for details."
        rm -f "$PID_FILE"
        exit 1
    fi
}

show_status() {
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            log_info "PyClaw Gateway is running (PID $pid)."
        else
            log_info "PID file exists but process $pid is not running."
        fi
    else
        log_info "PyClaw Gateway is not running."
    fi
}

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Restart (stop + start) the PyClaw Gateway server.

Options:
  --stop        Stop the gateway only (do not restart)
  --status      Show whether the gateway is running
  --host HOST   Override bind host  (default: 127.0.0.1 or \$PYCLAW_HOST)
  --port PORT   Override bind port  (default: 18789 or \$PYCLAW_PORT)
  -h, --help    Show this help message

Environment variables:
  PYCLAW_STATE_DIR   State directory (default: ~/.pyclaw)
  PYCLAW_HOST        Bind host (default: 127.0.0.1)
  PYCLAW_PORT        Bind port (default: 18789)

Examples:
  $(basename "$0")                  # restart with defaults
  $(basename "$0") --port 8080      # restart on port 8080
  $(basename "$0") --stop           # stop only
  $(basename "$0") --status         # check status
EOF
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

ACTION="restart"

while [ $# -gt 0 ]; do
    case "$1" in
        start)     ACTION="start";   shift ;;
        stop|--stop)      ACTION="stop";    shift ;;
        restart)   ACTION="restart"; shift ;;
        status|--status)  ACTION="status";  shift ;;
        -h|--help) usage; exit 0 ;;
        --host)    export PYCLAW_HOST="$2"; shift 2 ;;
        --port)    export PYCLAW_PORT="$2"; shift 2 ;;
        *)
            log_error "Unknown argument: $1"
            usage
            exit 1
            ;;
    esac
done

case "$ACTION" in
    start)
        start_gateway
        ;;
    stop)
        stop_gateway
        ;;
    status)
        show_status
        ;;
    restart)
        stop_gateway
        start_gateway
        ;;
esac
