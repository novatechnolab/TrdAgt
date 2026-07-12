# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Application

**Start backend:**
```bash
source .venv/bin/activate
python app/backend/server.py
# Serves on http://localhost:5000 (also serves frontend static files)
```

**Health check:**
```bash
curl http://localhost:5000/api/health
curl http://localhost:5000/api/cache/stats
```

There is no frontend build step — the app uses vanilla HTML/CSS/JS loaded directly by Flask.

## Architecture

TradeSignal is an NSE F&O trading intelligence SPA with a Flask backend that proxies Kite Connect (Zerodha broker API) calls and caches responses in SQLite.

**Data flow:**
```
Browser → Flask (server.py) → Kite Connect SDK → NSE/NFO market data
                           ↕
                  SQLite (tradesignal_cache.db)
```

**Authentication:** Kite API credentials (`api_key`, `access_token`) are stored in browser `localStorage` and sent on every request as HTTP headers (`X-Kite-Api-Key`, `X-Kite-Access-Token`). The backend is stateless — no server-side session.

### Backend (`app/backend/server.py`)

Single Flask file (~918 lines) with 14+ REST endpoints. Key design:
- **Cache-aside pattern:** historical OHLCV data is cached in SQLite with a 12-hour TTL; instruments list is cached for 24 hours
- **Smart cache:** `GET /api/historical` checks SQLite before calling Kite API; `POST /api/cache/clear` purges stale data
- **Batch endpoints:** `/api/batch-snapshots` and `/api/stock-snapshot` aggregate equity + futures + ATM options into one enriched payload

### Frontend (`app/js/`)

Seven vanilla ES6 class modules, no bundler, no framework:

| File | Role |
|------|------|
| `app.js` | SPA controller — manages page routing and wires modules together |
| `kite-api.js` | REST wrapper around all backend endpoints; attempted WebSocket client |
| `scoring-engine.js` | 5-factor equity score + 6-factor options score (momentum, volume, OI, IV) |
| `equity-screener.js` | Scans 200+ F&O stocks sequentially, applies filters, ranks results |
| `options-chain.js` | Renders strike table, computes Max Pain and PCR |
| `charts.js` | TradingView Lightweight Charts candlestick + EMA/RSI/MACD overlays |
| `alerts.js` | Rule-based browser notifications, persisted to localStorage |

### Known Issues (from `CODEBASE_ANALYSIS.md`)

- **WebSocket `/ws/ticks` is not implemented** — real-time tick updates are broken
- **Equity screener scans are sequential** — causes 30+ second UI freezes
- **Fundamentals (P/E, ROE, earnings growth) are hardcoded** — not pulled from any data source
- **No token refresh** — Kite sessions expire after 4 hours
- **Options chain delta is hardcoded to 0** — not calculated from market data
- **Credentials stored in plaintext localStorage** — XSS risk

## Key External Dependency

All market data comes from [Kite Connect](https://kite.trade/docs/connect/v3/) (Zerodha). Users must generate a `request_token` via the Kite login flow and exchange it for an `access_token` via `POST /api/login`. The `access_token` is valid for one trading day.
