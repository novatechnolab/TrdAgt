#!/bin/bash

echo "Stopping any existing Vite and Uvicorn servers..."
# Cleanly kill processes listening on ports 5000 and 8000
fuser -k 5000/tcp 2>/dev/null || true
fuser -k 8000/tcp 2>/dev/null || true

# Force kill any remaining processes by pattern
pkill -f "uvicorn" || true
pkill -f "vite" || true

sleep 2

echo "Starting Backend (FastAPI) on port 8000..."
nohup /home/rajk/Downloads/TradeSignal-NextGen/backend/.venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload --app-dir /home/rajk/Downloads/TradeSignal-NextGen/backend > backend.log 2>&1 &

echo "Starting Frontend (Vite) on port 5000..."
nohup npm --prefix /home/rajk/Downloads/TradeSignal-NextGen/frontend run dev -- --port 5000 > frontend.log 2>&1 &

sleep 2
echo "Done! Running servers:"
ss -tulpn | grep -E "5000|8000" || true
