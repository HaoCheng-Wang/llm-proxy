#!/bin/bash
# Stop the LLM Proxy backend + frontend (vite) gracefully (or forcefully).
# Usage: ./stop.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="$SCRIPT_DIR/back.pid"
VITE_PIDFILE="$SCRIPT_DIR/vite.pid"

stop_by_pidfile() {
    local name="$1" pidfile="$2" port="$3"
    if [ ! -f "$pidfile" ]; then
        echo "ℹ️  No PID file for $name at $pidfile — checking port $port..."
        local orphans
        # 只匹配 LISTEN 状态的进程，避免误杀 vscode 端口转发等连接方进程
        orphans=$(lsof -ti :"$port" -sTCP:LISTEN 2>/dev/null || true)
        if [ -n "$orphans" ]; then
            echo "⚠️  Found orphan $name process(es) on port $port: $orphans"
            echo "   Kill them with: kill -9 $orphans"
        else
            echo "   No $name process found on port $port."
        fi
        return 1
    fi

    local pid
    pid=$(cat "$pidfile")

    # ── Step 1: Graceful shutdown (SIGTERM to the process group) ──
    if kill -0 "$pid" 2>/dev/null; then
        echo "🛑 Sending SIGTERM to $name (PGID $pid)..."
        # Negative PID = whole process group. vite 由 nohup+setsid 启动，
        # npx 包装进程与 node 子进程同组，必须整组关闭才能杀干净。
        kill -- -"$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true

        # Wait up to 20 seconds for graceful shutdown
        for _ in $(seq 1 20); do
            if ! kill -0 "$pid" 2>/dev/null; then
                echo "✅ $name stopped gracefully (PID $pid)"
                rm -f "$pidfile"
                return 0
            fi
            sleep 1
        done

        # ── Step 2: Force kill the whole group if still alive ──
        echo "⚠️  Graceful shutdown timed out — sending SIGKILL to $name group..."
        kill -9 -- -"$pid" 2>/dev/null || kill -9 "$pid" 2>/dev/null || true
        sleep 1
    else
        echo "🧹 $name PID $pid is not running (stale PID file)"
    fi

    # ── Step 3: Clean up any remaining LISTEN processes on the port ──
    local orphans
    orphans=$(lsof -ti :"$port" -sTCP:LISTEN 2>/dev/null || true)
    if [ -n "$orphans" ]; then
        echo "⚠️  Forcing kill of remaining $name process(es) on port $port: $orphans"
        kill -9 $orphans 2>/dev/null || true
        sleep 1
    fi

    if lsof -ti :"$port" -sTCP:LISTEN >/dev/null 2>&1; then
        echo "❌ Port $port is still occupied! Manual intervention required."
        return 1
    fi

    rm -f "$pidfile"
    echo "✅ $name stopped, port $port released."
    return 0
}

# Stop frontend first (it holds upstream connections to the backend),
# then the backend.
stop_by_pidfile "frontend (vite)" "$VITE_PIDFILE" 3999 || true
stop_by_pidfile "backend" "$PIDFILE" 3998
