#!/bin/bash
# Start the LLM Proxy backend in background.
#
# Usage:
#   ./start.sh                          # auto-detect python
#   ./start.sh -p python3               # use system python3
#   ./start.sh -p /path/to/venv/bin/python
#   ./start.sh -c metaharness           # use conda env "metaharness"
#   ./start.sh -u                       # use "uv run python"
#   PYTHON_BIN=python3.12 ./start.sh    # env-var override
#
# Auto-detection order (when no flag given):
#   1. $PYTHON_BIN env var
#   2. .venv/bin/python  (project-local venv)
#   3. active conda environment's python
#   4. python3  (system)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="$SCRIPT_DIR/back.pid"
LOGFILE="$SCRIPT_DIR/back.log"

# ── Parse flags ──
PYTHON_CMD=""
USE_UV=false
while getopts "p:c:uh" opt; do
    case "$opt" in
        p) PYTHON_CMD="$OPTARG" ;;
        c) PYTHON_CMD="$(conda run -n "$OPTARG" which python 2>/dev/null || true)"
           if [ -z "$PYTHON_CMD" ]; then
               echo "❌ Conda environment '$OPTARG' not found or has no python."
               exit 1
           fi ;;
        u) USE_UV=true ;;
        h) echo "Usage: $0 [-p PYTHON_BIN] [-c CONDA_ENV] [-u (uv)]"
           echo ""
           echo "Options:"
           echo "  -p PATH   Use specific python binary"
           echo "  -c NAME   Activate conda environment NAME"
           echo "  -u        Use 'uv run python'"
           echo ""
           echo "Examples:"
           echo "  $0                        # auto-detect"
           echo "  $0 -p /usr/bin/python3.12"
           echo "  $0 -c metaharness"
           echo "  $0 -u"
           echo ""
           echo "Or set PYTHON_BIN env var:"
           echo "  PYTHON_BIN=python3.10 $0"
           exit 0 ;;
        *) exit 1 ;;
    esac
done

# ── Determine Python ──
if [ "$USE_UV" = true ]; then
    # uv run auto-creates venv if pyproject.toml exists
    if ! command -v uv &>/dev/null; then
        echo "❌ 'uv' not found in PATH. Install: pip install uv"
        exit 1
    fi
    PYTHON_CMD="uv run python"
elif [ -z "$PYTHON_CMD" ]; then
    # Priority: env var > .venv > active conda env > system python3
    if [ -n "${PYTHON_BIN:-}" ]; then
        PYTHON_CMD="$PYTHON_BIN"
    elif [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
        PYTHON_CMD="$SCRIPT_DIR/.venv/bin/python"
    elif [ -n "${CONDA_PREFIX:-}" ] && [ -x "$CONDA_PREFIX/bin/python" ]; then
        PYTHON_CMD="$CONDA_PREFIX/bin/python"
    else
        PYTHON_CMD="python3"
    fi
fi

# ── Verify Python works ──
if ! $PYTHON_CMD -c "import sys; print(sys.executable)" &>/dev/null; then
    echo "❌ Cannot run: $PYTHON_CMD"
    echo "   Try: $0 -p /path/to/python"
    exit 1
fi

PY_VER=$($PYTHON_CMD --version 2>&1)
echo "🐍 Using: $PY_VER  ($PYTHON_CMD)"

# ── Pre-flight: kill any existing backend process ──
if [ -f "$PIDFILE" ]; then
    OLD_PID=$(cat "$PIDFILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "⚠️  Backend is already running (PID $OLD_PID)."
        echo "   Run ./stop.sh first, or: kill $OLD_PID"
        exit 1
    else
        echo "🧹 Stale PID file found (PID $OLD_PID no longer exists) — cleaning up"
        rm -f "$PIDFILE"
    fi
fi

# ── Safety net: kill any orphan processes on port 3998 ──
ORPHANS=$(lsof -ti :3998 2>/dev/null || true)
if [ -n "$ORPHANS" ]; then
    echo "🧹 Killing orphan process(es) on port 3998: $ORPHANS"
    kill -9 $ORPHANS 2>/dev/null || true
    sleep 1
fi

# ── Start backend ──
cd "$SCRIPT_DIR"
echo "🚀 Starting backend..."
nohup $PYTHON_CMD backend/main.py > "$LOGFILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PIDFILE"
echo "✅ Backend started (PID $NEW_PID, log: $LOGFILE)"
echo "   Stop with: ./stop.sh"
