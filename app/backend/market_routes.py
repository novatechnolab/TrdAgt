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

import os
import threading
import time
from flask import Blueprint, jsonify, request, render_template, session as flask_session
from market_engine import MarketRegistry
try:
    from session_utils import get_session_mode, is_market_hours
except ImportError:
    # Fallback if session_utils not on path
    import datetime
    def get_session_mode():
        n = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
        if n.weekday() >= 5: return 'historical'
        m = n.hour * 60 + n.minute
        if 555 <= m <= 930: return 'live'       # 9:15–15:30
        if 540 <= m < 555:  return 'premarket'  # 9:00–9:15
        return 'historical'
    def is_market_hours():
        return get_session_mode() == 'live'

# ── Shared registry ───────────────────────────────────────────────────────────
market_registry = MarketRegistry()
market_bp       = Blueprint("market", __name__, url_prefix="/market")

# ── Streaming state ───────────────────────────────────────────────────────────
_ticker      = None
_oi_thread   = None
_ticker_lock = threading.Lock()
_oi_lock     = threading.Lock()
OI_POLL_SEC  = 5
_kite_ref    = None

# ── NFO instruments cache (populated lazily on first subscribe) ───────────────
_nfo_cache      : list = []
_nfo_cache_lock = threading.Lock()


def _resolve_fo_token(symbol: str) -> int | None:
    """
    Find the nearest-expiry NSE stock/index futures token from NFO instruments.
    Uses a module-level cache refreshed at most once per session to avoid
    repeated API calls.

    Returns the instrument_token (int) of the front-month FUT, or None if not found.
    """
    global _nfo_cache
    with _nfo_cache_lock:
        if not _nfo_cache and _kite_ref is not None:
            try:
                from db_instruments import get_cached_instruments
                _nfo_cache = get_cached_instruments('NFO')
                print(f'[MARKET] NFO cache loaded from DB: {len(_nfo_cache)} instruments')
            except Exception as e:
                print(f'[MARKET] NFO instrument fetch failed: {e}')
                return None
        cache = list(_nfo_cache)

    if not cache:
        return None

    # Normalise: strip spaces so "NIFTY 50" → "NIFTY50", "NIFTY BANK" → "NIFTYBANK"
    # Kite NFO names use plain symbols like NIFTY, BANKNIFTY, ONGC etc.
    clean = symbol.replace(' ', '').upper()
    # Map common index aliases
    alias = {
        'NIFTY50': 'NIFTY', 'NIFTYBANK': 'BANKNIFTY',
        'NIFTYFINSERVICE': 'FINNIFTY', 'NIFTYMIDCAPSELECT': 'MIDCPNIFTY',
        'NIFTY100': 'NIFTY100', 'NIFTYIT': 'NIFTYIT',
    }
    name = alias.get(clean, clean)

    import datetime
    today_str = datetime.date.today().strftime('%Y-%m-%d')

    # Gather all FUT contracts for this name
    futs = []
    for i in cache:
        if (i.get('instrument_type') == 'FUT'
            and i.get('name', '').upper() == name
            and i.get('segment') == 'NFO-FUT'):
            exp = i.get('expiry')
            if exp:
                exp_str = exp.isoformat() if hasattr(exp, 'isoformat') else str(exp).split('T')[0]
                if exp_str >= today_str:
                    futs.append((exp_str, i))
    if not futs:
        return None

    # Sort by expiry (nearest first)
    futs.sort(key=lambda x: x[0])
    tok = futs[0][1].get('instrument_token')
    exp = futs[0][1].get('expiry')
    sym = futs[0][1].get('tradingsymbol')
    print(f'[MARKET] Resolved fo_token for {symbol}: {sym} (token={tok}, expiry={exp})')
    return int(tok) if tok else None


def _ensure_kite():
    """
    Lazy Kite initialization called at the start of every market API request.
    - If _kite_ref is already set (normal flow after login) this is a no-op.
    - If the server restarted OR user navigated directly to market-profiler.html,
      this reconstructs KiteConnect from the Flask session cookie so data flows
      without requiring a manual re-login on the main dashboard.
    Priority: central server.get_kite()
    """
    global _kite_ref
    if _kite_ref is not None:
        return  # already initialised
    try:
        from server import get_kite
        kite = get_kite()
        if kite:
            start_market_stream(kite)
            print('[MARKET] _ensure_kite: verified stream restarted.')
    except Exception as e:
        print(f'[MARKET] _ensure_kite failed: {e}')


def start_market_stream(kite):
    """Call once after Kite login. Safe to call only once."""
    global _ticker, _oi_thread, _kite_ref

    with _ticker_lock:
        if _ticker is not None:
            return   # already running

    if not kite:
        print("[MARKET] Stream initialization deferred: no Kite client.")
        return

    try:
        # Verify connectivity before starting market ticker/polling stream
        kite.profile()
    except Exception as e:
        print(f"[MARKET] Stream initialization deferred: connectivity check failed: {e}")
        _kite_ref = None
        return

    try:
        from kiteconnect import KiteTicker
    except ImportError:
        print("[MARKET] pip install kiteconnect")
        return

    _kite_ref = kite

    # ── WebSocket ticker ──────────────────────────────────────────────────────
    with _ticker_lock:

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

        syms   = market_registry.list_symbols()
        tokens = [s["token"] for s in syms]
        from global_ticker import get_ticker_for_feature
        _ticker            = get_ticker_for_feature("market_stream", tokens, on_ticks, mode="FULL")
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
    """Background thread: polls kite.quote() for OI every OI_POLL_SEC.

    kite.quote() accepts raw integer instrument tokens.
    Response is keyed by "EXCHANGE:TRADINGSYMBOL"; match back via
    quote["instrument_token"] which Kite always includes in the body.
    """
    while True:
        time.sleep(OI_POLL_SEC)
        if _kite_ref is None:
            continue
        try:
            syms = market_registry.list_symbols()
            if not syms:
                continue

            # query_token (int) → spot_token (int)
            # Use fo_token (futures) when available, else spot token
            tok_to_spot = {}
            for s in syms:
                fo  = s.get("fo_token")
                tok = int(s["token"])
                tok_to_spot[int(fo) if fo else tok] = tok

            if not tok_to_spot:
                continue

            quotes = _kite_ref.quote(list(tok_to_spot.keys()))

            for _key, quote in quotes.items():
                inst_tok = quote.get("instrument_token")
                if inst_tok is not None:
                    spot_tok = tok_to_spot.get(int(inst_tok))
                    if spot_tok is not None:
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
      "exchange": "NSE",         ← (optional, default NSE)
      "fo_token": 11592706       ← (optional) active futures token for OI polling;
                                    auto-resolved from NFO if omitted
    }
    """
    _ensure_kite()  # make sure _kite_ref is set
    d        = request.get_json(force=True)
    symbol   = d.get("symbol", "").strip().upper()
    token    = d.get("token")
    fo_token = d.get("fo_token")
    exchange = d.get("exchange", "NSE")

    if not symbol or not token:
        return jsonify({"error": "symbol and token required"}), 400

    # Auto-resolve fo_token from NFO instruments when not supplied by caller.
    # Indices (VIX, etc.) won't have a FUT contract — _resolve_fo_token returns None.
    if fo_token is None:
        fo_token = _resolve_fo_token(symbol)

    market_registry.add(symbol, int(token), exchange,
                        int(fo_token) if fo_token else None)
    _subscribe(int(token))
    return jsonify({"status": "ok", "symbol": symbol, "fo_token": fo_token})


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


@market_bp.route("/api/status")
def api_status():
    """Returns current session mode + Kite connection state for the frontend."""
    _ensure_kite()  # Lazy-init from session cookie if needed
    session = get_session_mode()
    kite_ok = _kite_ref is not None
    return jsonify({"session": session, "market_open": session == 'live', "kite_ok": kite_ok})


@market_bp.route("/api/all")
def api_all():
    """Returns all symbol snapshots enriched with session context + premarket quotes."""
    _ensure_kite()  # Lazy-init from session cookie if needed
    session     = get_session_mode()
    market_open = (session == 'live')
    snapshots  = market_registry.all_snapshots()

    # ── Off-hours: enrich each snapshot with previous-close data from kite.quote()
    premarket_quotes = {}
    if not market_open and _kite_ref is not None:
        try:
            syms = market_registry.list_symbols()
            if syms:
                # Use exchange-prefixed keys for better kite.quote() compatibility
                token_map = {f"{s['exchange']}:{s['symbol']}": s['token'] for s in syms}
                raw = _kite_ref.quote(list(token_map.keys()))
                for key, q in raw.items():
                    tok = token_map.get(key)
                    if tok:
                        ohlc = q.get('ohlc', {})
                        premarket_quotes[tok] = {
                            'last_price' : q.get('last_price'),
                            'prev_close' : ohlc.get('close'),
                            'open'       : ohlc.get('open'),
                            'high'       : ohlc.get('high'),
                            'low'        : ohlc.get('low'),
                            'net_change' : q.get('net_change'),
                            'change_pct' : round(q.get('change', 0), 2),
                            'volume'     : q.get('volume'),
                            'oi'         : q.get('oi'),
                        }
        except Exception as e:
            print(f'[MARKET] premarket quote error: {e}')

    # Inject session + premarket fields into every snapshot
    for snap in snapshots:
        snap['session']     = session
        snap['market_open'] = market_open
        tok = snap.get('token')
        if tok in premarket_quotes:
            snap['premarket'] = premarket_quotes[tok]

    return jsonify(snapshots)


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
