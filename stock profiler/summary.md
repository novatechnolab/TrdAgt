# Market Profiler — Summary

A live trading dashboard that plugs into your existing Flask + Kite API app.
Drop 3 files, put dashboard.html in `/templates/`, add 3 lines to `app.py`, visit `/market/`.

---

## What It Does

Streams live NSE/BSE F&O data via Kite WebSocket and displays 4 indicators
per symbol — all updating every second.

---

## 4 Live Panels Per Symbol

| Panel | Signal It Gives |
|---|---|
| **① CVD** | Are buyers or sellers in control? Tick-by-tick volume delta |
| **② VWAP** | Is price above/below fair value with rising volume? |
| **③ Order Book** | Where are institutions? Any walls being absorbed? |
| **④ OI + Volume** | Fresh positions building or trend unwinding? |
| **◈ Overall** | Majority vote across all 4 → STRONG BULL / STRONG BEAR / NEUTRAL |

---

## Key Features

- **Multi-symbol** — add any number of FNO stocks or indices live
- **CVD** — tick-by-tick buy/sell volume delta + divergence detection
- **VWAP** — intraday VWAP + ±1σ bands + volume trend signal + chart
- **Order Book** — full 20-level depth, clusters (3×avg), walls (5×avg), wall absorption with price-proximity check
- **OI + Volume** — polled every 5s, compared over 25s rolling window (not noisy tick-to-tick), 4-state signal
- **3 live charts per symbol** — CVD vs Price, VWAP vs Price with bands, OI vs Volume
- **Composite signal** — all 4 new order book signals (STRONG_BUY_PRESSURE, ASK_WALL_ABSORBED etc.) correctly scored
- **Enter key** submits add-symbol form
- **Reset** clears all 4 engines including order book

---

## Files

| File | Lines | Purpose |
|---|---|---|
| `market_engine.py` | 589 | All logic — CVD, VWAP, OrderBook (20L), OI engines |
| `market_routes.py` | 208 | Flask blueprint + WebSocket stream + OI poller + API routes |
| `templates/dashboard.html` | 609 | Full live UI — 4 panels + 3 charts per symbol |
| `integration_guide.py` | 131 | 3 lines to add + token reference + panel explanations |

---

## Project Structure

```
your_project/
├── app.py
├── market_engine.py
├── market_routes.py
└── templates/
    └── dashboard.html
```

---

## Integration (3 lines in app.py)

```python
from market_routes import market_bp, start_market_stream
app.register_blueprint(market_bp)
start_market_stream(kite)   # call after Kite login
```

Dashboard → `http://localhost:5000/market/`

---

## Bugs Fixed in This Version

| # | Bug | Fix |
|---|---|---|
| 1 | Docstring said 5-level order book | Updated to 20-level |
| 2 | `_overall_signal()` called snapshots twice (double lock) | Snapshots taken once, passed as args |
| 3 | `reset()` didn't reset OrderBookEngine | OrderBook now included in reset |
| 4 | Unused `dtime` import | Removed |
| 5 | OI poller thread could spawn twice | Guarded by separate `_oi_lock` |
| 6 | `kite.quote()` key parsing fragile | Normalised to bare token, handles `"NFO:256265"` format |
| 7 | Imbalance bar showed hardcoded gradient | Bar now correctly reflects live bid% vs ask% |
| 8 | `removeSym()` didn't destroy VWAP chart | All 3 charts now destroyed on remove |
| 9 | Wall absorption triggered on any disappearing level | Cross-checks price proximity to LTP |
| 10 | New book signals not scored in overall signal | All 6 book signals now mapped |
| 11 | OI signal compared tick-to-tick (too noisy) | Now compares over 25s rolling window (5 polls) |
| 12 | No VWAP chart | Added VWAP vs Price + ±1σ bands chart |
| 13 | No Enter key shortcut | Enter key on all 3 input fields submits |
| 14 | `integration_guide.py` missing `market_engine.py` requirement | Added to guide |
