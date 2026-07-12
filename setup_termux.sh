#!/data/data/com.termux/files/usr/bin/bash
# ╔══════════════════════════════════════════════════════╗
# ║   TradeSignal — Termux Setup Script                 ║
# ║   Run once after cloning the repo.                  ║
# ╚══════════════════════════════════════════════════════╝

set -e
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
cd "$DIR"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()    { echo -e "${GREEN}✅ $*${NC}"; }
warn()    { echo -e "${YELLOW}⚠️  $*${NC}"; }
error()   { echo -e "${RED}❌ $*${NC}"; exit 1; }
section() { echo -e "\n${GREEN}━━━ $* ━━━${NC}"; }

# ── 0. Verify we are on Termux ──────────────────────────────────────────────
if [ ! -d /data/data/com.termux ]; then
    warn "Not running inside Termux. Use launch_tradesignal.sh instead."
else
    # Resolve Termux venv symbol loading bug (cannot locate symbol: pyexc_warning)
    PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "3.13")
    export LD_PRELOAD="/data/data/com.termux/files/usr/lib/libpython${PY_VER}.so"
fi

section "Step 1: Updating Termux packages"
if ! (pkg update -y && pkg upgrade -y); then
    error "Termux package update failed.\n  - Ensure you did NOT install Termux from the Google Play Store (which is deprecated and offline).\n  - If you are on the F-Droid version, run 'termux-change-repo', choose a working mirror, and run this script again."
fi

section "Step 2: Installing required system packages"
# python                         — interpreter
# python-pip                     — package manager
# python-numpy/pandas/cryptography — precompiled python libraries for Android/ARM
# termux-api                     — for native notification helpers

# Enable Termux User Repository (TUR) to get precompiled numpy/pandas
if ! pkg install -y tur-repo; then
    error "Failed to install tur-repo.\n  - Run 'termux-change-repo' to choose a different mirror, update, and run this script again."
fi

if ! pkg update -y; then
    error "Failed to update package index after enabling tur-repo."
fi

if ! pkg install -y python python-pip python-numpy python-pandas python-cryptography termux-api; then
    error "Failed to install core system packages.\n  - Try running 'pkg update && pkg upgrade' manually and run this script again."
fi

# Optional: git (if not already installed)
pkg install -y git 2>/dev/null || true

info "System packages installed."

section "Step 3: Setting up Python virtual environment"
if [ ! -d "$DIR/.venv" ]; then
    # Use --system-site-packages so the virtual environment inherits precompiled system numpy/pandas
    python -m venv --system-site-packages "$DIR/.venv"
    info "Created .venv with system site packages"
else
    info ".venv already exists — skipping creation."
fi

# Activate venv
source "$DIR/.venv/bin/activate"

section "Step 4: Upgrading pip, wheel, setuptools"
pip install --upgrade pip wheel setuptools

section "Step 5: Installing Python dependencies (Termux build)"
# Use Termux-specific requirements (relaxed version pins, no gunicorn/waitress)
if [ -f "$DIR/requirements-termux.txt" ]; then
    pip install -r "$DIR/requirements-termux.txt"
    info "Installed from requirements-termux.txt"
else
    pip install -r "$DIR/requirements.txt"
fi

section "Step 5b: Installing Gemini AI SDK (google-genai)"
python "$DIR/app/backend/install_termux_deps.py"




section "Step 6: Validating .env"
ENV_FILE="$DIR/.env"

if [ ! -f "$ENV_FILE" ]; then
    # .env should have come from git — if it's missing something went wrong
    cat > "$ENV_FILE" << 'ENVEOF'
# TradeSignal — Environment Variables
# This file should have been populated by git pull.
# Fill in your Kite credentials if they are missing.
APP_SECRET_KEY=a3f8c2e1d94b76052f18e3a0c5d72b9f4e6a81c3d05f2e7b9a4c8d1e3f6b20a5
KITE_API_KEY=your_api_key_here
KITE_API_SECRET=your_api_secret_here
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
APP_USERNAME=
APP_PASSWORD=
ENVEOF
    warn ".env was missing (should have come from git). Created a template."
    warn "Edit .env and add your KITE_API_KEY and KITE_API_SECRET before launching!"
else
    # Validate that credentials are not placeholder values
    if grep -q "your_api_key_here\|your_api_secret_here" "$ENV_FILE"; then
        warn ".env has placeholder credentials — edit it before launching!"
        warn "  nano $ENV_FILE"
    else
        info ".env is present with credentials ✓"
    fi
    # Ensure APP_SECRET_KEY is present (sessions die without it)
    if ! grep -q "^APP_SECRET_KEY=" "$ENV_FILE"; then
        echo 'APP_SECRET_KEY=a3f8c2e1d94b76052f18e3a0c5d72b9f4e6a81c3d05f2e7b9a4c8d1e3f6b20a5' >> "$ENV_FILE"
        info "Added missing APP_SECRET_KEY to .env"
    fi
    if ! grep -q "^TELEGRAM_BOT_TOKEN=" "$ENV_FILE"; then
        echo 'TELEGRAM_BOT_TOKEN=' >> "$ENV_FILE"
        info "Added missing TELEGRAM_BOT_TOKEN placeholder to .env"
    fi
    if ! grep -q "^TELEGRAM_CHAT_ID=" "$ENV_FILE"; then
        echo 'TELEGRAM_CHAT_ID=' >> "$ENV_FILE"
        info "Added missing TELEGRAM_CHAT_ID placeholder to .env"
    fi
fi

section "Step 7: Creating launch alias"
BASHRC="$HOME/.bashrc"
ALIAS_LINE="alias tradesignal='bash $DIR/launch_tradesignal.sh'"

if ! grep -qF "alias tradesignal=" "$BASHRC" 2>/dev/null; then
    echo "" >> "$BASHRC"
    echo "# TradeSignal launcher" >> "$BASHRC"
    echo "$ALIAS_LINE" >> "$BASHRC"
    info "Added 'tradesignal' alias to ~/.bashrc"
    info "Run: source ~/.bashrc  — then type: tradesignal"
else
    info "'tradesignal' alias already present in ~/.bashrc"
fi

section "Step 8: Smoke-test imports"
python -c "
import flask, flask_cors, flask_compress, flask_socketio, kiteconnect
import dotenv, feedparser, pandas, numpy, requests, pytz
import google.genai as gai
print('  All core imports OK (including google-genai)')
" && info "All Python imports verified." || warn "Some imports failed — check the errors above."

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║  Setup complete!                                     ║"
echo "║                                                      ║"
echo "║  Next steps:                                         ║"
echo "║  1. nano .env          → Add your Kite credentials  ║"
echo "║  2. source ~/.bashrc   → Load the alias             ║"
echo "║  3. tradesignal        → Start the app              ║"
echo "║                                                      ║"
echo "║  Or run directly:                                    ║"
echo "║    bash launch_tradesignal.sh                       ║"
echo "╚══════════════════════════════════════════════════════╝"
