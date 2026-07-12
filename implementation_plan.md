# TradeSignal — Master Implementation Plan

## Current App Snapshot

````carousel
![Dashboard — summary cards, sector table, score leaderboard](/home/rajk/.gemini/antigravity/brain/9d1c6cdc-6574-46bd-a41f-e884012eecbe/dashboard_page_1775329724947.png)
<!-- slide -->
![Stock Analysis — search + Pre-Market/Live toggle](/home/rajk/.gemini/antigravity/brain/9d1c6cdc-6574-46bd-a41f-e884012eecbe/stock_analysis_page_1775329792540.png)
<!-- slide -->
![Equity Screener](/home/rajk/.gemini/antigravity/brain/9d1c6cdc-6574-46bd-a41f-e884012eecbe/equity_screener_page_1775329738215.png)
<!-- slide -->
![Options Chain](/home/rajk/.gemini/antigravity/brain/9d1c6cdc-6574-46bd-a41f-e884012eecbe/options_chain_page_1775329760508.png)
<!-- slide -->
![Score Engine](/home/rajk/.gemini/antigravity/brain/9d1c6cdc-6574-46bd-a41f-e884012eecbe/score_engine_page_1775329776985.png)
<!-- slide -->
![Recommendations](/home/rajk/.gemini/antigravity/brain/9d1c6cdc-6574-46bd-a41f-e884012eecbe/recommendations_page_1775329809453.png)
<!-- slide -->
![Alerts](/home/rajk/.gemini/antigravity/brain/9d1c6cdc-6574-46bd-a41f-e884012eecbe/alerts_page_1775329823891.png)
<!-- slide -->
![Settings](/home/rajk/.gemini/antigravity/brain/9d1c6cdc-6574-46bd-a41f-e884012eecbe/settings_page_1775329841621.png)
````

## Architecture

| Layer | Files |
|---|---|
| **Frontend** | [index.html](file:///home/rajk/Downloads/TradeSignal/app/index.html), [main.css](file:///home/rajk/Downloads/TradeSignal/app/styles/main.css) |
| **Core JS** | [app.js](file:///home/rajk/Downloads/TradeSignal/app/js/app.js) (52KB, main controller) |
| **Modules** | [scoring-engine.js](file:///home/rajk/Downloads/TradeSignal/app/js/scoring-engine.js), [equity-screener.js](file:///home/rajk/Downloads/TradeSignal/app/js/equity-screener.js), [analysis.js](file:///home/rajk/Downloads/TradeSignal/app/js/analysis.js), [options-chain.js](file:///home/rajk/Downloads/TradeSignal/app/js/options-chain.js), [charts.js](file:///home/rajk/Downloads/TradeSignal/app/js/charts.js), [alerts.js](file:///home/rajk/Downloads/TradeSignal/app/js/alerts.js), [kite-api.js](file:///home/rajk/Downloads/TradeSignal/app/js/kite-api.js) |
| **Backend** | [server.py](file:///home/rajk/Downloads/TradeSignal/app/backend/server.py) (47KB, Flask API + SQLite cache) |

---

## Phase 1 — Quick Wins (1–2 sessions)

> [!TIP]
> These use data already available in the app and require minimal new infrastructure.

---

### Feature 1: Risk Management on Recommendations

Add SL, target, position sizing, and R:R ratio to every recommendation card.

#### [MODIFY] [scoring-engine.js](file:///home/rajk/Downloads/TradeSignal/app/js/scoring-engine.js)
- [scoreEquity()](file:///home/rajk/Downloads/TradeSignal/app/js/scoring-engine.js#117-286) → compute and return: `stopLoss` (last close − 1.5×ATR), `target1` (last close + 2×ATR), `target2` (last close + 3×ATR), `riskReward` ratio
- [scoreOptions()](file:///home/rajk/Downloads/TradeSignal/app/js/scoring-engine.js#287-545) → same pattern using ITM/OTM reference
- Add `computePositionSize(capital, riskPct, entryPrice, stopLoss)` method

#### [MODIFY] [app.js](file:///home/rajk/Downloads/TradeSignal/app/js/app.js)
- Update recommendation card rendering to show SL/Target/R:R
- Add capital input field in Settings page for position sizing

#### [MODIFY] [analysis.js](file:///home/rajk/Downloads/TradeSignal/app/js/analysis.js)
- [_render()](file:///home/rajk/Downloads/TradeSignal/app/js/analysis.js#328-444) → show SL/Target row below each recommendation card with entry/SL/T1/T2

#### [MODIFY] [main.css](file:///home/rajk/Downloads/TradeSignal/app/styles/main.css)
- Add `.risk-row`, `.sl-badge`, `.target-badge`, `.rr-badge` styles

---

### Feature 2: Watchlist

Personal stock watchlist with localStorage persistence.

#### [NEW] [watchlist.js](file:///home/rajk/Downloads/TradeSignal/app/js/watchlist.js)
- `class Watchlist` — CRUD methods for managing stocks
- localStorage key: `tradesignal_watchlist`
- Methods: [add(symbol)](file:///home/rajk/Downloads/TradeSignal/app/js/alerts.js#13-29), [remove(symbol)](file:///home/rajk/Downloads/TradeSignal/app/js/alerts.js#30-35), `getAll()`, `has(symbol)`
- Render watchlist cards with mini-scores and quick-analyse button

#### [MODIFY] [index.html](file:///home/rajk/Downloads/TradeSignal/app/index.html)
- Add sidebar nav item: `Watchlist` under MAIN section
- Add page section `#page-watchlist` with watchlist card grid

#### [MODIFY] [app.js](file:///home/rajk/Downloads/TradeSignal/app/js/app.js)
- Add "★ Watch" toggle button on screener/analysis results
- Route `page-watchlist` navigation

---

### Feature 3: Market Status Header

Show Nifty/BankNifty live + India VIX in the header bar.

#### [MODIFY] [server.py](file:///home/rajk/Downloads/TradeSignal/app/backend/server.py)
- New endpoint `GET /api/market-pulse` → returns Nifty, BankNifty, India VIX quotes from Kite, or last cached values

#### [MODIFY] [app.js](file:///home/rajk/Downloads/TradeSignal/app/js/app.js)
- Update header bar: replace static `NIFTY — —` with live values from `/api/market-pulse`
- Poll every 15s during market hours, show last-known after hours
- Add VIX gauge (green < 15, yellow 15-25, red > 25)

---

## Phase 2 — Core Features (3–4 sessions)

---

### Feature 4: Portfolio Tracker & P&L

Fetch holdings/positions from Kite, show real-time P&L.

#### [MODIFY] [server.py](file:///home/rajk/Downloads/TradeSignal/app/backend/server.py)
- New endpoints:
  - `GET /api/portfolio/holdings` → `kite.holdings()` 
  - `GET /api/portfolio/positions` → `kite.positions()`
  - `GET /api/portfolio/summary` → aggregated P&L, sector allocation

#### [NEW] [portfolio.js](file:///home/rajk/Downloads/TradeSignal/app/js/portfolio.js)
- `class Portfolio` — fetch and render holdings/positions
- Day P&L, overall P&L, sector pie chart (using lightweight-charts)
- Portfolio heat map: color grid by day P&L per stock
- Concentration risk: warn if any single stock > 20% of portfolio

#### [MODIFY] [index.html](file:///home/rajk/Downloads/TradeSignal/app/index.html)
- New sidebar item + page section `#page-portfolio`

---

### Feature 5: News & Sentiment Feed

RSS aggregation + sentiment scoring.

#### [MODIFY] [server.py](file:///home/rajk/Downloads/TradeSignal/app/backend/server.py)
- New endpoint: `GET /api/news?symbol=RELIANCE`
- Backend polls RSS feeds (Livemint, ET, MoneyControl) using `feedparser`
- Run FinBERT sentiment on headlines (or simpler rule-based if FinBERT too heavy for Termux)
- Cache results in SQLite `news` table, refresh every 5 min
- Return: `{headlines: [{title, source, url, sentiment, score, time}]}`

#### [NEW] [news-feed.js](file:///home/rajk/Downloads/TradeSignal/app/js/news-feed.js)
- Render news cards with sentiment badges (🟢 Bullish / 🔴 Bearish / ⚪ Neutral)
- Aggregate sentiment gauge on stock analysis page
- Filter by stock symbol

#### [MODIFY] [analysis.js](file:///home/rajk/Downloads/TradeSignal/app/js/analysis.js)
- Add news sentiment section below analysis results
- Show top 5 recent headlines for the analysed stock

#### [MODIFY] [index.html](file:///home/rajk/Downloads/TradeSignal/app/index.html)
- New sidebar item "News" + page section `#page-news`

---

### Feature 6: Intraday Screener (Live Movers)

Real-time movers during market hours.

#### [MODIFY] [server.py](file:///home/rajk/Downloads/TradeSignal/app/backend/server.py)
- New endpoint: `GET /api/live-movers`
- Batch quote all F&O stocks, sort by gainers/losers/volume-buzzers/OI-spikes

#### [NEW] [live-movers.js](file:///home/rajk/Downloads/TradeSignal/app/js/live-movers.js)
- Tabs: Top Gainers | Top Losers | Volume Buzzers | OI Spikes | 52W Breakouts
- Auto-refresh every 15s during market hours
- Click any row → jump to Stock Analysis

#### [MODIFY] [index.html](file:///home/rajk/Downloads/TradeSignal/app/index.html)
- New sidebar item "Live Movers" with 🔥 icon

---

## Phase 3 — Pro Features (4–5 sessions)

---

### Feature 7: Options Strategy Builder + Payoff Diagrams

Multi-leg strategy creation with visual payoff.

#### [NEW] [strategy-builder.js](file:///home/rajk/Downloads/TradeSignal/app/js/strategy-builder.js)
- Strategy leg editor: add CE/PE buy/sell at chosen strike/premium
- Pre-built templates: Bull Call Spread, Bear Put Spread, Iron Condor, Straddle, Strangle, Butterfly
- Payoff diagram: canvas-based P&L chart across price range
- Greeks calculator: Net Delta, Gamma, Theta, Vega for combined position
- Breakeven, max profit, max loss computation
- Suggested strategies based on scoring engine output (high IV → sell straddle, etc.)

#### [MODIFY] [server.py](file:///home/rajk/Downloads/TradeSignal/app/backend/server.py)
- New endpoint: `GET /api/options/strikes?symbol=RELIANCE&expiry=2026-04-24`
- Returns all strikes with LTP, IV, Greeks for strategy builder

#### [MODIFY] [index.html](file:///home/rajk/Downloads/TradeSignal/app/index.html)
- New sidebar item "Strategy Builder" with ⚡ icon + page section

---

### Feature 8: Backtesting Engine

Test scoring signals against historical OHLCV.

#### [NEW] [backtester.js](file:///home/rajk/Downloads/TradeSignal/app/js/backtester.js)
- Inputs: entry rule (score > X + direction), exit rule (SL/target/time), date range
- Run against cached OHLCV (already ~365 days in SQLite)
- Output: win rate, avg return, max drawdown, Sharpe ratio, trade log
- Equity curve chart (using lightweight-charts)
- Compare multiple strategies side-by-side

#### [MODIFY] [server.py](file:///home/rajk/Downloads/TradeSignal/app/backend/server.py)
- New endpoint: `POST /api/backtest` → run backtest server-side for speed
- Query OHLCV cache, apply entry/exit rules, return results

#### [MODIFY] [index.html](file:///home/rajk/Downloads/TradeSignal/app/index.html)
- New sidebar item "Backtest" + page section

---

### Feature 9: Advanced Technical Indicators

Overlay additional indicators on charts.

#### [MODIFY] [charts.js](file:///home/rajk/Downloads/TradeSignal/app/js/charts.js)
- Add indicator overlays: Bollinger Bands, VWAP, Supertrend, Fibonacci Retracement
- Add indicator toggle panel (checkboxes) below chart
- Each indicator as a `lightweight-charts` line/area series

#### [MODIFY] [scoring-engine.js](file:///home/rajk/Downloads/TradeSignal/app/js/scoring-engine.js)
- Add compute methods: `computeBollingerBands()`, `computeVWAP()`, `computeSupertrend()`

---

## Phase 4 — Premium Features (5+ sessions)

---

### Feature 10: AI-Powered Research Assistant

Chat interface for stock research powered by local LLM.

#### [NEW] [research-ai.js](file:///home/rajk/Downloads/TradeSignal/app/js/research-ai.js)
- Chat UI in a slide-out panel (right sidebar or modal)
- Send queries with context: OHLCV, scores, news, analyst data
- Stream responses from backend LLM endpoint

#### [MODIFY] [server.py](file:///home/rajk/Downloads/TradeSignal/app/backend/server.py)
- New endpoint: `POST /api/ai/research`
- Build context from: OHLCV cache, scoring results, news sentiment, analyst ratings
- Forward to Ollama API (Llama3) at `http://localhost:11434/api/generate`
- Stream response back to frontend via SSE

---

### Feature 11: Multi-Timeframe Analysis

Score across 15min, 1hr, daily, weekly intervals.

#### [MODIFY] [analysis.js](file:///home/rajk/Downloads/TradeSignal/app/js/analysis.js)
- Fetch OHLCV for multiple intervals, run scoring on each
- Display confluence matrix: rows = timeframes, cols = indicators
- Highlight when all timeframes agree (strongest signal)

#### [MODIFY] [server.py](file:///home/rajk/Downloads/TradeSignal/app/backend/server.py)
- Update `/api/stock-analysis` to accept `intervals` parameter
- Return OHLCV for each requested interval

---

### Feature 12: Trade Journal

Track and analyse personal trades.

#### [NEW] [trade-journal.js](file:///home/rajk/Downloads/TradeSignal/app/js/trade-journal.js)
- Manual entry: symbol, entry/exit price, qty, rationale, tags
- Auto-import from Kite order history (`kite.orders()`)
- Analytics: win rate, P&L by sector/strategy/day, monthly calendar
- SQLite table: `trade_journal`

---

### Feature 13: Sector & Market Breadth Dashboard

#### [MODIFY] [app.js](file:///home/rajk/Downloads/TradeSignal/app/js/app.js)
- Enhance existing Sector Performance table with breadth metrics
- Add advance/decline ratio, FII/DII flow data
- Sector rotation heat map visualization

---

### Feature 14: Earnings Calendar

#### [MODIFY] [server.py](file:///home/rajk/Downloads/TradeSignal/app/backend/server.py)
- New endpoint: `GET /api/earnings-calendar`
- Scrape NSE corporate actions for upcoming results dates

#### [MODIFY] [analysis.js](file:///home/rajk/Downloads/TradeSignal/app/js/analysis.js)
- Show "Earnings in X days" badge on analysis results

---

### Feature 15: Paper Trading

#### [NEW] [paper-trading.js](file:///home/rajk/Downloads/TradeSignal/app/js/paper-trading.js)
- Virtual portfolio with configurable starting capital
- Execute buy/sell at live price, track P&L
- SQLite table: `paper_trades`

---

## Verification Plan

### Automated Tests
- Each new endpoint: `curl` test with expected response shape
- Browser tests: navigate to each new page, verify data renders
- Scoring engine: verify SL/target computation with known ATR values

### Manual Verification
- Run each feature with Kite connected (live) and disconnected (cache)
- Mobile responsiveness check on Termux
- Verify localStorage persistence for watchlist across browser restarts

---

## Phased Delivery Summary

| Phase | Features | New Files | Modified Files | Sessions |
|---|---|---|---|---|
| **1 — Quick Wins** | Risk Mgmt, Watchlist, Market Header | 1 | 5 | 1–2 |
| **2 — Core** | Portfolio, News, Live Movers | 3 | 4 | 3–4 |
| **3 — Pro** | Strategy Builder, Backtest, Indicators | 2 | 4 | 4–5 |
| **4 — Premium** | AI Research, Multi-TF, Journal, Breadth, Earnings, Paper | 3 | 4 | 5+ |
