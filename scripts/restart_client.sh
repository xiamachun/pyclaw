#!/usr/bin/env bash
#
# PyClaw Channel Client manager.
#
# Reads pyclaw.json to determine which channel connectors are enabled,
# then starts/stops/restarts the corresponding background processes.
#
# Usage:
#   ./scripts/restart_client.sh                # restart all enabled channels
#   ./scripts/restart_client.sh dingtalk       # restart only DingTalk
#   ./scripts/restart_client.sh --stop         # stop all channel clients
#   ./scripts/restart_client.sh --status       # show running channels
#   ./scripts/restart_client.sh --list         # list available channels
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
STATE_DIR="${PYCLAW_STATE_DIR:-$HOME/.pyclaw}"
PYTHON="${PYCLAW_PYTHON:-python3}"

# Ensure state directory exists
mkdir -p "$STATE_DIR"

# ---------------------------------------------------------------------------
# Channel registry (bash 3.x compatible — no associative arrays)
# wechat_personal is excluded because it runs inside the Gateway process.
# ---------------------------------------------------------------------------
ALL_CHANNELS="dingtalk wechat feishu slack telegram"

channel_script() {
    case "$1" in
        dingtalk)  echo "pyclaw/channels/dingtalk/stream_client.py" ;;
        wechat)    echo "pyclaw/channels/wechat/client.py" ;;
        feishu)    echo "pyclaw/channels/feishu/client.py" ;;
        slack)     echo "pyclaw/channels/slack/client.py" ;;
        telegram)  echo "pyclaw/channels/telegram/client.py" ;;
        *)         echo "" ;;
    esac
}

channel_config_key() {
    case "$1" in
        dingtalk)  echo "dingtalk-connector" ;;
        wechat)    echo "wechat-connector" ;;
        feishu)    echo "feishu-connector" ;;
        slack)     echo "slack-connector" ;;
        telegram)  echo "telegram-connector" ;;
        *)         echo "" ;;
    esac
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log_info()  { echo "[pyclaw] $(date '+%H:%M:%S') $*"; }
log_error() { echo "[pyclaw] $(date '+%H:%M:%S') ERROR: $*" >&2; }

pid_file_for() { echo "$STATE_DIR/channel_${1}.pid"; }
log_file_for() { echo "$STATE_DIR/${1}.log"; }

is_channel_enabled() {
    local channel="$1"
    local config_key
    config_key="$(channel_config_key "$channel")"
    if [ -z "$config_key" ]; then
        return 1
    fi

    # Find pyclaw.json (same logic as Python config loader)
    local config_file="$PROJECT_DIR/pyclaw.json"
    if [ ! -f "$config_file" ]; then
        config_file="$STATE_DIR/pyclaw.json"
    fi
    if [ ! -f "$config_file" ]; then
        return 1
    fi

    # Use Python to parse JSON (portable, no jq dependency)
    "$PYTHON" -c "
import json, sys
with open('$config_file') as f:
    cfg = json.load(f)
ch = cfg.get('channels', {}).get('$config_key', {})
sys.exit(0 if ch.get('enabled', False) else 1)
" 2>/dev/null
}

stop_channel() {
    local channel="$1"
    local pf
    pf="$(pid_file_for "$channel")"

    if [ -f "$pf" ]; then
        local pid
        pid=$(cat "$pf")
        if kill -0 "$pid" 2>/dev/null; then
            log_info "Stopping $channel client (PID $pid)..."
            kill "$pid"
            local waited=0
            while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt 5 ]; do
                sleep 1
                waited=$((waited + 1))
            done
            if kill -0 "$pid" 2>/dev/null; then
                kill -9 "$pid" 2>/dev/null || true
            fi
            log_info "$channel client stopped."
        else
            log_info "$channel: stale PID file (process $pid not running)."
        fi
        rm -f "$pf"
    else
        # Also try pkill as fallback for processes started by old script
        local script
        script="$(channel_script "$channel")"
        if [ -n "$script" ]; then
            pkill -f "$(basename "$script")" 2>/dev/null || true
        fi
    fi
}

start_channel() {
    local channel="$1"
    local script
    script="$(channel_script "$channel")"
    local log_file
    log_file="$(log_file_for "$channel")"
    local pf
    pf="$(pid_file_for "$channel")"

    if [ -z "$script" ]; then
        log_error "Unknown channel: $channel"
        return 1
    fi

    local full_script="$PROJECT_DIR/$script"
    if [ ! -f "$full_script" ]; then
        log_error "$channel client script not found: $script"
        return 1
    fi

    log_info "Starting $channel client..."
    cd "$PROJECT_DIR"
    nohup "$PYTHON" "$script" >> "$log_file" 2>&1 &
    local new_pid=$!
    echo "$new_pid" > "$pf"

    sleep 1
    if kill -0 "$new_pid" 2>/dev/null; then
        log_info "$channel client started (PID $new_pid). Logs: $log_file"
    else
        log_error "$channel client failed to start. Check $log_file"
        rm -f "$pf"
        return 1
    fi
}

show_status() {
    for channel in $ALL_CHANNELS; do
        local pf
        pf="$(pid_file_for "$channel")"
        local enabled="no"
        is_channel_enabled "$channel" && enabled="yes"

        if [ -f "$pf" ]; then
            local pid
            pid=$(cat "$pf")
            if kill -0 "$pid" 2>/dev/null; then
                log_info "$channel: RUNNING (PID $pid, enabled=$enabled)"
            else
                log_info "$channel: STOPPED (stale PID, enabled=$enabled)"
            fi
        else
            log_info "$channel: STOPPED (enabled=$enabled)"
        fi
    done
    log_info "wechat_personal: managed by Gateway (login via WebUI)"
}

list_channels() {
    echo "Available channels:"
    for channel in $ALL_CHANNELS; do
        local enabled="disabled"
        is_channel_enabled "$channel" && enabled="enabled"
        local script
        script="$(channel_script "$channel")"
        echo "  $channel ($enabled) -> $script"
    done
    echo "  wechat_personal (Gateway-managed, login via WebUI)"
}

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS] [CHANNEL...]

Manage PyClaw channel client processes.

If no CHANNEL is specified, operates on all enabled channels (from pyclaw.json).
If CHANNEL(s) are specified, operates only on those channels regardless of config.

Options:
  --stop        Stop channel clients (do not restart)
  --status      Show running channel clients
  --list        List available channels and their status
  -h, --help    Show this help message

Channels: $ALL_CHANNELS
  (wechat_personal is managed by Gateway, not this script)

Environment variables:
  PYCLAW_STATE_DIR   State directory (default: ~/.pyclaw)
  PYCLAW_PYTHON      Python interpreter (default: python3)

Examples:
  $(basename "$0")                  # restart all enabled channels
  $(basename "$0") dingtalk         # restart only DingTalk
  $(basename "$0") dingtalk wechat  # restart DingTalk and WeChat
  $(basename "$0") --stop           # stop all channel clients
  $(basename "$0") --stop dingtalk  # stop only DingTalk
  $(basename "$0") --status         # show status of all channels
EOF
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

ACTION="restart"
CHANNELS=""

while [ $# -gt 0 ]; do
    case "$1" in
        --stop)    ACTION="stop";    shift ;;
        --status)  ACTION="status";  shift ;;
        --list)    ACTION="list";    shift ;;
        -h|--help) usage; exit 0 ;;
        -*)
            log_error "Unknown option: $1"
            usage
            exit 1
            ;;
        *)
            CHANNELS="$CHANNELS $1"
            shift
            ;;
    esac
done

# Trim leading space
CHANNELS="$(echo "$CHANNELS" | sed 's/^ *//')"

case "$ACTION" in
    status)
        show_status
        exit 0
        ;;
    list)
        list_channels
        exit 0
        ;;
esac

# Determine which channels to operate on
if [ -z "$CHANNELS" ]; then
    for channel in $ALL_CHANNELS; do
        if is_channel_enabled "$channel"; then
            CHANNELS="$CHANNELS $channel"
        fi
    done
    CHANNELS="$(echo "$CHANNELS" | sed 's/^ *//')"

    if [ -z "$CHANNELS" ]; then
        log_info "No channels enabled in pyclaw.json. Use --list to see available channels."
        exit 0
    fi
fi

# Execute action
FAILED=0
for channel in $CHANNELS; do
    local_script="$(channel_script "$channel")"
    if [ -z "$local_script" ]; then
        if [ "$channel" = "wechat_personal" ]; then
            log_info "wechat_personal is managed by Gateway (login via WebUI), skipping."
            continue
        fi
        log_error "Unknown channel: $channel"
        FAILED=1
        continue
    fi

    case "$ACTION" in
        stop)
            stop_channel "$channel"
            ;;
        restart)
            stop_channel "$channel"
            sleep 1
            start_channel "$channel" || FAILED=1
            ;;
    esac
done

exit $FAILED
