#!/usr/bin/env bash
# ── APEX Backend Run Script ───────────────────────────────────────────────────
# Usage:
#   ./run.sh              → development (Flask dev server, auto-reload)
#   ./run.sh prod         → production (gunicorn, 4 workers)
#   ./run.sh install      → install dependencies
#   ./run.sh token <tok>  → push an access_token directly (no HTTP needed)

set -e

MODE=${1:-dev}
PORT=${PORT:-5000}

case "$MODE" in

  install)
    echo "📦 Installing dependencies..."
    pip install -r requirements.txt
    echo "✅ Done"
    ;;

  token)
    TOKEN=$2
    if [ -z "$TOKEN" ]; then
      echo "❌ Usage: ./run.sh token <access_token>"
      exit 1
    fi
    API_KEY=$(grep KITE_API_KEY .env 2>/dev/null | cut -d= -f2 || echo "")
    echo "{\"api_key\": \"$API_KEY\", \"access_token\": \"$TOKEN\"}" > kite_config.json
    echo "✅ Token saved to kite_config.json"
    ;;

  prod)
    echo "🚀 Starting APEX backend (production) on port $PORT..."
    exec gunicorn \
      --bind "0.0.0.0:$PORT" \
      --workers 4 \
      --worker-class gthread \
      --threads 2 \
      --timeout 120 \
      --keep-alive 5 \
      --access-logfile - \
      --error-logfile - \
      app:app
    ;;

  dev|*)
    echo "🔧 Starting APEX backend (dev) on port $PORT..."
    export FLASK_DEBUG=1
    exec python app.py
    ;;

esac
