#!/bin/bash
# ╔══════════════════════════════════════════════════════╗
# ║   TradeSignal — Universal Launcher                  ║
# ║   Works on: Ubuntu Desktop · Termux (Android)       ║
# ╚══════════════════════════════════════════════════════╝

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
cd "$DIR"

if [ -d /data/data/com.termux ]; then
    # Resolve Termux venv symbol loading bug (cannot locate symbol: pyexc_warning)
    PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "3.13")
    export LD_PRELOAD="/data/data/com.termux/files/usr/lib/libpython${PY_VER}.so"
fi

PORT="${PORT:-5000}"

# ── Activate venv ────────────────────────────────────────────────────────────
if [ -d "$DIR/.venv" ]; then
    source "$DIR/.venv/bin/activate"
else
    echo "⚠️  No .venv found. Run setup_termux.sh first (Termux) or install deps manually."
fi

# ── Start backend server ─────────────────────────────────────────────────────
echo "🚀 Starting TradeSignal backend on port $PORT..."
python "$DIR/app/backend/server.py" &
SERVER_PID=$!

# Cleanup: stop the server when this script exits
trap "echo '🛑 Shutting down TradeSignal server...'; kill $SERVER_PID 2>/dev/null; exit" EXIT INT TERM

# Wait for Flask to boot
sleep 3

# Verify server started successfully
if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "❌ Server failed to start. Check the output above for errors."
    exit 1
fi

echo "✅ Server running (PID $SERVER_PID)"
echo ""

# ── Open the UI ──────────────────────────────────────────────────────────────
APP_URL="http://localhost:${PORT}/"

if command -v termux-open-url &> /dev/null; then
    # ── Termux (Android) ──
    echo "📱 Opening in Android browser..."
    termux-open-url "$APP_URL" &

    # On Termux, keep the server alive. Show instructions.
    echo ""
    echo "╔══════════════════════════════════════════════════════╗"
    echo "║  TradeSignal is running!                            ║"
    echo "║                                                      ║"
    echo "║  URL:  http://localhost:${PORT}/                        ║"
    echo "║                                                      ║"
    echo "║  💡 If the browser didn't open automatically,       ║"
    echo "║     open your browser and go to the URL above.      ║"
    echo "║                                                      ║"
    echo "║  Press Ctrl+C to stop the server.                   ║"
    echo "╚══════════════════════════════════════════════════════╝"

    # Send a Termux notification (if available)
    if command -v termux-notification &> /dev/null; then
        termux-notification \
            --title "TradeSignal Running" \
            --content "Tap to open · http://localhost:${PORT}/" \
            --id "tradesignal" \
            --ongoing 2>/dev/null &
    fi

    # Keep server alive — wait in a loop so Ctrl+C works cleanly
    while kill -0 "$SERVER_PID" 2>/dev/null; do
        sleep 5
    done

elif command -v google-chrome &> /dev/null; then
    # ── Ubuntu Desktop — Chrome App Mode ──
    echo "🖥️  Opening in Chrome app mode..."
    google-chrome --app="$APP_URL" &

elif command -v chromium-browser &> /dev/null; then
    # ── Ubuntu Desktop — Chromium App Mode ──
    echo "🖥️  Opening in Chromium app mode..."
    chromium-browser --app="$APP_URL" &

elif command -v chromium &> /dev/null; then
    echo "🖥️  Opening in Chromium..."
    chromium --app="$APP_URL" &

else
    # ── Fallback: xdg-open ──
    echo "🌐 Opening in default browser..."
    xdg-open "$APP_URL" 2>/dev/null || true
fi

# ── Keep server alive (desktop) ──────────────────────────────────────────────
echo "======================================================="
echo "  TradeSignal Server is running in the background."
echo "  URL: $APP_URL"
echo "  Close this terminal window (or press Ctrl+C) to stop."
echo "======================================================="
wait $SERVER_PID
