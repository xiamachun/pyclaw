#!/usr/bin/env bash
#
# PyClaw one-click launcher — starts Gateway + all enabled Channel clients.
#
# Usage:
#   ./scripts/start_all.sh           # start everything
#   ./scripts/start_all.sh --stop    # stop everything
#   ./scripts/start_all.sh --status  # show status of all components
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

log_info()  { echo "[pyclaw] $(date '+%H:%M:%S') $*"; }
log_error() { echo "[pyclaw] $(date '+%H:%M:%S') ERROR: $*" >&2; }

ACTION="start"
if [ "${1:-}" = "--stop" ]; then
    ACTION="stop"
elif [ "${1:-}" = "--status" ]; then
    ACTION="status"
fi

case "$ACTION" in
    stop)
        log_info "Stopping all PyClaw components..."
        bash "$SCRIPT_DIR/restart_client.sh" --stop 2>/dev/null || true
        bash "$SCRIPT_DIR/restart_pyclaw.sh" --stop 2>/dev/null || true
        log_info "All PyClaw components stopped."
        ;;

    status)
        log_info "=== Gateway ==="
        local_pid_file="${PYCLAW_STATE_DIR:-$HOME/.pyclaw}/pyclaw.pid"
        if [ -f "$local_pid_file" ] && kill -0 "$(cat "$local_pid_file")" 2>/dev/null; then
            log_info "Gateway: RUNNING (PID $(cat "$local_pid_file"))"
        else
            log_info "Gateway: STOPPED"
        fi
        echo ""
        log_info "=== Channels ==="
        bash "$SCRIPT_DIR/restart_client.sh" --status
        ;;

    start)
        log_info "============================================================"
        log_info "  PyClaw — Starting all components"
        log_info "============================================================"
        echo ""

        # 1. Start Gateway
        log_info ">>> Starting Gateway..."
        bash "$SCRIPT_DIR/restart_pyclaw.sh"
        echo ""

        # 2. Wait a moment for Gateway to be ready
        sleep 1

        # 3. Start all enabled Channel clients
        log_info ">>> Starting Channel clients..."
        bash "$SCRIPT_DIR/restart_client.sh"
        echo ""

        log_info "============================================================"
        log_info "  PyClaw is ready!"
        log_info "  Gateway:  http://127.0.0.1:18789"
        log_info "  Web UI:   http://127.0.0.1:18789/webui"
        log_info "============================================================"
        ;;
esac
