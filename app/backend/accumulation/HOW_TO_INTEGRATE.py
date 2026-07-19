# ─────────────────────────────────────────────────────────────────────────────
# HOW TO PLUG THE ACCUMULATION SCANNER INTO YOUR EXISTING app.py
# Copy the relevant sections below into your existing Flask app.
# ─────────────────────────────────────────────────────────────────────────────

# 1. COPY THE SCANNER FILES into your project root (same folder as app.py):
#    accumulation_scanner.py, bollinger_squeeze.py, oi_engine.py,
#    historical_profile.py, sector_map.py, event_calendar.py,
#    flask_routes.py, error_handler.py, config.py
#    fno_symbols.json → project root
#    templates/accumulation.html → your templates/ folder

# 2. ADD THESE IMPORTS near the top of your app.py:
from error_handler import setup_logging
from flask_routes import register_accumulation_routes

# 3. CALL setup_logging() BEFORE app = Flask(__name__) or right after:
setup_logging()

# 4. AFTER your `kite` session is authenticated and your helper functions
#    exist, add this ONE call (adjust function names to match yours):

register_accumulation_routes(
    app,                    # your Flask app instance
    kite,                   # authenticated KiteConnect instance
    get_vwap,               # fn: symbol:str -> float (VWAP)
    get_ltp,                # fn: symbol:str -> float (LTP)
    india_vix,              # fn: () -> float (India VIX)
    get_gift_nifty          # fn: () -> float|None  ← optional, pass None if not available
)

# That's it. The scanner registers these routes automatically:
#   GET  /accumulation          → dashboard page
#   GET  /api/accumulation      → full scan JSON (poll every 30s from UI)
#   GET  /api/accumulation/stock/<symbol>
#   GET  /api/accumulation/sector
#   POST /api/symbols/refresh   → trigger FnO list refresh
#   GET  /api/symbols/status
#   POST /api/gift-nifty        → manual Gift Nifty input
#   GET  /api/errors            → last 5 scan errors
#   POST /api/scan/trigger      → force immediate scan (dev/test)

# 5. NIGHTLY PROFILE UPDATE (run after market close, e.g. via cron or APScheduler):
#    from historical_profile import run_nightly_update
#    from sector_map import load_fno_symbols
#    run_nightly_update(kite, load_fno_symbols())

# 6. SUNDAY SYMBOL REFRESH (via APScheduler or cron):
#    from sector_map import refresh_fno_symbols
#    refresh_fno_symbols(kite)

# ── Minimal example if you need stubs for missing helper functions ─────────
# Uncomment and adapt if your app doesn't already have these:

# def get_vwap(symbol: str) -> float:
#     """Return VWAP for symbol — plug into your existing VWAP computation."""
#     try:
#         q = kite.quote([f"NSE:{symbol}"])
#         return q[f"NSE:{symbol}"].get("average_price", 0.0)
#     except Exception:
#         return 0.0

# def get_ltp(symbol: str) -> float:
#     """Return Last Traded Price."""
#     try:
#         q = kite.ltp([f"NSE:{symbol}"])
#         return q[f"NSE:{symbol}"]["last_price"]
#     except Exception:
#         return 0.0

# def india_vix() -> float:
#     """Return India VIX level."""
#     try:
#         q = kite.quote(["NSE:INDIA VIX"])
#         return q["NSE:INDIA VIX"]["last_price"]
#     except Exception:
#         return 15.0

# def get_gift_nifty() -> float | None:
#     """Return Gift Nifty price. Return None if unavailable."""
#     return None   # replace with your data vendor feed if available

# ── Requirements (add to requirements.txt) ───────────────────────────────
# flask
# kiteconnect
# pandas
# numpy
# ─────────────────────────────────────────────────────────────────────────────
