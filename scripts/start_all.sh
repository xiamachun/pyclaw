#!/usr/bin/env bash
#
# PyClaw one-click launcher — manages Gateway + Channel clients + Tunnel.
#
# Usage:
#   ./scripts/start_all.sh                # start everything (idempotent)
#   ./scripts/start_all.sh --stop         # stop gateway + channels (keep tunnel)
#   ./scripts/start_all.sh --stop-all     # stop everything including cloudflared
#   ./scripts/start_all.sh --restart      # restart gateway + channels (keep tunnel)
#   ./scripts/start_all.sh --restart-tunnel  # restart cloudflared tunnel only
#   ./scripts/start_all.sh --status       # show status of all components
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_DIR="${PYCLAW_STATE_DIR:-$HOME/.pyclaw}"

log_info()  { echo "[pyclaw] $(date '+%H:%M:%S') $*"; }
log_error() { echo "[pyclaw] $(date '+%H:%M:%S') ERROR: $*" >&2; }
# Green bold text for important notices
log_highlight() { printf "\033[1;32m%s\033[0m\n" "$*"; }

stop_tunnel() {
    log_info "Stopping cloudflared tunnel..."
    pkill -f "cloudflared tunnel" 2>/dev/null || true
    rm -f "$STATE_DIR/cloudflared.pid" "$STATE_DIR/cloudflared_url.txt"
    log_info "Cloudflared tunnel stopped."
}

show_tunnel_status() {
    local pid_file="$STATE_DIR/cloudflared.pid"
    local url_file="$STATE_DIR/cloudflared_url.txt"
    if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
        local url="(unknown)"
        [ -f "$url_file" ] && url="$(cat "$url_file")"
        log_info "Tunnel:  RUNNING (PID $(cat "$pid_file"), URL: $url)"
    else
        log_info "Tunnel:  STOPPED"
    fi
}

usage() {
    cat <<EOF
Usage: $(basename "$0") [COMMAND]

Manage all PyClaw components (Gateway + Channels + Tunnel).

Commands:
  (none)            Start everything (idempotent — skips already running)
  --stop            Stop Gateway + Channels (keep cloudflared tunnel alive)
  --stop-all        Stop everything including cloudflared tunnel
  --restart         Restart Gateway + Channels (keep tunnel alive)
  --restart-tunnel  Restart cloudflared tunnel only (new URL will be assigned)
  --status          Show status of all components
  -h, --help        Show this help message

Tunnel behavior:
  cloudflared tunnel runs as an independent process. Restarting Gateway or
  Channels does NOT restart the tunnel, so the callback URL stays the same.
  Only use --restart-tunnel or --stop-all when the URL actually needs to change
  (e.g. after network change or machine reboot).

Examples:
  $(basename "$0")                  # first-time start
  $(basename "$0") --restart        # restart after code change (tunnel kept)
  $(basename "$0") --stop           # stop for the night (tunnel kept for tomorrow)
  $(basename "$0") --stop-all       # full shutdown including tunnel
  $(basename "$0") --restart-tunnel # force new tunnel URL
  $(basename "$0") --status         # check what's running
EOF
}

ACTION="start"
case "${1:-}" in
    --stop)             ACTION="stop" ;;
    --stop-all)         ACTION="stop-all" ;;
    --restart)          ACTION="restart" ;;
    --restart-tunnel)   ACTION="restart-tunnel" ;;
    --status)           ACTION="status" ;;
    -h|--help)          usage; exit 0 ;;
    "")                 ACTION="start" ;;
    *)
        log_error "Unknown option: $1"
        usage
        exit 1
        ;;
esac

case "$ACTION" in
    stop)
        log_info "Stopping Gateway + Channels (tunnel kept alive)..."
        bash "$SCRIPT_DIR/restart_client.sh" --stop 2>/dev/null || true
        bash "$SCRIPT_DIR/restart_pyclaw.sh" --stop 2>/dev/null || true
        log_info "Gateway + Channels stopped. Tunnel still running."
        ;;

    stop-all)
        log_info "Stopping ALL PyClaw components (including tunnel)..."
        bash "$SCRIPT_DIR/restart_client.sh" --stop 2>/dev/null || true
        bash "$SCRIPT_DIR/restart_pyclaw.sh" --stop 2>/dev/null || true
        stop_tunnel
        log_info "All PyClaw components stopped."
        ;;

    restart)
        log_info "Restarting Gateway + Channels (tunnel kept alive)..."
        bash "$SCRIPT_DIR/restart_client.sh" --stop 2>/dev/null || true
        bash "$SCRIPT_DIR/restart_pyclaw.sh" stop 2>/dev/null || true
        sleep 1
        bash "$SCRIPT_DIR/restart_pyclaw.sh" start
        sleep 1
        bash "$SCRIPT_DIR/restart_client.sh"
        log_info "Restart complete."
        ;;

    restart-tunnel)
        stop_tunnel
        log_info "Tunnel stopped. It will be re-created on next wecom client start."
        log_info "Run: ./scripts/start_all.sh --restart  to restart everything."
        ;;

    status)
        log_info "=== Gateway ==="
        bash "$SCRIPT_DIR/restart_pyclaw.sh" --status
        echo ""
        log_info "=== Channels ==="
        bash "$SCRIPT_DIR/restart_client.sh" --status
        echo ""
        log_info "=== Tunnel ==="
        show_tunnel_status
        ;;

    start)
        log_info "============================================================"
        log_info "  PyClaw — Starting all components"
        log_info "============================================================"
        echo ""

        # 1. Start Gateway (skip if already running)
        local_pid_file="$STATE_DIR/pyclaw.pid"
        if [ -f "$local_pid_file" ] && kill -0 "$(cat "$local_pid_file")" 2>/dev/null; then
            log_info ">>> Gateway already running (PID $(cat "$local_pid_file")), skipping."
        else
            log_info ">>> Starting Gateway..."
            bash "$SCRIPT_DIR/restart_pyclaw.sh" start
        fi
        echo ""

        # 2. Wait a moment for Gateway to be ready
        sleep 1

        # 3. Start all enabled Channel clients
        log_info ">>> Starting Channel clients..."
        bash "$SCRIPT_DIR/restart_client.sh"
        echo ""

        # 4. Show tunnel URL prominently if available
        tunnel_url=""
        if [ -f "$STATE_DIR/cloudflared_url.txt" ]; then
            tunnel_url="$(cat "$STATE_DIR/cloudflared_url.txt")"
        fi

        echo ""
        log_info "============================================================"
        log_info "  PyClaw is ready!"
        log_info "  Gateway:  http://127.0.0.1:18789"
        log_info "  Web UI:   http://127.0.0.1:18789/webui"
        if [ -n "$tunnel_url" ]; then
            log_highlight "  Tunnel:   ${tunnel_url}"
            log_highlight "  Callback: ${tunnel_url}/wecom/callback"
        fi
        log_info "============================================================"
        ;;
esac
