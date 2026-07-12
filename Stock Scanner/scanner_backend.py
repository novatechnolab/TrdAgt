"""
Stock Scanner Backend - Flask API
Integrates with Zerodha Kite API for live market data
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
from kiteconnect import KiteConnect
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import logging

app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Kite Setup ────────────────────────────────────────────────────────────────
# Replace with your actual API key and access token
API_KEY = "your_api_key"
ACCESS_TOKEN = "your_access_token"

kite = KiteConnect(api_key=API_KEY)
kite.set_access_token(ACCESS_TOKEN)

# ─── Helper: Fetch OHLCV + 52W data ────────────────────────────────────────────

def get_historical_data(instrument_token, from_date, to_date, interval="day"):
    try:
        data = kite.historical_data(instrument_token, from_date, to_date, interval)
        return pd.DataFrame(data)
    except Exception as e:
        logger.error(f"Error fetching historical data for {instrument_token}: {e}")
        return None


def compute_ema(series, period=20):
    return series.ewm(span=period, adjust=False).mean()


def analyze_stock(instrument_token, tradingsymbol, filters, mode):
    """
    Fetch data and apply filters for a single stock.
    Returns dict with stock info + filter results, or None if insufficient data.
    """
    today = datetime.now().date()
    from_date_52w = today - timedelta(days=365)
    from_date_hist = today - timedelta(days=60)  # enough for 20 EMA + yesterday

    df = get_historical_data(instrument_token, from_date_hist, today)
    df_52w = get_historical_data(instrument_token, from_date_52w, today)

    if df is None or len(df) < 22:
        return None
    if df_52w is None or len(df_52w) < 2:
        return None

    df = df.sort_values("date").reset_index(drop=True)
    df_52w = df_52w.sort_values("date").reset_index(drop=True)

    # Latest (today's) and yesterday's row
    latest = df.iloc[-1]
    yesterday = df.iloc[-2]

    close = float(latest["close"])
    volume = int(latest["volume"])
    open_ = float(latest["open"])
    high = float(latest["high"])
    low = float(latest["low"])

    prev_close = float(yesterday["close"])
    prev_volume = int(yesterday["volume"])
    prev_high = float(yesterday["high"])
    prev_low = float(yesterday["low"])

    # 52-week high/low
    w52_high = float(df_52w["high"].max())
    w52_low = float(df_52w["low"].min())

    # 20 EMA on close
    df["ema20"] = compute_ema(df["close"], 20)
    ema20 = float(df["ema20"].iloc[-1])

    # ─── Apply filters ──────────────────────────────────────────────────────────
    filter_results = {}
    passed = True

    if mode == "bullish":
        f = filters.get("bullish", {})

        checks = {
            "close_gt_min":     (close > f.get("min_close", 110),               f"Close ₹{close:.2f} > ₹{f.get('min_close',110)}"),
            "volume_gt_min":    (volume > f.get("min_volume", 100000),           f"Vol {volume:,} > {f.get('min_volume',100000):,}"),
            "close_gt_52w":     (close > w52_high * f.get("pct_52w_high", 0.6), f"Close > 52W High × {f.get('pct_52w_high',0.6)}"),
            "volume_surge":     (volume > prev_volume * f.get("volume_multiplier", 2.0), f"Vol {volume:,} > {f.get('volume_multiplier',2)}× prev"),
            "close_gt_prev":    (close > prev_close,                             f"Close ₹{close:.2f} > Prev ₹{prev_close:.2f}"),
            "close_gt_ema20":   (close > ema20,                                  f"Close ₹{close:.2f} > EMA20 ₹{ema20:.2f}"),
            "close_gt_prev_high":(close > prev_high,                             f"Close ₹{close:.2f} > Prev High ₹{prev_high:.2f}"),
        }

        # Only apply enabled filters
        enabled = f.get("enabled", {k: True for k in checks})
        for key, (result, label) in checks.items():
            if enabled.get(key, True):
                filter_results[key] = {"passed": result, "label": label}
                if not result:
                    passed = False
            else:
                filter_results[key] = {"passed": None, "label": label + " (disabled)"}

    else:  # bearish
        f = filters.get("bearish", {})

        checks = {
            "volume_gt_min":    (volume > f.get("min_volume", 100000),           f"Vol {volume:,} > {f.get('min_volume',100000):,}"),
            "close_lt_52w":     (close < w52_low * f.get("pct_52w_low", 1.4),   f"Close < 52W Low × {f.get('pct_52w_low',1.4)}"),
            "volume_surge":     (volume > prev_volume * f.get("volume_multiplier", 2.0), f"Vol {volume:,} > {f.get('volume_multiplier',2)}× prev"),
            "close_lt_prev":    (close < prev_close,                             f"Close ₹{close:.2f} < Prev ₹{prev_close:.2f}"),
            "close_lt_ema20":   (close < ema20,                                  f"Close ₹{close:.2f} < EMA20 ₹{ema20:.2f}"),
            "close_lt_prev_low":(close < prev_low,                               f"Close ₹{close:.2f} < Prev Low ₹{prev_low:.2f}"),
        }

        enabled = f.get("enabled", {k: True for k in checks})
        for key, (result, label) in checks.items():
            if enabled.get(key, True):
                filter_results[key] = {"passed": result, "label": label}
                if not result:
                    passed = False
            else:
                filter_results[key] = {"passed": None, "label": label + " (disabled)"}

    if not passed:
        return None

    change_pct = ((close - prev_close) / prev_close) * 100 if prev_close else 0
    volume_ratio = volume / prev_volume if prev_volume else 0

    return {
        "symbol":        tradingsymbol,
        "close":         round(close, 2),
        "open":          round(open_, 2),
        "high":          round(high, 2),
        "low":           round(low, 2),
        "volume":        volume,
        "prev_close":    round(prev_close, 2),
        "prev_volume":   prev_volume,
        "prev_high":     round(prev_high, 2),
        "prev_low":      round(prev_low, 2),
        "ema20":         round(ema20, 2),
        "52w_high":      round(w52_high, 2),
        "52w_low":       round(w52_low, 2),
        "change_pct":    round(change_pct, 2),
        "volume_ratio":  round(volume_ratio, 2),
        "filter_results": filter_results,
        "mode":          mode,
    }


# ─── Routes ────────────────────────────────────────────────────────────────────

@app.route("/api/instruments", methods=["GET"])
def get_instruments():
    """Return list of NSE instruments for the watchlist selector."""
    try:
        instruments = kite.instruments("NSE")
        result = [
            {"token": i["instrument_token"], "symbol": i["tradingsymbol"], "name": i["name"]}
            for i in instruments
            if i["instrument_type"] == "EQ"
        ]
        return jsonify({"status": "ok", "instruments": result[:500]})  # limit for perf
    except Exception as e:
        logger.error(f"Instruments error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/scan", methods=["POST"])
def scan():
    """
    Run stock scanner.
    Body:
    {
        "mode": "bullish" | "bearish",
        "symbols": ["RELIANCE", "TCS", ...],   // list of NSE symbols
        "filters": {
            "bullish": {
                "min_close": 110,
                "min_volume": 100000,
                "pct_52w_high": 0.6,
                "volume_multiplier": 2.0,
                "enabled": {
                    "close_gt_min": true,
                    "volume_gt_min": true,
                    ...
                }
            },
            "bearish": { ... }
        }
    }
    """
    data = request.get_json()
    mode = data.get("mode", "bullish")
    symbols = data.get("symbols", [])
    filters = data.get("filters", {})

    if not symbols:
        return jsonify({"status": "error", "message": "No symbols provided"}), 400

    # Fetch instrument map
    try:
        all_instruments = kite.instruments("NSE")
        instrument_map = {
            i["tradingsymbol"]: i["instrument_token"]
            for i in all_instruments
            if i["instrument_type"] == "EQ"
        }
    except Exception as e:
        return jsonify({"status": "error", "message": f"Failed to fetch instruments: {e}"}), 500

    results = []
    errors = []

    for symbol in symbols:
        token = instrument_map.get(symbol)
        if not token:
            errors.append({"symbol": symbol, "error": "Instrument not found"})
            continue
        try:
            result = analyze_stock(token, symbol, filters, mode)
            if result:
                results.append(result)
        except Exception as e:
            errors.append({"symbol": symbol, "error": str(e)})
            logger.error(f"Error scanning {symbol}: {e}")

    return jsonify({
        "status": "ok",
        "mode": mode,
        "scanned": len(symbols),
        "matched": len(results),
        "results": results,
        "errors": errors,
        "timestamp": datetime.now().isoformat(),
    })


@app.route("/api/quote", methods=["GET"])
def quote():
    """Quick quote for a single symbol."""
    symbol = request.args.get("symbol")
    if not symbol:
        return jsonify({"status": "error", "message": "symbol required"}), 400
    try:
        q = kite.quote(f"NSE:{symbol}")
        return jsonify({"status": "ok", "data": q})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)
