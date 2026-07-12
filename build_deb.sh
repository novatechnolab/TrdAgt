#!/bin/bash
set -e

APP_NAME="tradesignal"
VERSION="1.0"
ARCH="amd64"
PKG_DIR="${APP_NAME}_${VERSION}_${ARCH}"
DEST_DIR="$HOME/Downloads"

echo "🧹 Cleaning up old builds..."
rm -rf "$PKG_DIR"

echo "📁 Creating package structure..."
mkdir -p "$PKG_DIR/DEBIAN"
mkdir -p "$PKG_DIR/opt/$APP_NAME"
mkdir -p "$PKG_DIR/usr/share/applications"
mkdir -p "$PKG_DIR/usr/local/bin"
mkdir -p "$PKG_DIR/usr/share/icons/hicolor/256x256/apps"

echo "📝 Creating control file..."
cat << 'CTRL' > "$PKG_DIR/DEBIAN/control"
Package: tradesignal
Version: 1.0
Section: custom
Priority: optional
Architecture: amd64
Depends: python3, python3-venv, bash
Maintainer: Raj <raj@example.com>
Description: TradeSignal Financial Dashboard
 A powerful stock market screening and multi-charting platform.
CTRL

echo "📜 Creating post-installation script (venv setup)..."
cat << 'POSTINST' > "$PKG_DIR/DEBIAN/postinst"
#!/bin/bash
set -e
echo "Setting up Python virtual environment in /opt/tradesignal..."
cd /opt/tradesignal
python3 -m venv .venv
source .venv/bin/activate
# Install requirements if present
if [ -f "requirements.txt" ]; then
    pip install -r requirements.txt
fi
# Set permissions
chown -R $SUDO_USER:$SUDO_USER /opt/tradesignal
chmod +x /opt/tradesignal/start.sh
exit 0
POSTINST
chmod 755 "$PKG_DIR/DEBIAN/postinst"

echo "🖥️ Creating launcher scripts..."
cat << 'STARTSH' > "$PKG_DIR/opt/$APP_NAME/start.sh"
#!/bin/bash
cd /opt/tradesignal
source .venv/bin/activate
python3 app/backend/server.py &
sleep 2
xdg-open "http://localhost:5000"
wait
STARTSH
chmod +x "$PKG_DIR/opt/$APP_NAME/start.sh"

cat << 'BINSH' > "$PKG_DIR/usr/local/bin/tradesignal"
#!/bin/bash
/opt/tradesignal/start.sh
BINSH
chmod +x "$PKG_DIR/usr/local/bin/tradesignal"

echo "🎯 Creating desktop entry..."
cat << 'DESKTOP' > "$PKG_DIR/usr/share/applications/tradesignal.desktop"
[Desktop Entry]
Name=TradeSignal
Comment=Stock Market Screening Dashboard
Exec=/usr/local/bin/tradesignal
Terminal=true
Type=Application
Categories=Finance;Utility;
DESKTOP

echo "📦 Copying application files..."
rsync -av --exclude='.git' --exclude='.venv' --exclude='__pycache__' --exclude='build_deb.sh' --exclude="${APP_NAME}_*" ./ "$PKG_DIR/opt/$APP_NAME/"

echo "🔨 Building .deb package..."
dpkg-deb --build "$PKG_DIR"

echo "🚚 Moving package to Downloads..."
mv "${PKG_DIR}.deb" "$DEST_DIR/"

echo "🧹 Cleaning up..."
rm -rf "$PKG_DIR"

echo "✅ Success! Package available at: $DEST_DIR/${PKG_DIR}.deb"
echo "To install: sudo apt install $DEST_DIR/${PKG_DIR}.deb"
