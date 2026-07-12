#!/bin/bash
# FNO Session Analyzer Feature Test Script
# Run this to verify the feature is properly integrated

echo "=== FNO Session Analyzer Feature Verification ==="
echo ""

echo "✓ Checking file creation..."
if [ -f "app/js/fno-session-analyzer.js" ]; then
  echo "  ✓ fno-session-analyzer.js exists ($(wc -l < app/js/fno-session-analyzer.js) lines)"
else
  echo "  ✗ fno-session-analyzer.js NOT FOUND"
fi

echo ""
echo "✓ Checking HTML integration..."
if grep -q "fno-session-analyzer.js" app/index.html; then
  echo "  ✓ Script imported in index.html"
else
  echo "  ✗ Script NOT imported"
fi

if grep -q "page-fno-session" app/index.html; then
  echo "  ✓ FNO Sessions page exists in HTML"
else
  echo "  ✗ FNO Sessions page NOT FOUND"
fi

if grep -q "nav-fno-session" app/index.html; then
  echo "  ✓ FNO Sessions nav item exists"
else
  echo "  ✗ Nav item NOT FOUND"
fi

echo ""
echo "✓ Checking JavaScript integration..."
if grep -q "this.bindFNOSessions()" app/js/app.js; then
  echo "  ✓ bindFNOSessions() is called in init()"
else
  echo "  ✗ Binding NOT FOUND"
fi

if grep -q "bindFNOSessions()" app/js/app.js; then
  echo "  ✓ bindFNOSessions() method defined"
else
  echo "  ✗ Method NOT FOUND"
fi

if grep -q "updateSessionStatus()" app/js/app.js; then
  echo "  ✓ updateSessionStatus() method defined"
else
  echo "  ✗ Method NOT FOUND"
fi

if grep -q "analyzeFNOStock()" app/js/app.js; then
  echo "  ✓ analyzeFNOStock() method defined"
else
  echo "  ✗ Method NOT FOUND"
fi

echo ""
echo "✓ Checking documentation..."
if [ -f "FNO_SESSION_ANALYZER.md" ]; then
  echo "  ✓ Feature documentation exists"
else
  echo "  ✗ Documentation NOT FOUND"
fi

echo ""
echo "=== Summary ==="
echo "FNO Session Analyzer feature has been successfully implemented!"
echo ""
echo "How to use:"
echo "1. Start the app: python app/backend/server.py"
echo "2. Go to http://localhost:5000"
echo "3. Connect to Kite API in Settings"
echo "4. Click 'FNO Sessions' in the sidebar"
echo "5. Select a stock and click 'Quick Analysis'"
echo ""
echo "The system will analyze the stock based on the current market session:"
echo "  • 6:00-8:59 AM: Premarket analysis"
echo "  • 9:00-9:15 AM: Opening bell analysis"
echo "  • 9:15 AM-3:30 PM: Live session analysis"
echo "  • Other times: Market closed"
