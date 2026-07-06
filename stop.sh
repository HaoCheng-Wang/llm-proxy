#!/bin/bash
# Stop the LLM Proxy backend gracefully (or forcefully).
# Usage: ./stop.sh
set -euo pipefail

PIDFILE="$(dirname "$0")/back.pid"

if [ ! -f "$PIDFILE" ]; then
    echo "❌ No PID file found at $PIDFILE"
    echo "   Checking for orphan processes on port 3998..."
    ORPHANS=$(lsof -ti :3998 2>/dev/null || true)
    if [ -n "$ORPHANS" ]; then
        echo "⚠️  Found process(es) on port 3998: $ORPHANS"
        echo "   Kill them with: kill -9 $ORPHANS"
    else
        echo "   No process found on port 3998."
    fi
    exit 1
fi

PID=$(cat "$PIDFILE")

# ── Step 1: Graceful shutdown (SIGTERM) ──
if kill -0 "$PID" 2>/dev/null; then
    echo "🛑 Sending SIGTERM to PID $PID..."
    kill "$PID"

    # Wait up to 20 seconds for graceful shutdown
    for i in $(seq 1 20); do
        if ! kill -0 "$PID" 2>/dev/null; then
            echo "✅ Backend stopped gracefully (PID $PID)"
            rm -f "$PIDFILE"
            exit 0
        fi
        sleep 1
    done

    # ── Step 2: Force kill if still alive ──
    echo "⚠️  Graceful shutdown timed out — sending SIGKILL..."
    kill -9 "$PID" 2>/dev/null || true
    sleep 1
else
    echo "🧹 PID $PID is not running (stale PID file)"
fi

# ── Step 3: Clean up any remaining processes on port 3998 ──
ORPHANS=$(lsof -ti :3998 2>/dev/null || true)
if [ -n "$ORPHANS" ]; then
    echo "⚠️  Forcing kill of remaining process(es) on port 3998: $ORPHANS"
    kill -9 $ORPHANS 2>/dev/null || true
    sleep 1
fi

# Final verification
if lsof -ti :3998 >/dev/null 2>&1; then
    echo "❌ Port 3998 is still occupied! Manual intervention required."
    exit 1
fi

rm -f "$PIDFILE"
echo "✅ Backend stopped, port 3998 released."
