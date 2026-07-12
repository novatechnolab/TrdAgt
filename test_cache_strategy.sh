#!/bin/bash

# Test caching strategy for validate-entry endpoint
# Replay mode: Check SQLite first, then Kite API
# Live mode: Always use Kite API

set -e

BASE_URL="http://localhost:5000"
SYMBOL="TRENT"
PRICE="3944"
DATE="2026-04-15"
TIME="09:50"

echo "════════════════════════════════════════════════════════════════"
echo "Test: Replay Mode Cache Strategy"
echo "════════════════════════════════════════════════════════════════"
echo ""

# First request (cache miss - should call Kite API)
echo "1️⃣  FIRST REQUEST (Cache Miss - Kite API)"
echo "   Symbol: $SYMBOL | Price: $PRICE | Date: $DATE | Time: $TIME"
echo ""

curl -s -X POST "$BASE_URL/api/validate-entry" \
  -H "Content-Type: application/json" \
  -d @- <<EOF | jq '.data_source, .count, .candles[-1] | {source, count, lastCandle: .}'
{
  "symbol": "$SYMBOL",
  "price": $PRICE,
  "direction": "CALL",
  "interval": "5minute",
  "replay_date": "$DATE",
  "replay_time": "$TIME"
}
EOF

echo ""
echo "   ⏳ Waiting 2 seconds before second request..."
sleep 2
echo ""

# Second request (cache hit - should use SQLite)
echo "2️⃣  SECOND REQUEST (Cache Hit - SQLite)"
echo "   Same parameters as first request"
echo ""

curl -s -X POST "$BASE_URL/api/validate-entry" \
  -H "Content-Type: application/json" \
  -d @- <<EOF | jq '.data_source, .count'
{
  "symbol": "$SYMBOL",
  "price": $PRICE,
  "direction": "CALL",
  "interval": "5minute",
  "replay_date": "$DATE",
  "replay_time": "$TIME"
}
EOF

echo ""
echo "════════════════════════════════════════════════════════════════"
echo "Expected Results:"
echo "  1️⃣  First request: data_source = 'kite_api' or 'sqlite_cache'"
echo "  2️⃣  Second request: data_source = 'sqlite_cache' (should be faster)"
echo "════════════════════════════════════════════════════════════════"
echo ""
