#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
DESKTOP_FILE="$HOME/.local/share/applications/tradesignal.desktop"

# 1. Make the launcher executable
chmod +x "$DIR/launch_tradesignal.sh"

# 2. Create the Desktop Entry for Ubuntu
cat > "$DESKTOP_FILE" << EOF
[Desktop Entry]
Version=1.0
Name=TradeSignal
Comment=Institutional NSE F&O Intelligence Suite
Exec="$DIR/launch_tradesignal.sh"
Icon=utilities-system-monitor
Terminal=true
Type=Application
Categories=Finance;Utility;Office;
Keywords=trading;stock;nse;dashboard;
EOF

# 3. Make it executable and update desktop database
chmod +x "$DESKTOP_FILE"
if command -v update-desktop-database &> /dev/null; then
    update-desktop-database "$HOME/.local/share/applications/"
fi

echo "=========================================================="
echo "✅ TradeSignal packaged and installed successfully!"
echo "=========================================================="
echo "You can now open your Ubuntu app launcher (press the Super/Windows key)"
echo "and search for 'TradeSignal' to launch it like a native app."
echo ""
echo "It will automatically start the backend server in the background"
echo "and open the dashboard in a clean, app-like window."
