"""
market_routes.py
────────────────
Unified Flask blueprint: CVD + VWAP + Order Book (20-level) + OI/Volume

In app.py add:
    from market_routes import market_bp, start_market_stream
    app.register_blueprint(market_bp)
    # after kite login:
    start_market_stream(kite)

Dashboard at: http://localhost:5000/market/
Requires: market_engine.py in the same directory.
"""

import threading
import time
from flask import Blueprint, jsonify, request, render_template
from market_engine import MarketRegistry

# ── Shared registry ───────────────────────────────────────────────────────────
market_registry = MarketRegistry()
market_bp       = Blueprint("market", __name__, url_prefix="/market")

# ── Streaming state ───────────────────────────────────────────────────────────
_ticker      = None
_oi_thread   = None
_ticker_lock = threading.Lock()
_oi_lock     = threading.Lock()   # FIX: separate lock for OI thread guard
OI_POLL_SEC  = 5
_kite_ref    = None


def start_market_stream(kite):
    """Call once after Kite login. Safe to call only once."""
    global _ticker, _oi_thread, _kite_ref

    try:
        from kiteconnect import KiteTicker
    except ImportError:
        print("[MARKET] pip install kiteconnect")
        return

    _kite_ref = kite

    # ── WebSocket ticker ──────────────────────────────────────────────────────
    with _ticker_lock:
        if _ticker is not None:
            return   # already running

        def on_ticks(ws, ticks):
            for tick in ticks:
                market_registry.process_tick(tick)

        def on_connect(ws, response):
            syms   = market_registry.list_symbols()
            tokens = [s["token"] for s in syms]
            if tokens:
                ws.subscribe(tokens)
                ws.set_mode(ws.MODE_FULL, tokens)
            print(f"[MARKET] WebSocket connected. Tokens: {tokens}")

        def on_error(ws, code, reason):
            print(f"[MARKET] WS error {code}: {reason}")

        def on_close(ws, code, reason):
            print(f"[MARKET] WS closed {code}: {reason}")

        _ticker            = KiteTicker(kite.api_key, kite.access_token)
        _ticker.on_ticks   = on_ticks
        _ticker.on_connect = on_connect
        _ticker.on_error   = on_error
        _ticker.on_close   = on_close

        threading.Thread(
            target=_ticker.connect,
            kwargs={"threaded": True},
            daemon=True,
        ).start()

    # ── OI poller — FIX: guarded by separate lock so only one thread spawns ──
    with _oi_lock:
        if _oi_thread is None or not _oi_thread.is_alive():
            _oi_thread = threading.Thread(target=_oi_poller, daemon=True)
            _oi_thread.start()

    print("[MARKET] Stream started.")


def _oi_poller():
    """Background thread: polls kite.quote() for OI every OI_POLL_SEC."""
    while True:
        time.sleep(OI_POLL_SEC)
        if _kite_ref is None:
            continue
        try:
            syms = market_registry.list_symbols()
            if not syms:
                continue

            # FIX: kite.quote() needs string instrument keys e.g. "NFO:256265"
            # Build map: quote_key → spot_token for registry lookup
            token_map = {}
            for s in syms:
                fo  = s.get("fo_token")
                tok = s["token"]
                # Use fo_token for OI if available, else spot token
                query_tok = fo if fo else tok
                token_map[str(query_tok)] = tok   # str key → spot token

            quotes = _kite_ref.quote(list(token_map.keys()))

            for key, quote in quotes.items():
                # Kite returns keys as "NFO:256265" or just "256265"
                # normalise to bare token int
                bare = key.split(":")[-1]
                spot_tok = token_map.get(bare) or token_map.get(key)
                if spot_tok:
                    market_registry.update_oi(spot_tok, quote)

        except Exception as e:
            print(f"[MARKET] OI poll error: {e}")


def _subscribe(token: int):
    with _ticker_lock:
        if _ticker:
            _ticker.subscribe([token])
            _ticker.set_mode(_ticker.MODE_FULL, [token])


def _unsubscribe(token: int):
    with _ticker_lock:
        if _ticker:
            _ticker.unsubscribe([token])


# ── API Routes ────────────────────────────────────────────────────────────────

@market_bp.route("/api/symbols")
def api_symbols():
    return jsonify(market_registry.list_symbols())


@market_bp.route("/api/subscribe", methods=["POST"])
def api_subscribe():
    """
    Body: {
      "symbol"  : "NIFTY 50",
      "token"   : 256265,        ← spot/index instrument token (WebSocket)
      "fo_token": 11592706       ← (optional) active futures token for OI polling
    }
    """
    d        = request.get_json(force=True)
    symbol   = d.get("symbol", "").strip().upper()
    token    = d.get("token")
    fo_token = d.get("fo_token")

    if not symbol or not token:
        return jsonify({"error": "symbol and token required"}), 400

    market_registry.add(symbol, int(token),
                        int(fo_token) if fo_token else None)
    _subscribe(int(token))
    return jsonify({"status": "ok", "symbol": symbol})


@market_bp.route("/api/unsubscribe", methods=["POST"])
def api_unsubscribe():
    d     = request.get_json(force=True)
    token = d.get("token")
    if not token:
        return jsonify({"error": "token required"}), 400
    _unsubscribe(int(token))
    market_registry.remove(int(token))
    return jsonify({"status": "ok"})


@market_bp.route("/api/snapshot/<symbol>")
def api_snapshot(symbol):
    p = market_registry.get(symbol)
    if not p:
        return jsonify({"error": f"{symbol} not tracked"}), 404
    return jsonify(p.full_snapshot())


@market_bp.route("/api/all")
def api_all():
    return jsonify(market_registry.all_snapshots())


@market_bp.route("/api/reset/<symbol>", methods=["POST"])
def api_reset(symbol):
    p = market_registry.get(symbol)
    if not p:
        return jsonify({"error": "not found"}), 404
    p.reset()   # resets CVD + VWAP + OrderBook + OI
    return jsonify({"status": "reset"})


# ── Dashboard ─────────────────────────────────────────────────────────────────

@market_bp.route("/")
def dashboard():
    return render_template("dashboard.html")

# NOTE: place dashboard.html inside your Flask app's /templates/ folder
# NOTE: place market_engine.py in the same directory as market_routes.py
