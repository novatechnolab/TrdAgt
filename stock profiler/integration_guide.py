# ══════════════════════════════════════════════════════════════════
# INTEGRATION — add these 3 lines to your app.py
# ══════════════════════════════════════════════════════════════════

# 1. Import (top of app.py)
from market_routes import market_bp, start_market_stream

# 2. Register blueprint (after app = Flask(__name__))
app.register_blueprint(market_bp)

# 3. Start stream (after kite.set_access_token(...) succeeds)
start_market_stream(kite)

# Dashboard: http://localhost:5000/market/


# ══════════════════════════════════════════════════════════════════
# REQUIRED FILES — keep all in same directory as app.py
# ══════════════════════════════════════════════════════════════════
#
#  your_project/
#  ├── app.py
#  ├── market_engine.py      ← core logic (all 4 engines)
#  ├── market_routes.py      ← Flask blueprint + WebSocket + OI poller
#  └── templates/
#      └── dashboard.html    ← live UI


# ══════════════════════════════════════════════════════════════════
# TYPICAL app.py SKELETON
# ══════════════════════════════════════════════════════════════════
"""
from flask import Flask, redirect, request
from kiteconnect import KiteConnect
from market_routes import market_bp, start_market_stream   # ← ADD

app = Flask(__name__)
app.secret_key = "your-secret"
app.register_blueprint(market_bp)                          # ← ADD

kite = KiteConnect(api_key="YOUR_API_KEY")

@app.route("/login")
def login():
    return redirect(kite.login_url())

@app.route("/callback")
def callback():
    data = kite.generate_session(
        request.args["request_token"],
        api_secret="YOUR_SECRET"
    )
    kite.set_access_token(data["access_token"])
    start_market_stream(kite)                              # ← ADD
    return redirect("/market/")

if __name__ == "__main__":
    app.run(debug=True)
"""


# ══════════════════════════════════════════════════════════════════
# QUICK-START TOKENS (NSE Spot)
# ══════════════════════════════════════════════════════════════════
#
#  Symbol        Spot Token    Notes
#  ────────────  ──────────    ──────────────────────────────────
#  NIFTY 50      256265        Index — no OI on spot token
#  BANKNIFTY     260105        Index — no OI on spot token
#  FINNIFTY      257801        Index
#  MIDCPNIFTY    288009        Index
#
# For OI, provide the active FUTURES token as fo_token:
#
#   from kiteconnect import KiteConnect
#   instruments = kite.instruments("NFO")
#
#   # NIFTY futures (nearest expiry)
#   nifty_fut = [i for i in instruments
#                if i["name"] == "NIFTY" and i["instrument_type"] == "FUT"]
#   nifty_fut.sort(key=lambda x: x["expiry"])
#   fo_token = nifty_fut[0]["instrument_token"]
#
#   # Stock F&O example (RELIANCE)
#   nse_instr = kite.instruments("NSE")
#   reliance_spot = next(i for i in nse_instr if i["tradingsymbol"] == "RELIANCE")
#   reliance_fut  = [i for i in instruments
#                    if i["name"] == "RELIANCE" and i["instrument_type"] == "FUT"]
#   reliance_fut.sort(key=lambda x: x["expiry"])
#   spot_token = reliance_spot["instrument_token"]
#   fo_token   = reliance_fut[0]["instrument_token"]


# ══════════════════════════════════════════════════════════════════
# HOW EACH PANEL WORKS
# ══════════════════════════════════════════════════════════════════
#
# ① CVD (Cumulative Volume Delta)
#    Source : Kite WebSocket MODE_FULL → last_price + last_quantity
#    Logic  : uptick = +qty, downtick = −qty, running sum = CVD
#    Signals: BULLISH / BEARISH / NEUTRAL + divergence detection
#    Chart  : CVD line vs Price line
#
# ② VWAP + Volume
#    Source : Same WebSocket ticks (resets on .reset())
#    Logic  : VWAP = Σ(price×vol) / Σ(vol), bands = VWAP ± 1σ
#             Volume trend = recent 10 ticks vs prior 10 ticks
#    Signals: BULLISH / BEARISH / ABOVE_VWAP / BELOW_VWAP / AT_VWAP
#    Chart  : VWAP + ±1σ bands + Price line
#
# ③ Order Book (20-level depth)
#    Source : WebSocket MODE_FULL tick["depth"]["buy"/"sell"]
#    Logic  : Imbalance = total_bid_qty / total_ask_qty
#             Cluster   = level qty > 3× average  (institutional)
#             Wall      = level qty > 5× average  (strong S/R)
#             Absorbed  = wall disappeared AND price traded near it
#    Signals: STRONG_BUY/SELL_PRESSURE, BUY/SELL_PRESSURE,
#             ASK/BID_WALL_ABSORBED, BALANCED
#
# ④ OI + Volume (F&O)
#    Source : kite.quote(fo_token) polled every 5 seconds
#    Logic  : Compares current vs 25s-ago (5 polls) to avoid noise
#             Rising OI + Rising Vol   → FRESH_POSITIONS (strong trend)
#             Rising OI + Falling Vol  → WEAK_TREND
#             Falling OI + Rising Vol  → UNWINDING
#             Falling OI + Falling Vol → INDECISION
#    Chart  : OI line vs Volume line
#
# ◈ OVERALL SIGNAL
#    Majority vote across all 4 panels (max 4 points each side):
#    3+ bull → STRONG_BULL | 2 bull → MILD_BULL | tied → NEUTRAL
