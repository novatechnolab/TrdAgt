# TradeSignal Behavioral Blueprint

## Navigation System
- **Pattern**: `app.navigateTo(page)` updates sidebar + page visibility + title
- **Pages**: dashboard, screener, options, scoring, analysis, watchlist, portfolio, live-movers, index-movers, news, strategy, backtest, journal, paper, recommendations, settings, multi-chart, smc-dashboard, fno-session, historical
- **Auto-routing**: URL params (`request_token`, `status`) force navigation to settings
- **State**: `app.currentPage` tracks current page

## Event Binding Pattern
- **Init Order**: Navigation first, then conditional bindings based on page
- **Method**: `bind[FeatureName]()` functions attach event listeners
- **Fallback**: Try-catch wraps non-critical bindings

## Data Flow Patterns
1. **API Calls**: `app.apiFetch(endpoint, options)` with credentials
2. **Real-time**: Socket.IO for live data updates
3. **State**: Global objects store data (app.stockData, etc.)
4. **Persistence**: localStorage for settings

## Critical Behaviors to Preserve
1. **Auto-login flow**: Check URL params, generate session, load instruments
2. **Search context**: Different actions based on current page (multi-chart vs scoring vs historical)
3. **Watchlist toggles**: Inline star buttons with event.stopPropagation()
4. **Lazy loading**: Charts initialize only when needed
5. **Background processes**: Instrument loading doesn't block UI

## Component Interactions
- **Dashboard**: Run scoring, refresh breadth, leaderboard tabs, sector sorting
- **Screener**: Sector/signal filters, sorting, run scan
- **Scoring**: Symbol input, score calculation, result display
- **Charts**: Historical data, technical indicators, multi-chart tracking

## State Management
- **Global State**: app object with currentPage, stockData, scoringMode, etc.
- **Module State**: Each module (equityScreener, chartManager) manages its own state
- **Event Bus**: Custom events for cross-module communication
