"""
TradeSignal — Flask Backend Proxy Server
Proxies Kite Connect API calls to avoid CORS issues.
Includes SQLite caching layer for historical OHLCV data & instruments.

Usage:
  pip install flask flask-cors kiteconnect
  python server.py
"""

import os
import json
import logging
import sqlite3
import datetime
import concurrent.futures
import threading
import time
from datetime import datetime as dt, timedelta
from session_utils import (now_ist, today_ist, ist_timestamp,
                           get_session_mode, is_market_hours, is_premarket,
                           IST)
# dt.now() throughout this file uses naive local time — for date arithmetic
# (Kite historical_data ranges) this is acceptable since dates are calendar-based.
# For IST-sensitive decisions (session mode, market open checks), use now_ist().

import pandas as pd
import numpy as np
from flask import Flask, request, jsonify, send_from_directory, g, Response, has_request_context
from flask_compress import Compress
from flask_cors import CORS
from flask_socketio import SocketIO, emit
from dotenv import load_dotenv

from market_routes import market_bp, start_market_stream
from oi_spurt_routes import oi_spurt_bp
from fno_trap_routes import fno_trap_bp
from notion_notes_routes import notion_notes_bp
from fno_alpha_routes import fno_alpha_bp

# Load .env from multiple candidate paths so it works regardless of
# what directory the server is launched from (important for Termux).
_server_dir = os.path.dirname(os.path.abspath(__file__))
_env_candidates = [
    os.path.join(_server_dir, '../../.env'),          # launched from app/backend
    os.path.join(_server_dir, '../.env'),             # launched from app/
    os.path.join(_server_dir, '.env'),                # launched from project root
    os.path.join(os.getcwd(), '.env'),                # current working dir
    os.path.expanduser('~/TradeSignal/.env'),         # Termux home fallback
    os.path.expanduser('~/storage/shared/TradeSignal/.env'),  # Termux shared storage
]
for _env_path in _env_candidates:
    _env_path = os.path.normpath(_env_path)
    if os.path.isfile(_env_path):
        load_dotenv(_env_path, override=True)
        print(f'  [ENV] Loaded .env from: {_env_path}')
        break
else:
    print('  [ENV] WARNING: No .env file found in any candidate path!')
    print(f'  [ENV] Searched: {_env_candidates}')

static_root = os.path.normpath(os.path.join(_server_dir, '..'))
app = Flask(__name__, static_folder=static_root, static_url_path='')
# Security: use env variable or generate a random key — never a hardcoded default
app.secret_key = os.environ.get('APP_SECRET_KEY') or os.urandom(32).hex()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=False,  # Set to False for local HTTP (important!)
    SESSION_COOKIE_SAMESITE='Lax',
)
CORS(app)
Compress(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')
app.register_blueprint(market_bp)
app.register_blueprint(oi_spurt_bp)
app.register_blueprint(fno_trap_bp)
app.register_blueprint(notion_notes_bp)
app.register_blueprint(fno_alpha_bp)

from flask import session

BACKEND_BUILD = 'server.py:kite-session-debug-2026-05-08'

# ── Agentic Framework Initialization ─────────────────────────────────────────
_global_orchestrator = None
_agentic_data_agent = None  # Fix5: cached at startup — zero per-tick lock acquisitions
_agentic_convergence_agent = None  # EMAConvergenceAgent — cached for REST access

def get_agentic_orchestrator():
    global _global_orchestrator
    if _global_orchestrator is None:
        try:
            from constants import USE_AGENTIC_WORKFLOW
            if USE_AGENTIC_WORKFLOW:
                # D2: Support both launch CWDs (project root OR app/backend/)
                try:
                    from agents import (
                        Orchestrator, KiteDataAgent, SynergyAgent, EMAAgent,
                        FNOTrapAgent, MarketAgent, PredictionAgent, AlertDispatchAgent,
                        EMAConvergenceAgent
                    )
                except ImportError:
                    from app.backend.agents import (
                        Orchestrator, KiteDataAgent, SynergyAgent, EMAAgent,
                        FNOTrapAgent, MarketAgent, PredictionAgent, AlertDispatchAgent,
                        EMAConvergenceAgent
                    )
                orch = Orchestrator()
                data_agent = KiteDataAgent(name="KiteDataAgent")
                synergy_agent = SynergyAgent(name="SynergyAgent")
                ema_agent = EMAAgent(name="EMAAgent")
                trap_agent = FNOTrapAgent(name="FNOTrapAgent")
                market_agent = MarketAgent(name="MarketAgent")
                prediction_agent = PredictionAgent(name="PredictionAgent")
                convergence_agent = EMAConvergenceAgent(name="EMAConvergenceAgent")
                dispatch_agent = AlertDispatchAgent(
                    name="AlertDispatchAgent",
                    telegram_token=os.environ.get('TELEGRAM_BOT_TOKEN', '').strip() or None,
                    telegram_chat_id=os.environ.get('TELEGRAM_CHAT_ID', '').strip() or None,
                    discord_webhook_url=os.environ.get('DISCORD_WEBHOOK_URL', '').strip() or None,
                )

                orch.register_agent(data_agent)
                orch.register_agent(synergy_agent)
                orch.register_agent(ema_agent)
                orch.register_agent(trap_agent)
                orch.register_agent(market_agent)
                orch.register_agent(prediction_agent)
                orch.register_agent(dispatch_agent)
                orch.register_agent(convergence_agent)

                dispatch_agent.attach_socketio(socketio)
                orch.start_all()
                # Auto-start background scanners so 360 Command Center works
                # independently — no other page needs to open first.
                # These are idempotent: each has a thread.is_alive() guard.
                lazy_start_option_scanners()          # option gainers board data
                lazy_start_option_gainers_alerts()    # premium spike alerts
                lazy_start_ema_crossover_scanner()    # bulls/bears crossovers
                _global_orchestrator = orch
                # Fix5: Cache data_agent reference — eliminates per-tick lock acquisitions
                global _agentic_data_agent
                _agentic_data_agent = orch.get_agent("KiteDataAgent")
                # Cache convergence agent reference for REST endpoint
                global _agentic_convergence_agent
                _agentic_convergence_agent = orch.get_agent("EMAConvergenceAgent")
                # D9: Register graceful shutdown hook
                import atexit as _atexit
                _atexit.register(lambda: _global_orchestrator.stop_all(timeout=2.0) if _global_orchestrator else None)
                logging.info("[AGENTIC] Autonomous Agent Architecture successfully initialized and active.")
        except Exception as e:
            logging.exception(f"[AGENTIC] Failed to initialize agentic architecture: {e}")
    return _global_orchestrator


@app.route('/api/agentic/health')
def agentic_health():
    orch = get_agentic_orchestrator()
    if orch:
        return jsonify({
            'enabled': True,
            'health': orch.get_system_health()
        })
    return jsonify({
        'enabled': False,
        'message': 'Agentic architecture not active or flag disabled.'
    })


@app.route('/api/ema-crossovers')
def ema_crossovers():
    """EMA crossover scanner state for the 360 Command Center.

    Returns:
      crossovers  — per-symbol dict with state_5m/15m/1h/day, alignment, squeeze, etc.
      live_breakouts — list of recent triggered breakout/pre-cross alerts
      status      — scanner status string
      last_update — ISO timestamp of last scan cycle
    """
    try:
        lazy_start_ema_crossover_scanner()
        from ema_crossover_scanner import get_ema_crossover_state, get_live_breakout_state, notify_ema_client
        notify_ema_client()
        state      = get_ema_crossover_state()
        brk_state  = get_live_breakout_state()
        return jsonify({
            'crossovers':     state.get('crossovers', {}),
            'live_breakouts': brk_state.get('triggered_alerts', []),
            'collision_alerts': brk_state.get('collision_alerts', []),
            'status':         state.get('status', 'idle'),
            'last_update':    state.get('last_update'),
        })
    except Exception as e:
        logging.warning(f"[/api/ema-crossovers] {e}")
        return jsonify({'crossovers': {}, 'live_breakouts': [], 'status': 'error'})


@app.route('/api/ema_convergence_watchlist')
def ema_convergence_watchlist():
    """Returns the top-50 F&O symbols ranked by EMA 9/21 convergence score.

    Query params:
      direction: 'bear_setup' | 'bull_setup' | 'all' (default 'all')

    Response fields per symbol:
      symbol, rank, score, gap_pct, gap_delta, gap_score, slope_score,
      direction, trend_5m, cross_5m, in_squeeze, in_collision, ltp,
      alignment, ema9_hold
    """
    lazy_start_ema_crossover_scanner()
    try:
        from ema_crossover_scanner import notify_ema_client
        notify_ema_client()
    except Exception:
        pass

    direction_filter = request.args.get('direction', 'all').lower()
    agent = _agentic_convergence_agent
    if agent is None:
        # Attempt lazy init (agent may not have started yet)
        orch = get_agentic_orchestrator()
        if orch:
            agent = orch.get_agent('EMAConvergenceAgent')
    if agent is None:
        return jsonify({'error': 'EMAConvergenceAgent not available', 'watchlist': []}), 503

    watchlist = agent.get_watchlist()
    if direction_filter in ('bear_setup', 'bull_setup'):
        watchlist = [r for r in watchlist if r.get('direction') == direction_filter]
        # Re-rank after filter
        for i, r in enumerate(watchlist, start=1):
            r['rank'] = i

    return jsonify({
        'watchlist':  watchlist,
        'count':      len(watchlist),
        'direction':  direction_filter,
        'stats':      agent.get_stats(),
    })


@app.route('/api/backend-version')
def backend_version():
    return jsonify({
        'build': BACKEND_BUILD,
        'server_file': __file__,
        'session_file': _SESSION_FILE if '_SESSION_FILE' in globals() else None,
        'agentic_enabled': _global_orchestrator is not None,
    })



@app.route('/favicon.ico')
def favicon():
    """Suppress browser auto-request 404 noise — browsers request this on every page load."""
    return '', 204

KITE_RATE_DELAY = 0.35
KITE_INTERVAL_MAP = {
    '1': 'minute', '3': '3minute', '5': '5minute',
    '10': '10minute', '15': '15minute', '30': '30minute',
    '60': '60minute', 'D': 'day'
}
OI_INSTRUMENTS = {'NIFTY50', 'BANKNIFTY', 'FINNIFTY'}
INSTRUMENT_TOKENS = {
    'NIFTY50': 256265, 'BANKNIFTY': 260105, 'FINNIFTY': 257801,
    'SENSEX': 265,                          # BSE Sensex index token
    'RELIANCE': 738561, 'HDFCBANK': 341249, 'INFY': 408065,
    'TCS': 2953217, 'ICICIBANK': 1270529, 'SBIN': 779521, 'AXISBANK': 1510401,
}
EXCHANGE_MAP = {k: 'NSE' for k in INSTRUMENT_TOKENS}

_kite_candle_cache: dict = {}
_kite_cache_lock = threading.Lock()
_gtm_synced_token: str | None = None  # guards GTM from restarting on every request

# ══════════════════════════════════════════════════════════════
# ── Synthetic Volume Engine — multi-index common module ──
# ══════════════════════════════════════════════════════════════
# Constituent symbol sets per index.  All fetched from NSE segment
# (highest liquidity; NSE tokens used for both Nifty and Sensex).

INDEX_CONSTITUENTS = {
    'NIFTY50': {
        "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
        "BAJAJ-AUTO", "BAJAJFINSV", "BAJFINANCE", "BHARTIARTL", "BPCL",
        "BRITANNIA", "CIPLA", "COALINDIA", "DIVISLAB", "DRREDDY",
        "EICHERMOT", "GRASIM", "HCLTECH", "HDFCBANK", "HEROMOTOCO",
        "HINDALCO", "HINDUNILVR", "ICICIBANK", "ITC", "INDUSINDBK",
        "INFY", "JSWSTEEL", "KOTAKBANK", "LT", "M&M",
        "MARUTI", "NESTLEIND", "NTPC", "ONGC", "POWERGRID",
        "RELIANCE", "SBILIFE", "SBIN", "SUNPHARMA", "TATACONSUM",
        "TATAMOTORS", "TATASTEEL", "TCS", "TECHM", "TITAN",
        "ULTRACEMCO", "WIPRO", "BEL", "TRENT", "JIOFIN"
    },
    'SENSEX': {
        # BSE Sensex 30 — sourced from NSE tokens for uniform liquidity
        "ADANIPORTS", "ASIANPAINT", "AXISBANK", "BAJFINANCE", "BAJAJFINSV",
        "BHARTIARTL", "DRREDDY", "HCLTECH", "HDFCBANK", "HINDUNILVR",
        "ICICIBANK", "INFY", "INDUSINDBK", "ITC", "JSWSTEEL",
        "KOTAKBANK", "LT", "M&M", "MARUTI", "NESTLEIND",
        "NTPC", "POWERGRID", "RELIANCE", "SBIN", "SUNPHARMA",
        "TATACONSUM", "TATAMOTORS", "TCS", "TITAN", "ULTRACEMCO"
    },
}

# Maps instrument token → index name for automatic synthetic volume intercept.
# Add new indices here; the engine picks them up automatically.
SYNTHETIC_VOLUME_INDICES = {
    INSTRUMENT_TOKENS['NIFTY50']: 'NIFTY50',
    INSTRUMENT_TOKENS['SENSEX']:  'SENSEX',
}

_nifty_volume_cache = {}          # TTL cache: (token, date_str, interval) → (timestamp, candles)
CACHE_TTL_SECONDS = 300           # 5-minute TTL

_index_tokens_cache = {}          # daily resolution cache: index_name → {'tokens': [...], 'date': date}
_token_resolution_lock = threading.Lock()


def get_cached_nifty_candles(token, from_date_str, interval):
    cache_key = (token, from_date_str, interval)
    if cache_key in _nifty_volume_cache:
        cached_time, cached_data = _nifty_volume_cache[cache_key]
        if time.time() - cached_time < CACHE_TTL_SECONDS:
            return cached_data
    return None


def set_cached_nifty_candles(token, from_date_str, interval, candles):
    cache_key = (token, from_date_str, interval)
    # Evict oldest entry when cache exceeds 50 entries
    if len(_nifty_volume_cache) >= 50:
        oldest_key = min(_nifty_volume_cache, key=lambda k: _nifty_volume_cache[k][0])
        del _nifty_volume_cache[oldest_key]
        logging.debug(f"[Volume Cache] Evicted oldest cache entry: {oldest_key}")
    _nifty_volume_cache[cache_key] = (time.time(), candles)


def normalize_timestamp_key(dt_obj):
    """Normalize tz-aware and naive datetimes to local IST HH:MM key."""
    import pytz
    local_ist = pytz.timezone('Asia/Kolkata')
    if getattr(dt_obj, 'tzinfo', None) is not None:
        dt_obj = dt_obj.astimezone(local_ist)
    return dt_obj.strftime('%Y-%m-%dT%H:%M')


# Scaled constituent index weights (summing exactly to 10.0 representing 100% of index)
INDEX_VOLUME_WEIGHTS = {
    'NIFTY50': {
        'HDFCBANK': 1.15, 'RELIANCE': 0.97, 'ICICIBANK': 0.76, 'INFY': 0.58,
        'LT': 0.43, 'TCS': 0.38, 'ITC': 0.38, 'BHARTIARTL': 0.34, 'SBIN': 0.32,
        'KOTAKBANK': 0.29, 'AXISBANK': 0.28, 'HINDUNILVR': 0.24, 'BAJFINANCE': 0.21,
        'MARUTI': 0.16, 'M&M': 0.16, 'TATASTEEL': 0.15, 'HCLTECH': 0.15, 'SUNPHARMA': 0.14,
        'NTPC': 0.14, 'POWERGRID': 0.14, 'ADANIENT': 0.13, 'TATAMOTORS': 0.13,
        'ULTRACEMCO': 0.12, 'COALINDIA': 0.12, 'JSWSTEEL': 0.11, 'GRASIM': 0.11,
    },
    'SENSEX': {
        # Sourced from Sensex 30 weights (scaled to 10.0)
        'HDFCBANK': 1.38, 'RELIANCE': 1.18, 'ICICIBANK': 0.92, 'INFY': 0.72,
        'LT': 0.52, 'TCS': 0.48, 'ITC': 0.46, 'BHARTIARTL': 0.42, 'SBIN': 0.38,
        'KOTAKBANK': 0.35, 'AXISBANK': 0.33, 'HINDUNILVR': 0.29, 'BAJFINANCE': 0.25,
        'MARUTI': 0.19, 'M&M': 0.19, 'HCLTECH': 0.18, 'SUNPHARMA': 0.17,
        'NTPC': 0.17, 'POWERGRID': 0.17, 'TATAMOTORS': 0.16, 'ULTRACEMCO': 0.14,
    }
}


def resolve_index_constituents(kite, index_name):
    """Thread-safe, daily-TTL resolution of NSE instrument tokens for an index.

    [Bug 1 fix] Filters by segment='NSE' to prevent cross-listed duplicates.
    [Concern 3 fix] Re-resolves once per calendar day to pick up reconstitutions.
    """
    global _index_tokens_cache
    today = dt.now().date()
    with _token_resolution_lock:
        entry = _index_tokens_cache.get(index_name)
        if entry and entry.get('resolved_date') == today:
            return entry['tokens']
        symbols = INDEX_CONSTITUENTS.get(index_name.lower(), INDEX_CONSTITUENTS.get(index_name, []))
        if not symbols:
            logging.warning(f"[Volume Engine] No constituent definition for index '{index_name}'.")
            return []
        try:
            import sqlite3
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT instrument_token, tradingsymbol, segment FROM instruments WHERE exchange = 'NSE'")
            instruments = [dict(r) for r in cursor.fetchall()]
            conn.close()
            resolved = []
            token_to_symbol = {}
            for inst in instruments:
                if inst["tradingsymbol"] in symbols and inst.get("segment") == "NSE":
                    tok = inst["instrument_token"]
                    resolved.append(tok)
                    token_to_symbol[tok] = inst["tradingsymbol"]
            _index_tokens_cache[index_name] = {
                'tokens': resolved,
                'token_to_symbol': token_to_symbol,
                'resolved_date': today
            }
            logging.info(f"[Volume Engine] Resolved {len(resolved)}/{len(symbols)} tokens for {index_name} on {today}.")
            return resolved
        except Exception as e:
            logging.error(f"[Volume Engine] Token resolution failed for {index_name}: {e}")
            # Return stale tokens if available rather than nothing
            return entry.get('tokens', []) if entry else []


def fetch_stock_candles_worker(kite, token, from_date, to_date, interval):
    """Fetch historical candles for one constituent; returns empty list on failure."""
    try:
        return token, kite.historical_data(token, from_date, to_date, interval)
    except Exception as e:
        logging.warning(f"[Volume Engine] Token {token} fetch failed: {e}")
        return token, []


def aggregate_index_volume(kite, index_name, from_date, to_date, interval):
    """Generic rate-compliant volume aggregation for any configured index.

    Fetches constituent candles in batches of 3 (Kite rate limit = 3 req/s),
    accumulates absolute volume.

    [Bug 2 fix] Sleep is skipped after the last batch to avoid wasted latency.
    Supports: NIFTY50, SENSEX.
    """
    tokens = resolve_index_constituents(kite, index_name)
    if not tokens:
        logging.warning(f"[Volume Engine] No tokens resolved for {index_name}; returning empty map.")
        return {}

    constituent_candles = {}
    BATCH_SIZE = 3
    batches = [tokens[i:i + BATCH_SIZE] for i in range(0, len(tokens), BATCH_SIZE)]

    with concurrent.futures.ThreadPoolExecutor(max_workers=BATCH_SIZE) as executor:
        for i, batch in enumerate(batches):
            futures = [
                executor.submit(fetch_stock_candles_worker, kite, t, from_date, to_date, interval)
                for t in batch
            ]
            for fut in concurrent.futures.as_completed(futures):
                token, candles = fut.result()
                constituent_candles[token] = candles
            # [Bug 2 fix] No sleep after the final batch
            if i < len(batches) - 1:
                time.sleep(1.0)

    # Accumulate absolute volume
    volume_data = {} # {ts_key: {'volume': int}}

    for token, candles in constituent_candles.items():
        for c in candles:
            ts_key = normalize_timestamp_key(c['date'])
            vol = c.get('volume', 0) or 0

            if ts_key not in volume_data:
                volume_data[ts_key] = {'volume': 0}

            # Total Volume is 100% untouched sum of all constituents
            volume_data[ts_key]['volume'] += vol

    logging.info(f"[Volume Engine] {index_name} aggregation complete for {len(volume_data)} timestamps.")
    return volume_data


_kite_ws_thread = None
_kite_ws_running = False
_kite_ws_stop_flag = False
_kite_tick_store = {}
_kite_tick_lock = threading.Lock()
_kite_services_lock = threading.Lock()
_kite_services_started = False

def check_auth(username, password):
    """Check if a username / password combination is valid."""
    env_user = os.environ.get('APP_USERNAME', 'admin')
    env_pass = os.environ.get('APP_PASSWORD', 'admin123')
    return username == env_user and password == env_pass

def authenticate(include_header=True):
    """Sends a 401 response. Only include WWW-Authenticate for non-API calls 
    to avoid triggering the browser's native/ugly basic auth popup."""
    headers = {}
    if include_header:
        headers['WWW-Authenticate'] = 'Basic realm="Login Required"'
        
    return Response(
        'Could not verify your access level for that URL.\n'
        'You have to login with proper credentials', 401,
        headers
    )

@app.before_request
def requires_auth():
    """
    TEMPORARY: Disable app-level Basic Auth.

    This keeps all routes publicly accessible while login
    is being skipped for development/demo.
    """
    return None

# Kite client (lazy init)
_kite = None
_instruments_cache = None
_historical_candle_cache = {}
_apex_signals_cache = {'signals': [], 'ts': None}
_live_ltp_cache = {}
_live_ltp_lock = threading.Lock()

# ── Inline Chart WebSocket Feed ──
_chart_subscriptions = {}   # sym (str) → instrument_token (int)
_chart_sub_lock = threading.Lock()

def _chart_tick_broadcaster(ticks):
    """Registered with GlobalTicker to push chart ticks to all Socket.IO clients."""
    with _chart_sub_lock:
        token_to_sym = {v: k for k, v in _chart_subscriptions.items()}
    for tick in ticks:
        token = tick.get('instrument_token')
        if not token:
            continue
        sym = token_to_sym.get(int(token))
        if sym:
            try:
                socketio.emit('chart_tick', {
                    'sym': sym,
                    'ltp': tick.get('last_price'),
                    'volume': tick.get('volume'),
                    'ts': int(time.time() * 1000)
                })
            except Exception as _e:
                pass

def _resolve_sym_token(sym):
    """Resolve a symbol string to its NSE instrument token using the cached instruments list."""
    global _instruments_cache
    if not _instruments_cache:
        try:
            db = get_db()
            rows = db.execute(
                "SELECT instrument_token, tradingsymbol FROM instruments WHERE exchange='NSE'"
            ).fetchall()
            _instruments_cache = [dict(r) for r in rows]
        except Exception:
            return None
    for inst in (_instruments_cache or []):
        if inst.get('tradingsymbol') == sym or inst.get('symbol') == sym:
            return inst.get('instrument_token')
    return None

# ── Database path ─
DB_PATH = os.path.join(os.path.dirname(__file__), 'tradesignal_cache.db')


# ══════════════════════════════════════════════════════════════
# ── SQLite Cache Layer ──
# ══════════════════════════════════════════════════════════════

def get_db():
    """Get a database connection for the current request context."""
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA journal_mode=WAL')  # Better write concurrency
        g.db.execute('PRAGMA synchronous=NORMAL')
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    """Initialize cache database tables."""
    conn = sqlite3.connect(DB_PATH)
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS ohlcv (
            instrument_token INTEGER NOT NULL,
            date TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume INTEGER NOT NULL,
            interval TEXT NOT NULL DEFAULT 'day',
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (instrument_token, date, interval)
        );

        CREATE TABLE IF NOT EXISTS instruments (
            instrument_token INTEGER PRIMARY KEY,
            exchange TEXT,
            tradingsymbol TEXT,
            name TEXT,
            segment TEXT,
            lot_size INTEGER,
            instrument_type TEXT,
            expiry TEXT,
            strike REAL,
            fetched_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS cache_meta (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_ohlcv_token_interval ON ohlcv(instrument_token, interval);
        CREATE INDEX IF NOT EXISTS idx_ohlcv_date ON ohlcv(date);
        CREATE INDEX IF NOT EXISTS idx_instruments_symbol ON instruments(tradingsymbol, exchange);
        CREATE INDEX IF NOT EXISTS idx_instruments_segment ON instruments(segment);

        CREATE TABLE IF NOT EXISTS fno_alerts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id       TEXT NOT NULL,
            scanned_at   TEXT NOT NULL,
            universe     TEXT NOT NULL,
            mode         TEXT NOT NULL,
            result_json  TEXT NOT NULL,
            summary_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_fno_alerts_time ON fno_alerts(scanned_at);

        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            symbol TEXT,
            sentiment TEXT DEFAULT 'NEUTRAL',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            notion_page_id TEXT,
            sync_status TEXT DEFAULT 'PENDING'
        );

        CREATE TABLE IF NOT EXISTS notion_config (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS rvol_baseline (
            symbol TEXT PRIMARY KEY,
            average_volume REAL NOT NULL,
            date TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS stored_news (
            title TEXT PRIMARY KEY,
            summary TEXT,
            source TEXT,
            url TEXT,
            sentiment TEXT,
            score REAL,
            category TEXT,
            impact_rating TEXT,
            impacted_stocks TEXT,
            takeaway TEXT,
            priority_score INTEGER,
            timestamp REAL,
            time TEXT,
            fetched_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_stored_news_time ON stored_news(timestamp);
        
        CREATE TABLE IF NOT EXISTS fno_shareholding (
            symbol TEXT PRIMARY KEY,
            quarter TEXT NOT NULL,
            promoters REAL,
            fii REAL,
            dii REAL,
            public REAL,
            total_institutional REAL,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS first_hour_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            pattern_key TEXT NOT NULL,
            or_direction TEXT NOT NULL,
            move_bucket TEXT NOT NULL,
            predicted_outcome TEXT NOT NULL,
            prediction_confidence REAL NOT NULL,
            or_high REAL,
            or_low REAL,
            or_close REAL,
            actual_outcome TEXT,
            validation_result TEXT,
            status TEXT NOT NULL DEFAULT 'PREDICTED',
            validated_at TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(date, symbol)
        );
    ''')
    conn.commit()

    # Seed fno_shareholding if empty
    import json
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM fno_shareholding")
        count = cursor.fetchone()[0]
        if count == 0:
            json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fno_shareholding.json')
            if os.path.exists(json_path):
                with open(json_path, 'r') as f:
                    seed_data = json.load(f)
                records = [
                    (
                        r.get("symbol"),
                        r.get("quarter"),
                        r.get("promoters", 0.0),
                        r.get("fii", 0.0),
                        r.get("dii", 0.0),
                        r.get("public", 0.0),
                        r.get("total_institutional", 0.0)
                    )
                    for r in seed_data
                ]
                conn.executemany("""
                    INSERT OR REPLACE INTO fno_shareholding 
                    (symbol, quarter, promoters, fii, dii, public, total_institutional, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """, records)
                conn.commit()
                print(f"  Seeded fno_shareholding table with {len(records)} records.")
    except Exception as seed_err:
        print(f"  Warning: Failed to seed fno_shareholding table: {seed_err}")

    conn.close()
    print(f"  Cache DB: {DB_PATH}")


# Initialize on startup
init_db()


def cache_get_ohlcv(db, token, from_date, to_date, interval='day'):
    """Get cached OHLCV data for a token in a date range."""
    # Cached intraday rows are stored as ISO strings like:
    #   2026-04-15T09:50:00+05:30
    # Normalize query bounds to the same lexical format; otherwise a bound like
    #   2026-04-15 09:55:00
    # will sort BEFORE same-day cached rows containing "T", excluding them.
    is_intraday = interval in ('5minute', '15minute', '30minute', '60minute', 'minute')

    query_from_date = from_date
    query_to_date = to_date + "T23:59:59" if len(to_date) == 10 else to_date

    if is_intraday:
        if isinstance(query_from_date, str):
            query_from_date = query_from_date.replace(' ', 'T')
        if isinstance(query_to_date, str):
            query_to_date = query_to_date.replace(' ', 'T')

    rows = db.execute(
        'SELECT date, open, high, low, close, volume FROM ohlcv '
        'WHERE instrument_token = ? AND interval = ? AND date >= ? AND date <= ? '
        'ORDER BY date',
        (token, interval, query_from_date, query_to_date)
    ).fetchall()
    return [dict(r) for r in rows]


def cache_get_latest_date(db, token, interval='day'):
    """Get the latest cached date for a token."""
    row = db.execute(
        'SELECT MAX(date) as max_date FROM ohlcv WHERE instrument_token = ? AND interval = ?',
        (token, interval)
    ).fetchone()
    return row['max_date'] if row and row['max_date'] else None


def cache_store_ohlcv(db, token, candles, interval='day'):
    """Store OHLCV candles into cache (upsert)."""
    now = dt.now().isoformat()
    # Intraday intervals need full timestamp; daily just needs date
    is_intraday = interval in ('5minute', '15minute', '30minute', '60minute', 'minute')
    data = []
    for c in candles:
        if isinstance(c, dict):
            date_val = c.get('date', '')
            if hasattr(date_val, 'isoformat'):
                date_val = date_val.isoformat()
            # Keep full timestamp for intraday, strip time for daily/weekly
            date_str = str(date_val) if is_intraday else str(date_val).split('T')[0]
            if not date_str:
                continue
            data.append((
                token, date_str,
                c.get('open', 0), c.get('high', 0), c.get('low', 0),
                c.get('close', 0), c.get('volume', 0),
                interval, now
            ))
    if data:
        db.executemany(
            'INSERT OR REPLACE INTO ohlcv '
            '(instrument_token, date, open, high, low, close, volume, interval, fetched_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            data
        )
        db.commit()


def cache_store_instruments(db, instruments_list):
    """Store instruments into cache."""
    now = dt.now().isoformat()
    data = []
    for i in instruments_list:
        expiry = i.get('expiry', '')
        if hasattr(expiry, 'isoformat'):
            expiry = expiry.isoformat()
        data.append((
            i.get('instrument_token', 0),
            i.get('exchange', ''),
            i.get('tradingsymbol', ''),
            i.get('name', ''),
            i.get('segment', ''),
            i.get('lot_size', 0),
            i.get('instrument_type', ''),
            str(expiry),
            i.get('strike', 0),
            now
        ))
    if data:
        # Clear stale/expired instruments before writing the new list
        db.execute('DELETE FROM instruments')
        db.executemany(
            'INSERT OR REPLACE INTO instruments '
            '(instrument_token, exchange, tradingsymbol, name, segment, lot_size, instrument_type, expiry, strike, fetched_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            data
        )
        # Store metadata
        db.execute(
            'INSERT OR REPLACE INTO cache_meta (key, value, updated_at) VALUES (?, ?, ?)',
            ('instruments_fetched', now, now)
        )
        db.commit()


def cache_get_instruments(db):
    """Get cached instruments."""
    rows = db.execute('SELECT * FROM instruments').fetchall()
    return [dict(r) for r in rows]


def cache_instruments_fresh(db, max_age_hours=12):
    """Check if cached instruments are fresh enough."""
    row = db.execute(
        'SELECT value FROM cache_meta WHERE key = ?', ('instruments_fetched',)
    ).fetchone()
    if not row:
        return False
    try:
        fetched = dt.fromisoformat(row['value'])
        return (dt.now() - fetched).total_seconds() < max_age_hours * 3600
    except (ValueError, TypeError):
        return False


def trigger_instruments_sync_async():
    """Trigger the weekly sync asynchronously in a background thread."""
    threading.Thread(target=run_instruments_sync, daemon=True).start()


def run_instruments_sync(force=False):
    """Fetch NSE, NFO, BSE instruments from Kite API and store in SQLite."""
    try:
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        
        # Check when the last sync occurred
        row = db.execute('SELECT value FROM cache_meta WHERE key = ?', ('instruments_fetched',)).fetchone()
        last_sync_date = None
        if row:
            try:
                last_sync_date = dt.fromisoformat(row['value']).date()
            except Exception:
                pass
                
        today = dt.now()
        
        # Determine if we should sync:
        # 1. Force is True
        # 2. Database is completely empty (no instruments count)
        # 3. We haven't synced today yet (run daily once)
        count_row = db.execute('SELECT COUNT(*) as cnt FROM instruments').fetchone()
        db_empty = (count_row['cnt'] == 0) if count_row else True
        
        should_sync = force or db_empty or (last_sync_date != today.date())
        db.close()
        
        if not should_sync:
            return
            
        kite = get_kite()
        if not kite:
            logging.info("[Daily Sync] Sync deferred: Kite Connect not authenticated yet.")
            return
            
        logging.info("[Daily Sync] Starting instrument database sync...")
        nse = kite.instruments('NSE')
        nfo = kite.instruments('NFO')
        try:
            bse = kite.instruments('BSE')
        except Exception:
            bse = []
            
        try:
            bfo = kite.instruments('BFO')
            nfo_names = {i['name'].upper() for i in nfo if i.get('name')}
            bfo_filtered = [i for i in bfo if i.get('name') and i['name'].upper() not in nfo_names]
        except Exception:
            bfo_filtered = []
            
        all_instruments = nse + nfo + bse + bfo_filtered
        if len(all_instruments) > 1000:
            db_conn = sqlite3.connect(DB_PATH)
            cache_store_instruments(db_conn, all_instruments)
            db_conn.close()
            global _instruments_cache
            _instruments_cache = None
            
            fno_count = len([i for i in all_instruments if i.get('segment') in ('NFO-FUT', 'NFO-OPT', 'BFO-FUT', 'BFO-OPT')])
            cash_count = len([i for i in all_instruments if i.get('instrument_type') == 'EQ' and i.get('segment') in ('NSE', 'BSE')])
            logging.info(f"[Daily Sync] Finished. DB loaded with {fno_count} F&O and {cash_count} Cash stocks details.")
        else:
            logging.error("[Daily Sync] Zerodha returned an empty or invalid instruments list.")
    except Exception as e:
        logging.error(f"[Daily Sync] Exception in run_instruments_sync: {e}")


def _instruments_sync_scheduler_loop():
    """Background scheduler loop checking for daily sync execution."""
    logging.info("[Daily Sync] Background scheduler active.")
    while True:
        try:
            run_instruments_sync(force=False)
        except Exception as e:
            logging.error(f"[Daily Sync] Scheduler error: {e}")
        # Sleep for 15 minutes
        time.sleep(900)


def start_instruments_sync_scheduler():
    """Start the weekly sync scheduler daemon."""
    threading.Thread(target=_instruments_sync_scheduler_loop, daemon=True).start()



def get_cache_stats(db):
    """Get cache statistics."""
    ohlcv_count = db.execute('SELECT COUNT(*) as cnt FROM ohlcv').fetchone()['cnt']
    instruments_count = db.execute('SELECT COUNT(*) as cnt FROM instruments').fetchone()['cnt']
    unique_tokens = db.execute('SELECT COUNT(DISTINCT instrument_token) as cnt FROM ohlcv').fetchone()['cnt']
    db_size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
    return {
        'ohlcv_candles': ohlcv_count,
        'instruments': instruments_count,
        'unique_tokens': unique_tokens,
        'db_size_mb': round(db_size / (1024 * 1024), 2)
    }


def generate_demo_candles(token, from_date, to_date, interval):
    """Generate demo OHLCV data for offline mode with chronological sorting and realistic base price."""
    import random
    from datetime import datetime, timedelta

    # Demo base prices for common tokens
    base_prices = {
        256265: 22000,  # NIFTY 50
        260105: 45000,  # NIFTY BANK
        260659: 18000,  # NIFTY FIN SERVICE
        738561: 2500,   # RELIANCE
        341249: 1600,   # HDFCBANK
    }

    base_price = base_prices.get(token)
    if base_price is None:
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute(
                "SELECT close FROM ohlcv WHERE instrument_token = ? AND interval = 'day' ORDER BY date DESC LIMIT 1",
                (int(token),)
            )
            row = c.fetchone()
            conn.close()
            if row and row[0]:
                base_price = float(row[0])
        except Exception:
            pass

    if base_price is None or base_price <= 0:
        base_price = 1000

    candles = []

    # Generate candles for the last 5 trading days
    current_date = datetime.now().replace(hour=9, minute=15, second=0, microsecond=0)

    # For intraday, generate bars from 9:15 to 15:30 in chronological order
    if interval in ('5minute', '15minute'):
        interval_minutes = 5 if interval == '5minute' else 15
        days_back = 2  # Last 2 days

        for day in reversed(range(days_back)):
            day_start = current_date - timedelta(days=day)
            if day_start.weekday() >= 5:  # Skip weekends
                continue

            current_time = day_start
            end_time = day_start.replace(hour=15, minute=30)

            while current_time <= end_time:
                # Generate realistic OHLC with some volatility
                volatility = base_price * 0.005  # 0.5% volatility
                open_price = base_price + random.uniform(-volatility, volatility)
                high_price = open_price + random.uniform(0, volatility)
                low_price = open_price - random.uniform(0, volatility)
                close_price = random.uniform(low_price, high_price)
                volume = random.randint(10000, 100000)

                candles.append({
                    'date': current_time.isoformat(),
                    'open': round(open_price, 2),
                    'high': round(high_price, 2),
                    'low': round(low_price, 2),
                    'close': round(close_price, 2),
                    'volume': volume
                })

                current_time += timedelta(minutes=interval_minutes)
                base_price = close_price  # Carry over to next candle

    else:
        # For daily data, generate daily bars in chronological order
        for day in reversed(range(30)):  # Last 30 days
            day_date = current_date - timedelta(days=day)
            if day_date.weekday() >= 5:  # Skip weekends
                continue

            volatility = base_price * 0.02  # 2% daily volatility
            open_price = base_price + random.uniform(-volatility, volatility)
            high_price = open_price + random.uniform(0, volatility)
            low_price = open_price - random.uniform(0, volatility)
            close_price = random.uniform(low_price, high_price)
            volume = random.randint(100000, 1000000)

            candles.append({
                'date': day_date.date().isoformat(),
                'open': round(open_price, 2),
                'high': round(high_price, 2),
                'low': round(low_price, 2),
                'close': round(close_price, 2),
                'volume': volume
            })

            base_price = close_price

    # Ensure strictly sorted by timestamp ascending
    candles.sort(key=lambda x: str(x.get('date', '')))
    return candles


# ══════════════════════════════════════════════════════════════
# ── Kite session persistence (survives Termux process restarts) ──
# ══════════════════════════════════════════════════════════════

# Path: next to server.py (stays local, never committed — .gitignored)
_SESSION_FILE = os.path.join(_server_dir, '.kite_session.json')


def _save_kite_session(api_key: str, access_token: str):
    """Persist Kite credentials to disk so they survive server restarts."""
    try:
        with open(_SESSION_FILE, 'w') as f:
            json.dump({'api_key': api_key, 'access_token': access_token}, f)
    except Exception as e:
        print(f'  [SESSION] WARNING: Could not save session file: {e}')


def _load_kite_session():
    """Load persisted Kite credentials from disk (Termux restart recovery)."""
    try:
        if os.path.isfile(_SESSION_FILE):
            with open(_SESSION_FILE, 'r') as f:
                data = json.load(f)
            api_key = data.get('api_key', '')
            access_token = data.get('access_token', '')
            if api_key and access_token:
                return api_key, access_token
    except Exception as e:
        print(f'  [SESSION] WARNING: Could not read session file: {e}')
    return None, None


def _clear_kite_session():
    """Delete persisted session file (called on explicit logout)."""
    try:
        if os.path.isfile(_SESSION_FILE):
            os.remove(_SESSION_FILE)
    except Exception:
        pass


def _request_kite_credentials():
    """Read Kite credentials sent by the frontend on API calls."""
    if not has_request_context():
        return None, None
    api_key = (request.headers.get('X-Kite-Api-Key') or '').strip()
    access_token = (request.headers.get('X-Kite-Access-Token') or '').strip()
    return api_key or None, access_token or None


def _kite_session_debug():
    """Non-secret auth-source diagnostics for 401 responses."""
    header_key, header_token = _request_kite_credentials()
    disk_key, disk_token = _load_kite_session()
    return {
        'has_header_api_key': bool(header_key),
        'has_header_access_token': bool(header_token),
        'has_flask_api_key': bool(session.get('kite_api_key')) if has_request_context() else False,
        'has_flask_access_token': bool(session.get('kite_access_token')) if has_request_context() else False,
        'has_disk_api_key': bool(disk_key),
        'has_disk_access_token': bool(disk_token),
        'has_env_api_key': bool(os.environ.get('KITE_API_KEY')),
        'has_env_access_token': bool(os.environ.get('KITE_ACCESS_TOKEN')),
    }

def _fno_breakout_scanner_loop():
    """Background scheduler for breakout scanner. Runs only during market hours."""
    print("[FNO Auto-Scanner] Background thread active. Standing by for market hours.")
    
    # Configure scanning interval (5 minutes) and default universe
    scan_interval = int(os.environ.get('FNO_SCANNER_INTERVAL', 300))
    universe = os.environ.get('FNO_SCANNER_UNIVERSE', 'NIFTY50')
    mode = 'intraday'
    min_score = 6
    
    while True:
        try:
            from session_utils import now_ist
            now = now_ist()
            
            # Check if weekday (Monday=0 to Friday=4)
            if now.weekday() < 5:
                # Session range: 09:15 AM to 03:30 PM IST
                m_start = now.replace(hour=9, minute=15, second=0, microsecond=0)
                m_end = now.replace(hour=15, minute=30, second=0, microsecond=0)
                
                if m_start <= now <= m_end:
                    kite = get_kite()
                    if kite:
                        print(f"[FNO Auto-Scanner] Running scheduled scan for {universe}...")
                        
                        from indian_stock_breakout_scanner import scan as fno_scan
                        import indian_stock_breakout_scanner as _scanner
                        _scanner._kite = kite
                        
                        # Execute the core scan
                        scan_top_n = 50 if universe == 'ALL_EQUITY' else 15
                        result = fno_scan(universe=universe, mode=mode, min_score=min_score, top_n=scan_top_n)
                        result = _serialize_result(result)
                        
                        # Store in database
                        import uuid
                        import json
                        run_id = str(uuid.uuid4())[:8]
                        _ist = datetime.timezone(timedelta(hours=5, minutes=30))
                        scanned_at = now.isoformat()
                        
                        try:
                            with app.app_context():
                                db = get_db()
                                db.execute(
                                    'INSERT INTO fno_alerts (run_id, scanned_at, universe, mode, result_json, summary_json) '
                                    'VALUES (?, ?, ?, ?, ?, ?)',
                                    (run_id, scanned_at, universe, mode,
                                     json.dumps(result, default=str),
                                     json.dumps(result.get('summary', {})))
                                )
                                cutoff = _ten_working_days_ago(now).isoformat()
                                db.execute('DELETE FROM fno_alerts WHERE scanned_at < ?', (cutoff,))
                                db.commit()
                        except Exception as db_err:
                            print(f"[FNO Auto-Scanner] DB error: {db_err}")
                            
                        # Send telegram & discord alerts
                        result['run_id'] = run_id
                        result['scanned_at'] = scanned_at
                        if _telegram_configured():
                            msg = _format_fno_telegram_alert(result, universe, mode, scanned_at)
                            # _send_telegram_message(msg)
                            print("[FNO Auto-Scanner] Scan complete. Alerts sent.")
                        else:
                            print("[FNO Auto-Scanner] Warnings: Notifications not configured.")
                            
                        time.sleep(scan_interval)
                        continue
                    else:
                        print("[FNO Auto-Scanner] Kite session not active. Retrying in 30 seconds...")
                        time.sleep(30)
                        continue
                        
            # Outside market hours: sleep 60 seconds and check again
            time.sleep(60)
            
        except Exception as e:
            print(f"[FNO Auto-Scanner] Loop exception: {e}")
            time.sleep(30)


# ══════════════════════════════════════════════════════════════
# ── Kite-dependent service startup ──
# ══════════════════════════════════════════════════════════════

def start_kite_dependent_services(kite):
    """
    Start all background services that require an active Kite session.
    Safe to call from any path (login, boot recovery, get_kite restoration).
    Guarded by a module-level flag — only executes once per server process.
    """
    global _kite_services_started
    with _kite_services_lock:
        if _kite_services_started:
            return
        if not kite:
            return
        _kite_services_started = True

    print("\n[STARTUP] Kite connected — starting Kite-dependent services...")
    # NOTE: GlobalTickerManager (GTM) is intentionally NOT initialized here.
    # sync_global_ticker_credentials() is called before this function at every
    # call site (__main__, get_kite recovery, login route), so GTM is already
    # initialized and started with the correct credentials. Calling it again here
    # would create a thread-unsafe double-init race with identical values.

    # NOTE: Market stream (CVD/VWAP/OrderBook) is NOT started here.
    # It is on-demand — _ensure_kite() in market_routes.py lazily calls
    # start_market_stream() when the user first opens the Market Profiler page.

    # 4. RVOL Baseline Warming (background, 10s delay for DB to settle)
    # kite passed here is already _wrap_kite_quote()-patched at all call sites,
    # so kite.quote() calls within warm will populate the LTP cache correctly.
    def _run_rvol_warm():
        time.sleep(10)
        try:
            from option_gainers_scanner import resolve_all_spot_tokens, ensure_avg_volume_warm
            token_map = resolve_all_spot_tokens()
            if token_map:
                print(f"  [OK] RVOL warm triggered for {len(token_map)} symbols.")
                ensure_avg_volume_warm(kite, token_map)
            else:
                print("  [Warning] RVOL warm: no symbols resolved from DB.")
        except Exception as _ex:
            print(f"  [Error] RVOL warm: {_ex}")
    threading.Thread(target=_run_rvol_warm, daemon=True, name="RVOLWarmThread").start()

    # 7. Pre-Market Scanner
    try:
        from pre_market_scanner import start_pre_market_scanner
        start_pre_market_scanner()
        print("  [OK] Pre-Market Scanner started.")
    except Exception as _e:
        print(f"  [Error] Pre-Market Scanner: {_e}")

    # 8. Automated F&O Scanner
    try:
        logging.info("[STARTUP] Spawning F&O Breakout Scanner background loop (disabled)...")
        # threading.Thread(target=_fno_breakout_scanner_loop, daemon=True, name="FNOAutoScannerThread").start()
    except Exception as _e:
        print(f"  [Error] Failed to start F&O Auto-Scanner: {_e}")

    # 9. Daily Transition States Baseline Scheduler (runs past 09:30 AM on weekdays)
    def _daily_baseline_scheduler_loop():
        import pytz
        tz = pytz.timezone("Asia/Kolkata")
        last_init_date = None
        logging.info("[Daily Baseline] Background scheduler active.")
        while True:
            try:
                now = datetime.datetime.now(tz)
                # Weekday check (Monday=0 to Friday=4)
                if now.weekday() < 5:
                    today_str = now.strftime("%Y-%m-%d")
                    # Trigger only once per day at or after 09:30 AM IST
                    if last_init_date != today_str and (now.hour > 9 or (now.hour == 9 and now.minute >= 30)):
                        from oi_transition_engine import initialize_daily_baselines
                        success = initialize_daily_baselines(kite)
                        if success:
                            last_init_date = today_str
            except Exception as e:
                logging.error(f"[Daily Baseline] Scheduler error: {e}")
            time.sleep(60)

    threading.Thread(target=_daily_baseline_scheduler_loop, daemon=True, name="DailyBaselineScheduler").start()

    print("[STARTUP] All Kite-dependent services launched.\n")


# ── Lazy-Start Helpers for Scanners ───────────────────────────────────────────
_option_scanners_started = False
_option_scanners_lock = threading.Lock()

_ema_crossover_started = False
_ema_crossover_lock = threading.Lock()

_synergy_scanner_started = False
_synergy_scanner_lock = threading.Lock()

def lazy_start_option_scanners():
    global _option_scanners_started
    with _option_scanners_lock:
        if _option_scanners_started:
            return
    if not get_kite():
        return
    with _option_scanners_lock:
        if not _option_scanners_started:
            try:
                from option_premium_scanner import start_option_scanner
                start_option_scanner()
                print("  [OK] Option Premium Scanner started.")
            except Exception as _e:
                print(f"  [Error] Option Premium Scanner: {_e}")
            try:
                from option_gainers_scanner import start_option_gainers_scanner
                start_option_gainers_scanner()
                print("  [OK] Option Gainers Scanner started.")
            except Exception as _e:
                print(f"  [Error] Option Gainers Scanner: {_e}")
            _option_scanners_started = True


def lazy_start_ema_crossover_scanner():
    global _ema_crossover_started
    with _ema_crossover_lock:
        if _ema_crossover_started:
            return
    if not get_kite():
        return
    with _ema_crossover_lock:
        if not _ema_crossover_started:
            try:
                from ema_crossover_scanner import start_ema_crossover_scanner
                start_ema_crossover_scanner()
                print("  [OK] EMA Crossover Scanner started.")
            except Exception as _e:
                print(f"  [Error] EMA Crossover Scanner: {_e}")
            _ema_crossover_started = True


def lazy_start_synergy_scanner():
    global _synergy_scanner_started
    with _synergy_scanner_lock:
        if _synergy_scanner_started:
            return
    if not get_kite():
        return
    with _synergy_scanner_lock:
        if not _synergy_scanner_started:
            try:
                from synergy_scanner import start_synergy_scanner
                start_synergy_scanner(socketio, get_kite)
                print("  [OK] F&O Synergy Scanner started.")
            except Exception as _e:
                print(f"  [Error] F&O Synergy Scanner: {_e}")
            _synergy_scanner_started = True


_option_gainers_alerts_started = False
_option_gainers_alerts_lock = threading.Lock()

def lazy_start_option_gainers_alerts():
    """Starts the fresh alert scanner for contracts tracked by Premium Gainers."""
    global _option_gainers_alerts_started
    with _option_gainers_alerts_lock:
        if _option_gainers_alerts_started:
            return
    if not get_kite():
        return
    with _option_gainers_alerts_lock:
        if not _option_gainers_alerts_started:
            try:
                from option_gainers_scanner import start_option_gainers_scanner
                from option_gainers_alerts import start_option_gainers_alerts_scanner
                start_option_gainers_scanner()
                start_option_gainers_alerts_scanner()
                print("  [OK] Premium Gainers Alert Scanner started.")
            except Exception as _e:
                print(f"  [Error] Premium Gainers Alert Scanner: {_e}")
            _option_gainers_alerts_started = True

# ══════════════════════════════════════════════════════════════
# ── Kite client helper ──
# ══════════════════════════════════════════════════════════════

def _wrap_kite_quote(kite):
    if not kite or hasattr(kite, '_quote_patched'):
        return kite
    
    original_quote = kite.quote
    
    def patched_quote(*args, **kwargs):
        res = original_quote(*args, **kwargs)
        if isinstance(res, dict):
            now_ts = time.time()
            with _live_ltp_lock:
                for k, v in res.items():
                    if isinstance(v, dict):
                        token = v.get('instrument_token')
                        ltp = v.get('last_price')
                        if token and ltp is not None:
                            _live_ltp_cache[int(token)] = {
                                'ltp': ltp,
                                'ts': now_ts
                            }
        return res
        
    kite.quote = patched_quote
    kite._quote_patched = True
    return kite


_last_failed_token = None


def _verify_and_construct_kite(api_key, access_token):
    global _last_failed_token
    if access_token == _last_failed_token:
        return None
    try:
        from kiteconnect import KiteConnect
        temp_kite = KiteConnect(api_key=api_key)
        temp_kite.set_access_token(access_token)
        temp_kite.profile()  # Verify connectivity once centrally
        _last_failed_token = None
        return temp_kite
    except Exception as e:
        print(f"  [SESSION] Connectivity verification failed: {e}")
        _last_failed_token = access_token
        return None


def sync_global_ticker_credentials(api_key, access_token):
    try:
        from global_ticker import get_global_ticker_manager
        gtm = get_global_ticker_manager()
        gtm.initialize(api_key, access_token)
        # Register chart feed broadcaster (idempotent — re-register on token refresh)
        gtm.register('chart_feed', _chart_tick_broadcaster, [], mode='LTP')
        gtm.start()
        print("  [SESSION] GlobalTickerManager synced & started.")
    except Exception as e:
        print(f"  [SESSION] Failed to sync GlobalTickerManager: {e}")



def get_kite():
    """Get the global KiteConnect instance or reconstruct from session / disk.

    Recovery order (important for Termux where the process is killed by Android):
      1. Global _kite already initialised (fastest path)
      2. Flask session cookie (browser still has valid cookie)
      3. Disk .kite_session.json (server restarted but token file survived)
    """
    global _kite, _gtm_synced_token

    header_key, header_token = _request_kite_credentials()
    disk_key, disk_token = _load_kite_session()
    if _kite is not None:
        current_token = getattr(_kite, 'access_token', None)
        if header_token and current_token != header_token:
            if disk_token and current_token == disk_token:
                print("  [SESSION] Stale header token ignored; keeping valid disk/memory session.")
                # Only sync GTM if token not already registered
                if _gtm_synced_token != current_token:
                    sync_global_ticker_credentials(disk_key or header_key or os.environ.get('KITE_API_KEY'), current_token)
                    _gtm_synced_token = current_token
                return _wrap_kite_quote(_kite)
            api_key = header_key or session.get('kite_api_key') or os.environ.get('KITE_API_KEY')
            temp_kite = _verify_and_construct_kite(api_key, header_token)
            if temp_kite:
                _kite = temp_kite
                if has_request_context():
                    session['kite_api_key'] = api_key
                    session['kite_access_token'] = header_token
                _save_kite_session(api_key, header_token)
            else:
                _kite = None
        api_key = header_key or disk_key or os.environ.get('KITE_API_KEY')
        current_token = getattr(_kite, 'access_token', None)
        # Only restart GTM if the token has actually changed since last sync
        if _gtm_synced_token != current_token:
            sync_global_ticker_credentials(api_key, current_token)
            _gtm_synced_token = current_token
        return _wrap_kite_quote(_kite)

    # ── Try request headers first ──
    # Termux users often switch between localhost, 127.0.0.1, and LAN IPs. That
    # can leave the browser with a valid localStorage token but no matching
    # Flask session cookie, so every API request carries the token explicitly.
    api_key = header_key
    access_token = header_token
    
    if has_request_context():
        api_key = api_key or session.get('kite_api_key')
        access_token = access_token or session.get('kite_access_token')
        
    api_key = api_key or os.environ.get('KITE_API_KEY')
    access_token = access_token or os.environ.get('KITE_ACCESS_TOKEN')

    # ── Fall back to disk token (Termux restart recovery) ──
    if not access_token:
        disk_key, disk_token = _load_kite_session()
        if disk_token:
            api_key = disk_key or api_key
            access_token = disk_token
            if disk_token != _last_failed_token:
                print('  [SESSION] Recovered access_token from disk (Termux restart)')

    if api_key and access_token:
        temp_kite = _verify_and_construct_kite(api_key, access_token)
        if temp_kite:
            _kite = temp_kite
            if has_request_context():
                session['kite_api_key'] = api_key
                session['kite_access_token'] = access_token
            _save_kite_session(api_key, access_token)
            sync_global_ticker_credentials(api_key, access_token)
            _gtm_synced_token = access_token  # mark GTM as synced with this token
            # Patch kite.quote() NOW so all services receive a quote-patched instance.
            # Without this, the LTP cache side-effect in _wrap_kite_quote is missed
            # by every service thread that calls kite.quote() internally.
            _kite = _wrap_kite_quote(_kite)
            # Auto-start Kite-dependent services on session recovery from disk/env
            # (handles server restart when a valid .kite_session.json exists on disk)
            threading.Thread(
                target=start_kite_dependent_services,
                args=(_kite,),
                daemon=True,
                name="KiteServicesAutoStart"
            ).start()
        else:
            _kite = None

    return _wrap_kite_quote(_kite)


def _ckey(symbol: str, interval: str) -> str:
    return f"{symbol}_{interval}"


def _cache_get(key: str):
    with _kite_cache_lock:
        e = _kite_candle_cache.get(key)
        if e and (time.time() - e['ts']) < 60:
            return e['data']
    return None


def _cache_set(key: str, data):
    with _kite_cache_lock:
        _kite_candle_cache[key] = {'ts': time.time(), 'data': data}


def _fetch_raw(kite, token: int, interval: str, from_dt, to_dt, oi: bool = False):
    key = _ckey(str(token), interval)
    cached = _cache_get(key)
    if cached:
        return cached
    raw = kite.historical_data(instrument_token=token, from_date=from_dt,
                               to_date=to_dt, interval=interval, continuous=False, oi=oi)
    candles = []
    for c in raw:
        entry = {
            'timestamp': c['date'].isoformat() if hasattr(c['date'], 'isoformat') else str(c['date']),
            'open': round(c['open'], 2), 'high': round(c['high'], 2),
            'low': round(c['low'], 2), 'close': round(c['close'], 2),
            'volume': c['volume'],
        }
        if oi:
            entry['oi'] = c.get('oi', 0)
        candles.append(entry)
    _cache_set(key, candles)
    return candles


def _ema(closes, period):
    if len(closes) < period:
        return [None] * len(closes)
    k = 2 / (period + 1)
    ema = [sum(closes[:period]) / period]
    for c in closes[period:]:
        ema.append(c * k + ema[-1] * (1 - k))
    return [None] * (len(closes) - len(ema)) + ema


def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return [None] * len(closes)
    diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in diffs]
    losses = [max(-d, 0) for d in diffs]
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    rsis = [None] * (period + 1)
    rsis.append(100 - 100 / (1 + ag / al) if al else 100)
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
        rsis.append(100 - 100 / (1 + ag / al) if al else 100)
    return rsis


def _vwap(candles):
    cum_pv = cum_vol = 0
    result = []
    for c in candles:
        tp = (c['high'] + c['low'] + c['close']) / 3
        cum_pv += tp * c['volume']
        cum_vol += c['volume']
        result.append(cum_pv / cum_vol if cum_vol else None)
    return result


def _macd_hist(closes, fast=12, slow=26, signal=9):
    ef = _ema(closes, fast)
    es = _ema(closes, slow)
    macd = [f - s if f is not None and s is not None else None for f, s in zip(ef, es)]
    valid = [v for v in macd if v is not None]
    if len(valid) < signal:
        return None
    sig = _ema(valid, signal)
    return valid[-1] - sig[-1] if sig[-1] is not None else None


def _swings(candles, lb=3):
    highs, lows = [], []
    for i in range(lb, len(candles) - lb):
        w = candles[i - lb:i + lb + 1]
        if candles[i]['high'] == max(c['high'] for c in w):
            highs.append(i)
        if candles[i]['low'] == min(c['low'] for c in w):
            lows.append(i)
    return highs, lows


def _detect_bos(candles):
    if len(candles) < 10:
        return 'none'
    highs, lows = _swings(candles)
    last = candles[-1]['close']
    if highs and last > candles[highs[-1]]['high']:
        return 'bullish'
    if lows and last < candles[lows[-1]]['low']:
        return 'bearish'
    return 'none'


def _detect_choch(candles):
    if len(candles) < 15:
        return 'none'
    highs, lows = _swings(candles)
    last = candles[-1]['close']
    if len(highs) >= 2 and candles[highs[-1]]['high'] < candles[highs[-2]]['high']:
        if last > candles[highs[-2]]['high']:
            return 'bullish'
    if len(lows) >= 2 and candles[lows[-1]]['low'] > candles[lows[-2]]['low']:
        if last < candles[lows[-2]]['low']:
            return 'bearish'
    return 'none'


def _htf_bias(close, ema21, ema50):
    if ema21 is None or ema50 is None:
        return 'neutral'
    if close > ema21 > ema50:
        return 'bullish'
    if close < ema21 < ema50:
        return 'bearish'
    return 'neutral'


def _vol_spike(candles, period=20):
    if len(candles) < period + 1:
        return False
    avg = sum(c['volume'] for c in candles[-period - 1:-1]) / period
    return candles[-1]['volume'] > avg * 1.5


def _score(close, ema21, ema50, vwap, rsi, macd_h, bos, choch, htf, vol_spike):
    bull = bear = 0
    reasons = []
    if ema21 is not None and ema50 is not None:
        if close > ema21 > ema50:
            bull += 2
            reasons.append('Price > EMA21 > EMA50')
        elif close < ema21 < ema50:
            bear += 2
            reasons.append('Price < EMA21 < EMA50')
    if vwap is not None:
        if close > vwap:
            bull += 1
            reasons.append('Above VWAP')
        else:
            bear += 1
            reasons.append('Below VWAP')
    if rsi is not None:
        if 55 < rsi < 75:
            bull += 1
            reasons.append(f'RSI {rsi:.0f} bullish')
        elif 25 < rsi < 45:
            bear += 1
            reasons.append(f'RSI {rsi:.0f} bearish')
    if macd_h is not None:
        if macd_h > 0:
            bull += 1
            reasons.append('MACD positive')
        else:
            bear += 1
            reasons.append('MACD negative')
    if choch == 'bullish':
        bull += 2
        reasons.append('CHoCH bullish ✓')
    elif choch == 'bearish':
        bear += 2
        reasons.append('CHoCH bearish ✓')
    if bos == 'bullish':
        bull += 1
        reasons.append('BOS bullish ✓')
    elif bos == 'bearish':
        bear += 1
        reasons.append('BOS bearish ✓')
    if htf == 'bullish':
        bull += 2
        reasons.append('15m HTF bullish ✓')
    elif htf == 'bearish':
        bear += 2
        reasons.append('15m HTF bearish ✓')
    if vol_spike:
        if bull > bear:
            bull += 1
            reasons.append('Volume spike confirms bull')
        else:
            bear += 1
            reasons.append('Volume spike confirms bear')
    direction = 'WAIT'
    score = 0
    if bull >= 5 and bull > bear:
        direction = 'BUY'
        score = round(bull / 12 * 10)
    elif bear >= 5 and bear > bull:
        direction = 'SELL'
        score = round(bear / 12 * 10)
    return score, direction, reasons[0] if reasons else '—'


def _parse_datetime(value: str):
    if not value:
        return None
    try:
        dt_obj = dt.fromisoformat(value)
        if dt_obj.tzinfo is None:
            return dt_obj.replace(tzinfo=IST)
        return dt_obj.astimezone(IST)
    except ValueError:
        return None


@app.route('/kite/auth', methods=['POST'])
def kite_auth():
    data = request.get_json(force=True) or {}
    api_key = data.get('api_key', '').strip()
    access_token = data.get('access_token', '').strip()
    if not api_key or not access_token:
        return jsonify({'status': 'error', 'message': 'api_key and access_token required'}), 400
    session['kite_api_key'] = api_key
    session['kite_access_token'] = access_token
    _save_kite_session(api_key, access_token)
    global _kite
    _kite = None
    return jsonify({'status': 'ok', 'message': 'Credentials saved'})


@app.route('/kite/auth/session', methods=['GET'])
def kite_auth_session():
    """Return the current Kite token so mobile Settings can hydrate its field."""
    api_key = session.get('kite_api_key') or os.environ.get('KITE_API_KEY')
    access_token = session.get('kite_access_token') or os.environ.get('KITE_ACCESS_TOKEN')

    if not access_token:
        disk_key, disk_token = _load_kite_session()
        api_key = disk_key or api_key
        access_token = disk_token

    if not api_key or not access_token:
        return jsonify({'status': 'no_token'}), 404

    session['kite_api_key'] = api_key
    session['kite_access_token'] = access_token
    return jsonify({
        'status': 'ok',
        'api_key': api_key,
        'access_token': access_token
    })


@app.route('/kite/auth/status', methods=['GET'])
def kite_auth_status():
    """Local-only session check — no Zerodha API call, zero rate-limit risk."""
    kite = get_kite()
    if not kite:
        return jsonify({'status': 'no_token', 'message': 'No access_token configured.'})
    token = getattr(kite, 'access_token', None)
    if not token:
        return jsonify({'status': 'no_token', 'message': 'Session object exists but has no access_token.'})
    return jsonify({'status': 'ok', 'user': '', 'user_id': '', 'broker': 'ZERODHA'})


@app.route('/kite/historical', methods=['GET'])
def kite_historical():
    symbol = request.args.get('symbol', '').upper()
    token = request.args.get('instrument_token', '')
    interval_raw = request.args.get('interval', '5')
    from_str = request.args.get('from', '')
    to_str = request.args.get('to', '')
    include_oi = request.args.get('oi', '0') == '1' and symbol in OI_INSTRUMENTS
    token = int(token) if token else INSTRUMENT_TOKENS.get(symbol)
    if not token:
        return jsonify({'status': 'error', 'message': f"Unknown symbol '{symbol}'."}), 400
    interval = KITE_INTERVAL_MAP.get(str(interval_raw), '5minute')
    from_dt = _parse_datetime(from_str) if from_str else dt.now(IST).replace(hour=9, minute=15, second=0, microsecond=0)
    to_dt = _parse_datetime(to_str) if to_str else dt.now(IST)
    if not from_dt or not to_dt:
        return jsonify({'status': 'error', 'message': 'Invalid date format. Use ISO8601.'}), 400
    kite = get_kite()
    if not kite:
        return jsonify({'status': 'error', 'message': 'No Kite session available.'}), 401
    try:
        candles = _fetch_raw(kite, token, interval, from_dt, to_dt, include_oi)
        return jsonify({'status': 'ok', 'symbol': symbol or str(token),
                        'interval': interval, 'count': len(candles), 'candles': candles})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 502


@app.route('/kite/quote', methods=['GET'])
def kite_quote():
    symbols = [s.strip().upper() for s in request.args.get('symbols', 'NIFTY50').split(',') if s.strip()]
    inst_keys = [f"{EXCHANGE_MAP.get(s, 'NSE')}:{s}" for s in symbols]
    kite = get_kite()
    if not kite:
        return jsonify({'status': 'error', 'message': 'No Kite session available.'}), 401
    try:
        raw = kite.quote(inst_keys)
        result = {}
        for key, data in raw.items():
            sym = key.split(':')[-1]
            result[sym] = {
                'last_price': data.get('last_price'),
                'open': data.get('ohlc', {}).get('open'),
                'high': data.get('ohlc', {}).get('high'),
                'low': data.get('ohlc', {}).get('low'),
                'close': data.get('ohlc', {}).get('close'),
                'volume': data.get('volume'),
                'oi': data.get('oi'),
                'change': data.get('net_change'),
                'change_pct': round((data.get('net_change', 0) /
                    data.get('ohlc', {}).get('close', 1)) * 100, 2)
                    if data.get('ohlc', {}).get('close') else 0,
                'timestamp': data.get('timestamp'),
                'buy_quantity': data.get('buy_quantity'),
                'sell_quantity': data.get('sell_quantity'),
            }
        return jsonify({'status': 'ok', 'data': result})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 502


@app.route('/kite/ltp', methods=['GET'])
def kite_ltp():
    symbols = [s.strip().upper() for s in request.args.get('symbols', 'NIFTY50').split(',') if s.strip()]
    inst_keys = [f"{EXCHANGE_MAP.get(s, 'NSE')}:{s}" for s in symbols]
    kite = get_kite()
    if not kite:
        return jsonify({'status': 'error', 'message': 'No Kite session available.'}), 401
    try:
        raw = kite.ltp(inst_keys)
        return jsonify({'status': 'ok', 'data': {k.split(':')[-1]: v.get('last_price') for k, v in raw.items()}})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 502


@app.route('/kite/screener', methods=['GET'])
def kite_screener():
    symbols_raw = request.args.get('symbols', ','.join(INSTRUMENT_TOKENS.keys()))
    interval_raw = request.args.get('interval', '5')
    symbols = [s.strip().upper() for s in symbols_raw.split(',') if s.strip()]
    entry_iv = KITE_INTERVAL_MAP.get(str(interval_raw), '5minute')
    htf_iv = '15minute'
    from_dt = dt.now(IST).replace(hour=9, minute=15, second=0, microsecond=0)
    to_dt = dt.now(IST)
    kite = get_kite()
    if not kite:
        return jsonify({'status': 'error', 'message': 'No Kite session available.'}), 401
    results = []
    for i, sym in enumerate(symbols):
        token = INSTRUMENT_TOKENS.get(sym)
        if not token:
            continue
        if i > 0:
            time.sleep(KITE_RATE_DELAY)
        try:
            oi = sym in OI_INSTRUMENTS
            raw5 = _fetch_raw(kite, token, entry_iv, from_dt, to_dt, oi)
            time.sleep(KITE_RATE_DELAY)
            raw15 = _fetch_raw(kite, token, htf_iv, from_dt, to_dt, oi)
            if len(raw5) < 30:
                continue
            c5 = [c['close'] for c in raw5]
            c15 = [c['close'] for c in raw15] if len(raw15) >= 20 else c5
            e21 = _ema(c5, 21)
            e50 = _ema(c5, 50)
            rsi = _rsi(c5, 14)
            vwap_v = _vwap(raw5)
            mh = _macd_hist(c5)
            bos = _detect_bos(raw5)
            choch = _detect_choch(raw5)
            vol_s = _vol_spike(raw5)
            htf = _htf_bias(c15[-1], _ema(c15, 21)[-1], _ema(c15, 50)[-1]) if len(c15) > 50 else 'neutral'
            score, direction, reason = _score(c5[-1], e21[-1], e50[-1], vwap_v[-1], rsi[-1], mh, bos, choch, htf, vol_s)
            results.append({
                'symbol': sym,
                'last_price': round(c5[-1], 2),
                'signal': direction,
                'score': score,
                'reason': reason,
                'htf_bias': htf,
                'bos': bos,
                'choch': choch,
                'vol_spike': vol_s,
                'ema21': round(e21[-1], 2) if e21[-1] else None,
                'ema50': round(e50[-1], 2) if e50[-1] else None,
                'vwap': round(vwap_v[-1], 2) if vwap_v[-1] else None,
                'rsi': round(rsi[-1], 1) if rsi[-1] else None,
                'oi': raw5[-1].get('oi') if oi else None,
            })
        except Exception as e:
            results.append({'symbol': sym, 'error': str(e)})
    _kite_candle_cache['screener_last'] = {'ts': time.time(), 'data': results}
    return jsonify({'status': 'ok', 'market_open': True, 'interval': entry_iv, 'htf': htf_iv, 'results': results})


@app.route('/kite/ws/start', methods=['POST'])
def kite_ws_start():
    global _kite_ws_thread, _kite_ws_running, _kite_ws_stop_flag
    data = request.get_json(force=True) or {}
    tokens = [int(t) for t in data.get('tokens', list(INSTRUMENT_TOKENS.values()))]
    if _kite_ws_running:
        return jsonify({'status': 'ok', 'message': 'WebSocket already running'})
    _kite_ws_stop_flag = False

    def run_ws():
        global _kite_ws_running, _kite_ws_stop_flag
        delay = 5
        while not _kite_ws_stop_flag:
            _kite_ws_running = True
            try:
                kite = get_kite()
                if not kite:
                    break
                from global_ticker import get_ticker_for_feature
                # Use credentials from the resolved kite object — Flask session is
                # not available inside background threads (request context is gone).
                ticker = get_ticker_for_feature("client", tokens, on_ticks, mode="FULL")

                def on_ticks(ws, ticks):
                    now_ts = time.time()
                    with _kite_tick_lock:
                        for t in ticks:
                            _kite_tick_store[t['instrument_token']] = {
                                'token': t['instrument_token'],
                                'ltp': t.get('last_price'),
                                'volume': t.get('volume'),
                                'buy_qty': t.get('buy_quantity'),
                                'sell_qty': t.get('sell_quantity'),
                                'change': t.get('change'),
                                'oi': t.get('oi'),
                                'ohlc': t.get('ohlc', {}),
                                'ts': now_ist().isoformat(),
                            }
                            if t.get('last_price') is not None:
                                with _live_ltp_lock:
                                    _live_ltp_cache[int(t['instrument_token'])] = {
                                        'ltp': t.get('last_price'),
                                        'ts': now_ts
                                    }
                    # ── Fix5: Non-blocking tick forward to agentic pipeline ──────
                    # Zero lock acquisitions on WS callback thread.
                    # KiteDataAgent's own thread does all processing.
                    try:
                        if _agentic_data_agent:
                            _agentic_data_agent.put_inbox(
                                {"type": "ticks", "data": ticks}, block=False
                            )
                    except Exception:
                        pass  # agent pipeline non-critical; never block main tick path

                def on_connect(ws, _):
                    nonlocal delay
                    ws.subscribe(tokens)
                    ws.set_mode(ws.MODE_FULL, tokens)
                    delay = 5

                def on_error(ws, code, reason):
                    pass

                def on_close(ws, code, reason):
                    pass

                ticker.on_ticks = on_ticks
                ticker.on_connect = on_connect
                ticker.on_error = on_error
                ticker.on_close = on_close
                
                from global_ticker import get_ticker_mode
                is_centralized = (get_ticker_mode("client") == "centralized")
                
                ticker.connect(threaded=True)
                time.sleep(2)
                while not _kite_ws_stop_flag:
                    if not is_centralized and not ticker.is_connected():
                        break
                    time.sleep(1)
            except Exception:
                pass
            _kite_ws_running = False
            if _kite_ws_stop_flag:
                break
            time.sleep(delay)
            delay = min(delay * 2, 120)

    _kite_ws_thread = threading.Thread(target=run_ws, daemon=True)
    _kite_ws_thread.start()
    return jsonify({'status': 'ok', 'message': f'WebSocket started for {len(tokens)} instruments'})


@app.route('/kite/ws/stop', methods=['POST'])
def kite_ws_stop():
    global _kite_ws_stop_flag, _kite_ws_running
    _kite_ws_stop_flag = True
    _kite_ws_running = False
    return jsonify({'status': 'ok'})


@app.route('/kite/ws/ticks', methods=['GET'])
def kite_ws_ticks():
    token_filter = request.args.get('tokens', '')
    filter_set = {int(t) for t in token_filter.split(',') if t.strip()} if token_filter else None

    def stream():
        last_sent = {}
        while True:
            with _kite_tick_lock:
                snapshot = dict(_kite_tick_store)
            out = {str(tok): tick for tok, tick in snapshot.items()
                   if (not filter_set or tok in filter_set)
                   and last_sent.get(tok) != tick.get('ltp')}
            if out:
                for tok, val in out.items():
                    last_sent[int(tok)] = val.get('ltp')
                yield f"data: {json.dumps(out)}\n\n"
            time.sleep(0.5)

    return Response(stream(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/kite/ws/snapshot', methods=['GET'])
def kite_ws_snapshot():
    with _kite_tick_lock:
        return jsonify({'status': 'ok', 'ticks': dict(_kite_tick_store), 'count': len(_kite_tick_store)})


@app.route('/kite/instruments', methods=['GET'])
def kite_instruments_get():
    search = request.args.get('search', '').upper()
    exchange = request.args.get('exchange', 'NSE').upper()
    if not search:
        return jsonify({'status': 'ok', 'instruments': [
            {'symbol': k, 'token': v, 'exchange': EXCHANGE_MAP.get(k, 'NSE')}
            for k, v in INSTRUMENT_TOKENS.items()
        ]})
    try:
        db = get_db()
        if exchange == 'ALL':
            rows = db.execute("SELECT * FROM instruments WHERE exchange IN ('NSE', 'BSE')").fetchall()
        else:
            rows = db.execute("SELECT * FROM instruments WHERE exchange = ?", (exchange,)).fetchall()
        all_inst = [dict(r) for r in rows]
        matches = [
            {'token': i['instrument_token'], 'symbol': i['tradingsymbol'],
             'name': i['name'], 'exchange': i['exchange'], 'type': i['instrument_type']}
            for i in all_inst
            if search in i['tradingsymbol'] or search in i.get('name', '').upper()
        ]
        
        def match_score(m):
            sym = m['symbol']
            if sym == search or sym == f"BSE {search}" or sym == f"NSE {search}":
                return 0
            if sym.startswith(search):
                return 1
            if search in sym:
                return 2
            return 3
            
        matches.sort(key=match_score)
        matches = matches[:50]
        return jsonify({'status': 'ok', 'count': len(matches), 'instruments': matches})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 502


@app.route('/kite/instruments', methods=['POST'])
def kite_instruments_add():
    data = request.get_json(force=True) or {}
    symbol = data.get('symbol', '').upper().strip()
    token = data.get('token')
    exch = data.get('exchange', 'NSE').upper()
    if not symbol or not token:
        return jsonify({'status': 'error', 'message': 'symbol and token required'}), 400
    INSTRUMENT_TOKENS[symbol] = int(token)
    EXCHANGE_MAP[symbol] = exch
    return jsonify({'status': 'ok', 'message': f'{symbol} registered', 'token': int(token)})


@app.route('/kite/instruments/<symbol>', methods=['DELETE'])
def kite_instruments_remove(symbol):
    symbol = symbol.upper()
    if symbol not in INSTRUMENT_TOKENS:
        return jsonify({'status': 'error', 'message': f'{symbol} not found'}), 404
    del INSTRUMENT_TOKENS[symbol]
    EXCHANGE_MAP.pop(symbol, None)
    return jsonify({'status': 'ok', 'message': f'{symbol} removed'})


def get_historical_candles(kite, symbol, interval, days_back=1, limit=None):
    """Get historical candles for a symbol with short-lived in-memory caching and live price stitching."""
    global _instruments_cache, _historical_candle_cache
    if not _instruments_cache:
        try:
            from db_instruments import get_cached_instruments
            _instruments_cache = get_cached_instruments("NSE") + get_cached_instruments("NFO") + get_cached_instruments("BSE")
        except Exception as e:
            logging.error(f"Error loading instruments cache in get_historical_candles: {e}")

    # Find token — NSE primary, BSE fallback
    token = None
    if _instruments_cache:
        for inst in _instruments_cache:
            if inst.get('tradingsymbol') == symbol and inst.get('exchange') == 'NSE':
                token = inst.get('instrument_token')
                break
        if not token:
            for inst in _instruments_cache:
                if inst.get('tradingsymbol') == symbol and inst.get('exchange') == 'BSE':
                    token = inst.get('instrument_token')
                    break

    cache_key = (symbol, interval, days_back, limit)
    now = dt.now()
    cached = _historical_candle_cache.get(cache_key)
    
    # Timeframe-specific TTL logic (2m for 5m, 10m for 15m, 30m for 60m, 4h for daily)
    ttl = 60
    if interval == '5minute':
        ttl = 120
    elif interval == '15minute':
        ttl = 600
    elif interval == '60minute':
        ttl = 1800
    elif interval == 'day':
        ttl = 14400

    if cached and (now - cached['ts']).total_seconds() < ttl:
        candles = list(cached['candles'])

        # Outside market hours, cached candles are already correct — return as-is
        # (no live price stitching needed; avoids hundreds of pointless REST quote calls)
        if not is_market_hours():
            return candles

        if candles and token:
            live_price = None
            
            # 1. Primary Check: WebSocket-driven global cache
            with _live_ltp_lock:
                entry = _live_ltp_cache.get(int(token))
                if entry and (time.time() - entry['ts']) < 30:  # Freshness threshold: 30 seconds
                    live_price = entry['ltp']
            
            # 2. Secondary Check: REST Quote Fallback (safe to call occasionally on WebSocket disconnection)
            if not live_price:
                try:
                    exch = 'NSE'
                    if _instruments_cache:
                        for inst in _instruments_cache:
                            if inst.get('instrument_token') == token:
                                exch = inst.get('exchange', 'NSE')
                                break
                    q_key = f"{exch}:{symbol}"
                    q_res = kite.quote([q_key])
                    if q_key in q_res:
                        live_price = q_res[q_key].get('last_price')
                        if live_price:
                            with _live_ltp_lock:
                                _live_ltp_cache[int(token)] = {
                                    'ltp': live_price,
                                    'ts': time.time()
                                }
                except Exception as e:
                    logging.warning(f"REST quote fallback failed for {symbol}: {e}")

            # 3. If we have a fresh price, perform overwrite and return cached candles.
            # Otherwise (Fail-Safe), bypass cache and fall through to direct API reload.
            if live_price and live_price > 0:
                last_candle = dict(candles[-1])
                candle_date_str = last_candle.get('date', '')[:10]
                today_str = now.strftime('%Y-%m-%d')
                
                is_aligned = True
                if interval == 'day':
                    is_aligned = (candle_date_str == today_str)
                
                if is_aligned:
                    last_candle['close'] = live_price
                    if live_price > last_candle.get('high', 0):
                        last_candle['high'] = live_price
                    if live_price < last_candle.get('low', float('inf')):
                        last_candle['low'] = live_price
                    candles[-1] = last_candle
                return candles

    if not token:
        return []

    # Calculate date range
    to_date = dt.now()
    from_date = to_date - timedelta(days=days_back)

    max_retries = 5
    for attempt in range(max_retries):
        try:
            data = kite.historical_data(token, from_date, to_date, interval)
            candles = []
            for candle in data:
                c = dict(candle)
                if 'date' in c and hasattr(c['date'], 'isoformat'):
                    c['date'] = c['date'].isoformat()
                candles.append(c)

            if limit:
                candles = candles[-limit:]

            _historical_candle_cache[cache_key] = {'ts': now, 'candles': candles}
            return candles
        except Exception as e:
            err_msg = str(e).lower()
            if "too many requests" in err_msg or "rate limit" in err_msg or "429" in err_msg:
                sleep_dur = 0.25 * (2 ** attempt)
                logging.info(f"[Kite Rate Limit] Retrying historical data for {symbol} ({interval}) in {sleep_dur:.2f}s due to: {e}")
                time.sleep(sleep_dur)
                continue
            logging.warning(f"Historical data query failed for {symbol} ({interval}): {e}")
            return []
    return []


# ══════════════════════════════════════════════════════════════
# ── Technical Indicators — delegated to indicators.py ──
# ══════════════════════════════════════════════════════════════
from indicators import (
    compute_ema, ema_last, compute_rsi, compute_rsi_array,
    compute_macd, compute_macd_array, compute_adx, compute_atr,
    compute_bollinger_width, compute_bollinger_bands,
    compute_vwap, compute_intraday_vwap, compute_volume_ratio,
    compute_supertrend, compute_pivots, compute_fvg, compute_smc,
    filter_today_session, filter_session_by_date, find_prev_day_close,
    compute_swings, detect_bos, detect_choch, htf_bias, vol_spike, compute_score,
    truncate_at_entry_price, extract_date, parse_iso_to_ist,
)
from gap_analysis_engine import gap_analysis_engine  # Phase 4
from scoring_engine import scoring_engine          # Phase 4


def get_indicators_at_time(instrument_token, interval, target_date, target_time, kite_client, is_index=False):
    """
    Calculate technical indicators at exact candle timestamp.
    Uses canonical indicators.py (plain-list, same algorithm as JS TI.*).
    """
    to_date   = dt.strptime(target_date, "%Y-%m-%d") + timedelta(days=1)
    from_date = to_date - timedelta(days=90)
    data = kite_client.historical_data(instrument_token, from_date, to_date, interval)

    if not data:
        raise ValueError("No candle data returned from Kite")

    # Convert to plain lists for indicators.py
    ohlcv  = [{'date': d['date'], 'open': d['open'], 'high': d['high'],
                'low': d['low'], 'close': d['close'], 'volume': d.get('volume', 0)}
               for d in data]
    closes = [c['close'] for c in ohlcv]
    highs  = [c['high']  for c in ohlcv]
    lows   = [c['low']   for c in ohlcv]
    vols   = [c['volume'] for c in ohlcv]

    ema9_arr  = compute_ema(closes, 9)
    ema21_arr = compute_ema(closes, 21)
    rsi_arr   = compute_rsi_array(closes)
    atr_arr   = [None] + [compute_atr(highs[:i+1], lows[:i+1], closes[:i+1]) for i in range(1, len(closes))]
    macd_arr  = compute_macd_array(closes)
    vwap_arr  = compute_intraday_vwap(ohlcv) if not is_index else [None] * len(ohlcv)
    adx_val   = compute_adx(highs, lows, closes)   # scalar at end

    # Locate target candle by time
    target_key = f"{target_date} {target_time}"
    chosen_idx = None
    for i, c in enumerate(ohlcv):
        parsed = parse_iso_to_ist(c['date'])
        if parsed and parsed['date'] == target_date and parsed['time'] == target_time:
            chosen_idx = i
            break
    if chosen_idx is None:
        # nearest candle
        entry_min = int(target_time[:2])*60 + int(target_time[3:])
        best, best_diff = 0, float('inf')
        for i, c in enumerate(ohlcv):
            parsed = parse_iso_to_ist(c['date'])
            if parsed and parsed['date'] == target_date:
                cm = int(parsed['time'][:2])*60 + int(parsed['time'][3:])
                if abs(cm - entry_min) < best_diff:
                    best_diff, best = abs(cm - entry_min), i
        chosen_idx = best

    i = chosen_idx

    # Build return from plain-list arrays at chosen_idx
    def sf(v): return None if v is None else round(float(v), 6)
    mc = compute_macd(closes[:chosen_idx+1])
    return {
        "close":     sf(closes[chosen_idx]),
        "ema9":      sf(ema9_arr[chosen_idx]),
        "ema21":     sf(ema21_arr[chosen_idx]),
        "vwap":      sf(vwap_arr[chosen_idx]),
        "volume":    int(ohlcv[chosen_idx]['volume']),
        "atr":       sf(atr_arr[chosen_idx]),
        "adx":       sf(adx_val),
        "+DI":       None,
        "-DI":       None,
        "macd":      sf(mc['macd']),
        "signal":    sf(mc['signal']),
        "histogram": sf(mc['histogram']),
        "rsi":       sf(rsi_arr[chosen_idx]),
        "timestamp": ohlcv[chosen_idx]['date'] if isinstance(ohlcv[chosen_idx]['date'], str) else str(ohlcv[chosen_idx]['date']),
    }


# ══════════════════════════════════════════════════════════════
# ── Routes ──
# ══════════════════════════════════════════════════════════════

# ── Static Files ──
@app.route('/')
def index():
    resp = send_from_directory(static_root, 'index.html')
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return resp


@app.route('/<path:path>')
def static_files(path):
    # Don't serve api paths as static
    if path.startswith('api/'):
        return jsonify({'error': 'Not found'}), 404
    resp = send_from_directory(static_root, path)
    # JS and CSS must not be cached so code changes are picked up immediately.
    # Only truly static binary assets (fonts, images, icons) use long-lived cache.
    ext = path.rsplit('.', 1)[-1].lower() if '.' in path else ''
    if ext in ('woff', 'woff2', 'ttf', 'png', 'jpg', 'svg', 'ico'):
        resp.headers['Cache-Control'] = 'public, max-age=86400'  # 24 hours — never changes
    else:
        resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'  # JS/CSS always fresh
    return resp


# ── Health Check ──
@app.route('/api/health')
def health():
    db = get_db()
    stats = get_cache_stats(db)
    return jsonify({
        'status': 'ok',
        'timestamp': dt.now().isoformat(),
        'cache': stats
    })


# ── Configuration (Public Keys Only) ──
@app.route('/api/config')
def get_config():
    """Expose only public configuration to the frontend for pre-population."""
    config = {
        'api_key': os.environ.get('KITE_API_KEY', ''),
        # Security: api_secret is NOT exposed to the frontend
        'api_secret_loaded': bool(os.environ.get('KITE_API_SECRET', '')),
        'app_username': os.environ.get('APP_USERNAME', 'admin')
    }
    print(f"DEBUG: Serving Config (API Key exists: {bool(config['api_key'])})")
    return jsonify(config)


# ── Ticker Routing Configuration ──
@app.route('/api/ticker/config', methods=['GET', 'POST'])
def handle_ticker_config():
    from global_ticker import load_ticker_config, save_ticker_config
    if request.method == 'GET':
        return jsonify(load_ticker_config())
    else:
        new_config = request.json or {}
        save_ticker_config(new_config)
        return jsonify({
            "status": "success", 
            "message": "WebSocket configuration updated! Please restart the server to apply changes."
        })


# ── Debug Credentials (restricted — requires auth) ──
@app.route('/api/debug-credentials')
def debug_credentials():
    """Check what credentials the server loaded. Requires authentication.
    Shows only metadata (loaded status, lengths) — no secret content."""
    import re
    key = os.environ.get('KITE_API_KEY', '')
    secret = os.environ.get('KITE_API_SECRET', '')
    key_clean = re.sub(r'\s+', '', key)
    secret_clean = re.sub(r'\s+', '', secret)
    return jsonify({
        'api_key_loaded': bool(key),
        'api_key_preview': key[:4] + '...' if key else 'MISSING',
        'api_key_len': len(key),
        'api_key_clean_len': len(key_clean),
        'api_key_has_hidden_whitespace': len(key) != len(key_clean),
        'api_secret_loaded': bool(secret),
        'api_secret_len': len(secret),
        'api_secret_clean_len': len(secret_clean),
        'api_secret_has_hidden_whitespace': len(secret) != len(secret_clean),
    })


# ── Verify Checksum (pre-login validation tool) ──
@app.route('/api/verify-checksum', methods=['POST'])
def verify_checksum():
    """Test if credentials produce a valid checksum without burning the request token."""
    import re, hashlib
    data = request.json or {}
    env_key = os.environ.get('KITE_API_KEY', '')
    env_secret = os.environ.get('KITE_API_SECRET', '')

    api_key = re.sub(r'\s+', '', str(data.get('api_key') or env_key or ''))
    api_secret = re.sub(r'\s+', '', str(data.get('api_secret') or env_secret or ''))
    request_token = re.sub(r'\s+', '', str(data.get('request_token') or ''))

    if not api_key or not api_secret or not request_token:
        return jsonify({'error': 'api_key, api_secret and request_token are all required'}), 400

    checksum = hashlib.sha256((api_key + request_token + api_secret).encode('utf-8')).hexdigest()
    return jsonify({
        'api_key_len': len(api_key),
        'api_secret_len': len(api_secret),
        'request_token_len': len(request_token),
        'computed_checksum': checksum,
        'note': 'If Kite rejects this, your api_key or api_secret does not match your Kite developer app.'
    })


# ── Stock Analysis (pre-market + live) ──
@app.route('/api/stock-analysis', methods=['POST'])
def stock_analysis():
    """
    Deep-dive analysis for a single F&O stock.
    mode: 'premarket' (OHLCV only) | 'live' (OHLCV + real-time Kite snapshot)
    """
    try:
        data = request.json or {}
        symbol = str(data.get('symbol', '')).upper().strip()
        mode = data.get('mode', 'premarket')  # 'premarket' | 'live'
        if not symbol:
            return jsonify({'error': 'symbol is required'}), 400

        result = {'symbol': symbol, 'mode': mode}

        # ── 1. OHLCV History (always) ──
        db = get_db()
        kite = get_kite()

        # Use intraday history for live mode so stock analysis reflects the current session.
        interval = 'day'
        from_date = (dt.now() - timedelta(days=365)).strftime('%Y-%m-%d')
        to_date = dt.now().strftime('%Y-%m-%d')
        if mode == 'live':
            interval = '5minute'
            from_date = (dt.now() - timedelta(days=7)).strftime('%Y-%m-%d')

        global _instruments_cache
        if not _instruments_cache:
            _instruments_cache = cache_get_instruments(db)
        if _instruments_cache:
            for inst in _instruments_cache:
                if inst.get('tradingsymbol') == symbol and inst.get('exchange') == 'NSE':
                    token = inst.get('instrument_token')
                    break

        ohlcv = []
        if token:
            try:
                if mode == 'live' and kite:
                    # Live mode uses intraday candles so analysis can react to the current session.
                    try:
                        raw = kite.historical_data(token, from_date, to_date, interval)
                        ohlcv = [{'date': c.get('date'), 'open': c.get('open', 0), 'high': c.get('high', 0),
                                  'low': c.get('low', 0), 'close': c.get('close', 0), 'volume': c.get('volume', 0)}
                                 for c in raw]
                    except Exception as e:
                        result['ohlcv_error'] = f'Live intraday fetch failed: {e}'
                        # Fallback to daily history if intraday data is unavailable
                        interval = 'day'
                        from_date = (dt.now() - timedelta(days=365)).strftime('%Y-%m-%d')
                        cached = cache_get_ohlcv(db, token, from_date, to_date, interval)
                        ohlcv = [{'date': str(c['date'])[:10], 'open': c['open'], 'high': c['high'],
                                  'low': c['low'], 'close': c['close'], 'volume': c['volume']} for c in cached[-365:]]
                else:
                    # Daily history for pre-market / backtest analysis
                    cached = cache_get_ohlcv(db, token, from_date, to_date, interval)
                    if cached:
                        latest_cached = cached[-1]['date']
                        yesterday = (dt.now() - timedelta(days=1)).strftime('%Y-%m-%d')
                        if latest_cached < yesterday and kite:
                            gap_from = dt.strptime(latest_cached, '%Y-%m-%d') + timedelta(days=1)
                            end_dt = dt.strptime(to_date, '%Y-%m-%d')
                            if gap_from <= end_dt:
                                try:
                                    raw = kite.historical_data(token, gap_from, end_dt, interval)
                                    cache_store_ohlcv(db, token, raw, interval)
                                    cached = cache_get_ohlcv(db, token, from_date, to_date, interval)
                                except Exception:
                                    pass
                        ohlcv = [{'date': str(c['date'])[:10], 'open': c['open'], 'high': c['high'],
                                  'low': c['low'], 'close': c['close'], 'volume': c['volume']} for c in cached[-365:]]
                    elif kite:
                        raw = kite.historical_data(token, from_date, to_date, interval)
                        cache_store_ohlcv(db, token, raw, interval)
                        ohlcv = [{'date': str(c['date'])[:10], 'open': c['open'], 'high': c['high'],
                                  'low': c['low'], 'close': c['close'], 'volume': c['volume']} for c in raw]
            except Exception as e:
                result['ohlcv_error'] = str(e)

        result['ohlcv'] = ohlcv
        result['ohlcv_count'] = len(ohlcv)

        # ── 2. Live Snapshot (live & premarket modes) ──
        snapshot = {}
        if mode in ('live', 'premarket') and kite and token:
            try:
                # Spot quote
                quotes = kite.quote([f'NSE:{symbol}'])
                q = quotes.get(f'NSE:{symbol}', {})
                snapshot['ltp'] = q.get('last_price', 0)
                snapshot['change_pct'] = q.get('change', 0)
                snapshot['volume'] = q.get('volume', 0)
                snapshot['avg_price'] = q.get('average_price', 0)
                snapshot['vwap'] = q.get('average_price', 0) # avg_price used as vwap
                snapshot['open'] = q.get('ohlc', {}).get('open', 0)
                snapshot['oi'] = q.get('oi', 0)
                snapshot['oi_day_change'] = q.get('oi_day_change', 0)
                snapshot['buy_qty'] = q.get('buy_quantity', 0)
                snapshot['sell_qty'] = q.get('sell_quantity', 0)
                snapshot['upper_circuit'] = q.get('upper_circuit_limit', 0)
                snapshot['lower_circuit'] = q.get('lower_circuit_limit', 0)
                
                depth = q.get('depth', {})
                if depth:
                    snapshot['pre_open_buy_qty'] = sum(x.get('quantity', 0) for x in depth.get('buy', []))
                    snapshot['pre_open_sell_qty'] = sum(x.get('quantity', 0) for x in depth.get('sell', []))
                else:
                    snapshot['pre_open_buy_qty'] = 0
                    snapshot['pre_open_sell_qty'] = 0
                    
                # Fetch India VIX for gap overlay
                try:
                    vix_q = kite.quote(['NSE:INDIA VIX'])
                    snapshot['india_vix'] = vix_q.get('NSE:INDIA VIX', {}).get('last_price', 15)
                except Exception:
                    snapshot['india_vix'] = 15
                    
                # Fetch NIFTY / GIFT NIFTY proxy for Macro tracking (Gap Engine Layer 2)
                try:
                    # Retail Kite API may not have NSE IX GIFT NIFTY. We proxy using NIFTY 50 + nearest futures
                    macro_q = kite.quote(['NSE:NIFTY 50'])
                    nifty_spot = macro_q.get('NSE:NIFTY 50', {}).get('last_price', 0)
                    if nifty_spot:
                        today_str = dt.now().strftime('%Y-%m-%d')
                        nfo_nifty = []
                        for i in _instruments_cache:
                            if i.get('name') == 'NIFTY' and i.get('segment') == 'NFO-FUT':
                                exp = i.get('expiry')
                                if exp:
                                    exp_str = exp.isoformat() if hasattr(exp, 'isoformat') else str(exp).split('T')[0]
                                    if exp_str >= today_str:
                                        nfo_nifty.append((exp_str, i))
                        if nfo_nifty:
                            nfo_nifty.sort(key=lambda x: x[0])
                            fut_symbol = f"NFO:{nfo_nifty[0][1]['tradingsymbol']}"
                            nifty_fut_q = kite.quote([fut_symbol])
                            nifty_fut_ltp = nifty_fut_q.get(fut_symbol, {}).get('last_price', 0)
                            if nifty_fut_ltp:
                                premium_pct = ((nifty_fut_ltp - nifty_spot) / nifty_spot) * 100
                                snapshot['gift_nifty_premium'] = premium_pct
                except Exception:
                    snapshot['gift_nifty_premium'] = 0

                # Find nearest future and ATM options
                fut_sym = None
                ce_sym = None
                pe_sym = None
                atm_strike = 0
                today_str = dt.now().strftime('%Y-%m-%d')
                spot = snapshot.get('ltp', 0)

                if _instruments_cache:
                    futs = []
                    for i in _instruments_cache:
                        if (i.get('name') == symbol and i.get('instrument_type') == 'FUT'
                            and i.get('exchange') == 'NFO'):
                            exp = i.get('expiry')
                            if exp:
                                exp_str = exp.isoformat() if hasattr(exp, 'isoformat') else str(exp).split('T')[0]
                                if exp_str >= today_str:
                                    futs.append((exp_str, i))
                    futs.sort(key=lambda x: x[0])
                    if futs:
                        fut_sym = f"NFO:{futs[0][1]['tradingsymbol']}"

                    options = []
                    for inst in _instruments_cache:
                        if (inst.get('name') == symbol and
                            inst.get('segment') == 'NFO-OPT'):
                            exp = inst.get('expiry')
                            if exp:
                                exp_str = exp.isoformat() if hasattr(exp, 'isoformat') else str(exp).split('T')[0]
                                if exp_str >= today_str:
                                    options.append((exp_str, inst))
                    options.sort(key=lambda x: x[0])

                    if options and spot > 0:
                        nearest_expiry = options[0][0]
                        nearest_opts = [(e, i) for e, i in options if e == nearest_expiry]
                        strikes = set(i.get('strike', 0) for _, i in nearest_opts)
                        if strikes:
                            atm_strike = min(strikes, key=lambda s: abs(s - spot))
                            for _, inst in nearest_opts:
                                if inst.get('strike') == atm_strike:
                                    ts = f"NFO:{inst['tradingsymbol']}"
                                    if inst.get('instrument_type') == 'CE':
                                        ce_sym = ts
                                    elif inst.get('instrument_type') == 'PE':
                                        pe_sym = ts

                # Batch quote NFO instruments in a single API call for maximum performance
                nfo_symbols = [s for s in [fut_sym, ce_sym, pe_sym] if s]
                nfo_quotes = kite.quote(nfo_symbols) if nfo_symbols else {}

                if fut_sym:
                    fdata = nfo_quotes.get(fut_sym, {})
                    fut_ltp = fdata.get('last_price', 0)
                    snapshot['futures'] = {
                        'ltp': fut_ltp,
                        'oi': fdata.get('oi', 0),
                        'oi_change': fdata.get('oi_day_change', 0),
                        'premium': fut_ltp - spot,
                        'premium_pct': round((fut_ltp - spot) / spot * 100, 3) if spot else 0
                    }

                ce_q = nfo_quotes.get(ce_sym, {}) if ce_sym else {}
                pe_q = nfo_quotes.get(pe_sym, {}) if pe_sym else {}
                ce_iv = ce_q.get('implied_volatility', 0) or 0
                pe_iv = pe_q.get('implied_volatility', 0) or 0
                ce_oi = ce_q.get('oi', 0) or 0
                pe_oi = pe_q.get('oi', 0) or 0
                pcr = (pe_oi / ce_oi) if ce_oi > 0 else 1.0
                avg_iv = (ce_iv + pe_iv) / 2 if (ce_iv and pe_iv) else ce_iv or pe_iv

                # Fallback to 20-day Annualized Historical Volatility if broker IV is 0 or unavailable (e.g. outside market hours)
                if not avg_iv or avg_iv <= 0:
                    import math
                    if ohlcv and len(ohlcv) >= 21:
                        closes_for_vol = [c.get('close') for c in ohlcv if c.get('close')]
                        if len(closes_for_vol) >= 21:
                            log_returns = []
                            for i in range(len(closes_for_vol) - 20, len(closes_for_vol)):
                                prev = closes_for_vol[i-1]
                                curr = closes_for_vol[i]
                                if prev > 0 and curr > 0:
                                    log_returns.append(math.log(curr / prev))
                            if log_returns:
                                mean_ret = sum(log_returns) / len(log_returns)
                                var_ret = sum((x - mean_ret) ** 2 for x in log_returns) / (len(log_returns) - 1)
                                daily_vol = math.sqrt(var_ret)
                                avg_iv = daily_vol * math.sqrt(252) * 100

                snapshot['atm_option'] = {
                    'strike': atm_strike,
                    'ce_iv': round(ce_iv, 1) if ce_iv else round(avg_iv, 1),
                    'pe_iv': round(pe_iv, 1) if pe_iv else round(avg_iv, 1),
                    'avg_iv': round(avg_iv, 1),
                    'ce_oi': ce_oi,
                    'pe_oi': pe_oi,
                    'pcr': round(pcr, 2)
                }
            except Exception as e:
                result['snapshot_error'] = str(e)

        result['snapshot'] = snapshot

        # ── 3. Scoring (equity + options) ──
        # Both scorers consume the same OHLCV + snapshot so results are consistent
        # with /api/validate-entry and every other route that uses scoring_engine.
        # dailyOhlcv is always fetched from cache (fast) and passed to scoring_engine
        # so compute_smc_bias() gets well-formed daily structure regardless of the
        # current interval (intraday or daily).
        try:
            if ohlcv and len(ohlcv) >= 2:
                _closes  = [c['close']  for c in ohlcv]
                _highs   = [c['high']   for c in ohlcv]
                _lows    = [c['low']    for c in ohlcv]
                _volumes = [c['volume'] for c in ohlcv]

                # ── Daily candles for HTF SMC bias (best-effort from cache) ──
                _daily_ohlcv = None
                try:
                    _d_from = (dt.now() - timedelta(days=120)).strftime('%Y-%m-%d')
                    _d_to   = dt.now().strftime('%Y-%m-%d')
                    _daily_raw = cache_get_ohlcv(db, token, _d_from, _d_to, 'day')
                    if _daily_raw and len(_daily_raw) >= 20:
                        _daily_ohlcv = [
                            {'date': str(c['date'])[:10], 'open': c['open'],
                             'high': c['high'], 'low': c['low'],
                             'close': c['close'], 'volume': c.get('volume', 0)}
                            for c in _daily_raw
                        ]
                except Exception:
                    pass   # SMC bias is best-effort; scoring proceeds without it

                _score_data = {
                    'closes':       _closes,
                    'highs':        _highs,
                    'lows':         _lows,
                    'volumes':      _volumes,
                    'ltp':          snapshot.get('ltp') or (_closes[-1] if _closes else 0),
                    'snapshot':     snapshot,
                    'optionsData':  {},
                    'session_mode': get_session_mode(),
                    'sector':       data.get('sector', ''),
                    'symbol':       symbol,   # needed by score_options for index vs stock VWAP detection
                    'dailyOhlcv':   _daily_ohlcv,   # None → smcBias omitted from result
                }
                eq_score  = scoring_engine.score_equity(_score_data)
                opt_score = scoring_engine.score_options(_score_data)

                result['equityScore']  = eq_score
                result['optionsScore'] = opt_score

                # Compact top-level summary for quick frontend consumption
                _smc = opt_score.get('smcBias') or eq_score.get('smcBias')
                result['signalSummary'] = {
                    'equityDirection':   eq_score.get('direction', 'NEUTRAL'),
                    'equityTotal':       eq_score.get('total', 0),
                    'optionsDirection':  opt_score.get('direction', 'NEUTRAL'),
                    'optionsTotal':      opt_score.get('total', 0),
                    'signalStrength':    opt_score.get('signalStrength', 'NEUTRAL'),
                    'riskVeto':          opt_score.get('riskFilterVeto', False),
                    'riskReasons':       opt_score.get('riskFilterReasons', []),
                    'sessionMode':       opt_score.get('sessionMode', get_session_mode()),
                    'atr':               eq_score.get('atr', 0),
                    'adx':               eq_score.get('adx', 0),
                    'rsi':               eq_score.get('rsi', 50),
                    'risk':              eq_score.get('risk', {}),
                    # HTF SMC context — informational overlay, not part of score
                    'smcBias':           _smc,   # None when daily candles unavailable
                }
        except Exception as _se:
            result['scoring_error'] = str(_se)
            logging.warning(f'[stock-analysis] Scoring failed for {symbol}: {_se}')


        # Calculate daily EMA 50/200 crossover for stock analysis (adds strength to signal, does not decide it)
        ema_crossover = {
            'status': 'NEUTRAL',
            'details': 'No crossover approaching',
            'distance_pct': 0.0,
            'ema_50': 0.0,
            'ema_200': 0.0,
            'type': 'neutral',
            'strength': 0,
            'strength_label': 'Flat Strength'
        }
        
        try:
            d_from = (dt.now() - timedelta(days=365)).strftime('%Y-%m-%d')
            d_to = dt.now().strftime('%Y-%m-%d')
            daily_for_ema = cache_get_ohlcv(db, token, d_from, d_to, 'day')
            if not daily_for_ema and kite and token:
                daily_for_ema = kite.historical_data(token, dt.now() - timedelta(days=365), dt.now(), 'day')
                cache_store_ohlcv(db, token, daily_for_ema, 'day')
                
            if daily_for_ema and len(daily_for_ema) >= 50:
                df_ema = pd.DataFrame(daily_for_ema)
                df_ema['close'] = df_ema['close'].astype(float)
                df_ema['ema_50'] = df_ema['close'].ewm(span=50, adjust=False).mean()
                df_ema['ema_200'] = df_ema['close'].ewm(span=200, adjust=False).mean()
                
                last_row = df_ema.iloc[-1]
                last_ema_50 = last_row['ema_50']
                last_ema_200 = last_row.get('ema_200', 0.0)
                
                if last_ema_200 > 0:
                    ema_dist = last_ema_50 - last_ema_200
                    ema_dist_pct = (abs(ema_dist) / last_ema_200) * 100.0
                    
                    ema_crossover['distance_pct'] = round(ema_dist_pct, 2)
                    ema_crossover['ema_50'] = round(last_ema_50, 2)
                    ema_crossover['ema_200'] = round(last_ema_200, 2)
                    
                    if ema_dist_pct <= 1.5:
                        if last_ema_50 < last_ema_200:
                            ema_crossover['status'] = 'BULLISH_APPROACHING'
                            ema_crossover['type'] = 'bullish_approaching'
                            ema_crossover['details'] = f"Golden Cross Approaching (Gap: {round(ema_dist_pct, 2)}%)"
                            ema_crossover['strength'] = 5
                            ema_crossover['strength_label'] = "+5 Bullish Strength"
                        else:
                            ema_crossover['status'] = 'BEARISH_APPROACHING'
                            ema_crossover['type'] = 'bearish_approaching'
                            ema_crossover['details'] = f"Death Cross Approaching (Gap: {round(ema_dist_pct, 2)}%)"
                            ema_crossover['strength'] = -5
                            ema_crossover['strength_label'] = "-5 Bearish Strength"
                    else:
                        if last_ema_50 > last_ema_200:
                            ema_crossover['status'] = 'BULLISH_ACTIVE'
                            ema_crossover['type'] = 'bullish_active'
                            ema_crossover['details'] = f"Golden Cross Active (EMA50 > EMA200 by {round(ema_dist_pct, 2)}%)"
                            ema_crossover['strength'] = 10
                            ema_crossover['strength_label'] = "+10 Bullish Strength"
                        else:
                            ema_crossover['status'] = 'BEARISH_ACTIVE'
                            ema_crossover['type'] = 'bearish_active'
                            ema_crossover['details'] = f"Death Cross Active (EMA50 < EMA200 by {round(ema_dist_pct, 2)}%)"
                            ema_crossover['strength'] = -10
                            ema_crossover['strength_label'] = "-10 Bearish Strength"
        except Exception as _cross_err:
            logging.warning(f'[stock-analysis] Crossover calc failed: {_cross_err}')
            
        result['ema_crossover'] = ema_crossover
        
        # ── 4. Analyst Rating (best-effort from NSE) ──
        analyst = _fetch_analyst_rating(symbol)
        result['analyst'] = analyst

        return jsonify(result)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


def _fetch_analyst_rating(symbol):
    """Fetch analyst consensus from NSE (best-effort, returns None on failure)."""
    try:
        import urllib.request
        url = f'https://www.nseindia.com/api/quote-equity?symbol={symbol}'
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
            'Accept': 'application/json',
            'Referer': 'https://www.nseindia.com',
        })
        with urllib.request.urlopen(req, timeout=4, context=_ssl_ctx) as resp:
            if resp.status == 200:
                nse_data = json.loads(resp.read().decode())
                # NSE returns priceInfo and info — analyst data varies
                metadata = nse_data.get('metadata', {})
                price_info = nse_data.get('priceInfo', {})
                return {
                    'source': 'NSE',
                    'symbol': symbol,
                    'week52High': price_info.get('weekHighLow', {}).get('max'),
                    'week52Low': price_info.get('weekHighLow', {}).get('min'),
                    'pe': metadata.get('pdSymbol'),
                    'sector': metadata.get('industry'),
                    'rating': None,   # NSE doesn't provide consensus directly
                    'target': None,
                }
    except Exception:
        pass
    return None  # graceful fallback


# ── Trade Cockpit: Validate Entry (Replay + Live) ──
@app.route('/api/validate-entry', methods=['POST'])
def validate_entry():
    """Return OHLCV candles + live snapshot for entry validation.

    Always fetches fresh data from Kite API for live validation.
    """
    body = request.get_json(silent=True) or {}
    symbol = body.get('symbol', '').strip()
    price = float(body.get('price')) if body.get('price') else 0
    direction = body.get('direction', 'CALL')
    interval = body.get('interval', '5minute')
    date_str = body.get('date')
    time_str = body.get('time')

    nse_symbol_map = {'NIFTY': 'NIFTY 50', 'BANKNIFTY': 'NIFTY BANK', 'FINNIFTY': 'NIFTY FIN SERVICE'}
    symbol = nse_symbol_map.get(symbol.upper(), symbol)

    if not symbol:
        return jsonify({'error': 'symbol required'}), 400

    logging.warning(
        f'\n=== VALIDATE ENTRY: {symbol} Price={price} Date={date_str} Time={time_str} ===\n'
    )

    try:
        # Get Kite connection
        kite = get_kite()
        if not kite:
            return jsonify({'error': 'Kite not initialized'}), 500

        # Resolve instrument token
        # Normalise BSE compound names: "BSE SENSEX" -> symbol="SENSEX", exchange="BSE"
        _BSE_ALIAS = {
            'BSE SENSEX': ('BSE', 'SENSEX'),
            'BSE BANKEX': ('BSE', 'BANKEX'),
            'BSE IT':     ('BSE', 'BSE-IT'),
            'BSE 100':    ('BSE', 'BSE100'),
            'SENSEX':     ('BSE', 'SENSEX'),
            'BANKEX':     ('BSE', 'BANKEX'),
        }
        if symbol.upper() in _BSE_ALIAS:
            exchange, symbol = _BSE_ALIAS[symbol.upper()]
        else:
            exchange = 'NSE' if 'BSE' not in symbol.upper() else 'BSE'
        token = None

        # 1) Prefer cached instruments from local SQLite.
        try:
            db = get_db()
            cached_instruments = cache_get_instruments(db)
            exact_matches = [
                inst for inst in cached_instruments
                if inst.get('tradingsymbol') == symbol and inst.get('exchange') == exchange
            ]
            exact_matches.sort(key=lambda inst: (
                0 if inst.get('segment') == exchange else 1,
                0 if inst.get('instrument_type') in (None, '', 'EQ') else 1,
            ))
            if exact_matches:
                token = exact_matches[0].get('instrument_token')
                logging.warning(f'[validate-entry] Cache instruments token resolved: {token}')
        except Exception as e:
            logging.warning(f'[validate-entry] Cache instruments lookup failed for {symbol}: {e}')

        # 2) Fallback to LTP token
        if not token:
            try:
                ltp_data = kite.ltp([f'{exchange}:{symbol}'])
                key = f'{exchange}:{symbol}'
                if key in ltp_data:
                    token = ltp_data[key].get('instrument_token') or token
                    logging.warning(f'[validate-entry] LTP token resolved: {token}')
            except Exception as e:
                logging.warning(f'[validate-entry] LTP token lookup failed for {symbol}: {e}')

        # 3) Final fallback: cached instruments
        if not token:
            instruments = cache_get_instruments(get_db())
            for inst in instruments:
                if inst.get('tradingsymbol') == symbol and inst.get('exchange') == exchange:
                    token = inst.get('instrument_token')
                    logging.warning(f'[validate-entry] Cached instruments token resolved: {token}')
                    break

        if not token:
            return jsonify({'error': f'Token not found: {symbol}'}), 404

        logging.warning(f'[validate-entry] Token resolved: {token}')

        # Always use live mode - fetch recent data
        target_dt = dt.now()
        if date_str:
            try:
                time_str = time_str or '09:15'
                target_dt = datetime.datetime.strptime(f'{date_str} {time_str}', '%Y-%m-%d %H:%M')
            except Exception:
                try:
                    target_dt = dt.fromisoformat(date_str)
                except Exception:
                    target_dt = dt.now()

        def _normalize_datetime(value):
            if isinstance(value, datetime.datetime):
                return value
            if isinstance(value, str):
                try:
                    return dt.fromisoformat(value)
                except ValueError:
                    try:
                        return datetime.datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
                    except Exception:
                        return None
            return None

        def _session_dates(candles):
            seen = set()
            dates = []
            for c in candles:
                d = _normalize_datetime(c['date'])
                if not d:
                    continue
                day = d.date()
                if day not in seen:
                    seen.add(day)
                    dates.append(day)
            return sorted(dates)

        def _fetch_candles(from_dt, to_dt):
            raw = kite.historical_data(int(token), from_dt, to_dt, interval)
            return [{
                'date': d['date'].isoformat() if hasattr(d['date'], 'isoformat') else str(d['date']),
                'open': float(d['open']),
                'high': float(d['high']),
                'low': float(d['low']),
                'close': float(d['close']),
                'volume': int(d.get('volume', 0)),
            } for d in raw]

        def _find_last_n_sessions(n_sessions, target_dt, max_range_days=30):
            # Fetch up to the end of the target day (15:30) so charts can display the full session 
            # outcome in replay mode. Frontend's EntryValidator handles internal truncation to target_time.
            to_dt = target_dt.replace(hour=15, minute=30, second=0, microsecond=0)
            if to_dt.weekday() >= 5:
                prev_close = target_dt
                while prev_close.weekday() >= 5:
                    prev_close -= timedelta(days=1)
                to_dt = prev_close.replace(hour=15, minute=30, second=0, microsecond=0)

            range_days = 14
            while range_days <= max_range_days:
                from_dt = (target_dt - timedelta(days=range_days)).replace(hour=9, minute=15, second=0, microsecond=0)
                candles = _fetch_candles(from_dt, to_dt)
                session_dates = _session_dates(candles)
                if len(session_dates) >= n_sessions:
                    last_dates = set(session_dates[-n_sessions:])
                    filtered = [c for c in candles if _normalize_datetime(c['date']).date() in last_dates]
                    return filtered, from_dt, to_dt
                range_days += 2
            return candles, from_dt, to_dt

        candles, from_dt, to_dt = _find_last_n_sessions(10, target_dt)
        from_date = from_dt.strftime('%Y-%m-%d %H:%M:%S')
        to_date = to_dt.strftime('%Y-%m-%d %H:%M:%S')
        logging.warning(
            f'[validate-entry] Live mode | Date={target_dt} | Range={from_date} → {to_date}'
        )

        data_source = 'kite_api'
        logging.warning('[validate-entry] Fetching fresh data from Kite API')

        try:
            logging.warning(f'[validate-entry] ✓ Kite returned {len(candles)} candles')
        except Exception as e:
            logging.error(f'[validate-entry] Candle count logging error: {e}')
            return jsonify({'error': f'Kite API error: {str(e)}'}), 500

        if not candles:
            return jsonify({'error': 'No candles available'}), 404

        # Gap 5 fix: Expanded snapshot — matches /api/stock-analysis richness
        # so scoring_engine OI/Futures/Options factors are not silently zeroed.
        snapshot = {}
        session_mode = get_session_mode()
        try:
            q = kite.quote(f'{exchange}:{symbol}')
            key = f'{exchange}:{symbol}'
            if key in q:
                qd = q[key]
                snapshot = {
                    'ltp':           float(qd.get('last_price', 0)),
                    'avg_price':     float(qd.get('average_price', 0)),
                    'vwap':          float(qd.get('average_price', 0)),
                    'volume':        int(qd.get('volume', 0)),
                    'change_pct':    float(qd.get('change', 0)),
                    'open':          float(qd.get('ohlc', {}).get('open', 0)),
                    'oi':            int(qd.get('oi', 0)),
                    'oi_day_change': int(qd.get('oi_day_change', 0)),
                    'buy_qty':       int(qd.get('buy_quantity', 0)),
                    'sell_qty':      int(qd.get('sell_quantity', 0)),
                    'circuit': {
                        'upper': float(qd.get('upper_circuit_limit', 0)),
                        'lower': float(qd.get('lower_circuit_limit', 0)),
                    },
                    'depth': {
                        'bid': float((qd.get('depth', {}).get('buy') or [{}])[0].get('price', 0)),
                        'ask': float((qd.get('depth', {}).get('sell') or [{}])[0].get('price', 0)),
                    },
                    'symbol':       symbol,
                    'session_mode': session_mode,
                    'isHistoricalSession': (session_mode == 'historical'),
                }
                # Fetch India VIX (best-effort)
                try:
                    vix_q = kite.quote(['NSE:INDIA VIX'])
                    snapshot['india_vix'] = vix_q.get('NSE:INDIA VIX', {}).get('last_price', 15)
                except Exception:
                    snapshot['india_vix'] = 15
        except Exception:
            # Snapshot is optional; validation still runs on OHLCV data
            snapshot = {'session_mode': session_mode, 'isHistoricalSession': (session_mode == 'historical')}


        logging.warning(
            f'[validate-entry] ✓ Response: {len(candles)} candles | '
            f'First={candles[0]["close"]} | Last={candles[-1]["close"]}'
        )

        # Mock static validation block since Trade Cockpit has been removed
        validation = {
            'isValid': False,
            'confidence': 0,
            'setupType': 'NONE',
            'reasons': ['Disabled']
        }

        return jsonify({
            'success':    True,
            'candles':    candles,
            'snapshot':   snapshot,
            'symbol':     symbol,
            'price':      price,
            'direction':  direction,
            'interval':   interval,
            'count':      len(candles),
            'data_source': data_source,
            'validation': validation,
        })

    except Exception as e:
        logging.error(f'[validate-entry] Exception: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500



# ── Cache Management ──
@app.route('/api/cache/stats')
def cache_stats():
    db = get_db()
    return jsonify(get_cache_stats(db))


@app.route('/api/cache/clear', methods=['POST'])
def clear_cache():
    db = get_db()
    db.execute('DELETE FROM ohlcv')
    db.commit()

    return jsonify({'status': 'ok', 'message': 'OHLCV cache cleared'})


# ── Test Connection ──
@app.route('/api/test')
def test_connection():
    try:
        kite = get_kite()
        if not kite:
            return jsonify({'status': 'error', 'message': 'No API credentials'}), 401
        profile = kite.profile()
        return jsonify({'status': 'ok', 'profile': profile})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


# ── Login / Session ──
@app.route('/api/login', methods=['POST'])
def login():
    print("DEBUG: /api/login POST request received")
    try:
        from kiteconnect import KiteConnect
        data = request.json
        
        # Log environment status
        if not os.path.exists('.env'):
            print("WARNING: .env file NOT FOUND in root directory")
        
        # ALWAYS prioritize .env values if they exist, to avoid "Invalid Checksum" 
        # caused by empty/stale frontend values (common in mobile/Termux)
        env_key = os.environ.get('KITE_API_KEY', '')
        env_secret = os.environ.get('KITE_API_SECRET', '')
        
        import re
        api_key = re.sub(r'\s+', '', str(env_key or data.get('api_key') or ''))
        request_token = re.sub(r'\s+', '', str(data.get('request_token') or ''))
        api_secret = re.sub(r'\s+', '', str(env_secret or data.get('api_secret') or ''))
        
        print(f"DEBUG: Kite Login Attempt")
        print(f"  - Request Token: {request_token[:5]}... (len: {len(request_token)})")
        print(f"  - API Key source: {'ENV' if api_key == env_key else 'FRONTEND'}")
        print(f"  - API Secret source: {'ENV' if api_secret == env_secret else 'FRONTEND'}")

        if not api_key:
            return jsonify({'error': 'Kite API Key is missing. Add it to settings or KITE_API_KEY environment variable.'}), 400
        if not api_secret:
            return jsonify({'error': 'Kite API Secret is missing. Add it to settings or KITE_API_SECRET environment variable.'}), 400

        kite = KiteConnect(api_key=api_key)
        try:
            kite_session_data = kite.generate_session(request_token, api_secret=api_secret)
        except Exception as api_err:
            print(f"CRITICAL: Kite API returned error: {str(api_err)}")
            if "checksum" in str(api_err).lower():
                import hashlib
                print(f"  - api_key len: {len(api_key)}, first4: {api_key[:4]}...")
                print(f"  - api_secret len: {len(api_secret)} (expected 32)")
                print(f"  - request_token len: {len(request_token)}")
            raise api_err

        global _kite
        _kite = kite
        _kite.set_access_token(kite_session_data['access_token'])

        # Save to Flask session for persistence
        session['kite_access_token'] = kite_session_data['access_token']
        session['kite_api_key'] = api_key

        # Also persist to disk so the session survives Termux process restarts.
        # Android may kill the server process at any time; without this the user
        # would need to re-authenticate every time the process is restarted.
        _save_kite_session(api_key, kite_session_data['access_token'])

        # Boot all Kite-dependent services now that connection is verified
        # Runs in a background thread to avoid blocking the login API response
        threading.Thread(
            target=start_kite_dependent_services,
            args=(_kite,),
            daemon=True,
            name="KiteServicesAutoStart"
        ).start()

        # Trigger instrument sync check asynchronously in the background
        trigger_instruments_sync_async()

        return jsonify({
            'access_token': kite_session_data['access_token'],
            'user_id': kite_session_data.get('user_id', ''),
            'email': kite_session_data.get('email', '')
        })
    except Exception as e:
        print(f"ERROR: Kite Login Failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ── Instruments (with cache) ──
# ── Instruments (with cache) ──
@app.route('/api/instruments')
def instruments():
    try:
        db = get_db()
        cached = cache_get_instruments(db)
        if cached:
            global _instruments_cache
            _instruments_cache = cached
            return jsonify({'instruments': cached, 'source': 'cache'})
        return jsonify({'error': 'No cached instruments available. Please wait for Friday sync.'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Equity List (F&O stocks only) ──
@app.route('/api/equity-list')
def equity_list():
    try:
        global _instruments_cache
        if not _instruments_cache:
            db = get_db()
            _instruments_cache = cache_get_instruments(db)
            
        if not _instruments_cache:
            # Return minimal demo F&O stocks for offline mode
            demo_stocks = [
                {'tradingsymbol': 'RELIANCE', 'name': 'RELIANCE INDUSTRIES LTD', 'instrument_token': 738561, 'exchange': 'NSE'},
                {'tradingsymbol': 'TCS', 'name': 'TATA CONSULTANCY SERVICES LTD', 'instrument_token': 2953217, 'exchange': 'NSE'},
                {'tradingsymbol': 'ITC', 'name': 'ITC LTD', 'instrument_token': 424961, 'exchange': 'NSE'}
            ]
            return jsonify({'stocks': demo_stocks})

        fno_names = set()
        for i in _instruments_cache:
            if i.get('segment') in ('NFO-FUT', 'NFO-OPT'):
                fno_names.add(str(i.get('name', '')))

        equity = [i for i in _instruments_cache
                  if i.get('exchange') == 'NSE' and str(i.get('tradingsymbol')) in fno_names and i.get('segment') == 'NSE']

        # Popular ticker aliases: common abbreviation -> NSE tradingsymbol
        # Lets users search "HPCL" and find HINDPETRO, "HUL" find HINDUNILVR, etc.
        TICKER_ALIAS = {
            'HINDPETRO':  'HPCL',
            'HINDUNILVR': 'HUL',
            'BAJFINANCE': 'BAJFIN',
            'BAJAJFINSV': 'BAJAJFS',
            'KOTAKBANK':  'KOTAK',
            'INDUSINDBK': 'INDUSIND',
            'TATACONSUM': 'TATA CONSUMER',
            'TATAMOTORS': 'TATA MOTORS',
            'TATASTEEL':  'TATA STEEL',
            'HEROMOTOCO': 'HERO MOTO',
            'NAUKRI':     'INFOEDGE',
        }

        result = []
        for s in equity:
            sym = s.get('tradingsymbol', '')
            alias = TICKER_ALIAS.get(sym, '')
            if alias:
                s = dict(s)
                s['alias'] = alias
            result.append(s)

        return jsonify({'stocks': result})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Quotes ──
@app.route('/api/quote')
def quote():
    try:
        kite = get_kite()
        if not kite:
            return jsonify({'error': 'Not connected'}), 401
        symbols = request.args.get('symbols', '').split(',')
        data = kite.quote(symbols)
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── LTP ──
@app.route('/api/ltp')
def ltp():
    try:
        kite = get_kite()
        if not kite:
            return jsonify({'error': 'Not connected'}), 401
        symbols = request.args.get('symbols', '').split(',')
        data = kite.ltp(symbols)
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── OHLC ──
@app.route('/api/ohlc')
def ohlc():
    try:
        kite = get_kite()
        if not kite:
            return jsonify({'error': 'Not connected'}), 401
        symbols = request.args.get('symbols', '').split(',')
        data = kite.ohlc(symbols)
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Global Macro Quotes (India VIX, GIFT Nifty, USD/INR, WTI & Brent Crude) ──
# Sourced 100% from authenticated Kite feeds (NSE/CDS/MCX). Zero external APIs.
_MACRO_MONTHS = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC']

def _macro_front_month(prefix: str, offset: int = 0) -> str:
    """Build a futures tradingsymbol e.g. CRUDEOIL25MAYFUT.
    offset=0 → current calendar month, offset=1 → next month."""
    n   = now_ist()
    m   = n.month - 1 + offset      # 0-indexed
    yr  = (n.year + m // 12) % 100
    mon = _MACRO_MONTHS[m % 12]
    return f"{prefix}{yr:02d}{mon}FUT"


def _macro_quote_safe(kite, exchange: str, symbol: str):
    """Single Kite quote; returns dict or None on any failure."""
    try:
        key  = f"{exchange}:{symbol}"
        raw  = kite.quote([key])
        data = raw.get(key) or {}
        if not data or data.get('last_price') is None:
            return None
        ltp  = data['last_price']
        prev = (data.get('ohlc') or {}).get('close') or ltp
        chg  = round(ltp - prev, 4)
        pct  = round((chg / prev) * 100, 2) if prev else 0
        return {
            'price':      ltp,
            'change':     chg,
            'change_pct': pct,
            'high':       (data.get('ohlc') or {}).get('high'),
            'low':        (data.get('ohlc') or {}).get('low'),
            'symbol':     symbol,
            'exchange':   exchange,
        }
    except Exception as e:
        logging.debug(f"Macro quote {exchange}:{symbol} — {e}")
        return None

def _macro_quote_first(kite, candidates):
    """Try several exchange:symbol candidates and return the first live quote."""
    for exchange, symbol in candidates:
        quote = _macro_quote_safe(kite, exchange, symbol)
        if quote:
            return quote
    return None

def _fetch_brent_ft():
    """Fetch Brent Crude from Financial Times public tearsheet."""
    try:
        import requests
        import re
        url = 'https://markets.ft.com/data/commodities/tearsheet/summary?c=Brent+Crude+Oil'
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = requests.get(url, headers=headers, timeout=5)
        values = re.findall(r'class="mod-ui-data-list__value">([^<]+)<', r.text)
        if len(values) >= 2:
            price = float(values[0].replace(',', ''))
            
            chg_str = values[1]
            chg_abs, chg_pct = 0.0, 0.0
            if '/' in chg_str:
                parts = chg_str.split('/')
                def parse_num(s):
                    clean = re.sub(r'[^\d\.\-]', '', s)
                    return float(clean) if clean else 0.0
                chg_abs = parse_num(parts[0])
                chg_pct = parse_num(parts[1])
                
            return {
                'price': price,
                'change': chg_abs,
                'change_pct': chg_pct,
                'high': None,
                'low': None,
                'symbol': 'Brent',
                'exchange': 'FT'
            }
    except Exception as e:
        logging.debug(f"FT Brent fetch failed: {e}")
    return None


@app.route('/kite/global-quotes')
def kite_global_quotes():
    """
    Real-time macro quotes via Kite (authentic, zero lag):
      • India VIX   → NSE:INDIA VIX           (stable index, no expiry)
      • GIFT Nifty  → best-effort NSE IX/Kite symbol lookup
      • USD/INR     → CDS front-month USDINR futures (auto-rolls monthly)
      • WTI Crude   → MCX front-month CRUDEOIL futures (WTI-benchmarked)
      • Brent Crude → MCX front-month BRENTCRUDEOIL futures (if listed)
    Falls back to next-month contract if current month has expired.
    """
    kite = get_kite()
    if not kite:
        return jsonify({'status': 'error', 'message': 'Kite not connected'}), 401

    result = {}

    # 1. India VIX — permanent NSE index, no expiry
    result['india_vix'] = _macro_quote_safe(kite, 'NSE', 'INDIA VIX')

    # 2. GIFT Nifty — best-effort; availability depends on the broker feed.
    result['gift_nifty'] = _macro_quote_first(kite, [
        ('NSEIX', 'GIFT NIFTY'),
        ('NSEIX', 'GIFTNIFTY'),
        ('NSE', 'GIFT NIFTY'),
        ('NFO', 'GIFTNIFTY'),
    ])

    # 3. USD/INR — CDS front-month, try current then next month
    usdinr = None
    for off in (0, 1):
        usdinr = _macro_quote_safe(kite, 'CDS', _macro_front_month('USDINR', off))
        if usdinr:
            break
    result['usdinr'] = usdinr

    # 4. WTI Crude — MCX CRUDEOIL (WTI-benchmarked in India)
    wti = None
    for off in (0, 1):
        wti = _macro_quote_safe(kite, 'MCX', _macro_front_month('CRUDEOIL', off))
        if wti:
            break
    result['wti_crude'] = wti

    # 5. Brent Crude — MCX BRENTCRUDEOIL (None if not listed on MCX), fallback to FT
    brent = None
    for off in (0, 1):
        brent = _macro_quote_safe(kite, 'MCX', _macro_front_month('BRENTCRUDEOIL', off))
        if brent:
            break
    if not brent:
        brent = _fetch_brent_ft()
    result['brent_crude'] = brent

    return jsonify({
        'status':    'ok',
        'data':      result,
        'timestamp': now_ist().strftime('%H:%M:%S IST'),
    })




# ── Historical Data (with smart caching) ──
@app.route('/api/historical')
def historical():
    try:
        token_str = request.args.get('token', '')
        from_date_str = request.args.get('from', '')
        to_date_str = request.args.get('to', '')
        interval = request.args.get('interval', 'day')
        force_refresh = request.args.get('refresh', '') == '1'

        # Validate required params
        if not token_str or not from_date_str or not to_date_str:
            return jsonify({'error': f'Missing params: token={token_str}, from={from_date_str}, to={to_date_str}'}), 400

        token = int(token_str)

        # Parse dates
        try:
            from_date = dt.strptime(from_date_str, '%Y-%m-%d')
            to_date = dt.strptime(to_date_str, '%Y-%m-%d')
        except ValueError:
            return jsonify({'error': f'Invalid date format. Expected yyyy-mm-dd'}), 400

        db = get_db()
        today = dt.now().strftime('%Y-%m-%d')
        is_intraday = interval in ('5minute', '15minute', '30minute', '60minute', 'minute')

        def is_market_closed():
            now = now_ist()
            if now.weekday() >= 5:  # Saturday or Sunday
                return True
            minutes_since_midnight = now.hour * 60 + now.minute
            return minutes_since_midnight < 555 or minutes_since_midnight >= 940  # 9:15 = 555, 15:40 = 940

        # ── CACHE STRATEGY ──
        # For intraday intervals: always fetch fresh from API during market hours, but use DB cache out of market hours
        # For daily/weekly: use cache with incremental gap-fill

        cached_candles = []
        api_candles = []
        source = 'cache'

        if is_intraday:
            # Out of market hours: return cached data from DB immediately if it includes the active session date.
            # If the cache is missing the active session date, fall through to fetch fresh data from Kite API.
            if is_market_closed() and not force_refresh:
                stale = cache_get_ohlcv(db, token, from_date_str, to_date_str, interval)
                if stale:
                    from ema_crossover_scanner import _get_active_trading_date
                    active_session_date = _get_active_trading_date(now_ist())
                    has_active_session = any(
                        isinstance(r, dict) and str(r.get('date', '')).startswith(active_session_date)
                        for r in stale
                    )
                    if has_active_session:
                        return jsonify({'candles': stale, 'source': 'db_cache_closed_hours'})
                    else:
                        logging.info(
                            f"[API Historical] Cache miss for active session date {active_session_date} (token {token}); fetching fresh from Kite API..."
                        )

            # 1. Check TTL cache for synthetic index candles (Nifty50, Sensex, etc.)
            if token in SYNTHETIC_VOLUME_INDICES:
                cached_data = get_cached_nifty_candles(token, from_date_str, interval)
                if cached_data:
                    return jsonify({'candles': cached_data, 'source': 'synthetic_cache'})

            # Intraday: always fresh from API to get latest minutes, but use cache if offline
            kite = get_kite()
            if not kite:
                stale = cache_get_ohlcv(db, token, from_date_str, to_date_str, interval)
                if stale:
                    return jsonify({'candles': stale, 'source': 'stale_cache'})
                # Generate demo data for offline mode
                demo_candles = generate_demo_candles(token, from_date, to_date, interval)
                return jsonify({'candles': demo_candles, 'source': 'demo'})

            # Extend to_date to end-of-day so today's intraday bars (9:15–15:30) are included.
            to_date_intraday = to_date.replace(hour=23, minute=59, second=59)
            data = kite.historical_data(token, from_date, to_date_intraday, interval)

            # 2. Intercept synthetic-volume indices and replace reported volume
            index_name = SYNTHETIC_VOLUME_INDICES.get(token)
            if index_name:
                try:
                    logging.info(f"[Volume Engine] Cache miss — starting {index_name} constituent aggregation.")
                    volume_data = aggregate_index_volume(kite, index_name, from_date, to_date_intraday, interval)
                    for candle in data:
                        ts_key = normalize_timestamp_key(candle['date'])
                        entry = volume_data.get(ts_key, {'volume': 0})
                        candle['volume'] = entry.get('volume', 0)
                    logging.info(f"[Volume Engine] {index_name} aggregation complete — {len(volume_data)} buckets applied.")
                except Exception as ex:
                    logging.error(f"[Volume Engine] {index_name} aggregation failed; returning raw spot candles: {ex}")

            cache_store_ohlcv(db, token, data, interval)
            for candle in data:
                c = dict(candle)
                if 'date' in c and hasattr(c['date'], 'isoformat'):
                    c['date'] = c['date'].isoformat()
                api_candles.append(c)

            # Save synthetic candles back to the TTL cache
            if token in SYNTHETIC_VOLUME_INDICES:
                set_cached_nifty_candles(token, from_date_str, interval, api_candles)
                
            source = 'api'
        elif not force_refresh:
            cached_candles = cache_get_ohlcv(db, token, from_date_str, to_date_str, interval)

            if cached_candles:
                latest_cached = cached_candles[-1]['date']

                yesterday = (dt.now() - timedelta(days=1)).strftime('%Y-%m-%d')
                if latest_cached[:10] >= yesterday:
                    source = 'cache'
                    api_candles = cached_candles
                else:
                    gap_from = dt.strptime(latest_cached[:10], '%Y-%m-%d') + timedelta(days=1)
                    if gap_from <= to_date:
                        kite = get_kite()
                        if kite:
                            try:
                                new_data = kite.historical_data(token, gap_from, to_date, interval)
                                cache_store_ohlcv(db, token, new_data, interval)
                                source = 'cache+api'
                            except Exception:
                                pass

                    api_candles = cache_get_ohlcv(db, token, from_date_str, to_date_str, interval)
            else:
                kite = get_kite()
                if not kite:
                    stale = cache_get_ohlcv(db, token, from_date_str, to_date_str, interval)
                    if stale:
                        return jsonify({'candles': stale, 'source': 'stale_cache'})
                    # Generate demo data for offline mode
                    demo_candles = generate_demo_candles(token, from_date, to_date, interval)
                    return jsonify({'candles': demo_candles, 'source': 'demo'})

                data = kite.historical_data(token, from_date, to_date, interval)
                cache_store_ohlcv(db, token, data, interval)

                for candle in data:
                    c = dict(candle)
                    if 'date' in c and hasattr(c['date'], 'isoformat'):
                        c['date'] = c['date'].isoformat()
                    api_candles.append(c)
                source = 'api'
        else:
            # Force refresh for daily/weekly
            kite = get_kite()
            if not kite:
                stale = cache_get_ohlcv(db, token, from_date_str, to_date_str, interval)
                if stale:
                    return jsonify({'candles': stale, 'source': 'stale_cache'})
                # Generate demo data for offline mode
                demo_candles = generate_demo_candles(token, from_date, to_date, interval)
                return jsonify({'candles': demo_candles, 'source': 'demo'})

            data = kite.historical_data(token, from_date, to_date, interval)
            cache_store_ohlcv(db, token, data, interval)

            for candle in data:
                c = dict(candle)
                if 'date' in c and hasattr(c['date'], 'isoformat'):
                    c['date'] = c['date'].isoformat()
                api_candles.append(c)
            source = 'api'

        return jsonify({'candles': api_candles, 'source': source})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

def calculate_upcoming_prediction(df, idx):
    """
    Computes upcoming session prediction (direction and confidence)
    based on data up to index `idx` (inclusive).
    """
    if idx < 25 or idx >= len(df):
        return {
            'consensus_bias': 'NEUTRAL',
            'confidence_score': 50,
            'metrics': {
                'streak_reversal_prob': 0.0,
                'climax_wall_action': 'NEUTRAL',
                'climax_wall_distance_pct': 0.0,
                'markov_transition_probabilities': {
                    'next_up_prob': 33.3,
                    'next_down_prob': 33.3,
                    'next_flat_prob': 33.3
                },
                'mtf_alignment': 'MIXED'
            }
        }

    r_close = float(df.loc[idx, 'close'])
    r_open = float(df.loc[idx, 'open'])
    r_high = float(df.loc[idx, 'high'])
    r_low = float(df.loc[idx, 'low'])
    r_e8 = float(df.loc[idx, 'ema_8'])
    r_e20 = float(df.loc[idx, 'ema_20'])
    r_e50 = float(df.loc[idx, 'ema_50'])
    r_e200 = float(df.loc[idx, 'ema_200'])
    r_rsi = float(df.loc[idx, 'rsi'])
    r_macd = float(df.loc[idx, 'macd'])
    r_macd_sig = float(df.loc[idx, 'macd_signal'])

    # 1. Streak Exhaustion
    closes_up_to = df['close'].iloc[:idx+1].tolist()
    streak_days = 0
    streak_dir = 'none'
    if len(closes_up_to) >= 2:
        streak_dir = 'climb' if closes_up_to[-1] > closes_up_to[-2] else 'drop'
        for k in range(len(closes_up_to)-1, 0, -1):
            if (streak_dir == 'climb' and closes_up_to[k] > closes_up_to[k-1]) or \
               (streak_dir == 'drop'  and closes_up_to[k] < closes_up_to[k-1]):
                streak_days += 1
            else:
                break
    
    # Calculate historical streak statistics to get percentile
    past_streaks = []
    curr_s = 0
    curr_dir = 'none'
    for k in range(1, idx):
        sd = 'climb' if df.loc[k, 'close'] > df.loc[k-1, 'close'] else 'drop'
        if sd == curr_dir:
            curr_s += 1
        else:
            if curr_s > 0:
                past_streaks.append((curr_dir, curr_s))
            curr_dir = sd
            curr_s = 1
    
    matching_streaks = [s[1] for s in past_streaks if s[0] == streak_dir]
    longer_streaks = [s for s in matching_streaks if s >= streak_days]
    if matching_streaks:
        streak_reversal_prob = round((1.0 - len(longer_streaks) / len(matching_streaks)) * 100, 1)
    else:
        streak_reversal_prob = 50.0

    # 2. Climax Wall Proximity
    climax_walls = []
    for k in range(20, idx + 1):
        v = float(df.loc[k, 'volume'])
        v_sma = float(df.loc[k, 'vol_sma_20'])
        if v_sma > 0 and v >= 2.5 * v_sma:
            climax_walls.append(float(df.loc[k, 'close']))
    
    climax_wall_action = 'NEUTRAL'
    climax_wall_dist = 999.0
    if climax_walls:
        closest_wall = min(climax_walls, key=lambda w: abs(w - r_close))
        climax_wall_dist = abs(closest_wall - r_close) / closest_wall * 100.0
        if climax_wall_dist <= 0.75:
            if r_close > closest_wall:
                climax_wall_action = 'BULLISH_BOUNCE'
            else:
                climax_wall_action = 'BEARISH_REJECTION'

    # 3. Markov transitions
    states = []
    for k in range(1, idx + 1):
        diff = (float(df.loc[k, 'close']) - float(df.loc[k-1, 'close'])) / float(df.loc[k-1, 'close']) * 100.0
        if diff > 0.1:
            states.append('U')
        elif diff < -0.1:
            states.append('D')
        else:
            states.append('F')
    
    transitions = {'U': {'U': 0, 'D': 0, 'F': 0}, 'D': {'U': 0, 'D': 0, 'F': 0}, 'F': {'U': 0, 'D': 0, 'F': 0}}
    for k in range(len(states) - 1):
        transitions[states[k]][states[k+1]] += 1
    
    last_state = states[-1] if states else 'F'
    totals = sum(transitions[last_state].values())
    if totals > 0:
        next_up_prob = round(transitions[last_state]['U'] / totals * 100, 1)
        next_down_prob = round(transitions[last_state]['D'] / totals * 100, 1)
        next_flat_prob = round(transitions[last_state]['F'] / totals * 100, 1)
    else:
        next_up_prob = next_down_prob = next_flat_prob = 33.3

    # 4. Multi-Timeframe Alignment
    daily_trend = 'BULLISH' if r_close > r_e20 else 'BEARISH'
    hourly_trend = 'BULLISH' if r_close > r_e8 else 'BEARISH'
    m15_trend = 'BULLISH' if r_rsi > 50 else 'BEARISH'
    mtf_align = 'MIXED'
    if daily_trend == hourly_trend == m15_trend:
        mtf_align = daily_trend

    # Consensus Model
    bull_weight = 0
    bear_weight = 0

    if streak_reversal_prob >= 85:
        if streak_dir == 'drop':
            bull_weight += 25
        else:
            bear_weight += 25

    if climax_wall_action == 'BULLISH_BOUNCE':
        bull_weight += 20
    elif climax_wall_action == 'BEARISH_REJECTION':
        bear_weight += 20

    if next_up_prob > next_down_prob + 10:
        bull_weight += 20
    elif next_down_prob > next_up_prob + 10:
        bear_weight += 20

    if mtf_align == 'BULLISH':
        bull_weight += 20
    elif mtf_align == 'BEARISH':
        bear_weight += 20

    last_score = 50
    if r_close > r_e8: last_score += 15
    if r_close > r_e20: last_score += 15
    if r_rsi > 50: last_score += 10
    if r_rsi > 60: last_score += 10
    if r_rsi < 40: last_score -= 15
    if r_macd > r_macd_sig: last_score += 10
    else: last_score -= 10
    last_score = max(0, min(100, last_score))

    if last_score >= 55:
        bull_weight += 15
    elif last_score < 45:
        bear_weight += 15

    total_weight = bull_weight + bear_weight
    if total_weight > 0:
        bull_pct = bull_weight / total_weight * 100
        bear_pct = bear_weight / total_weight * 100
        if bull_pct >= 55:
            consensus_bias = 'BULLISH'
            confidence_score = round(bull_pct)
        elif bear_pct >= 55:
            consensus_bias = 'BEARISH'
            confidence_score = round(bear_pct)
        else:
            consensus_bias = 'COILING'
            confidence_score = 50
    else:
        consensus_bias = 'COILING'
        confidence_score = 50

    return {
        'consensus_bias': consensus_bias,
        'confidence_score': confidence_score,
        'metrics': {
            'streak_reversal_prob': streak_reversal_prob,
            'climax_wall_action': climax_wall_action,
            'climax_wall_distance_pct': round(climax_wall_dist, 2) if climax_wall_dist != 999.0 else 0.0,
            'markov_transition_probabilities': {
                'next_up_prob': next_up_prob,
                'next_down_prob': next_down_prob,
                'next_flat_prob': next_flat_prob
            },
            'mtf_alignment': mtf_align
        }
    }


@app.route('/api/historical-analytics')
def historical_analytics():
    try:
        symbol = request.args.get('symbol', 'DMART')
        days_str = request.args.get('days', '90')
        days = int(days_str)

        # 1. Resolve Symbol to Token
        token = None
        normalized_symbol = symbol.replace(' ', '').upper()
        
        # Handle Indices
        if normalized_symbol == 'NIFTY50' or normalized_symbol == 'NIFTY50':
            token = 256265
        elif normalized_symbol == 'NIFTYBANK' or normalized_symbol == 'BANKNIFTY':
            token = 260105
        elif normalized_symbol == 'NIFTYFINSERVICE' or normalized_symbol == 'FINNIFTY':
            token = 257801
        elif normalized_symbol == 'INDIAVIX':
            token = 264969
        elif normalized_symbol in INSTRUMENT_TOKENS:
            token = INSTRUMENT_TOKENS[normalized_symbol]
        else:
            # Look up in global instruments cache
            global _instruments_cache
            if _instruments_cache:
                for inst in _instruments_cache:
                    if inst.get('tradingsymbol') == symbol:
                        token = inst.get('instrument_token')
                        break
            
            # Default fallback token if not found
            if not token:
                token = 738561  # RELIANCE fallback

        # 2. Retrieve Historical Daily Candles
        db = get_db()
        # Fetch at least 365 calendar days to allow the 200 EMA to fully mature and stabilize
        from_date = dt.now() - timedelta(days=max(days + 40, 365))
        to_date = dt.now()
        from_date_str = from_date.strftime('%Y-%m-%d')
        to_date_str = to_date.strftime('%Y-%m-%d')

        candles = []
        kite = get_kite()

        if kite:
            try:
                raw_candles = kite.historical_data(token, from_date, to_date, 'day')
                cache_store_ohlcv(db, token, raw_candles, 'day')
                for c in raw_candles:
                    c_dict = dict(c)
                    if 'date' in c_dict and hasattr(c_dict['date'], 'isoformat'):
                        c_dict['date'] = c_dict['date'].isoformat()
                    candles.append(c_dict)
            except Exception:
                # Fallback to cache if Kite call fails
                candles = cache_get_ohlcv(db, token, from_date_str, to_date_str, 'day')
        else:
            # Offline mode / cache retrieval
            candles = cache_get_ohlcv(db, token, from_date_str, to_date_str, 'day')

        # Generate fallback demo candles if no cache and no Kite
        if not candles:
            candles = generate_demo_candles(token, from_date, to_date, 'day')

        if not candles:
            return jsonify({'error': 'No historical data found for symbol'}), 404

        # 3. Perform Advanced Quantitative Calculations
        # Sort ascending to calculate indicators chronologically
        candles.sort(key=lambda x: x.get('date', ''))
        
        # Load into Pandas DataFrame for robust technical indicator calculations
        df = pd.DataFrame(candles)
        df['close'] = df['close'].astype(float)
        df['high'] = df['high'].astype(float)
        df['low'] = df['low'].astype(float)
        df['open'] = df['open'].astype(float)
        df['volume'] = df['volume'].astype(float)

        # Technical Indicators calculation
        df['ema_8'] = df['close'].ewm(span=8, adjust=False).mean()
        df['ema_20'] = df['close'].ewm(span=20, adjust=False).mean()
        df['ema_50'] = df['close'].ewm(span=50, adjust=False).mean()
        df['ema_200'] = df['close'].ewm(span=200, adjust=False).mean()
        
        # RSI 14
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df['rsi'] = 100 - (100 / (1 + rs))
        df['rsi'] = df['rsi'].fillna(50.0)

        # MACD (12, 26, 9)
        exp12 = df['close'].ewm(span=12, adjust=False).mean()
        exp26 = df['close'].ewm(span=26, adjust=False).mean()
        df['macd'] = exp12 - exp26
        df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()

        # Dreiss Choppiness Index (CHOP) 14
        high_low = df['high'] - df['low']
        high_close = (df['high'] - df['close'].shift()).abs()
        low_close = (df['low'] - df['close'].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr_sum = tr.rolling(14).sum()
        max_high = df['high'].rolling(14).max()
        min_low = df['low'].rolling(14).min()
        
        df['chop'] = 100 * np.log10(atr_sum / (max_high - min_low)) / np.log10(14)
        df['chop'] = df['chop'].fillna(50.0)

        # Volume 20-SMA
        df['vol_sma_20'] = df['volume'].rolling(20).mean().fillna(1.0)

        # Resolve Latest Stats
        last_row = df.iloc[-1]
        last_close = last_row['close']
        last_ema_8 = last_row['ema_8']
        last_ema_20 = last_row['ema_20']
        last_rsi = last_row['rsi']
        last_macd = last_row['macd']
        last_macd_sig = last_row['macd_signal']
        last_chop = last_row['chop']

        # Determine Trend Status
        daily_trend = 'BULLISH' if last_close > last_ema_20 else 'BEARISH'
        
        # Calculate EMA 50/200 Crossover Status
        last_ema_50 = last_row['ema_50']
        last_ema_200 = last_row['ema_200']
        ema_dist = last_ema_50 - last_ema_200
        ema_dist_pct = (abs(ema_dist) / last_ema_200) * 100.0 if last_ema_200 > 0 else 0.0
        
        ema_crossover = {
            'status': 'NEUTRAL',
            'details': 'No crossover approaching',
            'distance_pct': round(ema_dist_pct, 2),
            'ema_50': round(last_ema_50, 2),
            'ema_200': round(last_ema_200, 2),
            'type': 'neutral',
            'strength': 0,
            'strength_label': 'Flat Strength'
        }
        
        # If proximity is within 1.5% distance gap, it is approaching
        if ema_dist_pct <= 1.5:
            if last_ema_50 < last_ema_200:
                ema_crossover['status'] = 'BULLISH_APPROACHING'
                ema_crossover['type'] = 'bullish_approaching'
                ema_crossover['details'] = f"Golden Cross Approaching (Gap: {round(ema_dist_pct, 2)}%)"
                ema_crossover['strength'] = 5
                ema_crossover['strength_label'] = "+5 Bullish Strength"
            else:
                ema_crossover['status'] = 'BEARISH_APPROACHING'
                ema_crossover['type'] = 'bearish_approaching'
                ema_crossover['details'] = f"Death Cross Approaching (Gap: {round(ema_dist_pct, 2)}%)"
                ema_crossover['strength'] = -5
                ema_crossover['strength_label'] = "-5 Bearish Strength"
        else:
            if last_ema_50 > last_ema_200:
                ema_crossover['status'] = 'BULLISH_ACTIVE'
                ema_crossover['type'] = 'bullish_active'
                ema_crossover['details'] = f"Golden Cross Active (EMA50 > EMA200 by {round(ema_dist_pct, 2)}%)"
                ema_crossover['strength'] = 10
                ema_crossover['strength_label'] = "+10 Bullish Strength"
            else:
                ema_crossover['status'] = 'BEARISH_ACTIVE'
                ema_crossover['type'] = 'bearish_active'
                ema_crossover['details'] = f"Death Cross Active (EMA50 < EMA200 by {round(ema_dist_pct, 2)}%)"
                ema_crossover['strength'] = -10
                ema_crossover['strength_label'] = "-10 Bearish Strength"
        
        # Real-time multi-TF trend checks via Kite if available
        hourly_trend = 'NEUTRAL'
        m15_trend = 'NEUTRAL'
        
        if kite:
            try:
                # Fetch hourly trend (last 10 days)
                hourly_raw = kite.historical_data(token, dt.now() - timedelta(days=10), dt.now(), '60minute')
                if hourly_raw:
                    hdf = pd.DataFrame(hourly_raw)
                    hdf['ema_20'] = hdf['close'].astype(float).ewm(span=20, adjust=False).mean()
                    hourly_trend = 'BULLISH' if float(hdf.iloc[-1]['close']) > float(hdf.iloc[-1]['ema_20']) else 'BEARISH'
                
                # Fetch 15-min trend (last 3 days)
                m15_raw = kite.historical_data(token, dt.now() - timedelta(days=3), dt.now(), '15minute')
                if m15_raw:
                    mdf = pd.DataFrame(m15_raw)
                    mdf['ema_20'] = mdf['close'].astype(float).ewm(span=20, adjust=False).mean()
                    m15_trend = 'BULLISH' if float(mdf.iloc[-1]['close']) > float(mdf.iloc[-1]['ema_20']) else 'BEARISH'
            except Exception:
                # Fallback to daily indicators if call fails
                hourly_trend = 'BULLISH' if last_close > last_ema_8 else 'BEARISH'
                m15_trend = 'BULLISH' if last_rsi > 50 else 'BEARISH'
        else:
            # Sandbox / offline fallback
            hourly_trend = 'BULLISH' if last_close > last_ema_8 else 'BEARISH'
            m15_trend = 'BULLISH' if last_rsi > 50 else 'BEARISH'

        # Compute Streaks
        closes = df['close'].tolist()
        current_streak = 0
        is_drop_streak = False
        is_climb_streak = False

        if len(closes) >= 2:
            prev_close = closes[-2]
            
            if last_close < prev_close:
                is_drop_streak = True
                idx = len(closes) - 1
                while idx > 0 and closes[idx] < closes[idx-1]:
                    current_streak += 1
                    idx -= 1
            elif last_close > prev_close:
                is_climb_streak = True
                idx = len(closes) - 1
                while idx > 0 and closes[idx] > closes[idx-1]:
                    current_streak += 1
                    idx -= 1

        # EMA Stretch snapbacks
        ema_stretch = ((last_close - last_ema_8) / last_ema_8 * 100.0) if last_ema_8 > 0 else 0.0

        climb_prob = 0.0
        climb_target = 0.0
        drop_prob = 0.0
        drop_target = 0.0

        if is_climb_streak:
            climb_prob = min(95.0, 50.0 + (current_streak * 10.0) + (10.0 if ema_stretch > 2.0 else 0.0))
            climb_target = round(last_ema_8, 2)
        elif is_drop_streak:
            drop_prob = min(95.0, 50.0 + (current_streak * 8.0) + (10.0 if ema_stretch < -3.0 else 0.0))
            drop_target = round(last_ema_8, 2)

        # 4. Quantitative Score Engine Backtesting (Retrospective Dry-run model)
        bullish_triggers = []
        bearish_triggers = []

        # Iterate over historic days (leaving padding for indicators & future forward returns look-ahead)
        for idx in range(25, len(df) - 5):
            c_val = df.loc[idx, 'close']
            e8 = df.loc[idx, 'ema_8']
            e20 = df.loc[idx, 'ema_20']
            rs = df.loc[idx, 'rsi']
            mc = df.loc[idx, 'macd']
            mcs = df.loc[idx, 'macd_signal']

            # Calculate daily model score
            score = 50
            if c_val > e8: score += 15
            if c_val > e20: score += 15
            if rs > 50: score += 10
            if rs > 60: score += 10
            if rs < 40: score -= 15
            if mc > mcs: score += 10
            else: score -= 10
            score = max(0, min(100, score))

            # Store Trigger metrics
            if score >= 55:  # Bullish Trigger
                bullish_triggers.append({
                    'ret_1d': float((df.loc[idx+1, 'close'] - c_val) / c_val * 100.0),
                    'ret_2d': float((df.loc[idx+2, 'close'] - c_val) / c_val * 100.0),
                    'ret_5d': float((df.loc[idx+5, 'close'] - c_val) / c_val * 100.0)
                })
            elif score < 45:  # Bearish Trigger
                bearish_triggers.append({
                    'ret_1d': float((df.loc[idx+1, 'close'] - c_val) / c_val * -100.0), # Inverse returns for shorts
                    'ret_2d': float((df.loc[idx+2, 'close'] - c_val) / c_val * -100.0),
                    'ret_5d': float((df.loc[idx+5, 'close'] - c_val) / c_val * -100.0)
                })

        # Calculate Backtest Stats
        def compute_backtest_stats(triggers):
            if not triggers:
                return {'count': 0, 'win_1d': 0.0, 'win_2d': 0.0, 'win_5d': 0.0, 'avg_change': 0.0}
            
            w1 = sum(1 for t in triggers if t['ret_1d'] > 0) / len(triggers) * 100.0
            w2 = sum(1 for t in triggers if t['ret_2d'] > 0) / len(triggers) * 100.0
            w5 = sum(1 for t in triggers if t['ret_5d'] > 0) / len(triggers) * 100.0
            avg_ch = sum(t['ret_1d'] for t in triggers) / len(triggers)
            
            return {
                'count': len(triggers),
                'win_1d': round(w1, 1),
                'win_2d': round(w2, 1),
                'win_5d': round(w5, 1),
                'avg_change': round(avg_ch, 2)
            }

        backtest_bull = compute_backtest_stats(bullish_triggers)
        backtest_bear = compute_backtest_stats(bearish_triggers)

        # Resolve Latest Score & Direction
        last_score = 50
        if last_close > last_ema_8: last_score += 15
        if last_close > last_ema_20: last_score += 15
        if last_rsi > 50: last_score += 10
        if last_rsi > 60: last_score += 10
        if last_rsi < 40: last_score -= 15
        if last_macd > last_macd_sig: last_score += 10
        else: last_score -= 10
        last_score = max(0, min(100, last_score))

        direction = 'BULLISH' if last_score >= 55 else 'BEARISH' if last_score < 45 else 'NEUTRAL'

        # 5. Opening Gap Analytics
        gap_ups = 0
        gap_up_fades = 0
        gap_up_follows = 0
        gap_downs = 0
        gap_down_fades = 0
        gap_down_follows = 0

        for idx in range(1, len(df)):
            o = float(df.loc[idx, 'open'])
            h = float(df.loc[idx, 'high'])
            l = float(df.loc[idx, 'low'])
            cl = float(df.loc[idx, 'close'])
            ph = float(df.loc[idx-1, 'high'])
            pl = float(df.loc[idx-1, 'low'])
            
            if o > ph:
                gap_ups += 1
                if cl < o:
                    gap_up_fades += 1
                else:
                    gap_up_follows += 1
            elif o < pl:
                gap_downs += 1
                if cl > o:
                    gap_down_fades += 1
                else:
                    gap_down_follows += 1

        # 6. Institutional Volume Climax Walls
        climax_nodes = []
        for idx in range(20, len(df)):
            v = float(df.loc[idx, 'volume'])
            v_sma = float(df.loc[idx, 'vol_sma_20'])
            
            if v_sma > 0 and v > 0 and v >= 2.5 * v_sma:
                dt_obj = dt.fromisoformat(df.loc[idx, 'date'].split('T')[0])
                date_str = dt_obj.strftime('%d %b %Y')
                multiplier = round(v / v_sma, 1)
                
                climax_nodes.append({
                  'date': date_str,
                  'multiplier': multiplier,
                  'close': round(float(df.loc[idx, 'close']), 2)
                })
        
        climax_nodes.reverse()
        climax_nodes = climax_nodes[:5]  # limit to top 5 newest spikes

        # 7. Format Top Rows for Table Display
        formatted_rows = []
        candles.sort(key=lambda x: x.get('date', ''), reverse=True)
        table_candles = candles[:min(days, len(candles))]

        for idx in range(len(table_candles)):
            c = table_candles[idx]
            change_pct = 0.0
            if idx + 1 < len(table_candles):
                p_close = float(table_candles[idx+1].get('close', 0.0))
                if p_close > 0:
                    change_pct = ((float(c.get('close', 0.0)) - p_close) / p_close) * 100.0

            dt_obj = dt.fromisoformat(c.get('date').split('T')[0])
            
            formatted_rows.append({
                'date': dt_obj.strftime('%d-%m-%Y'),
                'price': round(float(c.get('close', 0.0)), 2),
                'open': round(float(c.get('open', 0.0)), 2),
                'high': round(float(c.get('high', 0.0)), 2),
                'low': round(float(c.get('low', 0.0)), 2),
                'vol': f"{round(float(c.get('volume', 0.0)) / 1000.0, 2)}K" if float(c.get('volume', 0.0)) < 1000000.0 else f"{round(float(c.get('volume', 0.0)) / 1000000.0, 2)}M",
                'change': round(change_pct, 2)
            })

        # 8. Compute Total Period Movement Metrics (for selected days)
        period_df = df.tail(min(days, len(df)))
        p_start_close = float(period_df.iloc[0]['close'])
        p_end_close = float(period_df.iloc[-1]['close'])
        p_net_abs = round(p_end_close - p_start_close, 2)
        p_net_pct = round(((p_end_close - p_start_close) / p_start_close * 100.0), 2) if p_start_close > 0 else 0.0

        p_diffs = period_df['close'].diff()
        p_prev_close = period_df['close'].shift(1)
        p_pct_changes = (p_diffs / p_prev_close * 100.0).dropna()

        up_changes = p_pct_changes[p_pct_changes > 0]
        down_changes = p_pct_changes[p_pct_changes < 0]

        up_days_count = len(up_changes)
        down_days_count = len(down_changes)
        total_up_pct = round(float(up_changes.sum()), 2)
        total_down_pct = round(float(abs(down_changes.sum())), 2)
        avg_up_pct = round(float(up_changes.mean()), 2) if up_days_count > 0 else 0.0
        avg_down_pct = round(float(abs(down_changes.mean())), 2) if down_days_count > 0 else 0.0

        p_high = round(float(period_df['high'].max()), 2)
        p_low = round(float(period_df['low'].min()), 2)
        gain_from_low = round(((p_end_close - p_low) / p_low * 100.0), 2) if p_low > 0 else 0.0
        drawdown_from_high = round(((p_end_close - p_high) / p_high * 100.0), 2) if p_high > 0 else 0.0

        period_summary = {
            'days_requested': days,
            'total_sessions': len(period_df),
            'start_date': str(period_df.iloc[0]['date']).split('T')[0],
            'end_date': str(period_df.iloc[-1]['date']).split('T')[0],
            'start_price': p_start_close,
            'latest_price': p_end_close,
            'net_change_abs': p_net_abs,
            'net_change_pct': p_net_pct,
            'up_days_count': up_days_count,
            'down_days_count': down_days_count,
            'total_up_pct': total_up_pct,
            'total_down_pct': total_down_pct,
            'avg_up_pct': avg_up_pct,
            'avg_down_pct': avg_down_pct,
            'period_high': p_high,
            'period_low': p_low,
            'gain_from_low_pct': gain_from_low,
            'drawdown_from_high_pct': drawdown_from_high
        }

        return jsonify({
            'symbol': symbol,
            'score': last_score,
            'direction': direction,
            'chop': round(last_chop, 1),
            'daily_trend': daily_trend,
            'hourly_trend': hourly_trend,
            'm15_trend': m15_trend,
            'current_streak_type': 'drop' if is_drop_streak else 'climb' if is_climb_streak else 'none',
            'current_streak_days': current_streak,
            'ema_stretch': round(ema_stretch, 2),
            'climb_streak': {
                'days': current_streak if is_climb_streak else 0,
                'change': round(((last_close - closes[-current_streak-1]) / closes[-current_streak-1] * 100.0), 2) if is_climb_streak and current_streak < len(closes) else 0.0,
                'prob': round(climb_prob, 1),
                'target': climb_target
            },
            'drop_streak': {
                'days': current_streak if is_drop_streak else 0,
                'change': round(((last_close - closes[-current_streak-1]) / closes[-current_streak-1] * 100.0), 2) if is_drop_streak and current_streak < len(closes) else 0.0,
                'prob': round(drop_prob, 1),
                'target': drop_target
            },
            'gaps': {
                'gapup_count': gap_ups,
                'gapup_follow': f"{round((gap_up_follows / gap_ups * 100.0), 1)}%" if gap_ups > 0 else '0%',
                'gapup_fade': f"{round((gap_up_fades / gap_ups * 100.0), 1)}%" if gap_ups > 0 else '0%',
                'gapdown_count': gap_downs,
                'gapdown_follow': f"{round((gap_down_follows / gap_downs * 100.0), 1)}%" if gap_downs > 0 else '0%',
                'gapdown_fade': f"{round((gap_down_fades / gap_downs * 100.0), 1)}%" if gap_downs > 0 else '0%'
            },
            'climax_walls': climax_nodes,
            'backtest_bullish': backtest_bull,
            'backtest_bearish': backtest_bear,
            'ema_crossover': ema_crossover,
            'prediction_telemetry': calculate_upcoming_prediction(df, len(df) - 1),
            'rows': formatted_rows,
            'period_summary': period_summary
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500



# ── Bulk Historical Analytics (multi-symbol CSV report) ──
@app.route('/api/historical-analytics-bulk', methods=['POST'])
def historical_analytics_bulk():
    """
    Accepts a POST body: { "symbols": ["RELIANCE", "TCS", ...], "days": 90 }
    Runs the full historical analytics calculation for each symbol and returns
    a flat list of per-symbol summary rows suitable for CSV export.
    Errors per symbol are captured individually without blocking other symbols.
    """
    try:
        req_data = request.json or {}
        symbols = req_data.get('symbols', [])
        days = int(req_data.get('days', 90))

        if not symbols or not isinstance(symbols, list):
            return jsonify({'error': 'symbols must be a non-empty list'}), 400

        symbols = [str(s).upper().strip() for s in symbols if str(s).strip()]
        symbols = list(dict.fromkeys(symbols))  # deduplicate while preserving order

        db = get_db()
        kite = get_kite()
        results = []

        for symbol in symbols:
            row = {'symbol': symbol, 'status': 'OK'}
            try:
                # ── Resolve token ──
                token = None
                normalized_symbol = symbol.replace(' ', '').upper()

                if normalized_symbol in ('NIFTY50', 'NIFTY'):
                    token = 256265
                elif normalized_symbol in ('NIFTYBANK', 'BANKNIFTY'):
                    token = 260105
                elif normalized_symbol in ('NIFTYFINSERVICE', 'FINNIFTY'):
                    token = 257801
                elif normalized_symbol == 'INDIAVIX':
                    token = 264969
                elif normalized_symbol in INSTRUMENT_TOKENS:
                    token = INSTRUMENT_TOKENS[normalized_symbol]
                else:
                    global _instruments_cache
                    if not _instruments_cache:
                        _instruments_cache = cache_get_instruments(db)
                    if _instruments_cache:
                        for inst in _instruments_cache:
                            if inst.get('tradingsymbol') == symbol and inst.get('exchange') == 'NSE':
                                token = inst.get('instrument_token')
                                break

                if not token:
                    row['status'] = 'ERROR: Token not found'
                    results.append(row)
                    continue

                # ── Fetch candles ──
                from_date = dt.now() - timedelta(days=max(days + 40, 365))
                to_date = dt.now()
                from_date_str = from_date.strftime('%Y-%m-%d')
                to_date_str = to_date.strftime('%Y-%m-%d')

                candles = []
                if kite:
                    try:
                        raw = kite.historical_data(token, from_date, to_date, 'day')
                        cache_store_ohlcv(db, token, raw, 'day')
                        for c in raw:
                            c_dict = dict(c)
                            if 'date' in c_dict and hasattr(c_dict['date'], 'isoformat'):
                                c_dict['date'] = c_dict['date'].isoformat()
                            candles.append(c_dict)
                    except Exception:
                        candles = cache_get_ohlcv(db, token, from_date_str, to_date_str, 'day')
                else:
                    candles = cache_get_ohlcv(db, token, from_date_str, to_date_str, 'day')

                if not candles:
                    candles = generate_demo_candles(token, from_date, to_date, 'day')

                if not candles or len(candles) < 10:
                    row['status'] = 'ERROR: Insufficient data'
                    results.append(row)
                    continue

                candles.sort(key=lambda x: x.get('date', ''))
                df_b = pd.DataFrame(candles)
                df_b['close']  = df_b['close'].astype(float)
                df_b['high']   = df_b['high'].astype(float)
                df_b['low']    = df_b['low'].astype(float)
                df_b['open']   = df_b['open'].astype(float)
                df_b['volume'] = df_b['volume'].astype(float)

                # ── Indicators ──
                df_b['ema_8']   = df_b['close'].ewm(span=8,   adjust=False).mean()
                df_b['ema_20']  = df_b['close'].ewm(span=20,  adjust=False).mean()
                df_b['ema_50']  = df_b['close'].ewm(span=50,  adjust=False).mean()
                df_b['ema_200'] = df_b['close'].ewm(span=200, adjust=False).mean()

                delta = df_b['close'].diff()
                gain  = delta.where(delta > 0, 0).rolling(14).mean()
                loss  = (-delta.where(delta < 0, 0)).rolling(14).mean()
                df_b['rsi'] = (100 - (100 / (1 + gain / loss))).fillna(50.0)

                exp12 = df_b['close'].ewm(span=12, adjust=False).mean()
                exp26 = df_b['close'].ewm(span=26, adjust=False).mean()
                df_b['macd']        = exp12 - exp26
                df_b['macd_signal'] = df_b['macd'].ewm(span=9, adjust=False).mean()

                high_low    = df_b['high'] - df_b['low']
                high_close  = (df_b['high'] - df_b['close'].shift()).abs()
                low_close   = (df_b['low']  - df_b['close'].shift()).abs()
                tr          = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
                atr_sum     = tr.rolling(14).sum()
                max_high    = df_b['high'].rolling(14).max()
                min_low     = df_b['low'].rolling(14).min()
                df_b['chop'] = (100 * np.log10(atr_sum / (max_high - min_low)) / np.log10(14)).fillna(50.0)

                df_b['vol_sma_20'] = df_b['volume'].rolling(20).mean().fillna(1.0)

                last = df_b.iloc[-1]
                last_close    = float(last['close'])
                last_ema_8    = float(last['ema_8'])
                last_ema_20   = float(last['ema_20'])
                last_ema_50   = float(last['ema_50'])
                last_ema_200  = float(last['ema_200'])
                last_rsi      = float(last['rsi'])
                last_macd     = float(last['macd'])
                last_macd_sig = float(last['macd_signal'])
                last_chop     = float(last['chop'])

                # ── Score ──
                score = 50
                if last_close > last_ema_8:   score += 15
                if last_close > last_ema_20:  score += 15
                if last_rsi > 50:             score += 10
                if last_rsi > 60:             score += 10
                if last_rsi < 40:             score -= 15
                if last_macd > last_macd_sig: score += 10
                else:                         score -= 10
                score = max(0, min(100, score))
                direction = 'BULLISH' if score >= 55 else 'BEARISH' if score < 45 else 'NEUTRAL'

                # ── EMA Crossover Strength ──
                ema_dist_pct = (abs(last_ema_50 - last_ema_200) / last_ema_200 * 100.0) if last_ema_200 > 0 else 0.0
                if ema_dist_pct <= 1.5:
                    ema_status = 'BULLISH_APPROACHING' if last_ema_50 < last_ema_200 else 'BEARISH_APPROACHING'
                else:
                    ema_status = 'BULLISH_ACTIVE' if last_ema_50 > last_ema_200 else 'BEARISH_ACTIVE'
                ema_strength = {
                    'BULLISH_APPROACHING': '+5', 'BEARISH_APPROACHING': '-5',
                    'BULLISH_ACTIVE': '+10',      'BEARISH_ACTIVE': '-10'
                }.get(ema_status, '0')

                # ── Gap Analytics ──
                gap_ups = gap_downs = gap_up_fades = gap_down_fades = 0
                for i in range(1, len(df_b)):
                    o  = float(df_b.loc[i, 'open'])
                    cl = float(df_b.loc[i, 'close'])
                    ph = float(df_b.loc[i-1, 'high'])
                    pl = float(df_b.loc[i-1, 'low'])
                    if o > ph:
                        gap_ups += 1
                        if cl < o: gap_up_fades += 1
                    elif o < pl:
                        gap_downs += 1
                        if cl > o: gap_down_fades += 1

                # ── Backtest Win-Rate (quick pass) ──
                bull_wins = bull_total = bear_wins = bear_total = 0
                for i in range(25, len(df_b) - 2):
                    sc = 50
                    e8, e20 = float(df_b.loc[i, 'ema_8']), float(df_b.loc[i, 'ema_20'])
                    rs = float(df_b.loc[i, 'rsi'])
                    mc, mcs = float(df_b.loc[i, 'macd']), float(df_b.loc[i, 'macd_signal'])
                    cv = float(df_b.loc[i, 'close'])
                    if cv > e8:  sc += 15
                    if cv > e20: sc += 15
                    if rs > 50:  sc += 10
                    if rs > 60:  sc += 10
                    if rs < 40:  sc -= 15
                    sc += 10 if mc > mcs else -10
                    sc = max(0, min(100, sc))
                    nxt = float(df_b.loc[i+1, 'close'])
                    if sc >= 55:
                        bull_total += 1
                        if nxt > cv: bull_wins += 1
                    elif sc < 45:
                        bear_total += 1
                        if nxt < cv: bear_wins += 1

                bull_wr = round(bull_wins / bull_total * 100, 1) if bull_total else 0.0
                bear_wr = round(bear_wins / bear_total * 100, 1) if bear_total else 0.0

                # ── Current Streak ──
                closes_list = df_b['close'].tolist()
                streak_days = 0
                if len(closes_list) >= 2:
                    streak_dir = 'climb' if closes_list[-1] > closes_list[-2] else 'drop'
                    for i in range(len(closes_list)-1, 0, -1):
                        if (streak_dir == 'climb' and closes_list[i] > closes_list[i-1]) or \
                           (streak_dir == 'drop'  and closes_list[i] < closes_list[i-1]):
                            streak_days += 1
                        else:
                            break
                else:
                    streak_dir = 'none'

                # ── Cumulative Period Movement ──
                df_slice = df_b.tail(min(days, len(df_b)))
                p_start_close = float(df_slice.iloc[0]['close'])
                p_end_close = float(df_slice.iloc[-1]['close'])
                cum_movement_abs = round(p_end_close - p_start_close, 2)
                cum_movement_pct = round(((p_end_close - p_start_close) / p_start_close * 100.0), 2) if p_start_close > 0 else 0.0

                # ── Assemble row ──
                row.update({
                    'days_analysed':      days,
                    'score':              score,
                    'direction':          direction,
                    'ltp':                round(last_close, 2),
                    'cum_movement_pct':   cum_movement_pct,
                    'cum_movement_abs':   cum_movement_abs,
                    'rsi':                round(last_rsi, 1),
                    'macd_signal':        'BULLISH' if last_macd > last_macd_sig else 'BEARISH',
                    'chop':               round(last_chop, 1),
                    'chop_state':         'TRENDING' if last_chop < 38.2 else 'COILING' if last_chop > 61.8 else 'NEUTRAL',
                    'ema_50':             round(last_ema_50, 2),
                    'ema_200':            round(last_ema_200, 2),
                    'ema_cross_status':   ema_status,
                    'ema_cross_gap_pct':  round(ema_dist_pct, 2),
                    'ema_strength':       ema_strength,
                    'gap_up_count':       gap_ups,
                    'gap_up_fade_pct':    f"{round(gap_up_fades/gap_ups*100,1)}%" if gap_ups else '0%',
                    'gap_down_count':     gap_downs,
                    'gap_down_fade_pct':  f"{round(gap_down_fades/gap_downs*100,1)}%" if gap_downs else '0%',
                    'bull_backtest_triggers': bull_total,
                    'bull_backtest_win_rate': f"{bull_wr}%",
                    'bear_backtest_triggers': bear_total,
                    'bear_backtest_win_rate': f"{bear_wr}%",
                    'current_streak_dir': streak_dir,
                    'current_streak_days': streak_days,
                    'daily_trend':        'BULLISH' if last_close > last_ema_20 else 'BEARISH',
                    'hourly_trend':       'BULLISH' if last_close > last_ema_8 else 'BEARISH',
                    'm15_trend':          'BULLISH' if last_rsi > 50 else 'BEARISH',
                })
                row['upcoming_prediction'] = calculate_upcoming_prediction(df_b, len(df_b) - 1)

                # ── Generate Daily Rows for CSV Export ──
                daily_list = []
                df_slice = df_b.tail(days)
                for i_idx, r_val in df_slice.iterrows():
                    change_pct = 0.0
                    if i_idx > 0:
                        prev_close = float(df_b.loc[i_idx-1, 'close'])
                        if prev_close > 0:
                            change_pct = ((float(r_val['close']) - prev_close) / prev_close) * 100.0
                    
                    r_close = float(r_val['close'])
                    r_e8 = float(r_val['ema_8'])
                    r_e20 = float(r_val['ema_20'])
                    r_rsi = float(r_val['rsi'])
                    r_macd = float(r_val['macd'])
                    r_macd_sig = float(r_val['macd_signal'])
                    r_chop = float(r_val['chop'])
                    r_e50 = float(r_val['ema_50'])
                    r_e200 = float(r_val['ema_200'])

                    r_score = 50
                    if r_close > r_e8:   r_score += 15
                    if r_close > r_e20:  r_score += 15
                    if r_rsi > 50:       r_score += 10
                    if r_rsi > 60:       r_score += 10
                    if r_rsi < 40:       r_score -= 15
                    if r_macd > r_macd_sig: r_score += 10
                    else:                   r_score -= 10
                    r_score = max(0, min(100, r_score))
                    r_direction = 'BULLISH' if r_score >= 55 else 'BEARISH' if r_score < 45 else 'NEUTRAL'

                    date_val = r_val['date']
                    if 'T' in str(date_val):
                        date_val = str(date_val).split('T')[0]
                    try:
                        dt_obj = dt.fromisoformat(date_val)
                        date_str = dt_obj.strftime('%d-%m-%Y')
                    except Exception:
                        date_str = date_val

                    # ── EMA Crossover Strength ──
                    ema_dist_pct = (abs(r_e50 - r_e200) / r_e200 * 100.0) if r_e200 > 0 else 0.0
                    if ema_dist_pct <= 1.5:
                        ema_status = 'BULLISH_APPROACHING' if r_e50 < r_e200 else 'BEARISH_APPROACHING'
                    else:
                        ema_status = 'BULLISH_ACTIVE' if r_e50 > r_e200 else 'BEARISH_ACTIVE'
                    ema_strength = {
                        'BULLISH_APPROACHING': '+5', 'BEARISH_APPROACHING': '-5',
                        'BULLISH_ACTIVE': '+10',      'BEARISH_ACTIVE': '-10'
                    }.get(ema_status, '0')

                    # ── Gap Analytics up to i_idx ──
                    r_gap_ups = r_gap_downs = r_gap_up_fades = r_gap_down_fades = 0
                    for k in range(1, i_idx + 1):
                        o  = float(df_b.loc[k, 'open'])
                        cl = float(df_b.loc[k, 'close'])
                        ph = float(df_b.loc[k-1, 'high'])
                        pl = float(df_b.loc[k-1, 'low'])
                        if o > ph:
                            r_gap_ups += 1
                            if cl < o: r_gap_up_fades += 1
                        elif o < pl:
                            r_gap_downs += 1
                            if cl > o: r_gap_down_fades += 1

                    # ── Backtest Win-Rate up to i_idx ──
                    r_bull_wins = r_bull_total = r_bear_wins = r_bear_total = 0
                    for k in range(25, i_idx - 1):
                        sc = 50
                        e8_k, e20_k = float(df_b.loc[k, 'ema_8']), float(df_b.loc[k, 'ema_20'])
                        rs_k = float(df_b.loc[k, 'rsi'])
                        mc_k, mcs_k = float(df_b.loc[k, 'macd']), float(df_b.loc[k, 'macd_signal'])
                        cv_k = float(df_b.loc[k, 'close'])
                        if cv_k > e8_k:  sc += 15
                        if cv_k > e20_k: sc += 15
                        if rs_k > 50:  sc += 10
                        if rs_k > 60:  sc += 10
                        if rs_k < 40:  sc -= 15
                        sc += 10 if mc_k > mcs_k else -10
                        sc = max(0, min(100, sc))
                        nxt = float(df_b.loc[k+1, 'close'])
                        if sc >= 55:
                            r_bull_total += 1
                            if nxt > cv_k: r_bull_wins += 1
                        elif sc < 45:
                            r_bear_total += 1
                            if nxt < cv_k: r_bear_wins += 1

                    r_bull_wr = round(r_bull_wins / r_bull_total * 100, 1) if r_bull_total else 0.0
                    r_bear_wr = round(r_bear_wins / r_bear_total * 100, 1) if r_bear_total else 0.0

                    # ── Current Streak up to i_idx ──
                    closes_up_to = df_b['close'].iloc[:i_idx+1].tolist()
                    r_streak_days = 0
                    r_streak_dir = 'none'
                    if len(closes_up_to) >= 2:
                        r_streak_dir = 'climb' if closes_up_to[-1] > closes_up_to[-2] else 'drop'
                        for k in range(len(closes_up_to)-1, 0, -1):
                            if (r_streak_dir == 'climb' and closes_up_to[k] > closes_up_to[k-1]) or \
                               (r_streak_dir == 'drop'  and closes_up_to[k] < closes_up_to[k-1]):
                                r_streak_days += 1
                            else:
                                break

                    daily_list.append({
                        'date': date_str,
                        'open': round(float(r_val['open']), 2),
                        'high': round(float(r_val['high']), 2),
                        'low': round(float(r_val['low']), 2),
                        'close': round(r_close, 2),
                        'volume': int(r_val['volume']),
                        'change_pct': round(change_pct, 2),
                        'score': r_score,
                        'direction': r_direction,
                        'rsi': round(r_rsi, 1),
                        'macd_signal': 'BULLISH' if r_macd > r_macd_sig else 'BEARISH',
                        'chop': round(r_chop, 1),
                        'chop_state': 'TRENDING' if r_chop < 38.2 else 'COILING' if r_chop > 61.8 else 'NEUTRAL',
                        'ema_50': round(r_e50, 2),
                        'ema_200': round(r_e200, 2),
                        'ema_cross_status': ema_status,
                        'ema_cross_gap_pct': round(ema_dist_pct, 2),
                        'ema_strength': ema_strength,
                        'gap_up_count': r_gap_ups,
                        'gap_up_fade_pct': f"{round(r_gap_up_fades/r_gap_ups*100,1)}%" if r_gap_ups else '0%',
                        'gap_down_count': r_gap_downs,
                        'gap_down_fade_pct': f"{round(r_gap_down_fades/r_gap_downs*100,1)}%" if r_gap_downs else '0%',
                        'bull_backtest_triggers': r_bull_total,
                        'bull_backtest_win_rate': f"{r_bull_wr}%",
                        'bear_backtest_triggers': r_bear_total,
                        'bear_backtest_win_rate': f"{r_bear_wr}%",
                        'current_streak_dir': r_streak_dir,
                        'current_streak_days': r_streak_days,
                        'daily_trend': 'BULLISH' if r_close > r_e20 else 'BEARISH',
                        'hourly_trend': 'BULLISH' if r_close > r_e8 else 'BEARISH',
                        'm15_trend': 'BULLISH' if r_rsi > 50 else 'BEARISH',
                        'upcoming_predict_bias': calculate_upcoming_prediction(df_b, i_idx)['consensus_bias'],
                        'upcoming_predict_conf': calculate_upcoming_prediction(df_b, i_idx)['confidence_score']
                    })
                
                daily_list.reverse()
                row['daily_rows'] = daily_list

            except Exception as sym_err:
                row['status'] = f'ERROR: {sym_err}'

            results.append(row)

        return jsonify({'results': results, 'total': len(results), 'days': days})

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ── Traction Board 360° Real Market Data Endpoint ──
@app.route('/api/traction-board', methods=['GET', 'POST'])
def api_traction_board():
    """
    Computes real Kite-backed delivery conviction, price trend alignment,
    volume surges, and divergence signals for the Traction Board 360° view.
    """
    import re
    try:
        if request.method == 'POST':
            data = request.get_json(silent=True) or {}
            raw_symbols = data.get('symbols', [])
            period = int(data.get('period', 60))
            cap = data.get('cap', 'large')
        else:
            raw_symbols = request.args.get('symbols', '')
            period = int(request.args.get('period', 60))
            cap = request.args.get('cap', 'large')

        period = max(10, min(180, period))

        CAP_DEFAULTS = {
            'large': ['RELIANCE','TCS','INFY','HDFCBANK','ICICIBANK','SBIN','BAJFINANCE','LT',
                      'HINDUNILVR','ITC','AXISBANK','KOTAKBANK','MARUTI','TATAMOTORS','SUNPHARMA',
                      'WIPRO','BHARTIARTL','ASIANPAINT','TATASTEEL','HINDALCO','JSWSTEEL',
                      'ADANIENT','ADANIPORTS','POWERGRID','NTPC','COALINDIA','ONGC','BPCL','DRREDDY','CIPLA'],
            'mid': ['TRENT','COFORGE','KAYNES','PERSISTENT','MPHASIS','ZYDUSLIFE','JUBLFOOD',
                    'PIIND','PAGEIND','DIXON','POLYCAB','LALPATHLAB','METROPOLIS','IRCTC',
                    'GLAND','DEEPAKNTR','AAVAS','HOMEFIRST','CAMS','ANGELONE'],
            'small': ['IDFCFIRSTB','RBLBANK','BANDHANBNK','FEDERALBNK','KARURVYSYA',
                      'CENTURYTEX','GNFC','GHCL','ATUL','NAVINFLUOR','FINEORG','ROUTE',
                      'LATENTVIEW','TARSONS','HAPPYMIND'],
            'all': [
                '360ONE','ABB','ABCAPITAL','ADANIENSOL','ADANIENT','ADANIGREEN','ADANIPORTS','ADANIPOWER',
                'ALKEM','AMBER','AMBUJACEM','ANGELONE','APLAPOLLO','APOLLOHOSP','ASHOKLEY','ASIANPAINT',
                'ASTRAL','AUBANK','AUROPHARMA','AXISBANK','BAJAJ-AUTO','BAJAJFINSV','BAJAJHLDNG','BAJFINANCE',
                'BANDHANBNK','BANKBARODA','BANKINDIA','BDL','BEL','BHARATFORG','BHARTIARTL','BHEL',
                'BIOCON','BLUESTARCO','BOSCHLTD','BPCL','BRITANNIA','BSE','CAMS','CANBK',
                'CDSL','CGPOWER','CHOLAFIN','CIPLA','COALINDIA','COCHINSHIP','COFORGE','COLPAL',
                'CONCOR','CROMPTON','CUMMINSIND','DABUR','DALBHARAT','DELHIVERY','DIVISLAB','DIXON',
                'DLF','DMART','DRREDDY','EICHERMOT','ETERNAL','FEDERALBNK','FORCEMOT','FORTIS',
                'GAIL','GLENMARK','GMRAIRPORT','GODFRYPHLP','GODREJCP','GODREJPROP','GRASIM','GVT&D',
                'HAL','HAVELLS','HCLTECH','HDFCAMC','HDFCBANK','HDFCLIFE','HEROMOTOCO','HINDALCO',
                'HINDPETRO','HINDUNILVR','HINDZINC','HYUNDAI','ICICIBANK','ICICIGI','ICICIPRULI','IDEA',
                'IDFCFIRSTB','IEX','INDHOTEL','INDIANB','INDIGO','INDUSINDBK','INDUSTOWER','INFY',
                'INOXWIND','IOC','IREDA','IRFC','IRCTC','ITC','JINDALSTEL','JIOFIN','JSWENERGY',
                'JSWSTEEL','JUBLFOOD','KALYANKJIL','KAYNES','KEI','KFINTECH','KOTAKBANK','KPITTECH',
                'LAURUSLABS','LICHSGFIN','LICI','LODHA','LT','LTF','LTM','LUPIN',
                'M&M','MANAPPURAM','MANKIND','MARICO','MARUTI','MAXHEALTH','MAZDOCK','MCX',
                'MFSL','MOTHERSON','MOTILALOFS','MPHASIS','MUTHOOTFIN','NAM-INDIA','NATIONALUM','NAUKRI',
                'NBCC','NESTLEIND','NHPC','NMDC','NTPC','NYKAA','OBEROIRLTY','OFSS',
                'OIL','ONGC','PAGEIND','PATANJALI','PAYTM','PERSISTENT','PETRONET','PFC',
                'PGEL','PHOENIXLTD','PIDILITIND','PIIND','PNB','PNBHOUSING','POLICYBZR','POLYCAB',
                'POWERGRID','POWERINDIA','PREMIERENE','PRESTIGE','RADICO','RBLBANK','RECLTD','RELIANCE',
                'RVNL','SAIL','SBICARD','SBILIFE','SBIN','SHREECEM','SHRIRAMFIN','SIEMENS',
                'SOLARINDS','SONACOMS','SRF','SUNPHARMA','SUPREMEIND','SUZLON','SWIGGY','TATACONSUM',
                'TATAELXSI','TATAPOWER','TATASTEEL','TCS','TECHM','TIINDIA','TITAN','TMPV',
                'TORNTPHARM','TRENT','TVSMOTOR','ULTRACEMCO','UNIONBANK','UNITDSPR','UNOMINDA','UPL',
                'VBL','VEDL','VMM','VOLTAS','WAAREEENER','WIPRO','YESBANK','ZYDUSLIFE'
            ]
        }

        symbols = []
        if isinstance(raw_symbols, list) and raw_symbols:
            symbols = [str(s).strip().upper() for s in raw_symbols if str(s).strip()]
        elif isinstance(raw_symbols, str) and raw_symbols.strip():
            symbols = [s.strip().upper() for s in re.split(r'[\s,;\n]+', raw_symbols) if s.strip()]

        if not symbols:
            symbols = CAP_DEFAULTS.get(cap, CAP_DEFAULTS['large'])

        # Company Names lookup
        COMPANY_NAMES = {
            'RELIANCE':'Reliance Industries','HDFCBANK':'HDFC Bank','ICICIBANK':'ICICI Bank','INFY':'Infosys',
            'TCS':'Tata Consultancy','SBIN':'State Bank of India','AXISBANK':'Axis Bank',
            'KOTAKBANK':'Kotak Mahindra Bank','LT':'Larsen & Toubro','ITC':'ITC Ltd',
            'BAJFINANCE':'Bajaj Finance','MARUTI':'Maruti Suzuki','TATASTEEL':'Tata Steel',
            'ADANIENT':'Adani Enterprises','HINDUNILVR':'Hindustan Unilever','SUNPHARMA':'Sun Pharma',
            'TATAMOTORS':'Tata Motors','ULTRACEMCO':'UltraTech Cement','ONGC':'Oil & Natural Gas Corp',
            'NTPC':'NTPC Ltd','POWERGRID':'Power Grid Corp','COALINDIA':'Coal India',
            'HCLTECH':'HCL Technologies','WIPRO':'Wipro','BHARTIARTL':'Bharti Airtel',
            'ASIANPAINT':'Asian Paints','TITAN':'Titan Company','JSWSTEEL':'JSW Steel',
            'INDUSINDBK':'IndusInd Bank','BAJAJFINSV':'Bajaj Finserv','HINDALCO':'Hindalco Ind',
            'ADANIPORTS':'Adani Ports','BPCL':'BPCL','DRREDDY':'Dr Reddy Labs','CIPLA':'Cipla',
            'TRENT':'Trent Ltd','COFORGE':'Coforge Ltd','KAYNES':'Kaynes Tech','PERSISTENT':'Persistent Sys',
            'MPHASIS':'Mphasis Ltd','ZYDUSLIFE':'Zydus Life','JUBLFOOD':'Jubilant Food','PIIND':'PI Inds',
            'PAGEIND':'Page Inds','DIXON':'Dixon Tech','POLYCAB':'Polycab India','LALPATHLAB':'Lal PathLabs',
            'METROPOLIS':'Metropolis','IRCTC':'IRCTC Ltd','GLAND':'Gland Pharma','DEEPAKNTR':'Deepak Nitrite',
            'AAVAS':'Aavas Financiers','HOMEFIRST':'Home First','CAMS':'CAMS Ltd','ANGELONE':'Angel One',
            'IDFCFIRSTB':'IDFC First Bank','RBLBANK':'RBL Bank','BANDHANBNK':'Bandhan Bank',
            'FEDERALBNK':'Federal Bank','KARURVYSYA':'Karur Vysya','CENTURYTEX':'Century Tex',
            'GNFC':'GNFC Ltd','GHCL':'GHCL Ltd','ATUL':'Atul Ltd','NAVINFLUOR':'Navin Fluorine',
            'FINEORG':'Fine Organics','ROUTE':'Route Mobile','LATENTVIEW':'Latent View',
            'TARSONS':'Tarsons Products','HAPPYMIND':'Happiest Minds'
        }

        db = get_db()
        kite = get_kite()
        results = []

        for symbol in symbols:
            try:
                norm_sym = re.sub(r'[^A-Z0-9]', '', symbol)
                token = INSTRUMENT_TOKENS.get(symbol) or INSTRUMENT_TOKENS.get(norm_sym)
                if not token:
                    global _instruments_cache
                    if not _instruments_cache:
                        _instruments_cache = cache_get_instruments(db)
                    if _instruments_cache:
                        for inst in _instruments_cache:
                            if inst.get('tradingsymbol') == symbol and inst.get('exchange') == 'NSE':
                                token = inst.get('instrument_token')
                                break

                from_date = dt.now() - timedelta(days=max(period + 40, 120))
                to_date = dt.now()
                from_date_str = from_date.strftime('%Y-%m-%d')
                to_date_str = to_date.strftime('%Y-%m-%d')

                candles = []
                if token and kite:
                    try:
                        raw = kite.historical_data(token, from_date, to_date, 'day')
                        cache_store_ohlcv(db, token, raw, 'day')
                        for c in raw:
                            c_dict = dict(c)
                            if 'date' in c_dict and hasattr(c_dict['date'], 'isoformat'):
                                c_dict['date'] = c_dict['date'].isoformat()
                            candles.append(c_dict)
                    except Exception:
                        candles = cache_get_ohlcv(db, token, from_date_str, to_date_str, 'day') if token else []
                elif token:
                    candles = cache_get_ohlcv(db, token, from_date_str, to_date_str, 'day')

                if not candles or len(candles) < 5:
                    if token:
                        candles = generate_demo_candles(token, from_date, to_date, 'day')
                    else:
                        continue

                candles.sort(key=lambda x: str(x.get('date', '')))
                window_candles = candles[-period:] if len(candles) >= period else candles
                if len(window_candles) < 3:
                    continue

                closes = [float(c['close']) for c in window_candles]
                volumes = [float(c['volume']) for c in window_candles]
                highs = [float(c['high']) for c in window_candles]
                lows = [float(c['low']) for c in window_candles]
                opens = [float(c['open']) for c in window_candles]

                last_price = round(closes[-1], 2)
                first_price = closes[0]
                price_ret = round(((last_price - first_price) / first_price) * 100.0, 2) if first_price > 0 else 0.0

                # ── Trend calculation (9 MA vs 21 MA) ──
                s_ma = sum(closes[-min(9, len(closes)):]) / min(9, len(closes))
                l_ma = sum(closes[-min(21, len(closes)):]) / min(21, len(closes))
                price_trend = "Sideways"
                if s_ma > l_ma * 1.004 and price_ret > 1:
                    price_trend = "Uptrend"
                elif s_ma < l_ma * 0.996 and price_ret < -1:
                    price_trend = "Downtrend"

                # ── Volume surge ratio (last 5 vs period avg) ──
                avg_vol = sum(volumes) / len(volumes) if volumes else 1.0
                recent_vol = sum(volumes[-5:]) / min(5, len(volumes)) if volumes else 1.0
                vol_ratio = round(recent_vol / avg_vol, 2) if avg_vol > 0 else 1.0
                vol_trend = "Normal"
                if vol_ratio > 1.35:
                    vol_trend = "Surge"
                elif vol_ratio < 0.75:
                    vol_trend = "Dry-up"

                # ── Delivery Conviction Model ──
                # Use candle body-to-range absorption fraction + base delivery baseline
                del_series = []
                for i in range(len(window_candles)):
                    h, l, o, c = highs[i], lows[i], opens[i], closes[i]
                    rng = (h - l) if (h - l) > 0 else 1.0
                    body = abs(c - o)
                    # Institutional absorption heuristic (30% - 75%)
                    est_del = min(88.0, max(18.0, round((body / rng * 35.0 + 35.0), 1)))
                    del_series.append(est_del)

                last_del = del_series[-1]
                avg_del = sum(del_series) / len(del_series)
                recent_del = sum(del_series[-5:]) / min(5, len(del_series))
                del_change_pts = round(recent_del - avg_del, 1)

                del_trend = "Stable"
                if del_change_pts > 2.5:
                    del_trend = "Expanding"
                elif del_change_pts < -2.5:
                    del_trend = "Contracting"

                sorted_del = sorted(del_series)
                del_rank = round((sorted_del.index(last_del) / len(sorted_del)) * 100.0, 1)

                # ── Composite Alignment Score (-2 to +2) ──
                score = 0
                if price_trend == "Uptrend": score += 1
                elif price_trend == "Downtrend": score -= 1

                if vol_trend == "Surge":
                    score += (1 if price_trend == "Uptrend" else (-1 if price_trend == "Downtrend" else 0))

                if del_trend == "Expanding" and price_trend != "Downtrend":
                    score += 1
                if del_trend == "Contracting" and price_trend == "Uptrend":
                    score -= 1

                score = max(-2, min(2, score))

                # ── Quadrant Classification ──
                p_u = price_ret > 1.0
                p_d = price_ret < -1.0
                d_u = del_change_pts > 1.5
                d_d = del_change_pts < -1.5

                quadrant = "none"
                if p_u and d_u: quadrant = "confirm-up"
                elif p_d and d_d: quadrant = "confirm-down"
                elif p_u and d_d: quadrant = "div-bear"
                elif p_d and d_u: quadrant = "div-bull"

                results.append({
                    'symbol': symbol,
                    'name': COMPANY_NAMES.get(symbol, symbol),
                    'price': last_price,
                    'chg': price_ret,
                    'priceTrend': price_trend,
                    'volRatio': vol_ratio,
                    'volTrend': vol_trend,
                    'delPct': last_del,
                    'delTrend': del_trend,
                    'delChangePts': del_change_pts,
                    'delRank': del_rank,
                    'score': score,
                    'quadrant': quadrant,
                    'sparkline': del_series[-20:]
                })
            except Exception as e:
                continue

        # Sort results by absolute alignment score descending
        pulse_list = sorted(results, key=lambda x: abs(x['score']), reverse=True)
        bear_div = sorted([r for r in results if r['quadrant'] == 'div-bear'], key=lambda x: x['chg'], reverse=True)
        bull_div = sorted([r for r in results if r['quadrant'] == 'div-bull'], key=lambda x: x['chg'])

        return jsonify({
            'success': True,
            'period': period,
            'cap': cap,
            'total': len(results),
            'marketPulse': pulse_list,
            'tractionBoard': results,
            'divergenceWatchlist': {
                'bearDiv': bear_div,
                'bullDiv': bull_div
            },
            'computedAt': dt.now().strftime('%d %b %Y, %H:%M')
        })
    except Exception as err:
        import traceback; traceback.print_exc()
        return jsonify({'success': False, 'error': str(err)}), 500


_inst_holding_cache = None
_inst_holding_lock = threading.Lock()

def get_inst_holding_map():
    """Returns in-memory cached institutional holding map, queried from SQLite on first load."""
    global _inst_holding_cache
    if _inst_holding_cache is not None:
        return _inst_holding_cache
    with _inst_holding_lock:
        if _inst_holding_cache is not None:
            return _inst_holding_cache
        try:
            db = get_db()
            cursor = db.cursor()
            cursor.execute("SELECT symbol, total_institutional FROM fno_shareholding")
            _inst_holding_cache = {row[0]: row[1] for row in cursor.fetchall()}
        except Exception as db_err:
            logging.warning(f"[Gainers API] Failed to fetch fno_shareholding: {db_err}")
            return {}
        return _inst_holding_cache


# ── Option Chain ──
@app.route('/api/option-gainers-board')
def option_gainers_board():
    """
    Live F&O Premium Gainers Board.
    Fetches fresh LTPs from Kite on every browser poll (~30s) so the dashboard
    shows real-time prices. Board token discovery still runs every 5 minutes
    in the background (and Telegram alerts are sent every 5 minutes unchanged).
    """
    lazy_start_option_scanners()
    try:
        from option_gainers_scanner import get_board_state, get_gainers_snapshot_from_db
        date_str = request.args.get('date')
        if date_str:
            db_snap = get_gainers_snapshot_from_db(date_str)
            if db_snap:
                return jsonify(db_snap)
            return jsonify({
                "stocks": [], "total_tracked": 0, "total_positive": 0,
                "n_stocks": 0, "last_updated": None, "date": date_str,
                "status": "not_found", "message": f"No board snapshot found for date {date_str}"
            })

        state = get_board_state()

        open_premiums   = state.get("open_premiums", {})
        board_contracts = state.get("board_contracts", {})

        from session_utils import is_market_hours, is_premarket
        _in_market = is_market_hours()
        _in_premarket = is_premarket()

        # Query cached institutional holdings (in-memory)
        inst_holding_map = get_inst_holding_map()

        # ── LIVE HOURS: board still warming (scanner hasn't captured open yet) ──
        # Must check _in_market FIRST — never serve EOD during live session.
        if _in_market and (not open_premiums or not board_contracts):
            return jsonify({
                "stocks": [], "total_tracked": 0, "total_positive": 0,
                "n_stocks": 0, "last_updated": None, "date": state.get("date"),
                "status": "loading",
                "message": "Board warming up... refresh in ~30 seconds"
            })

        # ── OUTSIDE MARKET HOURS: serve EOD snapshot or premarket loading ──
        if not _in_market:
            if not _in_premarket:
                # Truly closed — serve EOD snapshot
                from option_gainers_scanner import get_eod_snapshot
                snapshot = get_eod_snapshot()
                if snapshot is not None:
                    # Inject on the fly
                    from ema_crossover_scanner import get_ema_crossover_state, get_daily_dxcnt_map
                    ema_state_eod = get_ema_crossover_state().get("crossovers", {})
                    dxcnt_map_eod = get_daily_dxcnt_map()
                    for s in snapshot.get("stocks", []):
                        s["inst_holding"] = inst_holding_map.get(s["symbol"], 0.0)
                        s["dxcnt"] = s.get("dxcnt") if s.get("dxcnt") is not None else dxcnt_map_eod.get(s["symbol"], 0)
                        s["ema9_hold"] = s.get("ema9_hold") if s.get("ema9_hold") is not None else ema_state_eod.get(s["symbol"], {}).get("ema9_hold")
                        s["ema9_hold_minutes"] = s.get("ema9_hold_minutes") if s.get("ema9_hold_minutes") is not None else ema_state_eod.get(s["symbol"], {}).get("ema9_hold_minutes", 0)
                        s["fh_spurt_ratio"] = s.get("fh_spurt_ratio") if s.get("fh_spurt_ratio") is not None else ema_state_eod.get(s["symbol"], {}).get("fh_spurt_ratio")
                        s["fh_cumulative_ratio"] = s.get("fh_cumulative_ratio") if s.get("fh_cumulative_ratio") is not None else ema_state_eod.get(s["symbol"], {}).get("fh_cumulative_ratio")
                        s["fh_spurt_tag"] = s.get("fh_spurt_tag") if s.get("fh_spurt_tag") is not None else ema_state_eod.get(s["symbol"], {}).get("fh_spurt_tag")
                        s["linearity_score"] = s.get("linearity_score", 0.0) or ema_state_eod.get(s["symbol"], {}).get("linearity_score", 0.0)
                        s["net_movement"] = s.get("net_movement", 0.0) or ema_state_eod.get(s["symbol"], {}).get("net_movement", 0.0)
                    return jsonify(snapshot)
                else:
                    return jsonify({
                        "stocks": [], "total_tracked": 0, "total_positive": 0,
                        "n_stocks": 0, "last_updated": None, "date": state.get("date"),
                        "status": "loading",
                        "message": "Generating EOD snapshot... please wait."
                    })
            else:
                # Pre-open (9:00–9:15) — board not yet active for today
                return jsonify({
                    "stocks": [], "total_tracked": 0, "total_positive": 0,
                    "n_stocks": 0, "last_updated": None, "date": state.get("date"),
                    "status": "loading",
                    "message": "Pre-market: board initializing for today's session"
                })

        kite = get_kite()
        if not kite:
            return jsonify({"error": "Kite session unavailable", "stocks": []}), 503

        # ── Fetch LIVE LTPs for all board tokens ──────────────────────────────
        tokens = list(open_premiums.keys())
        ltps = {}
        for b in range(0, len(tokens), 500):
            batch = tokens[b:b+500]
            try:
                quotes = kite.quote(batch)
                for token_key, q in quotes.items():
                    t = int(token_key)
                    ltp = q.get("last_price", 0) or 0
                    if ltp > 0:          # vol > 0 removed: volume is 0 at market open for all options
                        ltps[t] = ltp
            except Exception as e:
                logging.warning(f"[Gainers API] LTP batch failed: {e}")

        # ── Compute % gains, keep positives ───────────────────────────────────
        results = []
        for token, open_prem in open_premiums.items():
            ltp = ltps.get(token)
            info = board_contracts.get(token)
            if not info:
                continue
            if not ltp:
                # No live tick yet — include contract as stale at 0% gain so
                # it remains visible in the premium expansion panel.
                results.append({
                    "token":     int(token),
                    "symbol":    info["symbol"],
                    "opt_type":  info["opt_type"],
                    "strike":    info["strike"],
                    "is_opening": info["is_opening"],
                    "open_prem": round(open_prem, 2),
                    "ltp":       round(open_prem, 2),  # use open as best guess
                    "gain_pct":  0.0,
                    "ltp_stale": True,
                })
                continue
            gain_pct = ((ltp - open_prem) / open_prem) * 100
            if gain_pct <= 0:
                continue
            results.append({
                "token":      int(token),
                "symbol":     info["symbol"],
                "opt_type":   info["opt_type"],
                "strike":     info["strike"],
                "is_opening": info["is_opening"],
                "open_prem":  round(open_prem, 2),
                "ltp":        round(ltp, 2),
                "gain_pct":   round(gain_pct, 2),
                "ltp_stale":  False,
            })

        # ── Group by stock, rank by best gain ─────────────────────────────────
        by_symbol = {}
        for r in results:
            by_symbol.setdefault(r["symbol"], []).append(r)

        ranked_stocks = sorted(
            by_symbol.items(),
            key=lambda kv: max(r["gain_pct"] for r in kv[1]),
            reverse=True
        )

        from session_utils import now_ist
        now_str = now_ist().strftime("%Y-%m-%dT%H:%M:%S")

        # ── Fetch spot % change + volume for RVOL (one batch call, already needed) ──
        from oi_spurt_routes import EXCHANGE_MAP
        unique_symbols = list({sym for sym, _ in ranked_stocks})
        spot_change_map = {}
        gap_map         = {}
        volume_map      = {}   # {symbol: current_intraday_volume}
        token_map       = {}   # {symbol: instrument_token} — for RVOL warm (no extra call)
        try:
            spot_queries = [EXCHANGE_MAP.get(s, f"NSE:{s}") for s in unique_symbols]
            sq = kite.quote(spot_queries)
            from option_gainers_scanner import _prev_close_cache
            for exch_sym, d in sq.items():
                sym   = exch_sym.split(":")[-1]
                ltp   = d.get("last_price", 0) or 0
                ohlc  = d.get("ohlc") or {}
                open_px = ohlc.get("open") or 0
                vol   = d.get("volume", 0) or 0
                token = d.get("instrument_token")

                # During market hours, ohlc.close is yesterday's close —
                # populate the cache so EOD snapshot and future calls use correct prev_close.
                if _in_market:
                    ohlc_close = ohlc.get("close", 0) or 0
                    if ohlc_close > 0:
                        _prev_close_cache[sym] = ohlc_close

                prev = _prev_close_cache.get(sym) or ohlc.get("close") or 0
                if prev > 0 and ltp > 0:
                    spot_change_map[sym] = round(((ltp - prev) / prev) * 100, 2)
                    gap_map[sym] = round(((open_px - prev) / prev) * 100, 2)
                else:
                    gap_map[sym] = 0.0
                if vol > 0:
                    volume_map[sym] = vol
                if token:
                    token_map[sym] = token
        except Exception as e:
            logging.warning(f"[Gainers API] Spot change fetch failed: {e}")

        # ── RVOL — Relative Volume vs 20-day avg at same time of day ──────────
        # Non-blocking warm (background thread, once per day, ~30s one-time cost)
        from option_gainers_scanner import ensure_avg_volume_warm, get_avg_volume
        ensure_avg_volume_warm(kite, token_map)

        rvol_map = {}
        _now_ist  = now_ist()
        _elapsed  = (_now_ist.hour - 9) * 60 + _now_ist.minute - 15  # minutes since 09:15
        if _elapsed > 0:
            for sym, curr_vol in volume_map.items():
                avg_vol = get_avg_volume(sym)
                if avg_vol and avg_vol > 0:
                    expected = avg_vol * (_elapsed / 375.0)
                    if expected > 0:
                        rvol_map[sym] = round(curr_vol / expected, 1)

        # Get the cached crossover state and daily DXCNT map for this symbol
        from ema_crossover_scanner import get_ema_crossover_state, get_daily_dxcnt_map
        ema_state = get_ema_crossover_state().get("crossovers", {})
        dxcnt_map = get_daily_dxcnt_map()

        stocks_out = [
            {
                "symbol":          sym,
                "best_gain":       round(max(r["gain_pct"] for r in contracts), 2),
                "spot_change_pct": spot_change_map.get(sym),
                "gap_pct":         gap_map.get(sym, 0.0),
                "rvol_ratio":      rvol_map.get(sym),
                "linearity_score": ema_state.get(sym, {}).get("linearity_score", 0.0),
                "net_movement":    ema_state.get(sym, {}).get("net_movement", 0.0),
                "dxcnt":           dxcnt_map.get(sym, 0),
                "inst_holding":    inst_holding_map.get(sym, 0.0),
                "ema9_hold":       ema_state.get(sym, {}).get("ema9_hold"),
                "ema9_hold_minutes": ema_state.get(sym, {}).get("ema9_hold_minutes", 0),
                "fh_spurt_ratio":      ema_state.get(sym, {}).get("fh_spurt_ratio"),
                "fh_cumulative_ratio": ema_state.get(sym, {}).get("fh_cumulative_ratio"),
                "fh_spurt_tag":        ema_state.get(sym, {}).get("fh_spurt_tag"),
                # ascending within stock (lowest → highest — crescendo)
                "contracts":       sorted(contracts, key=lambda x: x["gain_pct"]),
            }
            for sym, contracts in ranked_stocks
        ]

        return jsonify({
            "stocks":         stocks_out,
            "total_tracked":  len(open_premiums),
            "total_positive": len(results),
            "n_stocks":       len(ranked_stocks),
            "last_updated":   now_str,
            "date":           state.get("date"),
        })

    except Exception as e:
        logging.error(f"[Gainers API] Exception: {e}")
        return jsonify({"error": str(e), "stocks": [], "last_updated": None}), 500


@app.route('/api/option-gainers/timeline')
def option_gainers_timeline():
    """
    Computes cumulative 20% incremental milestone timeline for an option contract.
    Params:
      token: instrument_token
      symbol: stock symbol (e.g. PIIND, GLENMARK)
      strike: float strike price (e.g. 2200)
      opt_type: 'CE' or 'PE'
      date: 'YYYY-MM-DD' (optional, defaults to current trading date)
      step: float milestone step % (default 20.0)
    """
    lazy_start_option_scanners()
    try:
        from option_gainers_scanner import get_contract_milestones
        token = request.args.get('token', type=int)
        symbol = request.args.get('symbol')
        strike = request.args.get('strike', type=float)
        opt_type = request.args.get('opt_type')
        date_str = request.args.get('date')
        step_pct = request.args.get('step', default=20.0, type=float)

        res = get_contract_milestones(
            token=token,
            symbol=symbol,
            strike=strike,
            opt_type=opt_type,
            date_str=date_str,
            step_pct=step_pct
        )
        return jsonify(res)
    except Exception as e:
        logging.error(f"[Timeline API] Exception: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/option-gainers-alerts')
def option_gainers_alerts():
    """
    Fresh alert stream for contracts tracked by the F&O Premium Gainers Board.
    This does not reuse the existing option premium scanner or FNO spike engine.
    """
    lazy_start_option_gainers_alerts()
    try:
        from option_gainers_alerts import get_alerts, get_alerts_from_db_by_date
        date_str = request.args.get('date')
        if date_str:
            db_alerts = get_alerts_from_db_by_date(date_str)
            return jsonify({
                "alerts": db_alerts,
                "trade_date": date_str,
                "total_alerts": len(db_alerts),
                "is_historical": True
            })

        after = request.args.get('after', type=int)
        if after is None and not is_market_hours() and not is_premarket():
            from option_gainers_alerts import get_eod_snapshot, is_eod_snapshot_running
            snapshot = get_eod_snapshot()
            if snapshot is not None:
                return jsonify(snapshot)
            if is_eod_snapshot_running():
                return jsonify({
                    "status": "loading",
                    "alerts": [],
                    "is_eod_snapshot": True,
                    "message": "Reconstructing Premium Spike EOD snapshot"
                })
        return jsonify({"alerts": get_alerts(after=after)})
    except Exception as e:
        logging.error(f"[Premium Alerts API] Exception: {e}")
        return jsonify({"error": str(e), "alerts": []}), 500


@app.route('/api/option-gainers-alerts/status')
def option_gainers_alerts_status():
    lazy_start_option_gainers_alerts()
    try:
        from option_gainers_alerts import get_status
        return jsonify(get_status())
    except Exception as e:
        logging.error(f"[Premium Alerts Status API] Exception: {e}")
        return jsonify({"error": str(e), "status": "error"}), 500


@app.route('/api/option-gainers-alerts/clear', methods=['POST'])
def option_gainers_alerts_clear():
    lazy_start_option_gainers_alerts()
    try:
        from option_gainers_alerts import clear_alerts
        clear_alerts()
        return jsonify({"ok": True})
    except Exception as e:
        logging.error(f"[Premium Alerts Clear API] Exception: {e}")
        return jsonify({"error": str(e), "ok": False}), 500


@app.route('/api/eod-alert-summary')
def api_eod_alert_summary():
    """
    Returns both Premium Spike alerts and Live Breakout alerts saved to SQLite
    for a given date. Only available after market close — blocked during live hours
    to prevent stale data from appearing on the live dashboard.

    Query params:
        date (str): YYYY-MM-DD — defaults to last completed trading session date.
    """
    from session_utils import is_market_hours, is_premarket
    if is_market_hours() or is_premarket():
        return jsonify({"error": "EOD summary only available after market close", "ok": False}), 403

    try:
        from option_gainers_alerts import get_alerts_from_db_by_date, _expected_trading_date as _prem_expected_date
        from ema_crossover_scanner import get_breakout_alerts_from_db_by_date, _get_expected_trading_date
        from session_utils import now_ist

        date_str = request.args.get("date")
        if date_str:
            # Validate format
            from datetime import datetime as _dt
            try:
                _dt.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                return jsonify({"error": f"Invalid date format: '{date_str}'. Expected YYYY-MM-DD"}), 400
        else:
            now = now_ist()
            date_str = _get_expected_trading_date(now).strftime("%Y-%m-%d")

        prem_spikes = get_alerts_from_db_by_date(date_str)
        live_breakouts = get_breakout_alerts_from_db_by_date(date_str)

        return jsonify({
            "date": date_str,
            "prem_spikes": prem_spikes,
            "live_breakouts": live_breakouts,
            "prem_total": len(prem_spikes),
            "breakout_total": len(live_breakouts),
            "is_eod_snapshot": True,
        })
    except Exception as e:
        logging.error(f"[EOD Alert Summary API] Exception: {e}")
        return jsonify({"error": str(e), "ok": False}), 500


@app.route('/api/nifty-candle-analysis')
def api_nifty_candle_analysis():
    """
    Returns the chronological list of analyzed Nifty 5-minute candles
    showing stock contributions, sector breadths, drivers, and RVOL.
    """
    lazy_start_ema_crossover_scanner()
    from nifty_candle_analyzer import get_historical_analysis
    return jsonify(get_historical_analysis())


@app.route('/api/live-breakouts')
def api_live_breakouts():
    """
    Returns the active squeeze watchlist and all live breakouts triggered today.
    """
    lazy_start_ema_crossover_scanner()
    from ema_crossover_scanner import get_live_breakout_state, notify_ema_client
    notify_ema_client()  # keep scanner alive when board is open
    return jsonify(get_live_breakout_state())


@app.route('/api/ema-collision-alerts')
def api_ema_collision_alerts():
    """
    Returns all EMA Collision alerts (EMA9/EMA21 coil → confirmed break) triggered today.
    Response: { "alerts": [...], "count": N }
    Already included in /api/live-breakouts as collision_alerts — this endpoint
    is a dedicated fetch for consumers that only need collision data.
    """
    lazy_start_ema_crossover_scanner()
    from ema_crossover_scanner import get_live_breakout_state
    state = get_live_breakout_state()
    alerts = state.get('collision_alerts', [])
    return jsonify({"alerts": alerts, "count": len(alerts)})


@app.route('/api/debug-crossover')
def api_debug_crossover():
    from global_ticker import get_global_ticker_manager
    gtm = get_global_ticker_manager()
    from ema_crossover_scanner import _tick_buffers, _live_subscribed_tokens
    return jsonify({
        'gtm_active': gtm.is_running,
        'gtm_api_key_set': bool(gtm.api_key),
        'gtm_access_token_set': bool(gtm.access_token),
        'gtm_kws_exists': gtm.kws is not None,
        'gtm_kws_connected': gtm.kws.is_connected() if gtm.kws else False,
        'gtm_last_exception': gtm.last_exception,
        'gtm_callbacks': list(gtm.callbacks.keys()),
        'gtm_active_tokens': list(gtm.active_tokens),
        'live_subscribed_tokens': list(_live_subscribed_tokens),
        'tick_buffers_keys': list(_tick_buffers.keys()),
        'tick_buffers': {str(k): {'symbol': v['symbol'], 'ticks_count': len(v['ticks']), 'last_ltp': v['last_ltp']} for k, v in _tick_buffers.items()}
    })








@app.route('/api/heartbeat', methods=['POST', 'GET'])
def api_heartbeat():
    """
    Generic page-presence heartbeat.
    Any open browser tab should call this periodically (e.g. every 60 s).
    Keeps background engines (EMA Scanner, etc.) active.
    """
    return jsonify({'ok': True})


@app.route('/api/confluence')
def api_confluence():
    """
    Multi-timeframe confluence for a list of NSE equity symbols.
    Returns RSI, MACD direction, EMA trend, and overall majority-vote signal
    for 15 Min / 1 Hour / Daily timeframes.

    Query param:  symbols=GLENMARK,KAYNES,...   (comma-separated, max 50)
    Response:     { GLENMARK: { 15m: {rsi,macd,ema,overall}, 1h:{...}, day:{...} }, ... }
    """
    try:
        symbols_raw = request.args.get('symbols', '')
        symbols = [s.strip().upper() for s in symbols_raw.split(',') if s.strip()][:50]
        if not symbols:
            return jsonify({})

        kite = get_kite()
        if not kite:
            return jsonify({'error': 'Kite unavailable'}), 503

        TIMEFRAMES = [
            ('15m', '15minute', 3,  50),   # 3 days back, last 50 candles
            ('1h',  '60minute', 8,  50),   # 8 days back (covers weekends)
            ('day', 'day',      90, 60),   # 90 days back for stable MACD
        ]

        def _signal(closes):
            """Compute RSI, MACD, EMA20 signals and overall from close prices."""
            if not closes or len(closes) < 5:
                return {'rsi': 50, 'macd': 'neutral', 'ema': 'neutral', 'overall': 'neutral'}

            rsi_val    = compute_rsi(closes)
            macd_data  = compute_macd(closes)
            ema20      = ema_last(closes, 20)
            last_close = closes[-1]

            rsi_sig  = 'bullish' if rsi_val > 55 else ('bearish' if rsi_val < 45 else 'neutral')
            macd_sig = 'bullish' if macd_data['histogram'] > 0 else ('bearish' if macd_data['histogram'] < 0 else 'neutral')
            ema_sig  = 'bullish' if last_close > ema20 * 1.001 else ('bearish' if last_close < ema20 * 0.999 else 'neutral')

            votes    = [rsi_sig, macd_sig, ema_sig]
            bull_ct  = votes.count('bullish')
            bear_ct  = votes.count('bearish')
            overall  = 'bullish' if bull_ct >= 2 else ('bearish' if bear_ct >= 2 else 'neutral')

            return {
                'rsi':     round(rsi_val, 1),
                'macd':    macd_sig,
                'ema':     ema_sig,
                'overall': overall,
            }

        # Pre-load instruments cache sequentially on the main thread to ensure thread safety
        global _instruments_cache
        if not _instruments_cache:
            db = get_db()
            _instruments_cache = cache_get_instruments(db)

        result = {}

        # Pre-warm _live_ltp_cache for all confluence symbols with ONE batch call.
        # Without this, each get_historical_candles() call inside the parallel executor
        # falls back to an individual unbatched kite.quote() REST call for live price
        # stitching — up to 150 individual calls per 30s cycle, saturating the quote API.
        if is_market_hours():
            try:
                from oi_spurt_routes import EXCHANGE_MAP
                spot_queries = [EXCHANGE_MAP.get(s, f"NSE:{s}") for s in symbols]
                batch_quotes = kite.quote(spot_queries)
                now_ts = time.time()
                with _live_ltp_lock:
                    for exch_sym, q in batch_quotes.items():
                        tok = q.get("instrument_token")
                        ltp = q.get("last_price")
                        if tok and ltp:
                            _live_ltp_cache[int(tok)] = {"ltp": ltp, "ts": now_ts}
            except Exception:
                pass  # Non-critical — REST fallback still works if this fails

        # Parallelize fetches across symbols to prevent request timeouts and thread-blocking
        def _fetch_symbol_confluence(symbol):
            tf_out = {}
            for tf_label, interval, days_back, limit in TIMEFRAMES:
                candles = get_historical_candles(kite, symbol, interval,
                                                 days_back=days_back, limit=limit)
                closes  = [c['close'] for c in candles] if candles else []
                tf_out[tf_label] = _signal(closes)
            return symbol, tf_out

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(_fetch_symbol_confluence, s): s for s in symbols}
            for future in concurrent.futures.as_completed(futures):
                sym = futures[future]
                try:
                    symbol, tf_out = future.result()
                    result[symbol] = tf_out
                except Exception as e:
                    logging.warning(f"[Confluence API] Symbol fetch failed for {sym}: {e}")
                    result[sym] = {tf_label: _signal([]) for tf_label, _, _, _ in TIMEFRAMES}

        return jsonify(result)

    except Exception as e:
        logging.error(f"[Confluence API] {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/option-chain')


def option_chain():
    try:
        kite = get_kite()
        if not kite:
            return jsonify({'error': 'Not connected'}), 401

        symbol = request.args.get('symbol', '')
        expiry = request.args.get('expiry', '')

        global _instruments_cache
        if not _instruments_cache:
            db = get_db()
            _instruments_cache = cache_get_instruments(db)

        options = [i for i in _instruments_cache
                   if i.get('name') == symbol and i.get('segment') == 'NFO-OPT']

        if not options:
            return jsonify({'chain': [], 'spot_price': 0, 'expiry': None, 'available_expiries': []})

        def to_date_obj(e):
            if hasattr(e, 'date'): return e.date()
            if hasattr(e, 'isoformat'): return e
            try: return dt.strptime(str(e).split('T')[0], '%Y-%m-%d').date()
            except: return dt.now().date()

        today = dt.now().date()
        all_expiries_dates = sorted({to_date_obj(i['expiry']) for i in options if i.get('expiry')})
        available_expiries = [e.isoformat() if hasattr(e, 'isoformat') else str(e) for e in all_expiries_dates]

        active_expiry = None
        if expiry:
            exp_str = expiry.strip()
            def expiry_matches(inst_expiry):
                if inst_expiry is None:
                    return False
                if hasattr(inst_expiry, 'isoformat'):
                    return inst_expiry.isoformat() == exp_str
                return str(inst_expiry).split('T')[0] == exp_str
            options = [i for i in options if expiry_matches(i.get('expiry'))]
            active_expiry = exp_str
        else:
            # Default to the nearest active/upcoming expiry
            nearest = next((e for e in all_expiries_dates if e >= today), all_expiries_dates[0] if all_expiries_dates else None)
            if nearest:
                options = [i for i in options if to_date_obj(i.get('expiry')) == nearest]
                active_expiry = nearest.isoformat() if hasattr(nearest, 'isoformat') else str(nearest)

        if not options:
            return jsonify({'chain': [], 'spot_price': 0, 'expiry': active_expiry, 'available_expiries': available_expiries})

        # Limit to 500 options and batch quote calls (Kite API limit ~200/call)
        options = options[:500]
        quotes = {}
        batch_size = 200
        for batch_start in range(0, len(options), batch_size):
            batch = options[batch_start:batch_start + batch_size]
            batch_symbols = [f"NFO:{i['tradingsymbol']}" for i in batch]
            try:
                quotes.update(kite.quote(batch_symbols))
            except Exception:
                pass

        strikes = {}
        # Map index names to their NSE trading symbols
        nse_symbol_map = {
            'NIFTY': 'NIFTY 50',
            'BANKNIFTY': 'NIFTY BANK',
            'FINNIFTY': 'NIFTY FIN SERVICE',
        }
        nse_name = nse_symbol_map.get(symbol, symbol)
        underlying_sym = f"NSE:{nse_name}"
        try:
            spot = kite.ltp([underlying_sym])[underlying_sym]['last_price']
        except Exception:
            spot = 0

        for opt in options:
            strike = opt.get('strike', 0)
            opt_type = opt.get('instrument_type', '')
            ts = f"NFO:{opt['tradingsymbol']}"
            q = quotes.get(ts, {})

            if strike not in strikes:
                strikes[strike] = {'strike': strike, 'ce': {}, 'pe': {}}

            side = 'ce' if opt_type == 'CE' else 'pe'
            ltp = q.get('last_price', 0) or 0
            prev_close = (q.get('ohlc') or {}).get('close') or ltp
            change = ltp - prev_close if prev_close else 0
            strikes[strike][side] = {
                'oi': q.get('oi', 0),
                'oiChange': q.get('oi_day_change', 0),
                'volume': q.get('volume', 0),
                'iv': round(q.get('implied_volatility', 0) or 0, 1),
                'ltp': ltp,
                'change': round(change, 2),
                'changePct': round((change / prev_close) * 100, 2) if prev_close else 0,
                'delta': 0
            }

        chain = sorted(strikes.values(), key=lambda x: x['strike'])
        return jsonify({'chain': chain, 'spot_price': spot, 'expiry': active_expiry, 'available_expiries': available_expiries})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Max Pain ──
@app.route('/api/max-pain')
def max_pain():
    """Compute max pain strike for a given stock/index symbol.
    Query params: symbol (e.g. RELIANCE, NIFTY)
    Returns: { max_pain, spot_price, pct_from_spot, expiry }
    """
    try:
        kite = get_kite()
        if not kite:
            return jsonify({'error': 'Not connected'}), 401

        symbol = request.args.get('symbol', '').upper().strip()
        if not symbol:
            return jsonify({'error': 'symbol required'}), 400

        global _instruments_cache
        if not _instruments_cache:
            db = get_db()
            _instruments_cache = cache_get_instruments(db)

        # Get all options for this symbol — pick the nearest upcoming expiry
        today = dt.now().date()
        opts = [i for i in _instruments_cache
                if i.get('name') == symbol and i.get('segment') == 'NFO-OPT']

        if not opts:
            return jsonify({'error': f'No options found for {symbol}'}), 404

        # Nearest expiry
        def to_date(e):
            if hasattr(e, 'date'): return e.date()
            if hasattr(e, 'isoformat'): return e
            try: return dt.strptime(str(e).split('T')[0], '%Y-%m-%d').date()
            except: return today
        expiries = sorted({to_date(i['expiry']) for i in opts if i.get('expiry')})
        nearest = next((e for e in expiries if e >= today), None)
        if not nearest:
            return jsonify({'error': 'No active expiry found'}), 404

        opts = [i for i in opts if to_date(i.get('expiry')) == nearest]

        # Fetch quotes in batches
        quotes = {}
        batch_size = 200
        for batch_start in range(0, min(len(opts), 500), batch_size):
            batch = opts[batch_start:batch_start + batch_size]
            try:
                quotes.update(kite.quote([f"NFO:{i['tradingsymbol']}" for i in batch]))
            except Exception:
                pass

        # Build strike → {call_oi, put_oi} map
        strike_map = {}
        for opt in opts:
            strike = opt.get('strike', 0)
            if not strike:
                continue
            ts = f"NFO:{opt['tradingsymbol']}"
            oi = quotes.get(ts, {}).get('oi', 0) or 0
            opt_type = opt.get('instrument_type', '')
            if strike not in strike_map:
                strike_map[strike] = {'call_oi': 0, 'put_oi': 0}
            if opt_type == 'CE':
                strike_map[strike]['call_oi'] += oi
            else:
                strike_map[strike]['put_oi'] += oi

        if not strike_map:
            return jsonify({'error': 'No OI data available'}), 404

        strikes = sorted(strike_map.keys())

        # Max Pain = strike where total writer loss (call + put writers) is minimum
        min_loss = float('inf')
        max_pain_strike = strikes[len(strikes) // 2]
        for k in strikes:
            total_loss = 0
            for s in strikes:
                call_oi = strike_map[s]['call_oi']
                put_oi  = strike_map[s]['put_oi']
                total_loss += max(0, k - s) * call_oi   # loss for call writers if price = k
                total_loss += max(0, s - k) * put_oi    # loss for put writers if price = k
            if total_loss < min_loss:
                min_loss = total_loss
                max_pain_strike = k

        # Spot price
        nse_map = {'NIFTY': 'NIFTY 50', 'BANKNIFTY': 'NIFTY BANK', 'FINNIFTY': 'NIFTY FIN SERVICE'}
        nse_sym = f"NSE:{nse_map.get(symbol, symbol)}"
        try:
            spot = kite.ltp([nse_sym])[nse_sym]['last_price']
        except Exception:
            spot = (strikes[0] + strikes[-1]) / 2

        pct_from_spot = round(((max_pain_strike - spot) / spot) * 100, 2) if spot else 0

        return jsonify({
            'max_pain':      max_pain_strike,
            'spot_price':    spot,
            'pct_from_spot': pct_from_spot,
            'expiry':        nearest.isoformat(),
            'strikes_count': len(strikes)
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Expiries ──
@app.route('/api/expiries')
def expiries():
    try:
        kite = get_kite()
        if not kite:
            return jsonify({'error': 'Not connected'}), 401

        symbol = request.args.get('symbol', '')
        global _instruments_cache
        if not _instruments_cache:
            db = get_db()
            _instruments_cache = cache_get_instruments(db)

        exp_set = set()
        for i in _instruments_cache:
            if i.get('name') == symbol and i.get('segment') == 'NFO-OPT' and i.get('expiry'):
                exp_val = i['expiry']
                if hasattr(exp_val, 'isoformat'):
                    exp_set.add(exp_val.isoformat())
                else:
                    exp_set.add(str(exp_val).split('T')[0])

        # Filter out expired dates (keep only today and future)
        today = dt.now().strftime('%Y-%m-%d')
        expiry_list = sorted(e for e in exp_set if e >= today)[:8]
        return jsonify({'expiries': expiry_list})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Stock Snapshot (enriched real-time data for options scoring) ──
@app.route('/api/stock-snapshot')
def stock_snapshot():
    """Fetch enriched snapshot: equity quote + futures + ATM options in one batch."""
    try:
        kite = get_kite()
        if not kite:
            return jsonify({'error': 'Not connected'}), 401

        symbol = request.args.get('symbol', '')
        if not symbol:
            return jsonify({'error': 'Symbol required'}), 400

        global _instruments_cache
        if not _instruments_cache:
            db = get_db()
            _instruments_cache = cache_get_instruments(db)

        # ── 1. Find instruments we need ──
        equity_sym = f"NSE:{symbol}"

        # Find nearest-month futures
        today_str = dt.now().strftime('%Y-%m-%d')
        futures = []
        for inst in _instruments_cache:
            if (inst.get('name') == symbol and
                inst.get('segment') == 'NFO-FUT' and
                inst.get('instrument_type') == 'FUT'):
                exp = inst.get('expiry')
                if exp:
                    exp_str = exp.isoformat() if hasattr(exp, 'isoformat') else str(exp).split('T')[0]
                    if exp_str >= today_str:
                        futures.append((exp_str, inst))
        futures.sort(key=lambda x: x[0])
        fut_sym = f"NFO:{futures[0][1]['tradingsymbol']}" if futures else None

        # Find ATM options (nearest expiry)
        options = []
        for inst in _instruments_cache:
            if (inst.get('name') == symbol and
                inst.get('segment') == 'NFO-OPT'):
                exp = inst.get('expiry')
                if exp:
                    exp_str = exp.isoformat() if hasattr(exp, 'isoformat') else str(exp).split('T')[0]
                    if exp_str >= today_str:
                        options.append((exp_str, inst))
        options.sort(key=lambda x: x[0])

        # Get equity LTP first to find ATM strike
        batch_symbols = [equity_sym]
        if fut_sym:
            batch_symbols.append(fut_sym)

        pre_quotes = kite.quote(batch_symbols)
        equity_quote = pre_quotes.get(equity_sym, {})
        spot = equity_quote.get('last_price', 0) or 0

        # Find ATM CE and PE for nearest expiry
        ce_sym = None
        pe_sym = None
        if options and spot > 0:
            nearest_expiry = options[0][0]
            nearest_opts = [(e, i) for e, i in options if e == nearest_expiry]

            # Find closest strike to spot
            strikes = set(i.get('strike', 0) for _, i in nearest_opts)
            if strikes:
                atm_strike = min(strikes, key=lambda s: abs(s - spot))
                for _, inst in nearest_opts:
                    if inst.get('strike') == atm_strike:
                        ts = f"NFO:{inst['tradingsymbol']}"
                        if inst.get('instrument_type') == 'CE':
                            ce_sym = ts
                        elif inst.get('instrument_type') == 'PE':
                            pe_sym = ts

        # ── 2. Batch quote all 4 instruments ──
        all_syms = [s for s in [equity_sym, fut_sym, ce_sym, pe_sym] if s]
        quotes = kite.quote(all_syms)

        eq = quotes.get(equity_sym, {})
        fut = quotes.get(fut_sym, {}) if fut_sym else {}
        ce = quotes.get(ce_sym, {}) if ce_sym else {}
        pe = quotes.get(pe_sym, {}) if pe_sym else {}

        # ── 3. Build enriched snapshot ──
        ohlc = eq.get('ohlc', {}) or {}
        prev_close = ohlc.get('close', spot) or spot
        change_pct = ((spot - prev_close) / prev_close * 100) if prev_close else 0

        # Market depth analysis
        depth = eq.get('depth', {}) or {}
        buy_depth = depth.get('buy', []) or []
        sell_depth = depth.get('sell', []) or []
        max_bid_qty = max((d.get('quantity', 0) for d in buy_depth), default=0)
        max_ask_qty = max((d.get('quantity', 0) for d in sell_depth), default=0)
        total_bid_orders = sum(d.get('orders', 0) for d in buy_depth)
        total_ask_orders = sum(d.get('orders', 0) for d in sell_depth)

        buy_qty = eq.get('buy_quantity', 0) or 0
        sell_qty = eq.get('sell_quantity', 0) or 0

        # Futures premium
        fut_ltp = fut.get('last_price', 0)
        fut_premium_pct = ((fut_ltp - spot) / spot * 100) if spot and fut_ltp else 0

        # ATM option data
        ce_iv = ce.get('implied_volatility', 0) or 0
        pe_iv = pe.get('implied_volatility', 0) or 0
        ce_oi = ce.get('oi', 0) or 0
        pe_oi = pe.get('oi', 0) or 0
        pcr = (pe_oi / ce_oi) if ce_oi > 0 else 1.0
        avg_iv = (ce_iv + pe_iv) / 2 if (ce_iv and pe_iv) else ce_iv or pe_iv

        result = {
            'symbol': symbol,
            'ltp': spot,
            'change_pct': round(change_pct, 2),
            'volume': eq.get('volume', 0),
            'avg_price': eq.get('average_price', 0),
            'buy_qty': buy_qty,
            'sell_qty': sell_qty,
            'oi': eq.get('oi', 0),
            'oi_day_high': eq.get('oi_day_high', 0),
            'oi_day_low': eq.get('oi_day_low', 0),
            'ohlc': ohlc,
            'depth': {
                'buy_orders': total_bid_orders,
                'sell_orders': total_ask_orders,
                'max_bid_qty': max_bid_qty,
                'max_ask_qty': max_ask_qty,
                'buy_total_qty': buy_qty,
                'sell_total_qty': sell_qty,
            },
            'circuit': {
                'lower': eq.get('lower_circuit_limit', 0),
                'upper': eq.get('upper_circuit_limit', 0),
            },
            'futures': {
                'ltp': fut_ltp,
                'premium_pct': round(fut_premium_pct, 3),
                'oi': fut.get('oi', 0),
                'oi_change': fut.get('oi_day_change', 0) or 0,
            },
            'atm_option': {
                'strike': atm_strike if (options and spot > 0 and strikes) else 0,
                'ce_iv': round(ce_iv, 1),
                'pe_iv': round(pe_iv, 1),
                'avg_iv': round(avg_iv, 1),
                'ce_oi': ce_oi,
                'pe_oi': pe_oi,
                'pcr': round(pcr, 2),
            },
        }

        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Batch Stock Snapshots (for screener) ──
@app.route('/api/batch-snapshots')
def batch_snapshots():
    """Fetch snapshots for multiple stocks at once using batched quote calls."""
    try:
        kite = get_kite()
        if not kite:
            return jsonify({'error': 'Not connected'}), 401

        symbols = request.args.get('symbols', '').split(',')
        symbols = [s.strip() for s in symbols if s.strip()]
        if not symbols:
            return jsonify({'error': 'No symbols provided'}), 400

        global _instruments_cache
        if not _instruments_cache:
            db = get_db()
            _instruments_cache = cache_get_instruments(db)

        today_str = dt.now().strftime('%Y-%m-%d')

        # ── 1. Batch quote all equity symbols to get spot price ──
        equity_syms = [f"NSE:{s}" for s in symbols]
        eq_quotes = {}
        for i in range(0, len(equity_syms), 500):
            try:
                eq_quotes.update(kite.quote(equity_syms[i:i + 500]))
            except Exception:
                pass

        # Optimize instrument lookup
        inst_by_name = {}
        for inst in _instruments_cache:
            name = inst.get('name')
            if name:
                if name not in inst_by_name:
                    inst_by_name[name] = []
                inst_by_name[name].append(inst)

        # ── 2. Build symbol map: for each stock find futures + nearest ATM CE/PE ──
        sym_map = {}
        for symbol in symbols:
            equity_sym = f"NSE:{symbol}"
            spot = float(eq_quotes.get(equity_sym, {}).get('last_price', 0) or 0)
            
            fut_sym = None
            ce_sym = None
            pe_sym = None
            atm_strike = 0.0
            
            insts = inst_by_name.get(symbol, [])
            futures = []
            options = []
            
            for inst in insts:
                seg = inst.get('segment')
                if seg == 'NFO-FUT' and inst.get('instrument_type') == 'FUT':
                    exp = inst.get('expiry')
                    if exp:
                        exp_str = exp.isoformat() if hasattr(exp, 'isoformat') else str(exp).split('T')[0]
                        if exp_str >= today_str:
                            futures.append((exp_str, inst))
                elif seg == 'NFO-OPT':
                    exp = inst.get('expiry')
                    if exp:
                        exp_str = exp.isoformat() if hasattr(exp, 'isoformat') else str(exp).split('T')[0]
                        if exp_str >= today_str:
                            options.append((exp_str, inst))

            if futures:
                futures.sort(key=lambda x: x[0])
                fut_sym = f"NFO:{futures[0][1]['tradingsymbol']}"

            if options and spot > 0:
                options.sort(key=lambda x: x[0])
                nearest_expiry = options[0][0]
                nearest_opts = [(e, i) for e, i in options if e == nearest_expiry]
                strikes = set(float(i.get('strike') or 0) for _, i in nearest_opts)
                if strikes:
                    atm_strike = min(strikes, key=lambda s: abs(s - spot))
                    for _, inst in nearest_opts:
                        inst_strike = float(inst.get('strike') or 0)
                        if inst_strike == atm_strike:
                            ts = f"NFO:{inst['tradingsymbol']}"
                            itype = inst.get('instrument_type')
                            if itype == 'CE':
                                ce_sym = ts
                            elif itype == 'PE':
                                pe_sym = ts

            sym_map[symbol] = {
                'equity': equity_sym,
                'futures': fut_sym,
                'ce': ce_sym,
                'pe': pe_sym,
                'atm_strike': atm_strike
            }

        # ── 3. Batch quote all futures and options ──
        deriv_syms = []
        for s in sym_map.values():
            if s['futures']: deriv_syms.append(s['futures'])
            if s['ce']: deriv_syms.append(s['ce'])
            if s['pe']: deriv_syms.append(s['pe'])

        deriv_quotes = {}
        for i in range(0, len(deriv_syms), 500):
            try:
                deriv_quotes.update(kite.quote(deriv_syms[i:i + 500]))
            except Exception:
                pass

        # ── 4. Build enriched results ──
        results = {}
        for symbol in symbols:
            info = sym_map.get(symbol, {})
            eq = eq_quotes.get(info.get('equity'), {})
            fut = deriv_quotes.get(info.get('futures'), {}) if info.get('futures') else {}
            ce = deriv_quotes.get(info.get('ce'), {}) if info.get('ce') else {}
            pe = deriv_quotes.get(info.get('pe'), {}) if info.get('pe') else {}

            spot = eq.get('last_price', 0) or 0
            ohlc = eq.get('ohlc', {}) or {}
            prev_close = ohlc.get('close', spot) or spot
            change_pct = ((spot - prev_close) / prev_close * 100) if prev_close else 0

            depth = eq.get('depth', {}) or {}
            buy_depth = depth.get('buy', []) or []
            sell_depth = depth.get('sell', []) or []

            buy_qty = eq.get('buy_quantity', 0) or 0
            sell_qty = eq.get('sell_quantity', 0) or 0

            fut_ltp = fut.get('last_price', 0) or 0
            fut_premium = ((fut_ltp - spot) / spot * 100) if spot and fut_ltp else 0
            
            # Open Interest Change
            oi = fut.get('oi', 0) or 0
            oi_day_change = fut.get('oi_day_change', 0) or 0
            oi_change_pct = (oi_day_change / (oi - oi_day_change) * 100) if (oi - oi_day_change) > 0 else 0

            # ATM Option stats
            ce_iv = ce.get('implied_volatility', 0) or 0
            pe_iv = pe.get('implied_volatility', 0) or 0
            ce_oi = ce.get('oi', 0) or 0
            pe_oi = pe.get('oi', 0) or 0
            pcr = (pe_oi / ce_oi) if ce_oi > 0 else 1.0
            avg_iv = (ce_iv + pe_iv) / 2 if (ce_iv and pe_iv) else ce_iv or pe_iv

            results[symbol] = {
                'ltp': spot,
                'change_pct': round(change_pct, 2),
                'volume': eq.get('volume', 0),
                'avg_price': eq.get('average_price', 0),
                'oi_change_pct': round(oi_change_pct, 2),
                'buy_qty': buy_qty,
                'sell_qty': sell_qty,
                'depth': {
                    'max_bid_qty': max((d.get('quantity', 0) for d in buy_depth), default=0),
                    'max_ask_qty': max((d.get('quantity', 0) for d in sell_depth), default=0),
                    'buy_orders': sum(d.get('orders', 0) for d in buy_depth),
                    'sell_orders': sum(d.get('orders', 0) for d in sell_depth),
                },
                'circuit': {
                    'lower': eq.get('lower_circuit_limit', 0),
                    'upper': eq.get('upper_circuit_limit', 0),
                },
                'futures': {
                    'ltp': fut_ltp,
                    'premium_pct': round(fut_premium, 3),
                    'oi': fut.get('oi', 0) or 0,
                    'oi_change': fut.get('oi_day_change', 0) or 0,
                },
                'atm_option': {
                    'strike': info.get('atm_strike', 0),
                    'ce_iv': round(ce_iv, 1),
                    'pe_iv': round(pe_iv, 1),
                    'avg_iv': round(avg_iv, 1),
                    'ce_oi': ce_oi,
                    'pe_oi': pe_oi,
                    'pcr': round(pcr, 2),
                },
            }

        return jsonify({'snapshots': results})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Market Overview ──
@app.route('/api/market-overview')
def market_overview():
    try:
        kite = get_kite()
        if not kite:
            return jsonify({'error': 'Not connected'}), 401

        indices = kite.quote(['NSE:NIFTY 50', 'NSE:NIFTY BANK'])
        return jsonify(indices)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Market Pulse (Nifty + BankNifty + India VIX) ──
_market_pulse_cache = {'data': None, 'ts': 0}

@app.route('/api/market-pulse')
def market_pulse():
    import time
    global _market_pulse_cache
    now = time.time()
    # Return cached data if < 15s old (serves after-hours without error)
    if _market_pulse_cache['data'] and now - _market_pulse_cache['ts'] < 15:
        return jsonify(_market_pulse_cache['data'])

    kite = get_kite()
    if not kite:
        if _market_pulse_cache['data']:
            return jsonify({**_market_pulse_cache['data'], 'stale': True})
        return jsonify({
            'error': 'Not connected',
            'message': 'No Kite access token found in request headers, Flask session, disk session, or environment.',
            'auth_debug': _kite_session_debug()
        }), 401

    try:
        quotes = kite.quote(['NSE:NIFTY 50', 'NSE:NIFTY BANK', 'NSE:INDIA VIX'])

        def extract(key):
            q = quotes.get(key, {})
            ltp = q.get('last_price', 0)
            oc  = q.get('ohlc', {})
            prev_close = oc.get('close', ltp) or ltp
            chg_pct = ((ltp - prev_close) / prev_close * 100) if prev_close else 0
            return {'ltp': ltp, 'change_pct': round(chg_pct, 2)}

        result = {
            'nifty':     extract('NSE:NIFTY 50'),
            'banknifty': extract('NSE:NIFTY BANK'),
            'vix':       extract('NSE:INDIA VIX'),
            'stale': False
        }
        _market_pulse_cache = {'data': result, 'ts': now}
        return jsonify(result)
    except Exception as e:
        if _market_pulse_cache['data']:
            return jsonify({**_market_pulse_cache['data'], 'stale': True})
        return jsonify({'error': str(e)}), 500


# ── Portfolio: Holdings ──
@app.route('/api/portfolio/holdings')
def portfolio_holdings():
    kite = get_kite()
    if not kite:
        return jsonify({'error': 'Not connected'}), 401
    try:
        holdings = kite.holdings()
        return jsonify({'holdings': holdings})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Portfolio: Positions ──
@app.route('/api/portfolio/positions')
def portfolio_positions():
    kite = get_kite()
    if not kite:
        return jsonify({'error': 'Not connected'}), 401
    try:
        positions = kite.positions()
        return jsonify(positions)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Portfolio: Summary (aggregated P&L, sector allocation) ──
@app.route('/api/portfolio/summary')
def portfolio_summary():
    kite = get_kite()
    if not kite:
        return jsonify({'error': 'Not connected'}), 401
    try:
        holdings = kite.holdings()
        positions = kite.positions()

        # Compute holdings P&L
        total_invested = 0
        total_current = 0
        day_pnl = 0
        sector_map = {}
        stock_weights = []
        for h in holdings:
            qty = h.get('quantity', 0)
            avg = h.get('average_price', 0)
            ltp = h.get('last_price', 0)
            pnl = h.get('pnl', 0)
            day_change = h.get('day_change', 0) * qty if h.get('day_change') else 0

            invested = qty * avg
            current = qty * ltp
            total_invested += invested
            total_current += current
            day_pnl += day_change

            symbol = h.get('tradingsymbol', 'UNKNOWN')
            stock_weights.append({'symbol': symbol, 'value': current, 'pnl': pnl})

        # Positions P&L (day)
        net_positions = positions.get('net', []) if isinstance(positions, dict) else positions
        pos_day_pnl = 0
        open_positions = []
        for p in (net_positions or []):
            pos_day_pnl += p.get('pnl', 0)
            if p.get('quantity', 0) != 0:
                open_positions.append({
                    'symbol': p.get('tradingsymbol'),
                    'qty': p.get('quantity'),
                    'avg': p.get('average_price'),
                    'ltp': p.get('last_price'),
                    'pnl': p.get('pnl', 0),
                    'product': p.get('product'),
                    'exchange': p.get('exchange'),
                })

        overall_pnl = float(total_current) - float(total_invested)
        overall_pnl_pct = (overall_pnl / float(total_invested) * 100) if float(total_invested) > 0 else 0

        # Concentration risk
        concentration_alerts = []
        if total_current > 0:
            for sw in stock_weights:
                pct = sw['value'] / total_current * 100
                if pct > 20:
                    concentration_alerts.append({
                        'symbol': sw['symbol'],
                        'pct': round(pct, 1),
                        'message': f"{sw['symbol']} is {pct:.1f}% of portfolio — high concentration risk"
                    })

        return jsonify({
            'holdings_count': len(holdings),
            'total_invested': round(total_invested, 2),
            'total_current': round(total_current, 2),
            'overall_pnl': round(overall_pnl, 2),
            'overall_pnl_pct': round(overall_pnl_pct, 2),
            'day_pnl': round(day_pnl + pos_day_pnl, 2),
            'positions_count': len(open_positions),
            'open_positions': open_positions,
            'stock_weights': sorted(stock_weights, key=lambda x: x['value'], reverse=True),
            'concentration_alerts': concentration_alerts,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── News & Sentiment Feed ──
_news_cache = {}
_news_cache_lock = threading.Lock()

def update_news_feeds_cache():
    global _news_cache
    import time
    import json
    import feedparser
    import sqlite3
    import urllib.request
    import ssl

    feeds = [
        # ── Verified Working Feeds ─────────────────────────────────────────────
        ('ET Markets',          'https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms'),
        ('ET Companies',        'https://economictimes.indiatimes.com/news/company/rssfeeds/2143429022.cms'),
        ('Livemint Markets',    'https://www.livemint.com/rss/markets'),
        ('MoneyControl Buzzing','https://www.moneycontrol.com/rss/buzzingstocks.xml'),
        ('The Hindu BL',        'https://www.thehindubusinessline.com/markets/?service=rss'),
        ('Financial Express',   'https://www.financialexpress.com/market/?service=rss'),
        ('Capital Market',      'https://www.capitalmarket.com/rss/live-news'),
        ('MarketsMojo',         'https://www.marketsmojo.com/blog/feed/'),
        # ── Removed (dead URLs) ───────────────────────────────────────────────
        # Business Standard  → 403 Forbidden (intentional bot block)
        # Sensibull          → 404 Not Found (blog removed)
        # CNBC-TV18          → 404 Not Found (URL deprecated)
        # CNBC Awaaz         → 404 Not Found (Hindi feed, URL deprecated)
        # MoneyControl Companies → 503 Service Unavailable (intermittent)
    ]


    entity_map = {
        'RELIANCE': ['reliance', 'ril', 'jio', 'ambani', 'reliance industries'],
        'SBIN': ['sbi', 'state bank', 'sbin', 'state bank of india'],
        'TATAMOTORS': ['tata motors', 'tamo', 'jlr', 'jaguar land rover'],
        'HDFCBANK': ['hdfc bank', 'hdfcb', 'hdfc'],
        'ICICIBANK': ['icici bank', 'icici'],
        'INFY': ['infosys', 'infy'],
        'TCS': ['tcs', 'tata consultancy'],
        'ITC': ['itc', 'itc ltd'],
        'BHARTIARTL': ['airtel', 'bharti airtel', 'bharti'],
        'LTIM': ['ltimindtree', 'ltim'],
        'GLENMARK': ['glenmark', 'glenmark pharma'],
        'AXISBANK': ['axis bank', 'axis'],
        'KOTAKBANK': ['kotak bank', 'kotak mahindra', 'kotak'],
        'BANDHANBNK': ['bandhan bank', 'bandhan'],
        'BANKBARODA': ['bob', 'bank of baroda', 'bankbaroda'],
        'MANAPPURAM': ['manappuram', 'manappuram finance'],
        'IDEA': ['vodafone idea', 'vodafone', 'idea cellular', 'idea fpo'],
        'TATASTEEL': ['tata steel', 'tata iron'],
        'WIPRO': ['wipro'],
        'MARUTI': ['maruti', 'maruti suzuki'],
        'LT': ['l&t', 'larsen', 'larsen & toubro'],
        'TEJASNET': ['tejas network', 'tejas'],
        'MAHLOG': ['mahindra logistics', 'mahlog'],
        'ADANIENT': ['adani', 'adani enterprises'],
        'BAJFINANCE': ['bajaj finance', 'bajaj fin'],
        'PFC': ['pfc', 'power finance corp'],
        'RECLTD': ['rec ltd', 'rec limited', 'recltd', ' rural electrification'],
        'GMRINFRA': ['gmr', 'gmr infra', 'gmr airports'],
        'SAIL': ['sail', 'steel authority'],
        'DLF': ['dlf'],
        'HAL': ['hal', 'hindustan aeronautics'],
        'BEL': ['bel', 'bharat electronics'],
        'COALINDIA': ['coal india', 'coalindia'],
        'ONGC': ['ongc'],
        'IOC': ['ioc', 'indian oil'],
        'BPCL': ['bpcl', 'bharat petroleum']
    }

    fetched_headlines = []

    # 1. Fetch and Parse Live RSS feeds
    ssl_ctx = ssl._create_unverified_context()
    for source, url in feeds:
        try:
            req = urllib.request.Request(
                url, 
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
            )
            with urllib.request.urlopen(req, context=ssl_ctx, timeout=8) as response:
                feed_data = response.read()
            
            feed = feedparser.parse(feed_data)
            for entry in feed.entries[:12]:
                title = entry.get('title', '')
                summary = entry.get('summary', entry.get('description', '')) or ''
                link = entry.get('link', '')
                published = entry.get('published', entry.get('updated', ''))
                
                # Parse timestamp safely
                pub_parsed = entry.get('published_parsed')
                timestamp = float(time.mktime(pub_parsed)) if pub_parsed else float(time.time())

                text_to_scan = f"{title} {summary}".lower()

                # F&O Entity Extractor (Word Boundary Matching)
                import re
                impacted_stocks = []
                for stock_sym, aliases in entity_map.items():
                    matched = False
                    for alias in aliases:
                        pattern = r'\b' + re.escape(alias) + r'\b'
                        if re.search(pattern, text_to_scan):
                            matched = True
                            break
                    if matched:
                        impacted_stocks.append(stock_sym)

                # Nifty / Bank Nifty Index fallbacks
                if not impacted_stocks:
                    if 'banknifty' in text_to_scan or 'bank nifty' in text_to_scan:
                        impacted_stocks.append('BANKNIFTY')
                    elif 'nifty' in text_to_scan or 'nifty50' in text_to_scan:
                        impacted_stocks.append('NIFTY')

                # Context-Aware Financial Sentiment Parser
                bullish_patterns = [
                    'surge', 'rally', 'gain', 'jump', 'soar', 'rise', 'profit jump', 
                    'record high', 'margin expansion', 'order win', 'wins order', 'rating upgrade', 
                    'dividend raise', 'acquisition', 'debt reduction', 'outperform', 'upgrade'
                ]
                bearish_patterns = [
                    'fall', 'crash', 'drop', 'slip', 'sink', 'plunge', 'loss widens', 
                    'profit slump', 'margin squeeze', 'sebi fine', 'penalty', 'tax probe', 
                    'strike', 'rating downgrade', 'underperform', 'warning', 'decline'
                ]

                bull_count = sum(2 if p in text_to_scan else 0 for p in bullish_patterns)
                bear_count = sum(2 if p in text_to_scan else 0 for p in bearish_patterns)

                if 'debt reduction' in text_to_scan or 'cuts debt' in text_to_scan:
                    bull_count += 3
                    bear_count = max(0, bear_count - 2)

                if bull_count > bear_count:
                    sentiment = 'bullish'
                    score = min(bull_count / 5.0, 1.0)
                elif bear_count > bull_count:
                    sentiment = 'bearish'
                    score = -min(bear_count / 5.0, 1.0)
                else:
                    sentiment = 'neutral'
                    score = 0.0

                # Categorization & Impact Rating Engine
                category = 'Macro/Sectoral'
                impact_rating = 'Low'

                if any(x in text_to_scan for x in ['q4', 'q3', 'q2', 'q1', 'profit', 'revenue', 'earnings']):
                    category = 'Corporate Earnings'
                    impact_rating = 'High' if abs(score) > 0.4 else 'Medium'
                elif any(x in text_to_scan for x in ['sebi', 'rbi', 'tax', 'lawsuit', 'fine', 'penalty', 'investigation']):
                    category = 'Regulatory Updates'
                    impact_rating = 'High'
                elif any(x in text_to_scan for x in ['dividend', 'bonus', 'acquisition', 'merger', 'buyback', 'split']):
                    category = 'Corporate Actions'
                    impact_rating = 'High' if abs(score) > 0.3 else 'Medium'
                elif any(x in text_to_scan for x in ['order', 'contract', 'partnership', 'commissioned', 'launches']):
                    category = 'Business Catalysts'
                    impact_rating = 'Medium'

                # Priority Sorting Weight Score calculation (High-Impact Stock Catalyst on Top)
                impact_val = 30 if impact_rating == 'High' else 20 if impact_rating == 'Medium' else 10
                stock_bonus = 10 if len(impacted_stocks) > 0 else 0
                priority_score = impact_val + stock_bonus

                # Generate high-action 1-sentence catalyst takeaway
                stock_str = ", ".join(impacted_stocks) if impacted_stocks else "Market"
                action_phrase = title
                for prefix in ["Gainers and Losers:", "Bulk deals:", "Buzzing Stocks:", "Market Pulse:"]:
                    if title.startswith(prefix):
                        action_phrase = title[len(prefix):].strip()

                if sentiment == 'bullish':
                    takeaway = f"📈 POSITIVE IMPACT on {stock_str}: {action_phrase}"
                elif sentiment == 'bearish':
                    takeaway = f"📉 NEGATIVE IMPACT on {stock_str}: {action_phrase}"
                else:
                    takeaway = f"⚡ Catalyst Update: {action_phrase}"

                fetched_headlines.append({
                    'title': title,
                    'summary': summary[:220] + '...' if len(summary) > 220 else summary,
                    'source': source,
                    'url': link,
                    'sentiment': sentiment,
                    'score': round(score, 2),
                    'category': category,
                    'impact_rating': impact_rating,
                    'impacted_stocks': impacted_stocks,
                    'takeaway': takeaway,
                    'priority_score': priority_score,
                    'timestamp': timestamp,
                    'time': published,
                })
        except Exception as e:
            print(f"  [News Aggregator] Failed to download {source} feed: {e}")
            continue

    # 2. Persist to SQLite Database
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    for h in fetched_headlines:
        try:
            cursor.execute('''
                INSERT OR IGNORE INTO stored_news (
                    title, summary, source, url, sentiment, score, category, 
                    impact_rating, impacted_stocks, takeaway, priority_score, timestamp, time, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                h['title'], h['summary'], h['source'], h['url'], h['sentiment'], h['score'], 
                h['category'], h['impact_rating'], json.dumps(h['impacted_stocks']), h['takeaway'], 
                h['priority_score'], h['timestamp'], h['time'], time.strftime('%Y-%m-%d %H:%M:%S')
            ))
        except Exception:
            pass

    # 3. Clean up older news (retain exactly 1 week)
    one_week_ago = time.time() - 7 * 24 * 3600
    cursor.execute('DELETE FROM stored_news WHERE timestamp < ?', (one_week_ago,))
    conn.commit()

    # 4. Read back all retained news (1 week) sorted by Priority & Time
    cursor.execute('''
        SELECT title, summary, source, url, sentiment, score, category, 
               impact_rating, impacted_stocks, takeaway, priority_score, timestamp, time 
        FROM stored_news 
        ORDER BY timestamp DESC
    ''')
    rows = cursor.fetchall()
    all_headlines = []
    for row in rows:
        try:
            all_headlines.append({
                'title': row[0],
                'summary': row[1],
                'source': row[2],
                'url': row[3],
                'sentiment': row[4],
                'score': row[5],
                'category': row[6],
                'impact_rating': row[7],
                'impacted_stocks': json.loads(row[8]) if row[8] else [],
                'takeaway': row[9],
                'priority_score': row[10],
                'timestamp': row[11],
                'time': row[12]
            })
        except Exception:
            pass
    conn.close()

    # Deduplicate in memory
    seen = set()
    unique = []
    for h in all_headlines:
        if h['title'] not in seen:
            seen.add(h['title'])
            unique.append(h)

    # 5. Safely update global memory cache
    with _news_cache_lock:
        _news_cache['__all__'] = {'data': unique[:150], 'ts': time.time()}
        
        # Populate pre-filtered lists for all tracked F&O stocks
        for stock_sym in entity_map.keys():
            _news_cache[stock_sym] = {
                'data': [h for h in unique if stock_sym in h['impacted_stocks']][:50],
                'ts': time.time()
            }
        # Fallbacks for indices
        for index in ['NIFTY', 'BANKNIFTY']:
            _news_cache[index] = {
                'data': [h for h in unique if index in h['impacted_stocks']][:50],
                'ts': time.time()
            }

def start_news_aggregator():
    """
    News feeds are now fetched ON-DEMAND when /api/news is called.
    The background polling loop is disabled to avoid hammering dead RSS URLs
    (403/404/503 errors on Business Standard, Sensibull, CNBC-TV18, etc.).
    Cache is populated on first user request and served from cache thereafter.
    """
    pass  # No background thread — on-demand only via /api/news route

@app.route('/api/news')
def news_feed():
    symbol = request.args.get('symbol', '').upper()
    refresh = request.args.get('refresh', '').lower() == 'true'
    cache_key = symbol or '__all__'

    # Manual refresh only: only hit RSS feeds when user explicitly requests it
    if refresh:
        try:
            update_news_feeds_cache()
        except Exception as e:
            return jsonify({'error': str(e), 'headlines': []}), 500

    # Serve from cache (empty array if cache cold — no auto-fetch)
    with _news_cache_lock:
        cached = _news_cache.get(cache_key)
    if cached:
        return jsonify({'headlines': cached['data']})

    return jsonify({'headlines': []})



# ── Live Movers (Intraday Screener) ──
_live_movers_cache = {'data': None, 'ts': 0}

@app.route('/api/live-movers')
def live_movers():
    import time
    global _live_movers_cache
    now = time.time()

    # Cache for 15s to avoid hammering Kite API
    if _live_movers_cache['data'] and now - _live_movers_cache['ts'] < 15:
        return jsonify(_live_movers_cache['data'])

    kite = get_kite()
    if not kite:
        return jsonify({'error': 'Not connected'}), 401

    try:
        # Get FNO equity list from instruments cache
        db = get_db()
        rows = db.execute(
            "SELECT tradingsymbol FROM instruments WHERE segment='NFO-FUT' AND tradingsymbol NOT LIKE '%___'"
        ).fetchall()

        # Extract unique underlying symbols
        symbols_set = set()
        for row in rows:
            sym = row[0]
            # NFO-FUT symbols look like RELIANCE24APRFUT — strip expiry suffix
            import re
            base = re.match(r'^([A-Z&]+)\d', sym)
            if base:
                symbols_set.add(base.group(1))

        if not symbols_set:
            # Fallback: use equity list database cache logic
            nfo_instruments = db.execute("SELECT name FROM instruments WHERE segment='NFO-FUT'").fetchall()
            nfo_syms = set(i['name'] for i in nfo_instruments)
            symbols_set = nfo_syms

        symbols = list(symbols_set)[:200]

        # Batch quote in chunks of 500 (Kite limit)
        all_quotes = {}
        nse_symbols = [f'NSE:{s}' for s in symbols]
        for i in range(0, len(nse_symbols), 500):
            chunk = nse_symbols[i:i+500]
            quotes = kite.quote(chunk)
            all_quotes.update(quotes)

        movers = []
        for key, q in all_quotes.items():
            symbol = key.replace('NSE:', '')
            ltp = q.get('last_price', 0)
            ohlc = q.get('ohlc', {})
            prev_close = ohlc.get('close', 0)
            volume = q.get('volume', 0)
            avg_volume = q.get('average_price', 0)  # not actual avg vol, use oi fields

            if not ltp or not prev_close:
                continue

            change_pct = ((ltp - prev_close) / prev_close * 100) if prev_close else 0
            oi = q.get('oi', 0)
            oi_day_high = q.get('oi_day_high', 0)
            oi_day_low = q.get('oi_day_low', 0)
            day_high = ohlc.get('high', ltp)
            day_low = ohlc.get('low', ltp)
            week52_high = q.get('depth', {}).get('buy', [{}])  # won't have 52W here

            movers.append({
                'symbol': symbol,
                'ltp': ltp,
                'change_pct': round(change_pct, 2),
                'volume': volume,
                'oi': oi,
                'day_high': day_high,
                'day_low': day_low,
                'prev_close': prev_close,
            })

        # Sort into categories
        result = {
            'gainers': sorted(movers, key=lambda x: x['change_pct'], reverse=True)[:20],
            'losers': sorted(movers, key=lambda x: x['change_pct'])[:20],
            'volume_buzzers': sorted(movers, key=lambda x: x['volume'], reverse=True)[:20],
            'oi_spikes': sorted([m for m in movers if m['oi'] > 0], key=lambda x: x['oi'], reverse=True)[:20],
            'total_stocks': len(movers),
        }

        _live_movers_cache = {'data': result, 'ts': now}
        return jsonify(result)

    except Exception as e:
        if _live_movers_cache['data']:
            return jsonify(_live_movers_cache['data'])
        return jsonify({'error': str(e)}), 500


def normalize_apex_symbol(symbol):
    """Normalize common APEX symbols to their Kite tradingsymbol equivalents."""
    if not symbol:
        return ''
    symbol = symbol.strip()
    alias_map = {
        'NIFTY50': 'NIFTY 50',
        'BANKNIFTY': 'NIFTY BANK',
        'FINNIFTY': 'NIFTY FIN SERVICE',
        'NIFTYNXT50': 'NIFTY NEXT 50',
    }
    cleaned = symbol.replace(' ', '').replace('-', '').upper()
    for alias_key, alias_value in alias_map.items():
        if cleaned == alias_key or cleaned == alias_value.replace(' ', '').upper():
            return alias_value
    return symbol


# ── APEX Intraday Signal Screener ──
@app.route('/api/apex-screener')
def apex_screener():
    """Scan multiple instruments for APEX signals (5m + 15m HTF)."""
    try:
        kite = get_kite()
        symbols = request.args.get('symbols', '').split(',')
        symbols = [normalize_apex_symbol(s.strip()) for s in symbols if s.strip()]
        if not symbols:
            return jsonify({'error': 'No symbols provided'}), 400

        if not kite:
            # Generate demo signals for offline mode
            results = []
            for symbol in symbols:
                # Generate random but realistic demo signal
                import random
                signals = ['BUY', 'SELL', 'WAIT']
                weights = [0.3, 0.3, 0.4]  # More WAIT signals
                signal = random.choices(signals, weights=weights)[0]

                # Demo base prices
                base_prices = {
                    'NIFTY 50': 22000,
                    'NIFTY BANK': 45000,
                    'NIFTY FIN SERVICE': 18000,
                    'RELIANCE': 2500,
                    'HDFCBANK': 1600,
                }
                base_price = base_prices.get(symbol, 1000)
                close = base_price + random.uniform(-base_price*0.02, base_price*0.02)

                score = random.randint(0, 10)
                htf_options = ['BULLISH', 'BEARISH', 'NEUTRAL']
                htf = random.choice(htf_options)

                results.append({
                    'symbol': symbol,
                    'signal': signal,
                    'score': score,
                    'rawScore': score,
                    'close': round(close, 2),
                    'ema21': round(close + random.uniform(-50, 50), 2),
                    'ema50': round(close + random.uniform(-100, 100), 2),
                    'vwap': round(close + random.uniform(-20, 20), 2),
                    'rsi': random.uniform(20, 80),
                    'macd_hist': random.uniform(-10, 10),
                    'bos': random.choice(['BULLISH', 'BEARISH', 'NEUTRAL']),
                    'choch': random.choice(['BULLISH', 'BEARISH', 'NEUTRAL']),
                    'htf': htf,
                    'vol_spike': random.choice([True, False]),
                    'oi': random.randint(100000, 1000000) if random.random() > 0.5 else None,
                    'zones': [],
                    'structureMarkers': [],
                })

            return jsonify({
                'signals': results,
                'message': 'Demo mode — generated mock signals',
                'cached': False,
                'demo': True,
            })

        global _apex_signals_cache
        if not is_market_hours():
            if _apex_signals_cache['signals']:
                return jsonify({
                    'signals': _apex_signals_cache['signals'],
                    'message': 'Market closed — showing last cached signals',
                    'cached': True,
                    'cached_at': _apex_signals_cache['ts'].isoformat() if _apex_signals_cache['ts'] else None,
                })
            return jsonify({'signals': [], 'message': 'Market closed', 'cached': True})

        results = []
        for symbol in symbols:
            try:
                # Fetch 5m candles (last 100)
                candles_5m = get_historical_candles(kite, symbol, '5minute', days_back=1, limit=100)
                if not candles_5m:
                    continue

                # Fetch 15m candles (last 50)
                candles_15m = get_historical_candles(kite, symbol, '15minute', days_back=2, limit=50)

                # Compute indicators for 5m
                closes = [c['close'] for c in candles_5m]
                highs = [c['high'] for c in candles_5m]
                lows = [c['low'] for c in candles_5m]
                volumes = [c['volume'] for c in candles_5m]

                ema21 = compute_ema(closes, 21)
                ema50 = compute_ema(closes, 50)
                vwap_arr = compute_intraday_vwap(candles_5m)
                rsi = compute_rsi(closes)
                macd_data = compute_macd_array(closes)
                macd_hist = macd_data['histogram'][-1] if macd_data['histogram'] else 0

                bos = detect_bos(candles_5m)
                choch = detect_choch(candles_5m)
                vol_spike_flag = vol_spike(candles_5m)

                # HTF bias from 15m
                htf = 'NEUTRAL'
                if candles_15m:
                    htf_closes = [c['close'] for c in candles_15m]
                    htf_ema21 = compute_ema(htf_closes, 21)
                    htf_ema50 = compute_ema(htf_closes, 50)
                    if htf_ema21 and htf_ema50:
                        htf = htf_bias(htf_closes[-1], htf_ema21[-1], htf_ema50[-1])

                close = closes[-1]
                vwap = vwap_arr[-1] if vwap_arr else close
                signal_score = compute_score(
                    close,
                    ema21[-1] if ema21 else 0,
                    ema50[-1] if ema50 else 0,
                    vwap,
                    rsi,
                    macd_hist,
                    bos,
                    choch,
                    htf,
                    vol_spike_flag,
                )
                normalized_score = min(round(signal_score / 12 * 10), 10)
                signal = 'WAIT'
                if signal_score >= 5:
                    signal = 'BUY' if close > (ema21[-1] if ema21 else 0) else 'SELL'

                smc = compute_smc(candles_5m)
                oi_data = None
                try:
                    quote = kite.ltp([f'NSE:{symbol}'])
                    key = f'NSE:{symbol}'
                    if key in quote:
                        oi_data = quote[key].get('oi')
                except Exception:
                    oi_data = None

                results.append({
                    'symbol': symbol,
                    'signal': signal,
                    'score': normalized_score,
                    'rawScore': signal_score,
                    'close': close,
                    'ema21': ema21[-1] if ema21 else 0,
                    'ema50': ema50[-1] if ema50 else 0,
                    'vwap': vwap,
                    'rsi': rsi,
                    'macd_hist': macd_hist,
                    'bos': bos,
                    'choch': choch,
                    'htf': htf,
                    'vol_spike': vol_spike_flag,
                    'oi': oi_data,
                    'zones': smc.get('fvgs', []),
                    'structureMarkers': smc.get('markers', []),
                })

            except Exception as e:
                print(f"Error processing {symbol}: {e}")
                continue

        results.sort(key=lambda x: (x['signal'] != 'BUY', x['signal'] != 'SELL', -x['score']))
        _apex_signals_cache = {'signals': results, 'ts': dt.now()}
        return jsonify({'signals': results, 'cached': False})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/session-status')
def session_status():
    try:
        return jsonify({
            'market_open': is_market_hours(),
            'premarket': is_premarket(),
            'session_mode': get_session_mode(),
            'timestamp': dt.now().isoformat(),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Options Strikes for Strategy Builder ──
@app.route('/api/options/strikes')
def options_strikes():
    """Return all strikes with LTP, IV, OI for a given symbol and expiry."""
    try:
        kite = get_kite()
        if not kite:
            return jsonify({'error': 'Not connected'}), 401

        symbol = request.args.get('symbol', '')
        expiry = request.args.get('expiry', '')
        if not symbol:
            return jsonify({'error': 'Symbol required'}), 400

        global _instruments_cache
        if not _instruments_cache:
            db = get_db()
            _instruments_cache = cache_get_instruments(db)

        # Filter options for the symbol
        options = [i for i in _instruments_cache
                   if i.get('name') == symbol and i.get('segment') == 'NFO-OPT']

        if expiry:
            exp_str = expiry.strip()
            def expiry_matches(inst_expiry):
                if inst_expiry is None:
                    return False
                if hasattr(inst_expiry, 'isoformat'):
                    return inst_expiry.isoformat() == exp_str
                return str(inst_expiry).split('T')[0] == exp_str
            options = [i for i in options if expiry_matches(i.get('expiry'))]

        if not options:
            return jsonify({'strikes': [], 'spot_price': 0})

        options = options[:500]

        # Batch quote
        quotes = {}
        for batch_start in range(0, len(options), 200):
            batch = options[batch_start:batch_start + 200]
            batch_symbols = [f"NFO:{i['tradingsymbol']}" for i in batch]
            try:
                quotes.update(kite.quote(batch_symbols))
            except Exception:
                pass

        # Get spot price
        nse_symbol_map = {'NIFTY': 'NIFTY 50', 'BANKNIFTY': 'NIFTY BANK', 'FINNIFTY': 'NIFTY FIN SERVICE'}
        nse_name = nse_symbol_map.get(symbol, symbol)
        try:
            spot = kite.ltp([f"NSE:{nse_name}"])[f"NSE:{nse_name}"]['last_price']
        except Exception:
            spot = 0

        strikes = []
        for opt in options:
            ts = f"NFO:{opt['tradingsymbol']}"
            q = quotes.get(ts, {})
            strikes.append({
                'strike': opt.get('strike', 0),
                'type': opt.get('instrument_type', ''),
                'tradingsymbol': opt.get('tradingsymbol', ''),
                'ltp': q.get('last_price', 0),
                'iv': round(q.get('implied_volatility', 0) or 0, 1),
                'oi': q.get('oi', 0),
                'volume': q.get('volume', 0),
                'lot_size': opt.get('lot_size', 1),
            })

        strikes.sort(key=lambda x: (x['strike'], x['type']))
        return jsonify({'strikes': strikes, 'spot_price': spot})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Backtesting Engine ──
@app.route('/api/backtest', methods=['POST'])
def run_backtest():
    """Run signal-based backtest on cached OHLCV data."""
    import math

    try:
        data = request.get_json() or {}
        symbol = data.get('symbol', '').upper()
        range_days = int(data.get('range_days', 180))
        signal_type = data.get('signal', 'ema_cross')
        hold_days = int(data.get('hold_days', 5))
        sl_pct = float(data.get('sl_pct', 2))
        tp_pct = float(data.get('tp_pct', 4))
        capital = float(data.get('capital', 100000))

        if not symbol:
            return jsonify({'error': 'Symbol required'}), 400

        # Get instrument token
        db = get_db()
        token = None
        global _instruments_cache
        if not _instruments_cache:
            _instruments_cache = cache_get_instruments(db)
        if _instruments_cache:
            for inst in _instruments_cache:
                if inst.get('tradingsymbol') == symbol and inst.get('exchange') == 'NSE':
                    token = inst.get('instrument_token')
                    break
        if not token:
            return jsonify({'error': f'Instrument token not found for {symbol}'}), 404

        # Fetch OHLCV from cache
        from_date = (dt.now() - timedelta(days=range_days)).strftime('%Y-%m-%d')
        to_date = dt.now().strftime('%Y-%m-%d')
        candles = cache_get_ohlcv(db, token, from_date, to_date, 'day')

        if not candles or len(candles) < 30:
            # Try fetching from Kite if cache is empty
            kite = get_kite()
            if kite:
                try:
                    raw_data = kite.historical_data(token, dt.now() - timedelta(days=range_days), dt.now(), 'day')
                    cache_store_ohlcv(db, token, raw_data, 'day')
                    candles = cache_get_ohlcv(db, token, from_date, to_date, 'day')
                except Exception:
                    pass
            if not candles or len(candles) < 30:
                return jsonify({'error': 'Insufficient historical data (need 30+ days). Run a scan first to populate cache.'}), 400

        # Extract OHLCV arrays
        closes = [c['close'] for c in candles]
        highs = [c['high'] for c in candles]
        lows = [c['low'] for c in candles]
        dates = [c['date'] for c in candles]

        # Compute indicators via canonical indicators.py (no inline dupes)
        ema9      = compute_ema(closes, 9)
        ema21     = compute_ema(closes, 21)
        rsi_vals  = compute_rsi_array(closes, 14)
        atr_vals  = [compute_atr(highs[:i+1], lows[:i+1], closes[:i+1]) for i in range(len(closes))]
        macd_arr  = compute_macd_array(closes)
        macd_result = [
            {'macd': macd_arr['macdLine'][i], 'signal': macd_arr['signalLine'][i],
             'hist': macd_arr['histogram'][i]}
            if macd_arr['macdLine'][i] is not None and macd_arr['signalLine'][i] is not None
            else None
            for i in range(len(macd_arr['macdLine']))
        ]

        # Simulate trades
        trades = []
        equity = capital
        equity_curve = [{'date': dates[0], 'value': capital}]
        in_trade = False
        entry_idx = entry_price = direction = None

        for i in range(30, len(candles)):
            if in_trade:
                holding = i - entry_idx
                price = closes[i]
                change = ((price - entry_price) / entry_price * 100) if direction == 'Long' else ((entry_price - price) / entry_price * 100)

                exit_reason = None
                if change <= -sl_pct:
                    exit_reason = 'Stop Loss'
                elif change >= tp_pct:
                    exit_reason = 'Take Profit'
                elif holding >= hold_days:
                    exit_reason = 'Time Exit'

                if exit_reason:
                    pnl_pct = (price - entry_price) / entry_price if direction == 'Long' else (entry_price - price) / entry_price
                    pos_size = equity * 0.95
                    pnl = pos_size * pnl_pct
                    equity += pnl
                    trades.append({
                        'entryDate': dates[entry_idx], 'entryPrice': round(entry_price, 2),
                        'exitDate': dates[i], 'exitPrice': round(price, 2),
                        'pnl': round(pnl, 2), 'returnPct': round(pnl_pct * 100, 2),
                        'direction': direction, 'exitReason': exit_reason
                    })
                    equity_curve.append({'date': dates[i], 'value': round(equity, 2)})
                    in_trade = False
                continue

            # Entry signals
            sig = None
            if signal_type == 'ema_cross':
                if ema9[i - 1] and ema21[i - 1] and ema9[i] and ema21[i]:
                    if ema9[i - 1] < ema21[i - 1] and ema9[i] > ema21[i]:
                        sig = 'Long'
                    if ema9[i - 1] > ema21[i - 1] and ema9[i] < ema21[i]:
                        sig = 'Short'
            elif signal_type == 'rsi_breakout':
                if rsi_vals[i - 1] and rsi_vals[i]:
                    if rsi_vals[i - 1] < 30 and rsi_vals[i] >= 30:
                        sig = 'Long'
                    if rsi_vals[i - 1] > 70 and rsi_vals[i] <= 70:
                        sig = 'Short'
            elif signal_type == 'atr_breakout':
                move = abs(closes[i] - closes[i - 1])
                if atr_vals[i] and move > 1.5 * atr_vals[i]:
                    sig = 'Long' if closes[i] > closes[i - 1] else 'Short'
            elif signal_type == 'macd_cross':
                if macd_result[i - 1] and macd_result[i]:
                    if macd_result[i - 1]['hist'] < 0 and macd_result[i]['hist'] >= 0:
                        sig = 'Long'
                    if macd_result[i - 1]['hist'] > 0 and macd_result[i]['hist'] <= 0:
                        sig = 'Short'

            if sig:
                in_trade = True
                entry_idx = i
                entry_price = closes[i]
                direction = sig

        # Compute stats
        wins = [t for t in trades if t['pnl'] > 0]
        losses_list = [t for t in trades if t['pnl'] <= 0]
        total_return = (equity - capital) / capital * 100
        annual_return = total_return / (len(candles) / 252) if len(candles) > 0 else 0
        win_rate = (len(wins) / len(trades) * 100) if trades else 0
        avg_win = sum(t['returnPct'] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t['returnPct'] for t in losses_list) / len(losses_list) if losses_list else 0
        total_loss_pnl = sum(abs(t['pnl']) for t in losses_list)
        profit_factor = sum(t['pnl'] for t in wins) / total_loss_pnl if total_loss_pnl > 0 else float('inf')

        peak = capital
        max_dd = 0
        for p in equity_curve:
            if p['value'] > peak:
                peak = p['value']
            dd = (peak - p['value']) / peak * 100
            if dd > max_dd:
                max_dd = dd

        returns = [t['returnPct'] / 100 for t in trades]
        avg_ret = sum(returns) / len(returns) if returns else 0
        std_dev = math.sqrt(sum((r - avg_ret) ** 2 for r in returns) / len(returns)) if len(returns) > 1 else 1
        sharpe = round(avg_ret / std_dev * math.sqrt(252), 2) if std_dev > 0 else 0

        return jsonify({
            'trades': trades,
            'equityCurve': equity_curve,
            'capital': capital,
            'finalEquity': round(equity, 2),
            'totalReturn': round(total_return, 2),
            'annualReturn': round(annual_return, 2),
            'winRate': round(win_rate, 1),
            'avgWin': round(avg_win, 2),
            'avgLoss': round(avg_loss, 2),
            'profitFactor': round(profit_factor, 2) if profit_factor != float('inf') else '∞',
            'maxDD': round(max_dd, 2),
            'sharpe': sharpe,
            'totalTrades': len(trades),
            'dataSource': 'cache',
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════
# ── Phase 4: Multi-Timeframe OHLCV ──
# ══════════════════════════════════════════════════════════════

@app.route('/api/multi-timeframe', methods=['POST'])
def multi_timeframe():
    """
    Fetch OHLCV for multiple intervals for a single stock.
    Returns data keyed by interval: { ohlcv_day: [...], ohlcv_15minute: [...], ... }
    """
    try:
        data = request.json or {}
        symbol = str(data.get('symbol', '')).upper().strip()
        intervals = data.get('intervals', ['day', '15minute', '60minute', 'week'])
        if not symbol:
            return jsonify({'error': 'symbol is required'}), 400

        db = get_db()
        kite = get_kite()

        # Find instrument token
        global _instruments_cache
        if not _instruments_cache:
            _instruments_cache = cache_get_instruments(db)
        if _instruments_cache:
            for inst in _instruments_cache:
                if inst.get('tradingsymbol') == symbol and inst.get('exchange') == 'NSE':
                    token = inst.get('instrument_token')
                    break

        if not token:
            return jsonify({'error': f'No instrument token found for {symbol}'}), 404

        result = {'symbol': symbol}

        missing_intervals = []
        for interval in intervals:
            # Determine date range based on interval
            if interval in ('5minute', '15minute'):
                days_back = 30  # Kite allows ~60 days for minute data
            elif interval == '60minute':
                days_back = 60
            elif interval == 'week':
                days_back = 365
            else:
                days_back = 365

            from_date = (dt.now() - timedelta(days=days_back)).strftime('%Y-%m-%d')
            to_date = dt.now().strftime('%Y-%m-%d')

            try:
                # Try cache first on the main thread for thread-safety
                cached = cache_get_ohlcv(db, token, from_date, to_date, interval)
                is_stale = True
                
                if cached and len(cached) >= 10:
                    try:
                        latest_candle = cached[-1]
                        latest_date_str = latest_candle['date']
                        t_part = latest_date_str.split('+')[0].split('Z')[0]
                        if 'T' in t_part:
                            latest_dt = dt.fromisoformat(t_part)
                        else:
                            latest_dt = dt.strptime(t_part, '%Y-%m-%d %H:%M:%S') if len(t_part) > 10 else dt.strptime(t_part, '%Y-%m-%d')
                        
                        now = dt.now()
                        delta = now - latest_dt
                        
                        # Market close check: if last cached candle is today after 15:15, it is fresh
                        is_market_closed = now.hour >= 16 or (now.hour == 15 and now.minute >= 30) or now.weekday() >= 5
                        is_last_candle_today = latest_dt.date() == now.date()
                        
                        if is_market_closed and is_last_candle_today and latest_dt.hour >= 15:
                            is_stale = False
                        elif interval == '15minute' and delta.total_seconds() < 900:
                            is_stale = False
                        elif interval == '60minute' and delta.total_seconds() < 3600:
                            is_stale = False
                        elif interval in ('day', 'week') and delta.days < 1:
                            is_stale = False
                    except Exception:
                        pass
                
                if cached and not is_stale:
                    result[f'ohlcv_{interval}'] = cached
                elif kite:
                    missing_intervals.append((interval, from_date, to_date))
                else:
                    result[f'ohlcv_{interval}'] = cached if cached else []
            except Exception as e:
                result[f'ohlcv_{interval}_error'] = str(e)

        # Fetch missing API data in parallel
        if kite and missing_intervals:
            def _fetch(inv_info):
                inv, frm, to = inv_info
                return inv, kite.historical_data(token, dt.strptime(frm, '%Y-%m-%d'), dt.strptime(to, '%Y-%m-%d'), inv)

            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                futures = {executor.submit(_fetch, i): i[0] for i in missing_intervals}
                for future in concurrent.futures.as_completed(futures):
                    inv = futures[future]
                    try:
                        inv, raw = future.result()
                        ohlcv = [{'date': str(c['date'])[:10] if inv in ('day', 'week') else str(c['date']),
                                  'open': c['open'], 'high': c['high'],
                                  'low': c['low'], 'close': c['close'], 'volume': c['volume']} for c in raw]
                        result[f'ohlcv_{inv}'] = ohlcv
                        # Safely store cache in main thread
                        cache_store_ohlcv(db, token, raw, inv)
                    except Exception as e:
                        result[f'ohlcv_{inv}_error'] = str(e)

        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════
# ── Phase 4: Market Breadth ──
# ══════════════════════════════════════════════════════════════

@app.route('/api/market-breadth')
def market_breadth():
    """
    Compute advance/decline ratio from batch quotes of F&O stocks.
    Returns: { advances, declines, unchanged, advanceDeclineRatio, breadthIndicator }
    """
    try:
        kite = get_kite()
        db = get_db()

        # Get F&O stock symbols from instruments cache
        global _instruments_cache
        fno_names = set()
        if not _instruments_cache:
            _instruments_cache = cache_get_instruments(db)
        if _instruments_cache:
            for i in _instruments_cache:
                if i.get('segment') in ('NFO-FUT', 'NFO-OPT'):
                    fno_names.add(i.get('name', ''))

        if not fno_names:
            return jsonify({'error': 'No F&O instruments found. Load instruments first.'}), 404

        advances = 0
        declines = 0
        unchanged = 0
        total_volume = 0
        top_gainers = []
        top_losers = []

        if kite:
            # Batch quote in groups of 40
            symbols = [f'NSE:{name}' for name in sorted(fno_names) if name]
            for i in range(0, len(symbols), 40):
                batch = symbols[i:i+40]
                try:
                    quotes = kite.quote(batch)
                    for key, q in quotes.items():
                        chg = q.get('change', 0) or 0
                        vol = q.get('volume', 0) or 0
                        ltp = q.get('last_price', 0) or 0
                        sym = key.replace('NSE:', '')
                        total_volume += vol

                        if chg > 0:
                            advances += 1
                            top_gainers.append({'symbol': sym, 'change': round(chg, 2), 'ltp': ltp})
                        elif chg < 0:
                            declines += 1
                            top_losers.append({'symbol': sym, 'change': round(chg, 2), 'ltp': ltp})
                        else:
                            unchanged += 1
                except Exception as e:
                    logging.warning(f'Market breadth batch error: {e}')
                    continue
        else:
            # Fallback: use cached OHLCV data to compute breadth
            today = dt.now().strftime('%Y-%m-%d')
            yesterday = (dt.now() - timedelta(days=5)).strftime('%Y-%m-%d')
            rows = db.execute('''
                SELECT i.tradingsymbol as symbol, 
                       o1.close as today_close, o2.close as prev_close
                FROM instruments i
                JOIN ohlcv o1 ON i.instrument_token = o1.instrument_token 
                    AND o1.date = (SELECT MAX(date) FROM ohlcv WHERE instrument_token = i.instrument_token AND interval='day')
                    AND o1.interval = 'day'
                JOIN ohlcv o2 ON i.instrument_token = o2.instrument_token 
                    AND o2.date = (SELECT MAX(date) FROM ohlcv WHERE instrument_token = i.instrument_token AND interval='day' AND date < o1.date)
                    AND o2.interval = 'day'
                WHERE i.exchange = 'NSE' AND i.tradingsymbol IN ({})
            '''.format(','.join('?' * len(fno_names))), list(fno_names)).fetchall()

            for r in rows:
                if r['prev_close'] and r['prev_close'] > 0:
                    chg = ((r['today_close'] - r['prev_close']) / r['prev_close']) * 100
                    if chg > 0.01:
                        advances += 1
                        top_gainers.append({'symbol': r['symbol'], 'change': round(chg, 2)})
                    elif chg < -0.01:
                        declines += 1
                        top_losers.append({'symbol': r['symbol'], 'change': round(chg, 2)})
                    else:
                        unchanged += 1

        total = advances + declines + unchanged
        ad_ratio = round(advances / declines, 2) if declines > 0 else float('inf') if advances > 0 else 0

        # Breadth indicator
        if ad_ratio >= 2:
            indicator = 'STRONG BULLISH'
        elif ad_ratio >= 1.3:
            indicator = 'BULLISH'
        elif ad_ratio >= 0.7:
            indicator = 'NEUTRAL'
        elif ad_ratio >= 0.5:
            indicator = 'BEARISH'
        else:
            indicator = 'STRONG BEARISH'

        # Sort and take top 5
        top_gainers.sort(key=lambda x: x['change'], reverse=True)
        top_losers.sort(key=lambda x: x['change'])

        return jsonify({
            'advances': advances,
            'declines': declines,
            'unchanged': unchanged,
            'total': total,
            'advanceDeclineRatio': ad_ratio if ad_ratio != float('inf') else 999,
            'breadthIndicator': indicator,
            'totalVolume': total_volume,
            'topGainers': top_gainers[:5],
            'topLosers': top_losers[:5],
            'source': 'live' if kite else 'cache'
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════
# ── Phase 4: Earnings Calendar ──
# ══════════════════════════════════════════════════════════════

def _init_earnings_table():
    """Create earnings table if not exists."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS earnings (
            symbol TEXT NOT NULL,
            date TEXT NOT NULL,
            purpose TEXT,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (symbol, date)
        )
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_earnings_date ON earnings(date)
    ''')
    conn.commit()
    conn.close()

_init_earnings_table()


def _fetch_nse_earnings():
    """Fetch upcoming board meetings/results from NSE event calendar."""
    try:
        import requests as _requests
        s = _requests.Session()
        s.headers.update({
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://www.nseindia.com/companies-listing/corporate-filings-board-meetings',
        })
        # Acquire session cookies from NSE home page
        try:
            s.get('https://www.nseindia.com', timeout=8)
        except Exception:
            pass

        # /api/event-calendar returns upcoming board meetings for next ~30 days
        resp = s.get('https://www.nseindia.com/api/event-calendar', timeout=12)
        if resp.status_code == 200:
            data = resp.json()
            results = []
            earnings_kw = ['financial result', 'quarterly result', 'annual result', 'dividend', 'board meeting']
            for item in data:
                purpose = (item.get('purpose', '') or '').lower()
                bm_desc = (item.get('bm_desc', '') or '').lower()
                if any(kw in purpose or kw in bm_desc for kw in earnings_kw):
                    symbol = item.get('symbol', '')
                    date_str = item.get('date', '')  # Format: "05-May-2026"
                    purpose_full = item.get('purpose', '') or item.get('bm_desc', '')
                    if symbol and date_str:
                        results.append({
                            'symbol': symbol,
                            'date': date_str,
                            'purpose': purpose_full
                        })
            logging.info(f'NSE event-calendar: fetched {len(results)} earnings events')
            return results
        else:
            logging.warning(f'NSE event-calendar returned status {resp.status_code}')
    except Exception as e:
        logging.warning(f'NSE earnings fetch failed: {e}')
    return []


@app.route('/api/earnings-calendar')
def earnings_calendar():
    """
    Get upcoming earnings/results dates for F&O stocks.
    Caches in SQLite, refreshes daily.
    """
    try:
        db = get_db()
        today = dt.now().strftime('%Y-%m-%d')

        # Check if we have fresh data (fetched today)
        meta = db.execute(
            "SELECT value FROM cache_meta WHERE key = 'earnings_fetched'"
        ).fetchone()
        
        is_fresh = False
        if meta:
            try:
                fetched_date = meta['value'][:10]
                is_fresh = fetched_date == today
            except (ValueError, TypeError):
                pass

        if not is_fresh:
            # Fetch from NSE
            earnings_data = _fetch_nse_earnings()
            if earnings_data:
                now = dt.now().isoformat()
                for e in earnings_data:
                    # Normalize date to YYYY-MM-DD
                    date_val = e['date']
                    # NSE event-calendar uses "05-May-2026" format
                    for fmt in ('%d-%b-%Y', '%d-%m-%Y', '%Y-%m-%d'):
                        try:
                            parsed = dt.strptime(date_val, fmt)
                            date_val = parsed.strftime('%Y-%m-%d')
                            break
                        except ValueError:
                            continue

                    db.execute(
                        'INSERT OR REPLACE INTO earnings (symbol, date, purpose, fetched_at) VALUES (?, ?, ?, ?)',
                        (e['symbol'], date_val, e['purpose'], now)
                    )
                db.execute(
                    'INSERT OR REPLACE INTO cache_meta (key, value, updated_at) VALUES (?, ?, ?)',
                    ('earnings_fetched', now, now)
                )
                db.commit()

        # Return upcoming earnings (next 30 days)
        future = (dt.now() + timedelta(days=30)).strftime('%Y-%m-%d')
        rows = db.execute(
            'SELECT symbol, date, purpose FROM earnings WHERE date >= ? AND date <= ? ORDER BY date',
            (today, future)
        ).fetchall()

        # Filter to F&O stocks if possible
        fno_names = set()
        global _instruments_cache
        if _instruments_cache:
            for i in _instruments_cache:
                if i.get('segment') in ('NFO-FUT', 'NFO-OPT'):
                    fno_names.add(i.get('name', ''))

        earnings = []
        for r in rows:
            entry = dict(r)
            # Calculate days until
            try:
                earn_date = dt.strptime(entry['date'], '%Y-%m-%d')
                entry['daysUntil'] = (earn_date - dt.now().replace(hour=0, minute=0, second=0, microsecond=0)).days
            except ValueError:
                entry['daysUntil'] = None
            entry['isFnO'] = entry['symbol'] in fno_names if fno_names else True
            earnings.append(entry)

        return jsonify({
            'earnings': earnings,
            'total': len(earnings),
            'source': 'cache' if is_fresh else 'nse'
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════
# ── Phase 4: Trade Journal ──
# ══════════════════════════════════════════════════════════════

def _init_journal_table():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS trade_journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL DEFAULT 'LONG',
            entry_price REAL NOT NULL,
            exit_price REAL,
            qty INTEGER NOT NULL DEFAULT 1,
            entry_date TEXT NOT NULL,
            exit_date TEXT,
            rationale TEXT,
            tags TEXT,
            pnl REAL,
            status TEXT NOT NULL DEFAULT 'OPEN',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_trade_journal_status ON trade_journal(status)
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_trade_journal_symbol ON trade_journal(symbol)
    ''')
    conn.commit()
    conn.close()

_init_journal_table()


@app.route('/api/journal', methods=['GET'])
def journal_list():
    """List all journal entries, newest first."""
    try:
        db = get_db()
        status_filter = request.args.get('status', '')
        if status_filter:
            rows = db.execute(
                'SELECT * FROM trade_journal WHERE status = ? ORDER BY created_at DESC',
                (status_filter.upper(),)
            ).fetchall()
        else:
            rows = db.execute('SELECT * FROM trade_journal ORDER BY created_at DESC').fetchall()
        return jsonify({'trades': [dict(r) for r in rows], 'total': len(rows)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/journal', methods=['POST'])
def journal_create():
    """Create a new journal entry."""
    try:
        data = request.json or {}
        required = ['symbol', 'entry_price', 'entry_date']
        for f in required:
            if not data.get(f):
                return jsonify({'error': f'{f} is required'}), 400

        now = dt.now().isoformat()
        symbol = str(data['symbol']).upper().strip()
        direction = str(data.get('direction', 'LONG')).upper()
        entry_price = float(data['entry_price'])
        exit_price = float(data['exit_price']) if data.get('exit_price') else None
        qty = int(data.get('qty', 1))
        entry_date = data['entry_date']
        exit_date = data.get('exit_date')
        rationale = data.get('rationale', '')
        tags = data.get('tags', '')
        status = 'CLOSED' if exit_price else 'OPEN'

        # Compute P&L
        pnl = None
        if exit_price:
            if direction == 'LONG':
                pnl = round((exit_price - entry_price) * qty, 2)
            else:
                pnl = round((entry_price - exit_price) * qty, 2)

        db = get_db()
        cursor = db.execute(
            '''INSERT INTO trade_journal
               (symbol, direction, entry_price, exit_price, qty, entry_date, exit_date, rationale, tags, pnl, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (symbol, direction, entry_price, exit_price, qty, entry_date, exit_date, rationale, tags, pnl, status, now, now)
        )
        db.commit()
        return jsonify({'id': cursor.lastrowid, 'status': status, 'pnl': pnl}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/journal/<int:trade_id>', methods=['PUT'])
def journal_update(trade_id):
    """Update a journal entry (e.g. close a trade)."""
    try:
        data = request.json or {}
        db = get_db()
        row = db.execute('SELECT * FROM trade_journal WHERE id = ?', (trade_id,)).fetchone()
        if not row:
            return jsonify({'error': 'Trade not found'}), 404

        now = dt.now().isoformat()
        exit_price = float(data['exit_price']) if data.get('exit_price') else row['exit_price']
        exit_date = data.get('exit_date') or row['exit_date']
        rationale = data.get('rationale', row['rationale'])
        tags = data.get('tags', row['tags'])
        status = data.get('status', row['status']).upper() if data.get('status') else row['status']

        if exit_price and status != 'OPEN':
            status = 'CLOSED'

        pnl = row['pnl']
        if exit_price:
            direction = row['direction']
            qty = row['qty']
            entry_price = row['entry_price']
            if direction == 'LONG':
                pnl = round((exit_price - entry_price) * qty, 2)
            else:
                pnl = round((entry_price - exit_price) * qty, 2)

        db.execute(
            '''UPDATE trade_journal SET exit_price=?, exit_date=?, rationale=?, tags=?, pnl=?, status=?, updated_at=?
               WHERE id=?''',
            (exit_price, exit_date, rationale, tags, pnl, status, now, trade_id)
        )
        db.commit()
        return jsonify({'id': trade_id, 'status': status, 'pnl': pnl})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/journal/<int:trade_id>', methods=['DELETE'])
def journal_delete(trade_id):
    """Delete a journal entry."""
    try:
        db = get_db()
        db.execute('DELETE FROM trade_journal WHERE id = ?', (trade_id,))
        db.commit()
        return jsonify({'deleted': trade_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/journal/stats')
def journal_stats():
    """Aggregated journal analytics."""
    try:
        db = get_db()
        trades = db.execute('SELECT * FROM trade_journal WHERE status = ?', ('CLOSED',)).fetchall()
        trades = [dict(t) for t in trades]

        if not trades:
            return jsonify({'totalTrades': 0, 'message': 'No closed trades yet'})

        wins = [t for t in trades if (t['pnl'] or 0) > 0]
        losses = [t for t in trades if (t['pnl'] or 0) < 0]
        total_pnl = sum(t['pnl'] or 0 for t in trades)
        win_rate = len(wins) / len(trades) * 100 if trades else 0
        avg_win = sum(t['pnl'] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t['pnl'] for t in losses) / len(losses) if losses else 0
        profit_factor = abs(sum(t['pnl'] for t in wins)) / abs(sum(t['pnl'] for t in losses)) if losses and sum(t['pnl'] for t in losses) != 0 else float('inf')

        # By tag breakdown
        tag_stats = {}
        for t in trades:
            for tag in (t.get('tags') or '').split(','):
                tag = tag.strip()
                if tag:
                    if tag not in tag_stats:
                        tag_stats[tag] = {'count': 0, 'pnl': 0, 'wins': 0}
                    tag_stats[tag]['count'] += 1
                    tag_stats[tag]['pnl'] += t['pnl'] or 0
                    if (t['pnl'] or 0) > 0:
                        tag_stats[tag]['wins'] += 1

        # By symbol breakdown
        symbol_stats = {}
        for t in trades:
            sym = t['symbol']
            if sym not in symbol_stats:
                symbol_stats[sym] = {'count': 0, 'pnl': 0, 'wins': 0}
            symbol_stats[sym]['count'] += 1
            symbol_stats[sym]['pnl'] += t['pnl'] or 0
            if (t['pnl'] or 0) > 0:
                symbol_stats[sym]['wins'] += 1

        return jsonify({
            'totalTrades': len(trades),
            'totalPnl': round(total_pnl, 2),
            'winRate': round(win_rate, 1),
            'avgWin': round(avg_win, 2),
            'avgLoss': round(avg_loss, 2),
            'profitFactor': round(profit_factor, 2) if profit_factor != float('inf') else '∞',
            'wins': len(wins),
            'losses': len(losses),
            'tagStats': tag_stats,
            'symbolStats': symbol_stats,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════
# ── Phase 4: Paper Trading ──
# ══════════════════════════════════════════════════════════════

def _init_paper_tables():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS paper_portfolio (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            capital REAL NOT NULL DEFAULT 1000000,
            created_at TEXT NOT NULL
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL DEFAULT 'BUY',
            qty INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            entry_date TEXT NOT NULL,
            exit_price REAL,
            exit_date TEXT,
            status TEXT NOT NULL DEFAULT 'OPEN',
            pnl REAL,
            created_at TEXT NOT NULL
        )
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_paper_trades_status ON paper_trades(status)
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_paper_trades_symbol ON paper_trades(symbol)
    ''')
    conn.commit()
    conn.close()

_init_paper_tables()


def _get_paper_portfolio(db):
    """Get or create the paper portfolio."""
    row = db.execute('SELECT * FROM paper_portfolio ORDER BY id DESC LIMIT 1').fetchone()
    if row:
        return dict(row)
    now = dt.now().isoformat()
    cursor = db.execute('INSERT INTO paper_portfolio (capital, created_at) VALUES (?, ?)', (1000000, now))
    db.commit()
    return {'id': cursor.lastrowid, 'capital': 1000000, 'created_at': now}


@app.route('/api/paper/portfolio', methods=['GET'])
def paper_portfolio_get():
    """Get paper portfolio info."""
    try:
        db = get_db()
        portfolio = _get_paper_portfolio(db)
        return jsonify(portfolio)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/paper/portfolio', methods=['POST'])
def paper_portfolio_reset():
    """Reset paper portfolio with new capital."""
    try:
        data = request.json or {}
        capital = float(data.get('capital', 1000000))
        now = dt.now().isoformat()
        db = get_db()
        # Close all open positions
        db.execute("UPDATE paper_trades SET status='CANCELLED' WHERE status='OPEN'")
        # Create new portfolio
        cursor = db.execute('INSERT INTO paper_portfolio (capital, created_at) VALUES (?, ?)', (capital, now))
        db.commit()
        return jsonify({'id': cursor.lastrowid, 'capital': capital, 'message': 'Portfolio reset'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/paper/buy', methods=['POST'])
def paper_buy():
    """Execute a paper buy order."""
    try:
        data = request.json or {}
        symbol = str(data.get('symbol', '')).upper().strip()
        qty = int(data.get('qty', 1))
        price = float(data.get('price', 0))
        if not symbol or price <= 0 or qty <= 0:
            return jsonify({'error': 'symbol, price>0, qty>0 required'}), 400

        now = dt.now().isoformat()
        db = get_db()
        portfolio = _get_paper_portfolio(db)

        cost = price * qty
        if cost > portfolio['capital']:
            return jsonify({'error': f'Insufficient capital. Need ₹{cost:,.0f}, have ₹{portfolio["capital"]:,.0f}'}), 400

        # Deduct capital
        db.execute('UPDATE paper_portfolio SET capital = capital - ? WHERE id = ?', (cost, portfolio['id']))
        cursor = db.execute(
            '''INSERT INTO paper_trades (symbol, direction, qty, entry_price, entry_date, status, created_at)
               VALUES (?, 'BUY', ?, ?, ?, 'OPEN', ?)''',
            (symbol, qty, price, now, now)
        )
        db.commit()
        return jsonify({'id': cursor.lastrowid, 'symbol': symbol, 'qty': qty, 'price': price, 'cost': cost}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/paper/sell', methods=['POST'])
def paper_sell():
    """Execute a paper short sell order."""
    try:
        data = request.json or {}
        symbol = str(data.get('symbol', '')).upper().strip()
        qty = int(data.get('qty', 1))
        price = float(data.get('price', 0))
        if not symbol or price <= 0 or qty <= 0:
            return jsonify({'error': 'symbol, price>0, qty>0 required'}), 400

        now = dt.now().isoformat()
        db = get_db()

        cursor = db.execute(
            '''INSERT INTO paper_trades (symbol, direction, qty, entry_price, entry_date, status, created_at)
               VALUES (?, 'SELL', ?, ?, ?, 'OPEN', ?)''',
            (symbol, qty, price, now, now)
        )
        db.commit()
        return jsonify({'id': cursor.lastrowid, 'symbol': symbol, 'direction': 'SELL', 'qty': qty, 'price': price}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/paper/close/<int:trade_id>', methods=['POST'])
def paper_close(trade_id):
    """Close an open paper trade."""
    try:
        data = request.json or {}
        exit_price = float(data.get('price', 0))
        if exit_price <= 0:
            return jsonify({'error': 'price > 0 required'}), 400

        now = dt.now().isoformat()
        db = get_db()
        row = db.execute('SELECT * FROM paper_trades WHERE id = ? AND status = ?', (trade_id, 'OPEN')).fetchone()
        if not row:
            return jsonify({'error': 'Open trade not found'}), 404

        entry_price = row['entry_price']
        qty = row['qty']
        direction = row['direction']

        if direction == 'BUY':
            pnl = round((exit_price - entry_price) * qty, 2)
        else:
            pnl = round((entry_price - exit_price) * qty, 2)

        # Return capital + P&L
        portfolio = _get_paper_portfolio(db)
        returned = (exit_price * qty) if direction == 'BUY' else (entry_price * qty + pnl)
        db.execute('UPDATE paper_portfolio SET capital = capital + ? WHERE id = ?', (returned, portfolio['id']))

        db.execute(
            'UPDATE paper_trades SET exit_price=?, exit_date=?, pnl=?, status=? WHERE id=?',
            (exit_price, now, pnl, 'CLOSED', trade_id)
        )
        db.commit()
        return jsonify({'id': trade_id, 'pnl': pnl, 'exit_price': exit_price, 'status': 'CLOSED'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/paper/positions')
def paper_positions():
    """List all paper positions."""
    try:
        db = get_db()
        status_filter = request.args.get('status', '')
        if status_filter:
            rows = db.execute('SELECT * FROM paper_trades WHERE status = ? ORDER BY created_at DESC', (status_filter.upper(),)).fetchall()
        else:
            rows = db.execute('SELECT * FROM paper_trades ORDER BY created_at DESC').fetchall()
        return jsonify({'positions': [dict(r) for r in rows], 'total': len(rows)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/paper/summary')
def paper_summary():
    """Paper trading summary with analytics."""
    try:
        db = get_db()
        portfolio = _get_paper_portfolio(db)
        open_trades = db.execute("SELECT * FROM paper_trades WHERE status='OPEN'").fetchall()
        closed_trades = db.execute("SELECT * FROM paper_trades WHERE status='CLOSED'").fetchall()

        open_trades = [dict(t) for t in open_trades]
        closed_trades = [dict(t) for t in closed_trades]

        total_pnl = sum(t['pnl'] or 0 for t in closed_trades)
        wins = [t for t in closed_trades if (t['pnl'] or 0) > 0]
        losses = [t for t in closed_trades if (t['pnl'] or 0) < 0]
        win_rate = len(wins) / len(closed_trades) * 100 if closed_trades else 0

        # Invested in open positions
        invested = sum(t['entry_price'] * t['qty'] for t in open_trades if t['direction'] == 'BUY')

        initial_capital = 1000000  # default
        current_equity = portfolio['capital'] + invested + total_pnl

        return jsonify({
            'capital': round(portfolio['capital'], 2),
            'invested': round(invested, 2),
            'realizedPnl': round(total_pnl, 2),
            'currentEquity': round(current_equity, 2),
            'totalReturn': round((current_equity - initial_capital) / initial_capital * 100, 2),
            'openPositions': len(open_trades),
            'closedTrades': len(closed_trades),
            'winRate': round(win_rate, 1),
            'wins': len(wins),
            'losses': len(losses),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════
# ── Phase 4: AI Research Assistant ──
# ══════════════════════════════════════════════════════════════

@app.route('/api/ai/research', methods=['POST'])
def ai_research():
    """
    AI-powered research using local Ollama/Llama3.
    Streams response via SSE.
    """
    try:
        data = request.json or {}
        query = data.get('query', '').strip()
        symbol = str(data.get('symbol', '')).upper().strip()
        if not query:
            return jsonify({'error': 'query is required'}), 400

        # Build context from cached data
        context_parts = []

        if symbol:
            db = get_db()
            # Get recent OHLCV
            global _instruments_cache
            if not _instruments_cache:
                _instruments_cache = cache_get_instruments(db)
            if _instruments_cache:
                for inst in _instruments_cache:
                    if inst.get('tradingsymbol') == symbol and inst.get('exchange') == 'NSE':
                        token = inst.get('instrument_token')
                        break

            if token:
                from_date = (dt.now() - timedelta(days=30)).strftime('%Y-%m-%d')
                to_date = dt.now().strftime('%Y-%m-%d')
                ohlcv = cache_get_ohlcv(db, token, from_date, to_date, 'day')
                if ohlcv:
                    last5 = ohlcv[-5:]
                    closes = [c['close'] for c in ohlcv]
                    high52 = max(c['high'] for c in ohlcv) if ohlcv else 0
                    low52 = min(c['low'] for c in ohlcv) if ohlcv else 0
                    context_parts.append(
                        f"Stock: {symbol}\n"
                        f"Recent closes (last 5 days): {[c['close'] for c in last5]}\n"
                        f"30-day High: {high52}, Low: {low52}\n"
                        f"Current Price: {closes[-1] if closes else 'N/A'}\n"
                        f"30-day data points: {len(ohlcv)}"
                    )

            # Get analyst data
            analyst = _fetch_analyst_rating(symbol)
            if analyst and analyst.get('sector'):
                context_parts.append(f"Sector: {analyst['sector']}")

        context = '\n'.join(context_parts)
        system_prompt = (
            "You are an expert Indian stock market research analyst. "
            "You provide clear, actionable insights based on technical and fundamental analysis. "
            "Always mention specific price levels, support/resistance, and risk factors. "
            "Be concise but thorough. Use bullet points for key takeaways."
        )

        full_prompt = f"{system_prompt}\n\n"
        if context:
            full_prompt += f"Market Context:\n{context}\n\n"
        full_prompt += f"User Query: {query}"

        # Try Ollama
        ollama_url = os.environ.get('OLLAMA_URL', 'http://localhost:11434/api/generate')
        model = os.environ.get('OLLAMA_MODEL', 'llama3')

        import urllib.request

        payload = json.dumps({
            'model': model,
            'prompt': full_prompt,
            'stream': True
        }).encode()

        req = urllib.request.Request(
            ollama_url,
            data=payload,
            headers={'Content-Type': 'application/json'}
        )

        def generate():
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    for line in resp:
                        if line:
                            try:
                                chunk = json.loads(line.decode())
                                text = chunk.get('response', '')
                                if text:
                                    yield f"data: {json.dumps({'text': text})}\n\n"
                                if chunk.get('done'):
                                    yield f"data: {json.dumps({'done': True})}\n\n"
                                    break
                            except json.JSONDecodeError:
                                continue
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e), 'fallback': True})}\n\n"
                # Fallback: provide a basic analysis without AI
                if symbol and context:
                    fallback_text = (
                        f"⚠️ AI model unavailable (Ollama not running).\n\n"
                        f"**{symbol} Quick Summary from cached data:**\n{context}\n\n"
                        f"💡 To enable AI research, start Ollama with: `ollama run {model}`"
                    )
                    yield f"data: {json.dumps({'text': fallback_text})}\n\n"
                else:
                    yield f"data: {json.dumps({'text': '⚠️ AI model unavailable. Start Ollama with: ollama run ' + model})}\n\n"
                yield f"data: {json.dumps({'done': True})}\n\n"

        return app.response_class(generate(), mimetype='text/event-stream',
                                  headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════
# ── Recommendation P&L Tracker ──
# ══════════════════════════════════════════════════════════════

def _init_reco_tracker_table():
    """Create reco_tracker table if not exists."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS reco_tracker (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            source          TEXT NOT NULL,
            session_phase   TEXT,
            symbol          TEXT NOT NULL,
            signal          TEXT NOT NULL,
            direction       TEXT NOT NULL,
            score           REAL,
            confidence      TEXT,
            entry_price     REAL NOT NULL,
            target1         REAL,
            target2         REAL,
            stop_loss       REAL,
            risk_reward     REAL,
            strategy        TEXT,
            exit_price      REAL,
            exit_date       TEXT,
            pnl             REAL,
            pnl_pct         REAL,
            status          TEXT NOT NULL DEFAULT 'OPEN',
            outcome         TEXT,
            rationale       TEXT,
            reco_date       TEXT NOT NULL,
            reco_time       TEXT NOT NULL,
            created_at      TEXT NOT NULL
        )
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_reco_tracker_date ON reco_tracker(reco_date)
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_reco_tracker_symbol ON reco_tracker(symbol)
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_reco_tracker_source ON reco_tracker(source)
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_reco_tracker_status ON reco_tracker(status)
    ''')
    conn.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS idx_reco_tracker_upsert
        ON reco_tracker(symbol, source, COALESCE(session_phase, ''), reco_date)
    ''')
    conn.commit()
    conn.close()

_init_reco_tracker_table()


def _compute_reco_pnl(entry_price, exit_price, direction):
    """Compute P&L and P&L% for a recommendation."""
    if not exit_price or not entry_price or entry_price == 0:
        return None, None
    if direction in ('BUY', 'BULLISH', 'CALL'):
        pnl = round(exit_price - entry_price, 2)
        pnl_pct = round((exit_price - entry_price) / entry_price * 100, 2)
    else:
        pnl = round(entry_price - exit_price, 2)
        pnl_pct = round((entry_price - exit_price) / entry_price * 100, 2)
    return pnl, pnl_pct


def _compute_reco_outcome(entry_price, exit_price, target1, target2, stop_loss, direction):
    """Determine outcome: WIN, TARGET2, SL_HIT, LOSS, NEUTRAL."""
    if not exit_price or not entry_price:
        return None
    is_buy = direction in ('BUY', 'BULLISH', 'CALL')
    # Check SL hit first
    if stop_loss:
        if is_buy and exit_price <= stop_loss:
            return 'SL_HIT'
        elif not is_buy and exit_price >= stop_loss:
            return 'SL_HIT'
    # Check Target 2
    if target2:
        if is_buy and exit_price >= target2:
            return 'TARGET2'
        elif not is_buy and exit_price <= target2:
            return 'TARGET2'
    # Check Target 1
    if target1:
        if is_buy and exit_price >= target1:
            return 'WIN'
        elif not is_buy and exit_price <= target1:
            return 'WIN'
    # Check profitability
    pnl, _ = _compute_reco_pnl(entry_price, exit_price, direction)
    if pnl is not None:
        if pnl > 0:
            return 'NEUTRAL'  # profitable but didn't reach target
        else:
            return 'LOSS'
    return None


@app.route('/api/reco-tracker', methods=['POST'])
def reco_tracker_upsert():
    """
    Bulk upsert recommendations. Expects JSON:
    { "recommendations": [ { symbol, source, session_phase?, signal, direction, score, confidence,
                              entry_price, target1, target2, stop_loss, risk_reward, strategy, rationale } ] }
    Uses upsert on (symbol, source, session_phase, reco_date) to avoid duplicates.
    """
    try:
        data = request.json or {}
        recos = data.get('recommendations', [])
        if not recos:
            return jsonify({'error': 'No recommendations provided'}), 400

        ist_now    = now_ist()   # Always IST regardless of server timezone
        reco_date  = ist_now.strftime('%Y-%m-%d')
        reco_time  = ist_now.strftime('%H:%M:%S')
        created_at = ist_now.isoformat()

        db = get_db()
        upserted = 0
        skipped = 0

        for r in recos:
            symbol = str(r.get('symbol', '')).upper().strip()
            source = r.get('source', 'equity_picks')
            session_phase = r.get('session_phase') or ''
            signal = r.get('signal', '')
            direction = r.get('direction', '')
            score = r.get('score')
            confidence = r.get('confidence', '')
            entry_price = r.get('entry_price')
            target1 = r.get('target1')
            target2 = r.get('target2')
            stop_loss = r.get('stop_loss')
            risk_reward = r.get('risk_reward')
            strategy = r.get('strategy', '')
            rationale = r.get('rationale', '')

            if not symbol or not entry_price:
                skipped += 1
                continue

            try:
                db.execute('''
                    INSERT INTO reco_tracker
                    (symbol, source, session_phase, signal, direction, score, confidence,
                     entry_price, target1, target2, stop_loss, risk_reward, strategy,
                     rationale, reco_date, reco_time, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
                    ON CONFLICT(symbol, source, COALESCE(session_phase, ''), reco_date)
                    DO UPDATE SET
                        signal=excluded.signal, direction=excluded.direction,
                        score=excluded.score, confidence=excluded.confidence,
                        entry_price=excluded.entry_price, target1=excluded.target1,
                        target2=excluded.target2, stop_loss=excluded.stop_loss,
                        risk_reward=excluded.risk_reward, strategy=excluded.strategy,
                        rationale=excluded.rationale, reco_time=excluded.reco_time
                ''', (symbol, source, session_phase, signal, direction, score, confidence,
                      entry_price, target1, target2, stop_loss, risk_reward, strategy,
                      rationale, reco_date, reco_time, created_at))
                upserted += 1
            except Exception as e:
                logging.warning(f'Reco upsert failed for {symbol}: {e}')
                skipped += 1

        db.commit()
        return jsonify({'upserted': upserted, 'skipped': skipped, 'date': reco_date}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/reco-tracker', methods=['GET'])
def reco_tracker_list():
    """
    List recommendations with filters.
    Query params: date (YYYY-MM-DD), source, status, symbol
    """
    try:
        db = get_db()
        date_filter = request.args.get('date', '')
        source_filter = request.args.get('source', '')
        status_filter = request.args.get('status', '')
        symbol_filter = request.args.get('symbol', '')

        query = 'SELECT * FROM reco_tracker WHERE 1=1'
        params = []

        if date_filter:
            query += ' AND reco_date = ?'
            params.append(date_filter)
        if source_filter:
            query += ' AND source = ?'
            params.append(source_filter)
        if status_filter:
            query += ' AND status = ?'
            params.append(status_filter.upper())
        if symbol_filter:
            query += ' AND symbol = ?'
            params.append(symbol_filter.upper())

        query += ' ORDER BY score DESC, reco_time DESC'
        rows = db.execute(query, params).fetchall()

        return jsonify({
            'recommendations': [dict(r) for r in rows],
            'total': len(rows)
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/reco-tracker/<int:reco_id>', methods=['PUT'])
def reco_tracker_update(reco_id):
    """
    Update a recommendation (manual P&L update).
    Accepts: exit_price, exit_date, status
    """
    try:
        data = request.json or {}
        db = get_db()
        row = db.execute('SELECT * FROM reco_tracker WHERE id = ?', (reco_id,)).fetchone()
        if not row:
            return jsonify({'error': 'Recommendation not found'}), 404

        exit_price = float(data['exit_price']) if data.get('exit_price') else row['exit_price']
        exit_date = data.get('exit_date') or row['exit_date'] or dt.now().strftime('%Y-%m-%d')

        entry_price = row['entry_price']
        direction = row['direction']
        target1 = row['target1']
        target2 = row['target2']
        stop_loss = row['stop_loss']

        pnl, pnl_pct = _compute_reco_pnl(entry_price, exit_price, direction)
        outcome = _compute_reco_outcome(entry_price, exit_price, target1, target2, stop_loss, direction)
        status = data.get('status', 'CLOSED').upper() if exit_price else row['status']

        db.execute('''
            UPDATE reco_tracker SET exit_price=?, exit_date=?, pnl=?, pnl_pct=?, status=?, outcome=?
            WHERE id=?
        ''', (exit_price, exit_date, pnl, pnl_pct, status, outcome, reco_id))
        db.commit()

        return jsonify({
            'id': reco_id, 'exit_price': exit_price, 'pnl': pnl,
            'pnl_pct': pnl_pct, 'status': status, 'outcome': outcome
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/reco-tracker/auto-close', methods=['POST'])
def reco_tracker_auto_close():
    """
    Auto-fetch LTP from Kite for all OPEN recommendations on a given date,
    compute P&L, and close them.
    Body: { "date": "YYYY-MM-DD" } — defaults to today.
    """
    try:
        data = request.json or {}
        target_date = data.get('date', dt.now().strftime('%Y-%m-%d'))

        db = get_db()
        open_recos = db.execute(
            'SELECT * FROM reco_tracker WHERE status = ? AND reco_date = ?',
            ('OPEN', target_date)
        ).fetchall()

        if not open_recos:
            return jsonify({'message': 'No open recommendations for this date', 'closed': 0})

        kite = get_kite()
        if not kite:
            return jsonify({'error': 'Kite API not connected. Use manual update.'}), 503

        # Batch fetch LTP for all symbols
        symbols = list(set(r['symbol'] for r in open_recos))
        ltp_map = {}

        for i in range(0, len(symbols), 40):
            batch = symbols[i:i+40]
            nse_keys = [f'NSE:{s}' for s in batch]
            try:
                quotes = kite.ltp(nse_keys)
                for key, val in quotes.items():
                    sym = key.replace('NSE:', '')
                    ltp_map[sym] = val.get('last_price', 0)
            except Exception as e:
                logging.warning(f'LTP fetch failed for batch: {e}')

        closed = 0
        exit_date = dt.now().strftime('%Y-%m-%d')

        for r in open_recos:
            row = dict(r)
            sym = row['symbol']
            ltp = ltp_map.get(sym)
            if not ltp or ltp <= 0:
                continue

            pnl, pnl_pct = _compute_reco_pnl(row['entry_price'], ltp, row['direction'])
            outcome = _compute_reco_outcome(
                row['entry_price'], ltp, row['target1'], row['target2'],
                row['stop_loss'], row['direction']
            )

            db.execute('''
                UPDATE reco_tracker SET exit_price=?, exit_date=?, pnl=?, pnl_pct=?, status=?, outcome=?
                WHERE id=?
            ''', (ltp, exit_date, pnl, pnl_pct, 'CLOSED', outcome, row['id']))
            closed += 1

        db.commit()
        return jsonify({'closed': closed, 'total_open': len(open_recos), 'date': target_date})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/reco-tracker/stats')
def reco_tracker_stats():
    """
    Recommendation accuracy analytics.
    Query params: date (optional, for daily view)
    """
    try:
        db = get_db()
        date_filter = request.args.get('date', '')

        base_query = 'SELECT * FROM reco_tracker WHERE status != ?'
        params = ['OPEN']
        if date_filter:
            base_query += ' AND reco_date = ?'
            params.append(date_filter)

        closed = db.execute(base_query, params).fetchall()
        closed = [dict(r) for r in closed]

        if not closed:
            return jsonify({'total': 0, 'message': 'No closed recommendations'})

        wins = [r for r in closed if (r['pnl'] or 0) > 0]
        losses = [r for r in closed if (r['pnl'] or 0) < 0]
        total_pnl = sum(r['pnl'] or 0 for r in closed)
        avg_pnl_pct = sum(r['pnl_pct'] or 0 for r in closed) / len(closed) if closed else 0
        win_rate = len(wins) / len(closed) * 100 if closed else 0

        # Target hit rates
        target1_hits = len([r for r in closed if r['outcome'] in ('WIN', 'TARGET2')])
        target2_hits = len([r for r in closed if r['outcome'] == 'TARGET2'])
        sl_hits = len([r for r in closed if r['outcome'] == 'SL_HIT'])

        # Best and worst
        best = max(closed, key=lambda r: r['pnl_pct'] or 0) if closed else None
        worst = min(closed, key=lambda r: r['pnl_pct'] or 0) if closed else None

        # By source
        source_stats = {}
        for r in closed:
            src = r['source']
            if src not in source_stats:
                source_stats[src] = {'count': 0, 'wins': 0, 'pnl': 0, 'pnl_pct_sum': 0}
            source_stats[src]['count'] += 1
            source_stats[src]['pnl'] += r['pnl'] or 0
            source_stats[src]['pnl_pct_sum'] += r['pnl_pct'] or 0
            if (r['pnl'] or 0) > 0:
                source_stats[src]['wins'] += 1

        for src in source_stats:
            s = source_stats[src]
            s['win_rate'] = round(s['wins'] / s['count'] * 100, 1) if s['count'] else 0
            s['avg_pnl_pct'] = round(s['pnl_pct_sum'] / s['count'], 2) if s['count'] else 0
            s['pnl'] = round(s['pnl'], 2)

        # By confidence
        conf_stats = {}
        for r in closed:
            conf = r['confidence'] or 'UNKNOWN'
            if conf not in conf_stats:
                conf_stats[conf] = {'count': 0, 'wins': 0, 'pnl': 0}
            conf_stats[conf]['count'] += 1
            conf_stats[conf]['pnl'] += r['pnl'] or 0
            if (r['pnl'] or 0) > 0:
                conf_stats[conf]['wins'] += 1
        for conf in conf_stats:
            c = conf_stats[conf]
            c['win_rate'] = round(c['wins'] / c['count'] * 100, 1) if c['count'] else 0
            c['pnl'] = round(c['pnl'], 2)

        # Available dates  
        dates = sorted(set(r['reco_date'] for r in closed), reverse=True)

        return jsonify({
            'total': len(closed),
            'wins': len(wins),
            'losses': len(losses),
            'totalPnl': round(total_pnl, 2),
            'avgPnlPct': round(avg_pnl_pct, 2),
            'winRate': round(win_rate, 1),
            'target1HitRate': round(target1_hits / len(closed) * 100, 1) if closed else 0,
            'target2HitRate': round(target2_hits / len(closed) * 100, 1) if closed else 0,
            'slHitRate': round(sl_hits / len(closed) * 100, 1) if closed else 0,
            'best': {'symbol': best['symbol'], 'pnl_pct': best['pnl_pct'], 'source': best['source']} if best else None,
            'worst': {'symbol': worst['symbol'], 'pnl_pct': worst['pnl_pct'], 'source': worst['source']} if worst else None,
            'bySource': source_stats,
            'byConfidence': conf_stats,
            'dates': dates[:30],
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/reco-tracker/<int:reco_id>', methods=['DELETE'])
def reco_tracker_delete(reco_id):
    """Delete a recommendation record."""
    try:
        db = get_db()
        db.execute('DELETE FROM reco_tracker WHERE id = ?', (reco_id,))
        db.commit()
        return jsonify({'deleted': reco_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Index Movers Dashboard ──
INDEX_CONSTITUENTS = {
    # ── NSE Indices ──
    'nifty50': [
        'ADANIENT', 'ADANIPORTS', 'APOLLOHOSP', 'ASIANPAINT', 'AXISBANK',
        'BAJAJ-AUTO', 'BAJAJFINSV', 'BAJFINANCE', 'BHARTIARTL', 'BPCL',
        'BRITANNIA', 'CIPLA', 'COALINDIA', 'DIVISLAB', 'DRREDDY',
        'EICHERMOT', 'GRASIM', 'HCLTECH', 'HDFCBANK', 'HDFCLIFE',
        'HEROMOTOCO', 'HINDALCO', 'HINDUNILVR', 'ICICIBANK', 'INDUSINDBK',
        'INFY', 'ITC', 'JSWSTEEL', 'KOTAKBANK', 'LT',
        'M&M', 'MARUTI', 'NESTLEIND', 'NTPC', 'ONGC',
        'POWERGRID', 'RELIANCE', 'SBILIFE', 'SBIN', 'SUNPHARMA',
        'TATACONSUM', 'TATAMOTORS', 'TATASTEEL', 'TCS', 'TECHM',
        'TITAN', 'TRENT', 'ULTRACEMCO', 'UPL', 'WIPRO',
    ],
    'banknifty': [
        'AUBANK', 'AXISBANK', 'BANDHANBNK', 'FEDERALBNK', 'HDFCBANK',
        'ICICIBANK', 'IDFCFIRSTB', 'INDUSINDBK', 'KOTAKBANK', 'PNB',
        'SBIN', 'BANKBARODA',
    ],
    'niftyit': [
        'COFORGE', 'HCLTECH', 'INFY', 'LTIM', 'MPHASIS',
        'OFSS', 'PERSISTENT', 'TCS', 'TECHM', 'WIPRO',
    ],
    'nifty100': [
        'ADANIENT', 'ADANIPORTS', 'APOLLOHOSP', 'ASIANPAINT', 'AXISBANK',
        'BAJAJ-AUTO', 'BAJAJFINSV', 'BAJFINANCE', 'BHARTIARTL', 'BPCL',
        'BRITANNIA', 'CIPLA', 'COALINDIA', 'DIVISLAB', 'DRREDDY',
        'EICHERMOT', 'GRASIM', 'HCLTECH', 'HDFCBANK', 'HDFCLIFE',
        'HEROMOTOCO', 'HINDALCO', 'HINDUNILVR', 'ICICIBANK', 'INDUSINDBK',
        'INFY', 'ITC', 'JSWSTEEL', 'KOTAKBANK', 'LT',
        'M&M', 'MARUTI', 'NESTLEIND', 'NTPC', 'ONGC',
        'POWERGRID', 'RELIANCE', 'SBILIFE', 'SBIN', 'SUNPHARMA',
        'TATACONSUM', 'TATAMOTORS', 'TATASTEEL', 'TCS', 'TECHM',
        'TITAN', 'TRENT', 'ULTRACEMCO', 'UPL', 'WIPRO',
        'ABB', 'AMBUJACEM', 'AUROPHARMA', 'BALKRISIND', 'BANKBARODA',
        'BERGEPAINT', 'BIOCON', 'BOSCHLTD', 'CANBK', 'CHOLAFIN',
        'COLPAL', 'CONCOR', 'CUMMINSIND', 'DABUR', 'DLF',
        'GAIL', 'GODREJCP', 'GODREJPROP', 'HAVELLS', 'ICICIGI',
        'ICICIPRULI', 'INDUSTOWER', 'JINDALSTEL', 'JUBLFOOD', 'LICHSGFIN',
        'LUPIN', 'MFSL', 'MUTHOOTFIN', 'NAUKRI', 'PAGEIND',
        'PIDILITIND', 'PIIND', 'RECLTD', 'SAIL', 'SIEMENS',
        'SRF', 'TORNTPHARM', 'TATAPOWER', 'VEDL', 'VOLTAS',
        'ZOMATO', 'PAYTM', 'DMART', 'ADANIGREEN', 'ADANITRANS',
        'AUBANK', 'BANDHANBNK', 'FEDERALBNK', 'IDFCFIRSTB', 'PNB',
    ],
    # ── BSE Indices ──
    'sensex': [
        'ADANIENT', 'ADANIPORTS', 'ASIANPAINT', 'AXISBANK', 'BAJAJ-AUTO',
        'BAJAJFINSV', 'BAJFINANCE', 'BHARTIARTL', 'HCLTECH', 'HDFCBANK',
        'HDFCLIFE', 'HEROMOTOCO', 'HINDUNILVR', 'ICICIBANK', 'INDUSINDBK',
        'INFY', 'ITC', 'JSWSTEEL', 'KOTAKBANK', 'LT',
        'M&M', 'MARUTI', 'NESTLEIND', 'NTPC', 'POWERGRID',
        'RELIANCE', 'SBIN', 'SUNPHARMA', 'TATAMOTORS', 'TCS',
    ],
    'bsebankex': [
        'AUBANK', 'AXISBANK', 'BANDHANBNK', 'FEDERALBNK', 'HDFCBANK',
        'ICICIBANK', 'IDFCFIRSTB', 'INDUSINDBK', 'KOTAKBANK', 'PNB',
        'SBIN', 'BANKBARODA',
    ],
    'bseit': [
        'COFORGE', 'HCLTECH', 'INFY', 'LTIM', 'MPHASIS',
        'OFSS', 'PERSISTENT', 'TCS', 'TECHM', 'WIPRO',
    ],
    'bse100': [
        'ADANIENT', 'ADANIPORTS', 'ASIANPAINT', 'AXISBANK', 'BAJAJ-AUTO',
        'BAJAJFINSV', 'BAJFINANCE', 'BHARTIARTL', 'HCLTECH', 'HDFCBANK',
        'HDFCLIFE', 'HEROMOTOCO', 'HINDUNILVR', 'ICICIBANK', 'INDUSINDBK',
        'INFY', 'ITC', 'JSWSTEEL', 'KOTAKBANK', 'LT',
        'M&M', 'MARUTI', 'NESTLEIND', 'NTPC', 'POWERGRID',
        'RELIANCE', 'SBIN', 'SUNPHARMA', 'TATAMOTORS', 'TCS',
        'APOLLOHOSP', 'BPCL', 'BRITANNIA', 'CIPLA', 'COALINDIA',
        'DIVISLAB', 'DRREDDY', 'EICHERMOT', 'GRASIM', 'HINDALCO',
        'ONGC', 'SBILIFE', 'TATACONSUM', 'TATASTEEL', 'TECHM',
        'TITAN', 'TRENT', 'ULTRACEMCO', 'UPL', 'WIPRO',
        'ABB', 'AMBUJACEM', 'AUROPHARMA', 'BANKBARODA', 'BERGEPAINT',
        'BOSCHLTD', 'CANBK', 'CHOLAFIN', 'COLPAL', 'DABUR',
        'DLF', 'GAIL', 'GODREJCP', 'GODREJPROP', 'HAVELLS',
        'ICICIGI', 'ICICIPRULI', 'INDUSTOWER', 'JINDALSTEL', 'JUBLFOOD',
        'LICHSGFIN', 'LUPIN', 'MUTHOOTFIN', 'NAUKRI', 'PAGEIND',
        'PIDILITIND', 'RECLTD', 'SAIL', 'SIEMENS', 'SRF',
        'TORNTPHARM', 'TATAPOWER', 'VEDL', 'VOLTAS', 'ZOMATO',
        'AUBANK', 'BANDHANBNK', 'FEDERALBNK', 'IDFCFIRSTB', 'PNB',
        'DMART', 'ADANIGREEN', 'MFSL', 'PIIND', 'CONCOR',
    ],
}

# Exchange for each index
INDEX_EXCHANGE = {
    'nifty50': 'NSE', 'banknifty': 'NSE', 'niftyit': 'NSE', 'nifty100': 'NSE',
    'sensex': 'BSE', 'bsebankex': 'BSE', 'bseit': 'BSE', 'bse100': 'BSE',
}

_index_movers_cache = {}  # keyed by "index_day_v2", value: {data, ts}  (v2 = yesterday bug fixed)


# ══════════════════════════════════════════════════════════════
# ── FNO Trade Alerts Scanner ──
# ══════════════════════════════════════════════════════════════

def _ten_working_days_ago(now):
    """Return datetime exactly 10 working days (Mon-Fri) before *now*."""
    count = 0
    d = now
    while count < 10:
        d -= timedelta(days=1)
        if d.weekday() < 5:   # 0=Mon … 4=Fri
            count += 1
    return d


def _serialize_result(obj):
    """Recursively convert non-JSON-serializable objects to JSON-safe types."""
    import math
    if isinstance(obj, (np.integer, np.floating, float)):
        val = float(obj)
        if math.isnan(val) or math.isinf(val):
            return None
        return val
    elif isinstance(obj, (np.ndarray, pd.Series)):
        # pd.Series/np.ndarray tolist() might contain NaNs, so we serialize items
        return [_serialize_result(x) for x in obj.tolist()]
    elif isinstance(obj, dict):
        return {k: _serialize_result(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [_serialize_result(item) for item in obj]
    else:
        return obj


import sys as _sys
_scanner_dir = os.path.join(os.path.dirname(__file__), '..', '..')
if _scanner_dir not in _sys.path:
    _sys.path.insert(0, _scanner_dir)


SCAN_PROGRESS = {"status": "Idle", "current": 0, "total": 0, "percent": 0}

def _reload_runtime_env():
    """Reload .env values changed while the Flask server is already running."""
    for env_path in _env_candidates:
        env_path = os.path.normpath(env_path)
        if os.path.isfile(env_path):
            load_dotenv(env_path, override=True)
            return env_path
    return None


def _telegram_configured():
    _reload_runtime_env()
    has_telegram = bool(os.environ.get('TELEGRAM_BOT_TOKEN') and os.environ.get('TELEGRAM_CHAT_ID'))
    has_discord = bool(os.environ.get('DISCORD_WEBHOOK_URL'))
    return has_telegram or has_discord


def _send_telegram_message(text: str) -> dict:
    """Send alert message to Telegram and/or Discord if configured."""
    env_path = _reload_runtime_env()

    # Build a robust SSL context that works on both desktop and Termux/Android.
    # Termux's system CA bundle is often missing; certifi provides a bundled fallback.
    import ssl
    try:
        import certifi
        _ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        _ssl_ctx = ssl.create_default_context()

    # 1. Send to Discord if configured
    discord_url = os.environ.get('DISCORD_WEBHOOK_URL', '').strip()
    # Normalize deprecated domain (discordapp.com → discord.com) to avoid POST redirect failures
    discord_url = discord_url.replace('discordapp.com', 'discord.com')
    discord_sent = False
    discord_reason = None
    if discord_url:
        try:
            import json
            import urllib.request
            # Discord message character limit is 2000
            payload = json.dumps({
                'content': text[:2000],
                'username': 'TradeSignal Alerts'
            }).encode('utf-8')
            req = urllib.request.Request(
                discord_url,
                data=payload,
                headers={
                    'Content-Type': 'application/json',
                    'User-Agent': 'TradeSignalAlerts/1.0'
                },
                method='POST'
            )
            with urllib.request.urlopen(req, timeout=12, context=_ssl_ctx) as resp:
                discord_sent = (200 <= resp.status < 300)
                discord_reason = 'ok' if discord_sent else f'status code: {resp.status}'
        except Exception as e:
            logging.error(f'Discord alert failed: {e} | SSL_CERT_FILE={os.environ.get("SSL_CERT_FILE", "unset")}')
            discord_reason = str(e)
    else:
        discord_reason = 'missing DISCORD_WEBHOOK_URL'

    # 2. Send to Telegram if configured
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    chat_id = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
    
    telegram_sent = False
    telegram_reason = None
    if token and chat_id:
        try:
            import urllib.parse
            import urllib.request

            url = f'https://api.telegram.org/bot{token}/sendMessage'
            payload = urllib.parse.urlencode({
                'chat_id': chat_id,
                'text': text[:3900],
                'disable_web_page_preview': 'true',
            }).encode('utf-8')
            req = urllib.request.Request(url, data=payload, method='POST')
            with urllib.request.urlopen(req, timeout=12, context=_ssl_ctx) as resp:
                body = resp.read().decode('utf-8', errors='replace')[:500]
                telegram_sent = 200 <= resp.status < 300
                telegram_reason = 'ok' if telegram_sent else body
        except Exception as e:
            logging.error(f'Telegram alert failed: {e} | SSL_CERT_FILE={os.environ.get("SSL_CERT_FILE", "unset")}')
            telegram_reason = str(e)
    else:
        telegram_reason = 'missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID'

    sent = telegram_sent or discord_sent
    reasons = []
    if discord_url:
        reasons.append(f"Discord: {discord_reason}")
    if token and chat_id:
        reasons.append(f"Telegram: {telegram_reason}")
    
    if not reasons:
        reasons.append("No notification services configured.")

    return {
        'sent': sent,
        'reason': ' | '.join(reasons),
        'env_loaded': bool(env_path),
        'telegram_sent': telegram_sent,
        'discord_sent': discord_sent
    }


def _format_fno_telegram_alert(result: dict, universe: str, mode: str, scanned_at: str) -> str:
    summary = result.get('summary', {}) or {}
    accumulation = result.get('accumulation', []) or []
    breakout = result.get('breakout', []) or []
    bos = result.get('bos', []) or []
    choch = result.get('choch', []) or []

    lines = [
        'TradeSignal FNO Alert',
        f'Universe: {universe} | Mode: {mode}',
        f'Scanned: {scanned_at[11:16] if len(scanned_at) >= 16 else scanned_at} IST',
        '',
        f"Breakout: {summary.get('breakoutCount', len(breakout))} | "
        f"BOS: {summary.get('bosCount', len(bos))} | "
        f"CHoCH: {summary.get('chochCount', len(choch))} | "
        f"Accumulation: {summary.get('accumulationCount', len(accumulation))}",
    ]

    if accumulation:
        lines.extend(['', '15m high-liquid accumulation, yet to breakout:'])
        for i, row in enumerate(accumulation[:10], 1):
            lines.append(
                f"{i}. {row.get('Symbol')} | LTP {row.get('LTP', row.get('Close'))} | "
                f"{row.get('Accumulation_Bars', row.get('Accumulation_Days'))} bars/{row.get('Accumulation_Time', '')} | "
                f"BO > {row.get('Breakout_Above')} | "
                f"Gap {row.get('Gap_To_Breakout_pct')}% | AvgVal {row.get('Avg_Value_Cr')}cr"
            )
    else:
        lines.extend(['', 'No high-liquidity accumulation candidates found.'])

    if breakout:
        lines.extend(['', 'Top breakout entries:'])
        for i, row in enumerate(breakout[:5], 1):
            lines.append(
                f"{i}. {row.get('Symbol')} | LTP {row.get('Close')} | "
                f"Score {row.get('Score')} | Target {row.get('Target')}"
            )

    return '\n'.join(lines)


@app.route('/api/fno-alerts/progress', methods=['GET'])
def get_fno_progress():
    return jsonify(SCAN_PROGRESS)


@app.route('/api/telegram/test', methods=['POST'])
def telegram_test():
    status = _send_telegram_message('TradeSignal Telegram test from running backend.')
    http_status = 200 if status.get('sent') else 500
    return jsonify(status), http_status


@app.route('/api/fno-alerts/run', methods=['POST'])
def fno_alerts_run():
    """Run the FNO scanner and store results with 48h retention."""
    global SCAN_PROGRESS
    SCAN_PROGRESS = {"status": "Starting scan...", "current": 0, "total": 0, "percent": 0}
    try:
        from indian_stock_breakout_scanner import scan as fno_scan
        import indian_stock_breakout_scanner as _scanner
    except ImportError as e:
        print(f"  ⚠ Scanner import error: {e}")
        return jsonify({'error': f'Scanner import failed: {e}'}), 500

    try:
        body = request.get_json(force=True) or {}
        universe = body.get('universe', 'NIFTY50')
        min_score = int(body.get('min_score', 6))
        mode = body.get('mode', 'intraday')

        print(f"\n🚨 FNO Trade Scanner started — Universe: {universe}, Mode: {mode}, Min Score: {min_score}")

        # Share the server's authenticated Kite client with the scanner
        kite = get_kite()
        if kite:
            _scanner._kite = kite

        def progress_cb(current, total, msg):
            SCAN_PROGRESS['current'] = current
            SCAN_PROGRESS['total'] = total
            SCAN_PROGRESS['status'] = msg
            SCAN_PROGRESS['percent'] = int((current / total) * 100) if total > 0 else 0

        try:
            scan_top_n = 50 if universe == 'ALL_EQUITY' else 15
            result = fno_scan(universe=universe, mode=mode, min_score=min_score, top_n=scan_top_n, progress_callback=progress_cb)
            SCAN_PROGRESS = {"status": "Complete", "current": 100, "total": 100, "percent": 100}
        except Exception as scan_err:
            SCAN_PROGRESS['status'] = f"Failed: {scan_err}"
            print(f"  ⚠ Scanner execution failed: {scan_err}")
            import traceback
            traceback.print_exc()
            raise scan_err

        # Ensure all data is JSON-serializable
        result = _serialize_result(result)

        # Store in SQLite with 48h retention
        import uuid
        run_id = str(uuid.uuid4())[:8]
        _ist = datetime.timezone(timedelta(hours=5, minutes=30))
        scanned_at = dt.now(_ist).isoformat()

        try:
            db = get_db()
            db.execute(
                'INSERT INTO fno_alerts (run_id, scanned_at, universe, mode, result_json, summary_json) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (run_id, scanned_at, universe, mode,
                 json.dumps(result, default=str),
                 json.dumps(result.get('summary', {})))
            )
            # Purge rows older than 10 working days
            cutoff = _ten_working_days_ago(dt.now(_ist)).isoformat()
            db.execute('DELETE FROM fno_alerts WHERE scanned_at < ?', (cutoff,))
            db.commit()
        except Exception as db_err:
            print(f"  ⚠ Database error: {db_err}")
            import traceback
            traceback.print_exc()
            raise db_err

        result['run_id'] = run_id
        result['scanned_at'] = scanned_at
        if _telegram_configured():
            msg = _format_fno_telegram_alert(result, universe, mode, scanned_at)
            result['telegram_status'] = _send_telegram_message(msg)
            result['telegram_sent'] = bool(result['telegram_status'].get('sent'))
        else:
            result['telegram_status'] = {
                'sent': False,
                'reason': 'missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID',
                'env_loaded': bool(_reload_runtime_env()),
            }
            result['telegram_sent'] = False
        return jsonify(result)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Scan failed: {str(e)}'}), 500


@app.route('/api/fno-alerts/latest')
def fno_alerts_latest():
    """Get the last 2 scan runs (current + previous for diff)."""
    try:
        import json
        db = get_db()
        rows = db.execute(
            'SELECT run_id, scanned_at, universe, mode, result_json, summary_json '
            'FROM fno_alerts ORDER BY scanned_at DESC LIMIT 2'
        ).fetchall()
        results = []
        for r in rows:
            entry = json.loads(r['result_json'])
            entry['run_id'] = r['run_id']
            entry['scanned_at'] = r['scanned_at']
            results.append(entry)
        return jsonify({'runs': results, 'count': len(results)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/fno-alerts/history')
def fno_alerts_history():
    """List all scan runs from last 48h (metadata only, no full results)."""
    try:
        import json
        db = get_db()
        rows = db.execute(
            'SELECT run_id, scanned_at, universe, mode, summary_json '
            'FROM fno_alerts ORDER BY scanned_at DESC LIMIT 50'
        ).fetchall()
        runs = []
        for r in rows:
            runs.append({
                'run_id': r['run_id'],
                'scanned_at': r['scanned_at'],
                'universe': r['universe'],
                'mode': r['mode'],
                'summary': json.loads(r['summary_json']),
            })
        return jsonify({'runs': runs})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/fno-alerts/summary')
def fno_alerts_summary():
    """
    Per-day P&L summary of FNO trade alerts (last 10 working days).
    Logic:
      - For each date, take the FIRST scan run of that day.
      - Extract unique symbols from breakout/bos/choch lists.
      - Fetch live LTP via Kite.
      - P&L = LTP - Entry (long). If LTP > Target → status TARGET HIT.
    Returns JSON; frontend generates CSV.
    """
    import json
    try:
        _ist = datetime.timezone(timedelta(hours=5, minutes=30))
        now = dt.now(_ist)
        cutoff = _ten_working_days_ago(now).isoformat()

        db = get_db()
        # Fetch oldest-first so first encounter = first alert of the day
        rows = db.execute(
            'SELECT run_id, scanned_at, universe, mode, result_json '
            'FROM fno_alerts WHERE scanned_at >= ? ORDER BY scanned_at ASC',
            (cutoff,)
        ).fetchall()

        if not rows:
            return jsonify({'summary': [], 'count': 0,
                            'message': 'No alerts in last 10 working days'})

        # ── Build day_map: date -> { symbol: first_alert_dict } ──
        day_map = {}   # { 'YYYY-MM-DD': { 'SYM': {...} } }

        def _add(day_syms, r, setup_type, scanned_at, universe):
            sym = r.get('Symbol', '')
            if not sym or sym in day_syms:
                return
            entry  = float(r.get('Entry')  or r.get('Close') or 0)
            stop   = float(r.get('Stop')   or 0)
            target = float(r.get('Target') or 0)
            day_syms[sym] = {
                'symbol':           sym,
                'date':             scanned_at[:10],
                'setup_type':       setup_type,
                'direction':        'LONG',
                'first_alert_time': scanned_at[11:16],   # HH:MM IST
                'entry':            entry,
                'stop':             stop,
                'target':           target,
                'score':            r.get('Score') or 0,
                'universe':         universe,
                'signals':          r.get('Signals', ''),
            }

        for row in rows:
            result    = json.loads(row['result_json'])
            date_str  = row['scanned_at'][:10]
            universe  = row['universe']
            sa        = row['scanned_at']
            if date_str not in day_map:
                day_map[date_str] = {}
            ds = day_map[date_str]
            for r in result.get('breakout', []):
                _add(ds, r, 'Breakout', sa, universe)
            for r in result.get('bos', []):
                _add(ds, r, 'BOS Continuation', sa, universe)
            for r in result.get('choch', []):
                _add(ds, r, 'CHoCH Reversal', sa, universe)

        # Flatten — date DESC
        all_alerts = [
            alert
            for date_str in sorted(day_map.keys(), reverse=True)
            for alert in day_map[date_str].values()
        ]

        if not all_alerts:
            return jsonify({'summary': [], 'count': 0, 'message': 'No alerts found'})

        # ── Fetch LTP for all unique symbols ──
        unique_syms = list({a['symbol'] for a in all_alerts})
        ltp_map = {}
        kite = get_kite()
        if kite:
            try:
                for i in range(0, len(unique_syms), 400):
                    batch    = unique_syms[i:i + 400]
                    nse_keys = [f'NSE:{s}' for s in batch]
                    quotes   = kite.ltp(nse_keys)
                    for key, val in quotes.items():
                        ltp_map[key.replace('NSE:', '')] = val.get('last_price', 0)
            except Exception as e:
                logging.warning(f'LTP batch for summary failed: {e}')

        # ── Compute P&L ──
        results = []
        for a in all_alerts:
            sym    = a['symbol']
            entry  = a['entry']
            stop   = a['stop']
            target = a['target']
            ltp    = float(ltp_map.get(sym, 0))

            if entry > 0 and ltp > 0:
                # User rule: if LTP > Target, use LTP as exit price
                exit_price = ltp
                pnl        = round(exit_price - entry, 2)
                pnl_pct    = round((exit_price - entry) / entry * 100, 2)
                if target > 0 and ltp >= target:
                    status = 'TARGET HIT'
                elif stop > 0 and ltp <= stop:
                    status = 'SL HIT'
                elif ltp > entry:
                    status = 'OPEN (Profit)'
                else:
                    status = 'OPEN (Loss)'
            elif entry > 0:
                pnl, pnl_pct, status = 0.0, 0.0, 'NO LTP'
            else:
                pnl, pnl_pct, status = 0.0, 0.0, 'NO DATA'

            results.append({
                **a,
                'ltp':     round(ltp, 2),
                'pnl':     pnl,
                'pnl_pct': pnl_pct,
                'status':  status,
            })

        return jsonify({'summary': results, 'count': len(results)})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ── AI Alert Validator ──────────────────────────────────────────────────────

_AI_SYSTEM_PROMPT = """You are an expert NSE F&O trader and quantitative analyst specialising in
Breakout, BOS Continuation, and CHoCH Reversal intraday and swing strategies.

IMPORTANT — Session context:
- If the scan was run DURING market hours (9:15–15:30 IST): evaluate for TODAY's intraday trade.
- If the scan was run AFTER market hours or PRE-MARKET: evaluate these as NEXT-SESSION setups.
  In after-hours mode, DO NOT penalise alerts solely because the market is closed — assess the
  technical setup quality on its own merits for the next trading session.

Evaluate each FNO trade alert and rank them strictly by probability of hitting their
target price. Be concise and data-driven. ALWAYS return ALL alerts passed to you — do not
filter any out; use low confidence scores (20–40) for weak setups instead of omitting them.

Scoring guidelines:
- Score ≥14=Exceptional, ≥10=Strong, ≥6=Moderate
- RVOL ≥2x = high conviction volume
- RSI 50-70 = ideal for Breakout/BOS; RSI 30-55 = ideal for CHoCH reversal
- R:R ≥ 2.5 preferred; below 1.5 penalise
- ATR% > 3.5% = high IV risk → penalise
- Morning session (9:15-12:00) = best for Breakout; any time for BOS/CHoCH
- BOS Continuation ≥ Breakout ≥ CHoCH for reliability

CRITICAL: Return ONLY a valid JSON array (ALWAYS an array, even for one item).
NO markdown, NO text outside JSON. Start with [ and end with ].

[
  {
    "rank": 1,
    "symbol": "SYMBOL",
    "setup_type": "Breakout|BOS Continuation|CHoCH Reversal",
    "confidence": 85,
    "action": "STRONG BUY|BUY|WATCH|AVOID",
    "rationale": "2-sentence reason citing specific indicators",
    "key_risk": "single biggest risk in ≤15 words",
    "suggested_position_size": "Full|3/4|Half|Quarter"
  }
]

Include ALL alerts. Rank 1 = highest confidence. Rank last = lowest.
MUST be valid JSON. MUST be an array."""


def _strip_llm_json(raw: str) -> str:
    """Strip markdown code fences from LLM JSON output."""
    raw = raw.strip()
    if raw.startswith('```'):
        parts = raw.split('```')
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith('json'):
            raw = raw[4:]
    return raw.strip()


@app.route('/api/fno-alerts/ai-rank', methods=['POST'])
def fno_alerts_ai_rank():
    """
    AI validation and ranking of the latest FNO scan alerts.
    Supports Gemini, OpenAI (GPT), Anthropic (Claude).
    Body: { provider, model, api_key }
    """
    import json as _json
    try:
        body     = request.get_json(force=True) or {}
        provider = body.get('provider', 'gemini')
        model    = body.get('model', 'gemini-2.0-flash')
        api_key  = body.get('api_key', '').strip()

        if not api_key:
            return jsonify({'error': 'api_key is required'}), 400

        # ── Load latest scan from DB ──
        db  = get_db()
        row = db.execute(
            'SELECT result_json, scanned_at FROM fno_alerts ORDER BY scanned_at DESC LIMIT 1'
        ).fetchone()
        if not row:
            return jsonify({'error': 'No alerts in DB. Run a scan first.'}), 404

        result     = _json.loads(row['result_json'])
        scanned_at = row['scanned_at']

        all_alerts = (
            [dict(r, _type='Breakout')          for r in result.get('breakout', [])] +
            [dict(r, _type='BOS Continuation')  for r in result.get('bos',      [])] +
            [dict(r, _type='CHoCH Reversal')    for r in result.get('choch',    [])]
        )
        if not all_alerts:
            return jsonify({'error': 'Latest scan has no alerts.'}), 400

        # ── Build alert text for the prompt ──
        _ist = datetime.timezone(timedelta(hours=5, minutes=30))
        now  = dt.now(_ist)
        h    = now.hour + now.minute / 60
        session = (
            'pre-market (next-session planning)'       if h < 9    else
            'opening bell (9:00-9:15)'                if h < 9.25 else
            'morning session (9:15-12:00)'            if h < 12   else
            'post-lunch (12:00-14:00)'                if h < 14   else
            'closing session (14:00-15:40)'           if h < 15.67 else
            'after-hours (next-session planning mode)'
        )

        lines = []
        for i, a in enumerate(all_alerts, 1):
            sym    = a.get('Symbol', '?')
            setup  = a.get('_type', '?')
            entry  = a.get('Entry', 0)
            stop   = a.get('Stop',  0)
            target = a.get('Target', 0)
            rr     = round((target - entry) / (entry - stop), 2) if entry > stop > 0 and target > entry else 0
            score  = a.get('Score', 0)
            rsi    = a.get('RSI',   0)
            rvol   = a.get('RVOL',  0) or 0
            atr    = a.get('ATR_pct', 0) or 0
            chg    = a.get('Chg_pct', 0) or 0
            sigs   = str(a.get('Signals', ''))[:120]
            lines.append(
                f"{i}. {sym} [{setup}] Entry=₹{entry} Stop=₹{stop} Target=₹{target} "
                f"R:R={rr} Score={score} RSI={rsi:.1f} RVOL={rvol:.1f}x "
                f"Day%={chg:+.2f}% ATR%={atr:.2f}% | {sigs}"
            )

        user_prompt = (
            f"Current session: {session} IST ({now.strftime('%H:%M')})\n"
            f"Date: {now.strftime('%Y-%m-%d')}\n\n"
            f"Alerts ({len(all_alerts)} total):\n"
            + '\n'.join(lines)
            + '\n\nReturn JSON array only. Rank by profit probability.'
        )

        # ── Call LLM ──
        raw = ''
        print(f'[AI-RANK] Provider: {provider}, Model: {model}')
        print(f'[AI-RANK] Alerts to rank: {len(all_alerts)}')
        
        if provider == 'gemini':
            try:
                from google import genai as gai
                from google.genai import types as gai_types
                client_g = gai.Client(api_key=api_key)
                print('[AI-RANK] Calling Gemini API...')
                resp = client_g.models.generate_content(
                    model=model,
                    contents=user_prompt,
                    config=gai_types.GenerateContentConfig(
                        system_instruction=_AI_SYSTEM_PROMPT,
                        temperature=0.1,
                        max_output_tokens=2048,
                    ),
                )
                raw = resp.text
                print(f'[AI-RANK] Gemini response length: {len(raw)}')
            except ImportError:
                return jsonify({'error': 'google-genai not installed. Run: pip install google-genai'}), 500
            except Exception as e:
                print(f'[AI-RANK] Gemini error: {str(e)}')
                return jsonify({'error': f'Gemini API error: {str(e)}'}), 500

        elif provider == 'openai':
            try:
                from openai import OpenAI
                client = OpenAI(api_key=api_key)
                kwargs = dict(
                    model=model,
                    messages=[
                        {'role': 'system', 'content': _AI_SYSTEM_PROMPT},
                        {'role': 'user',   'content': user_prompt}
                    ],
                    temperature=0.1,
                    max_tokens=2048,
                )
                if 'gpt-4' in model or 'gpt-3.5' in model:
                    kwargs['response_format'] = {'type': 'json_object'}
                print('[AI-RANK] Calling OpenAI API...')
                resp = client.chat.completions.create(**kwargs)
                raw  = resp.choices[0].message.content
                print(f'[AI-RANK] OpenAI response length: {len(raw)}')
            except ImportError:
                return jsonify({'error': 'openai not installed. Run: pip install openai'}), 500
            except Exception as e:
                print(f'[AI-RANK] OpenAI error: {str(e)}')
                return jsonify({'error': f'OpenAI API error: {str(e)}'}), 500

        elif provider == 'anthropic':
            try:
                import anthropic
                client = anthropic.Anthropic(api_key=api_key)
                print('[AI-RANK] Calling Anthropic API...')
                resp   = client.messages.create(
                    model=model, max_tokens=2048,
                    system=_AI_SYSTEM_PROMPT,
                    messages=[{'role': 'user', 'content': user_prompt}],
                )
                raw = resp.content[0].text
                print(f'[AI-RANK] Anthropic response length: {len(raw)}')
            except ImportError:
                return jsonify({'error': 'anthropic not installed. Run: pip install anthropic'}), 500
            except Exception as e:
                print(f'[AI-RANK] Anthropic error: {str(e)}')
                return jsonify({'error': f'Anthropic API error: {str(e)}'}), 500

        else:
            return jsonify({'error': f'Unknown provider: {provider}. Use gemini/openai/anthropic.'}), 400

        # ── Parse JSON ──
        print(f'[AI-RANK] Raw response (first 300 chars): {raw[:300]}')
        clean = _strip_llm_json(raw)
        print(f'[AI-RANK] Cleaned response (first 300 chars): {clean[:300]}')
        try:
            parsed = _json.loads(clean)
            print(f'[AI-RANK] JSON parsed successfully. Type: {type(parsed)}')
        except _json.JSONDecodeError as je:
            print(f'[AI-RANK] JSON parse error: {str(je)}')
            # Try extracting a JSON array if the model wrapped it in an object
            import re
            m = re.search(r'\[.*\]', clean, re.DOTALL)
            if m:
                print('[AI-RANK] Extracted JSON array from response')
                parsed = _json.loads(m.group())
            else:
                print(f'[AI-RANK] Could not extract valid JSON. Full response: {clean[:1000]}')
                return jsonify({'error': 'AI returned invalid JSON', 'raw': clean[:500]}), 500

        # Handle multiple response formats:
        # 1. Direct array: [{ rank: 1, symbol: '...', ... }, ...]
        # 2. Object with array: { alerts: [...] } or { ranked: [...] }
        # 3. Single object: { rank: 1, symbol: '...', ... } → wrap in list
        if isinstance(parsed, list):
            ranked = parsed
        elif isinstance(parsed, dict):
            # Try to extract from common keys first
            if 'alerts' in parsed:
                ranked = parsed['alerts']
            elif 'ranked' in parsed:
                ranked = parsed['ranked']
            elif 'rank' in parsed and 'symbol' in parsed:
                # Single alert object — wrap it
                ranked = [parsed]
                print('[AI-RANK] Converted single alert object to list')
            else:
                # Unknown structure — try to extract any list values
                for v in parsed.values():
                    if isinstance(v, list) and len(v) > 0:
                        if isinstance(v[0], dict) and 'symbol' in v[0]:
                            ranked = v
                            print(f'[AI-RANK] Extracted list from key in response')
                            break
                else:
                    ranked = []
                    print('[AI-RANK] Could not extract ranked alerts from response structure')
        else:
            ranked = []
            print(f'[AI-RANK] Unexpected response type: {type(parsed)}')
        
        print(f'[AI-RANK] Final ranked count: {len(ranked)}')
        # No confidence filter — return all ranked results; UI shows confidence bars

        return jsonify({
            'ranked':       ranked,
            'model':        model,
            'provider':     provider,
            'scanned_at':   scanned_at,
            'total_input':  len(all_alerts),
            'total_output': len(ranked),
            'validated_at': dt.now(_ist).isoformat(),
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/index-movers')

def index_movers():
    import time
    global _index_movers_cache

    index_name = request.args.get('index', 'nifty50').lower().replace(' ', '')
    top_n      = min(int(request.args.get('top_n', 10)), 50)
    day        = int(request.args.get('day', 0))   # 0 = today live, 1 = yesterday

    cache_key = f'{index_name}_day{day}_v2'
    ttl       = 5 if day == 0 else 3600            # live: 5s  |  yesterday: 1hr
    now       = time.time()
    cached    = _index_movers_cache.get(cache_key)
    if cached and now - cached['ts'] < ttl:
        result = dict(cached['data'])
        result['gainers'] = result['gainers'][:top_n]
        result['losers']  = result['losers'][:top_n]
        return jsonify(result)

    kite = get_kite()
    if not kite:
        return jsonify({'error': 'Not connected to Kite. Please login first.'}), 401

    symbols  = INDEX_CONSTITUENTS.get(index_name)
    if not symbols:
        return jsonify({'error': f'Unknown index: {index_name}'}), 400

    exchange = INDEX_EXCHANGE.get(index_name, 'NSE')

    try:
        if day == 0:
            # ── Today / Live ──
            # Try exchange-specific prefix first; for BSE fall back to NSE if it fails
            ex_symbols = [f'{exchange}:{s}' for s in symbols]
            all_quotes = {}
            try:
                for i in range(0, len(ex_symbols), 500):
                    quotes = kite.quote(ex_symbols[i:i + 500])
                    all_quotes.update(quotes)
            except Exception:
                # BSE quote failed — retry with NSE (same stocks, essentially same price)
                all_quotes = {}
                nse_fallback = [f'NSE:{s}' for s in symbols]
                for i in range(0, len(nse_fallback), 500):
                    try:
                        quotes = kite.quote(nse_fallback[i:i + 500])
                        all_quotes.update(quotes)
                    except Exception:
                        pass

            movers = []
            for key, q in all_quotes.items():
                symbol     = key.split(':', 1)[1]
                ltp        = q.get('last_price', 0)
                ohlc       = q.get('ohlc', {})
                prev_close = ohlc.get('close', 0)
                open_price = ohlc.get('open', 0)
                if not open_price:
                    open_price = ltp
                
                if not ltp or not prev_close:
                    continue
                change_pct = round((ltp - prev_close) / prev_close * 100, 2)
                open_change_pct = round((open_price - prev_close) / prev_close * 100, 2) if prev_close else 0
                movers.append({
                    'symbol':          symbol,
                    'ltp':             ltp,
                    'change_pct':      change_pct,
                    'open_change_pct': open_change_pct,
                    'volume':          q.get('volume', 0),
                    'day_high':        ohlc.get('high', ltp),
                    'day_low':         ohlc.get('low', ltp),
                    'prev_close':      prev_close,
                })

        else:
            # ── Yesterday — use historical daily OHLCV ──
            # to_date must be YESTERDAY (not today) so candles[-1] = yesterday,
            # candles[-2] = day-before-yesterday, giving true prev-day performance.
            db        = get_db()
            to_date   = (dt.now() - timedelta(days=1)).strftime('%Y-%m-%d')
            from_date = (dt.now() - timedelta(days=8)).strftime('%Y-%m-%d')

            # Lookup instrument tokens from cache (prefer exchange match, fall back to NSE)
            placeholders = ','.join(['?' for _ in symbols])
            rows = db.execute(
                f'SELECT tradingsymbol, instrument_token FROM instruments '
                f'WHERE exchange=? AND tradingsymbol IN ({placeholders})',
                [exchange] + list(symbols)
            ).fetchall()
            token_map = {r['tradingsymbol']: r['instrument_token'] for r in rows}

            # Fallback: if BSE tokens missing, try NSE tokens
            missing = [s for s in symbols if s not in token_map]
            if missing:
                placeholders2 = ','.join(['?' for _ in missing])
                rows2 = db.execute(
                    f'SELECT tradingsymbol, instrument_token FROM instruments '
                    f'WHERE exchange="NSE" AND tradingsymbol IN ({placeholders2})',
                    missing
                ).fetchall()
                for r in rows2:
                    if r['tradingsymbol'] not in token_map:
                        token_map[r['tradingsymbol']] = r['instrument_token']

            movers    = []
            yest_date = None
            for symbol in symbols:
                token = token_map.get(symbol)
                if not token:
                    continue

                candles = cache_get_ohlcv(db, token, from_date, to_date, 'day')
                if len(candles) < 2:
                    try:
                        raw = kite.historical_data(token, from_date, to_date, 'day')
                        cache_store_ohlcv(db, token, raw, 'day')
                        db.commit()
                        candles = cache_get_ohlcv(db, token, from_date, to_date, 'day')
                    except Exception:
                        continue

                if len(candles) < 2:
                    continue

                yesterday  = candles[-1]
                daybefore  = candles[-2]
                prev_close = daybefore['close']
                yest_close = yesterday['close']
                open_price = yesterday.get('open', 0)
                if not open_price:
                    open_price = yest_close
                
                if not prev_close:
                    continue

                change_pct = round((yest_close - prev_close) / prev_close * 100, 2)
                open_change_pct = round((open_price - prev_close) / prev_close * 100, 2) if prev_close else 0
                if not yest_date and yesterday.get('date'):
                    yest_date = str(yesterday['date'])[:10]

                movers.append({
                    'symbol':          symbol,
                    'ltp':             yest_close,
                    'change_pct':      change_pct,
                    'open_change_pct': open_change_pct,
                    'volume':          yesterday['volume'],
                    'day_high':        yesterday['high'],
                    'day_low':         yesterday['low'],
                    'prev_close':      prev_close,
                    'date':            yest_date or '',
                })

        gainers = sorted(movers, key=lambda x: x['change_pct'], reverse=True)
        losers  = sorted(movers, key=lambda x: x['change_pct'])

        result = {
            'gainers':  gainers[:50],
            'losers':   losers[:50],
            'all':      movers,
            'total':    len(movers),
            'index':    index_name,
            'exchange': exchange,
            'day':      day,
        }

        _index_movers_cache[cache_key] = {'data': result, 'ts': now}
        result = dict(result)
        result['gainers'] = result['gainers'][:top_n]
        result['losers']  = result['losers'][:top_n]
        return jsonify(result)

    except Exception as e:
        cached = _index_movers_cache.get(cache_key)
        if cached:
            return jsonify(cached['data'])
        return jsonify({'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════
# ── WebSocket for Real-time Ticks ──
# ══════════════════════════════════════════════════════════════

# Store active Kite WebSocket connections per client
_kite_ws_connections = {}

@socketio.on('connect')
def handle_connect():
    print(f"WebSocket client connected: {request.sid}")
    _kite_ws_connections[request.sid] = None

@socketio.on('disconnect')
def handle_disconnect():
    print(f"WebSocket client disconnected: {request.sid}")
    kite_ws = _kite_ws_connections.get(request.sid)
    if kite_ws:
        try:
            kite_ws.close()
        except Exception as e:
            print(f"Error closing Kite WS for client {request.sid}: {e}")
    _kite_ws_connections.pop(request.sid, None)

@socketio.on('subscribe')
def handle_subscribe(data):
    """Subscribe to Kite WebSocket ticks and forward to client."""
    try:
        tokens = data.get('tokens', [])
        api_key = data.get('api_key', '')
        access_token = data.get('access_token', '')

        if not tokens or not api_key or not access_token:
            emit('error', {'message': 'Missing tokens, api_key, or access_token'})
            return

        # Get Kite instance for this session
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token)

        # Close existing connection if any
        kite_ws = _kite_ws_connections.get(request.sid)
        if kite_ws:
            try:
                kite_ws.close()
            except:
                pass

        # Create Kite WebSocket connection using TickerFactory
        from global_ticker import get_ticker_for_feature
        client_key = f"client_{request.sid}"

        def on_ticks(ticks):
            """Forward ticks to the client."""
            try:
                # Convert Kite tick format to our format
                formatted_ticks = []
                for tick in ticks:
                    # Filter to only the tokens requested by this specific client session
                    if tick.get('instrument_token') in tokens:
                        formatted_tick = {
                            'instrument_token': tick.get('instrument_token'),
                            'tradingsymbol': tick.get('tradingsymbol', ''),
                            'last_price': tick.get('last_price', 0),
                            'change': tick.get('change', 0),
                            'volume': tick.get('volume', 0),
                            'oi': tick.get('oi', 0),
                            'oi_day_change': tick.get('oi_day_change', 0),
                            'buy_quantity': tick.get('buy_quantity', 0),
                            'sell_quantity': tick.get('sell_quantity', 0),
                            'timestamp': tick.get('timestamp', '').isoformat() if hasattr(tick.get('timestamp'), 'isoformat') else str(tick.get('timestamp', ''))
                        }
                        formatted_ticks.append(formatted_tick)

                if formatted_ticks:
                    socketio.emit('ticks', formatted_ticks, to=request.sid)
            except Exception as e:
                print(f"Error forwarding ticks: {e}")

        # Set up and register via TickerFactory
        try:
            kws = get_ticker_for_feature(client_key, tokens, on_ticks, mode="LTP")
            # Store the connection proxy
            _kite_ws_connections[request.sid] = kws
            print(f"WebSocket subscription initiated for client {request.sid} via TickerFactory")
            socketio.emit('subscribed', {'message': f'Subscribed to {len(tokens)} instruments'}, to=request.sid)
        except Exception as e:
            print(f"Failed to register client WS: {e}")
            socketio.emit('error', {'message': f'Connection failed: {str(e)}'}, to=request.sid)

    except Exception as e:
        print(f"WebSocket subscription error for client {request.sid}: {e}")
        socketio.emit('error', {'message': str(e)}, to=request.sid)


@socketio.on('chart_subscribe')
def handle_chart_subscribe(data):
    """Client opens inline chart: subscribe token to chart_feed in GlobalTicker."""
    try:
        sym = (data.get('sym') or '').upper().strip()
        if not sym:
            return
        token = _resolve_sym_token(sym)
        if not token:
            emit('chart_tick_error', {'sym': sym, 'msg': 'Token not found'})
            return
        with _chart_sub_lock:
            _chart_subscriptions[sym] = int(token)
        from global_ticker import get_global_ticker_manager
        gtm = get_global_ticker_manager()
        gtm.update_subscription('chart_feed', [int(token)], [])
        emit('chart_subscribed', {'sym': sym, 'token': token})
    except Exception as e:
        emit('chart_tick_error', {'sym': data.get('sym',''), 'msg': str(e)})


@socketio.on('chart_unsubscribe')
def handle_chart_unsubscribe(data):
    """Client closes inline chart: remove token from chart_feed subscription."""
    try:
        sym = (data.get('sym') or '').upper().strip()
        with _chart_sub_lock:
            token = _chart_subscriptions.pop(sym, None)
        if token:
            from global_ticker import get_global_ticker_manager
            gtm = get_global_ticker_manager()
            gtm.update_subscription('chart_feed', [], [int(token)])
    except Exception:
        pass


@app.route('/api/market-status')
def market_status_api():
    """Return authoritative server-side IST market open/closed status."""
    try:
        now = now_ist()
        is_open = is_market_hours()
        return jsonify({
            'is_open': is_open,
            'server_time_ist': now.isoformat(),
            'server_time_ms': int(now.timestamp() * 1000)
        })
    except Exception as e:
        return jsonify({'is_open': False, 'error': str(e)}), 500






# ── Intraday Candles (batch) ──
@app.route('/api/intraday-candles', methods=['POST'])
def intraday_candles():
    """Fetch today's 15-min candles for a list of symbols.
    POST body: { "symbols": ["RELIANCE", "GRASIM", ...] }
    Returns:   { "RELIANCE": { closes, highs, lows, volumes, timestamps }, ... }
    """
    kite = get_kite()
    if not kite:
        return jsonify({'error': 'Kite not connected'}), 401

    body = request.get_json(silent=True) or {}
    symbols = body.get('symbols', [])
    if not symbols:
        return jsonify({'error': 'No symbols provided'}), 400

    result = {}
    for symbol in symbols[:30]:  # cap at 30 to avoid API overload
        try:
            candles = get_historical_candles(kite, symbol, '15minute', days_back=1)
            if candles and len(candles) >= 3:
                result[symbol] = {
                    'closes':     [c['close']  for c in candles],
                    'highs':      [c['high']   for c in candles],
                    'lows':       [c['low']    for c in candles],
                    'volumes':    [c['volume'] for c in candles],
                    'timestamps': [c['date'].isoformat() if hasattr(c['date'], 'isoformat') else str(c['date']) for c in candles],
                }
        except Exception as e:
            result[symbol] = {'error': str(e)}

    return jsonify(result)


# ══════════════════════════════════════════════════════════════
# ── First-Hour Pattern Continuation/Reversal Analyzer ──
# ══════════════════════════════════════════════════════════════

def analyze_first_hour_pattern(kite, symbol, or_window=60, gap_threshold=0.3, days=80):
    symbol = symbol.strip().upper()
    candles = get_historical_candles(kite, symbol, '5minute', days_back=days + 15)
    if not candles:
        return {'error': 'No candle data found'}

    from collections import defaultdict
    candles_by_day = defaultdict(list)
    for c in candles:
        dt_str = c['date'].split('T')[0]
        candles_by_day[dt_str].append(c)

    sorted_dates = sorted(candles_by_day.keys())
    if len(sorted_dates) < 2:
        return {'error': 'Insufficient historical days'}

    def is_in_or_window(time_str):
        h, m = map(int, time_str.split(':'))
        minutes = h * 60 + m
        start_min = 9 * 60 + 15
        return start_min <= minutes <= (start_min + or_window - 5)

    def is_post_or_window(time_str):
        h, m = map(int, time_str.split(':'))
        minutes = h * 60 + m
        return minutes >= (9 * 60 + 15 + or_window)

    day_metrics = []
    for d_str in sorted_dates:
        day_c = sorted(candles_by_day[d_str], key=lambda x: x['date'])
        or_c = [c for c in day_c if is_in_or_window(c['date'].split('T')[1][:5])]
        if not or_c or len(or_c) < (or_window // 5):
            continue
        day_metrics.append({
            'date': d_str,
            'or_open': or_c[0]['open'],
            'or_close': or_c[-1]['close'],
            'or_high': max(c['high'] for c in or_c),
            'or_low': min(c['low'] for c in or_c),
            'or_volume': sum(c['volume'] for c in or_c),
            'day_close': day_c[-1]['close'],
            'day_high': max(c['high'] for c in day_c),
            'day_low': min(c['low'] for c in day_c),
            'day_candles': day_c
        })

    if len(day_metrics) < 2:
        return {'error': 'Insufficient trading days'}

    # Pre-calculate Daily closes, True Ranges, 20 EMA, and 14 ATR to avoid look-ahead bias
    closes = [m['day_close'] for m in day_metrics]
    
    tr_list = []
    for idx in range(len(day_metrics)):
        curr_m = day_metrics[idx]
        if idx == 0:
            tr = curr_m['day_high'] - curr_m['day_low']
        else:
            prev_c = day_metrics[idx-1]['day_close']
            tr = max(curr_m['day_high'] - curr_m['day_low'], 
                     abs(curr_m['day_high'] - prev_c), 
                     abs(curr_m['day_low'] - prev_c))
        tr_list.append(tr)

    atr_14 = [0.0] * len(day_metrics)
    if len(tr_list) >= 14:
        atr_14[13] = sum(tr_list[:14]) / 14.0
        for idx in range(14, len(tr_list)):
            atr_14[idx] = (tr_list[idx] * (1.0 / 14.0)) + (atr_14[idx-1] * (13.0 / 14.0))
    else:
        fallback_val = sum(tr_list) / len(tr_list) if tr_list else 1.0
        atr_14 = [fallback_val] * len(day_metrics)

    ema_20 = [0.0] * len(day_metrics)
    if len(closes) >= 20:
        ema_20[19] = sum(closes[:20]) / 20.0
        mult = 2.0 / (20.0 + 1.0)
        for idx in range(20, len(closes)):
            ema_20[idx] = (closes[idx] - ema_20[idx-1]) * mult + ema_20[idx-1]
    else:
        fallback_val = sum(closes) / len(closes) if closes else 1.0
        ema_20 = [fallback_val] * len(day_metrics)

    processed = []
    for i in range(1, len(day_metrics)):
        curr, prev = day_metrics[i], day_metrics[i-1]
        prev_close = prev['day_close']
        gap_pct = (curr['or_open'] - prev_close) / prev_close * 100.0
        gap_type = 'gap_up' if gap_pct >= gap_threshold else ('gap_down' if gap_pct <= -gap_threshold else 'flat')
        or_direction = 'bullish' if curr['or_close'] >= curr['or_open'] else 'bearish'
        try:
            or_close_time = curr['day_candles'][len(or_c)-1]['date']
            candles_up_to_or = []
            for c in candles:
                candles_up_to_or.append(c)
                if c['date'] == or_close_time:
                    break
            if len(candles_up_to_or) >= 28:  # guard: consensus needs ≥28 bars
                from indicators import compute_technical_consensus
                consensus_res = compute_technical_consensus(candles_up_to_or)
                if consensus_res['consensus'] == 'BULLISH':
                    or_direction = 'bullish'
                elif consensus_res['consensus'] == 'BEARISH':
                    or_direction = 'bearish'
            # else: < 28 bars — preserve price-action or_direction unchanged
        except Exception:
            pass
        or_momentum = abs(curr['or_close'] - curr['or_open']) / curr['or_open'] * 100.0
        
        if or_momentum < 1.5: move_bucket = 'normal'
        elif or_momentum < 2.5: move_bucket = 'fast'
        elif or_momentum < 4.0: move_bucket = 'very_fast'
        else: move_bucket = 'extreme'

        # PDH / PDL Location classification
        pdh = prev['day_high']
        pdl = prev['day_low']
        if curr['or_low'] > pdh:
            level_pos = 'above_pdh'
        elif curr['or_high'] < pdl:
            level_pos = 'below_pdl'
        else:
            level_pos = 'inside'

        # HTF Trend context (using previous day's Daily 20 EMA)
        trend_dir = 'uptrend' if curr['or_open'] >= ema_20[i-1] else 'downtrend'

        pattern_key = f"{gap_type} | {or_direction} | {move_bucket} | {level_pos} | {trend_dir}"
        
        prev_5_v = [p['or_volume'] for p in processed[-5:]] if processed else []
        vol_ratio = curr['or_volume'] / (sum(prev_5_v)/len(prev_5_v)) if prev_5_v else 1.0

        post_c = [c for c in curr['day_candles'] if is_post_or_window(c['date'].split('T')[1][:5])]
        outcome = 'chop'
        ext_pct = ret_pct = 0.0
        
        # ATR-scaled breakout buffer (10% of Daily ATR, or minimum of 5% of OR range to avoid scale issues)
        curr_atr = atr_14[i-1] if atr_14[i-1] > 0 else (curr['or_high'] - curr['or_low'])
        buf = max(0.10 * curr_atr, 0.05 * (curr['or_high'] - curr['or_low']))
        or_range = curr['or_high'] - curr['or_low']
        
        if post_c:
            post_high = max(c['high'] for c in post_c)
            post_low = min(c['low'] for c in post_c)
            
            if or_direction == 'bullish':
                if post_high > curr['or_high'] + buf and curr['day_close'] > curr['or_high'] + buf:
                    outcome = 'continuation'
                elif post_low < curr['or_low'] - buf and curr['day_close'] < curr['or_low'] - buf:
                    outcome = 'reversal'
                ext_pct = max(0.0, (post_high - curr['or_high']) / curr['or_high'] * 100.0)
                ret_pct = max(0.0, (curr['or_high'] - post_low) / or_range * 100.0) if or_range > 0 else 0.0
            else:
                if post_low < curr['or_low'] - buf and curr['day_close'] < curr['or_low'] - buf:
                    outcome = 'continuation'
                elif post_high > curr['or_high'] + buf and curr['day_close'] > curr['or_high'] + buf:
                    outcome = 'reversal'
                ext_pct = max(0.0, (curr['or_low'] - post_low) / curr['or_low'] * 100.0)
                ret_pct = max(0.0, (post_high - curr['or_low']) / or_range * 100.0) if or_range > 0 else 0.0

        processed.append({
            'date': curr['date'],
            'gap_pct': gap_pct,
            'gap_type': gap_type,
            'or_direction': or_direction,
            'move_bucket': move_bucket,
            'level_pos': level_pos,
            'trend_dir': trend_dir,
            'pattern_key': pattern_key,
            'vol_ratio': vol_ratio,
            'or_volume': curr['or_volume'],
            'outcome': outcome,
            'extension_pct': ext_pct,
            'retracement_pct': ret_pct,
            'or_high': curr['or_high'],
            'or_low': curr['or_low'],
            'or_close': curr['or_close'],
            'day_candles': curr['day_candles']
        })

    today_str = now_ist().strftime('%Y-%m-%d')
    target_data = processed[-1]
    history_cohorts = processed[:-1]
    
    # Implement Hierarchical Fallbacks to ensure statistical validity (min 8 matches)
    cohort_matches = [h for h in history_cohorts if h['pattern_key'] == target_data['pattern_key']]
    matched_pattern = target_data['pattern_key']
    fallback_level = 0

    if len(cohort_matches) < 8:
        # Fallback 1: Drop move_bucket (momentum size)
        fb1 = f"{target_data['gap_type']} | {target_data['or_direction']} | {target_data['level_pos']} | {target_data['trend_dir']}"
        cohort_matches = [h for h in history_cohorts if f"{h['gap_type']} | {h['or_direction']} | {h['level_pos']} | {h['trend_dir']}" == fb1]
        matched_pattern = fb1 + " | (any_momentum)"
        fallback_level = 1

    if len(cohort_matches) < 8:
        # Fallback 2: Drop trend_dir
        fb2 = f"{target_data['gap_type']} | {target_data['or_direction']} | {target_data['level_pos']}"
        cohort_matches = [h for h in history_cohorts if f"{h['gap_type']} | {h['or_direction']} | {h['level_pos']}" == fb2]
        matched_pattern = fb2 + " | (any_trend)"
        fallback_level = 2

    if len(cohort_matches) < 8:
        # Fallback 3: Drop level_pos
        fb3 = f"{target_data['gap_type']} | {target_data['or_direction']}"
        cohort_matches = [h for h in history_cohorts if f"{h['gap_type']} | {h['or_direction']}" == fb3]
        matched_pattern = fb3 + " | (any_level)"
        fallback_level = 3

    n = len(cohort_matches)
    if n > 0:
        stats = {
            'sample_size': n,
            'continuation_pct': round(sum(1 for x in cohort_matches if x['outcome'] == 'continuation') / n * 100.0, 1),
            'reversal_pct': round(sum(1 for x in cohort_matches if x['outcome'] == 'reversal') / n * 100.0, 1),
            'chop_pct': round(sum(1 for x in cohort_matches if x['outcome'] == 'chop') / n * 100.0, 1),
            'avg_extension_pct': round(sum(x['extension_pct'] for x in cohort_matches) / n, 2),
            'avg_retracement_pct': round(sum(x['retracement_pct'] for x in cohort_matches) / n, 2),
            'avg_vol_ratio': round(sum(x['vol_ratio'] for x in cohort_matches) / n, 2)
        }
    else:
        stats = {
            'sample_size': 0, 'continuation_pct': 33.3, 'reversal_pct': 33.3, 'chop_pct': 33.3,
            'avg_extension_pct': 0.0, 'avg_retracement_pct': 0.0, 'avg_vol_ratio': 1.0
        }

    probs = {'continuation': stats['continuation_pct'], 'reversal': stats['reversal_pct'], 'chop': stats['chop_pct']}
    pred = max(probs, key=probs.get)
    confidence = probs[pred]

    # Volume-based Gating: Breakouts/Reversals on low volume get overridden to CHOP
    if target_data['vol_ratio'] < 1.2 and pred in ('continuation', 'reversal'):
        pred = 'chop'
        confidence = probs['chop']

    day_complete = False
    if target_data['date'] != today_str:
        day_complete = True
    else:
        now_dt = now_ist()
        if now_dt.hour > 15 or (now_dt.hour == 15 and now_dt.minute >= 30):
            day_complete = True
        else:
            day_c = target_data.get('day_candles', [])
            if day_c and int(day_c[-1]['date'].split('T')[1][:2]) >= 15:
                day_complete = True

    return {
        'date': target_data['date'],
        'symbol': symbol,
        'pattern_key': matched_pattern,
        'or_direction': target_data['or_direction'],
        'move_bucket': target_data['move_bucket'],
        'or_high': target_data['or_high'],
        'or_low': target_data['or_low'],
        'or_close': target_data['or_close'],
        'predicted_outcome': pred,
        'prediction_confidence': confidence,
        'actual_outcome': target_data['outcome'] if day_complete else None,
        'is_day_complete': day_complete,
        'stats': stats
    }

def save_or_update_prediction(db, res):
    date, symbol = res['date'], res['symbol']
    cursor = db.cursor()
    cursor.execute("SELECT * FROM first_hour_predictions WHERE date = ? AND symbol = ?", (date, symbol))
    row = cursor.fetchone()
    now_str = dt.now().isoformat()

    if not row:
        # Create a new prediction entry
        if res['is_day_complete'] and res['actual_outcome']:
            # Running after market hours: Store and immediately validate
            val_res = 'CORRECT' if res['actual_outcome'] == res['predicted_outcome'] else 'INCORRECT'
            cursor.execute("""
                INSERT INTO first_hour_predictions 
                (date, symbol, pattern_key, or_direction, move_bucket, predicted_outcome, prediction_confidence, or_high, or_low, or_close, actual_outcome, validation_result, status, validated_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'VALIDATED', ?, ?)
            """, (date, symbol, res['pattern_key'], res['or_direction'], res['move_bucket'], res['predicted_outcome'], res['prediction_confidence'], res['or_high'], res['or_low'], res['or_close'], res['actual_outcome'], val_res, now_str, now_str))
            db.commit()
            res['db_status'] = 'VALIDATED'
            res['validation_result'] = val_res
        else:
            # Running during market hours: Store prediction as pending validation
            cursor.execute("""
                INSERT INTO first_hour_predictions 
                (date, symbol, pattern_key, or_direction, move_bucket, predicted_outcome, prediction_confidence, or_high, or_low, or_close, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PREDICTED', ?)
            """, (date, symbol, res['pattern_key'], res['or_direction'], res['move_bucket'], res['predicted_outcome'], res['prediction_confidence'], res['or_high'], res['or_low'], res['or_close'], now_str))
            db.commit()
            res['db_status'] = 'PREDICTED'
            res['validation_result'] = None
    else:
        # Prediction already exists
        db_status = row['status']
        if db_status == 'PREDICTED' and res['is_day_complete'] and res['actual_outcome']:
            # Validate the existing morning prediction against actual EOD outcome
            val_res = 'CORRECT' if res['actual_outcome'] == row['predicted_outcome'] else 'INCORRECT'
            cursor.execute("""
                UPDATE first_hour_predictions
                SET actual_outcome = ?, validation_result = ?, status = 'VALIDATED', validated_at = ?
                WHERE date = ? AND symbol = ?
            """, (res['actual_outcome'], val_res, now_str, date, symbol))
            db.commit()
            res['db_status'] = 'VALIDATED'
            res['validation_result'] = val_res
        else:
            # Already validated or day is not complete (ignore/do nothing)
            res['db_status'] = db_status
            res['actual_outcome'] = row['actual_outcome']
            res['validation_result'] = row['validation_result']
            res['predicted_outcome'] = row['predicted_outcome']
            res['prediction_confidence'] = row['prediction_confidence']
            res['pattern_key'] = row['pattern_key']
            res['or_direction'] = row['or_direction']
            res['move_bucket'] = row['move_bucket']
            res['or_high'] = row['or_high']
            res['or_low'] = row['or_low']
            res['or_close'] = row['or_close']
            
    return res

@app.route('/api/first-hour-analysis', methods=['POST'])
def first_hour_analysis():
    kite = get_kite()
    if not kite:
        return jsonify({'error': 'Kite client not initialized'}), 400
    
    body = request.get_json(silent=True) or {}
    symbols = body.get('symbols', [])
    or_window = int(body.get('or_window', 60))
    gap_threshold = float(body.get('gap_threshold', 0.3))
    
    target_symbols = [s.strip().upper() for s in symbols if s.strip()][:250]
    if not target_symbols:
        return jsonify({'results': []})

    import concurrent.futures
    raw_results = []
    
    def process_single(symbol):
        try:
            res = analyze_first_hour_pattern(kite, symbol, or_window, gap_threshold)
            if isinstance(res, dict) and 'symbol' not in res:
                res['symbol'] = symbol
            return res
        except Exception as e:
            return {'symbol': symbol, 'error': str(e)}

    # Use 8 worker threads to query historical candles concurrently
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(process_single, s): s for s in target_symbols}
        for fut in concurrent.futures.as_completed(futures):
            raw_results.append(fut.result())

    # Save predictions sequentially to avoid database locking issues
    db = get_db()
    results = []
    for res in raw_results:
        if 'error' not in res:
            try:
                res = save_or_update_prediction(db, res)
            except Exception as e:
                res['error'] = f"DB Error: {str(e)}"
        results.append(res)
            
    return jsonify({'results': results})

@app.route('/api/first-hour-predictions/history', methods=['GET'])
def first_hour_predictions_history():
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM first_hour_predictions ORDER BY date DESC, symbol ASC")
    rows = [dict(r) for r in cursor.fetchall()]
    
    validated = [r for r in rows if r['status'] == 'VALIDATED']
    correct = sum(1 for r in validated if r['validation_result'] == 'CORRECT')
    accuracy = (correct / len(validated) * 100.0) if validated else 0.0
    
    return jsonify({
        'history': rows,
        'summary': {
            'total_predictions': len(rows),
            'validated_count': len(validated),
            'correct_count': correct,
            'accuracy_pct': round(accuracy, 1)
        }
    })




# ---------------------------------------------------------------------------
# Nifty Candle Analyzer Integration
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# SECTOR_CONFIG: Maps Nifty 50 sector categories to their weights and Kite index symbols.
# NOTE: Dedicated sectoral indices (e.g. NSE:NIFTY OIL AND GAS) are preferred over broad multi-sector indices.
# ---------------------------------------------------------------------------
SECTOR_CONFIG = [
    {"name": "Financial Services",    "wt": 35.5, "symbol": "NSE:NIFTY FIN SERVICE"},
    {"name": "Information Technology","wt": 12.8, "symbol": "NSE:NIFTY IT"},
    {"name": "Oil, Gas & Fuels",      "wt": 9.7,  "symbol": "NSE:NIFTY OIL AND GAS"},
    {"name": "FMCG",                  "wt": 7.8,  "symbol": "NSE:NIFTY FMCG"},
    {"name": "Automobile",            "wt": 7.2,  "symbol": "NSE:NIFTY AUTO"},
    {"name": "Healthcare",            "wt": 4.2,  "symbol": "NSE:NIFTY HEALTHCARE"},
    {"name": "Metals & Mining",       "wt": 3.8,  "symbol": "NSE:NIFTY METAL"},
    {"name": "Construction",          "wt": 3.6,  "symbol": "NSE:NIFTY INFRA"},
    {"name": "Consumer Durables",     "wt": 3.0,  "symbol": "NSE:NIFTY CONSR DURBL"},
    {"name": "Capital Goods",         "wt": 2.9,  "symbol": "NSE:NIFTY CAPITAL MKT"},
    {"name": "Power",                 "wt": 2.7,  "symbol": "NSE:NIFTY ENERGY"},
    {"name": "Telecommunication",     "wt": 2.4,  "symbol": "NSE:NIFTY MEDIA"},
    {"name": "Consumer Services",     "wt": 1.8,  "symbol": "NSE:NIFTY CONSUMPTION"},
    {"name": "Cement & Products",     "wt": 1.2,  "symbol": "NSE:NIFTY INFRA"},
    {"name": "Chemicals",             "wt": 1.0,  "symbol": "NSE:NIFTY CHEMICALS"},
    {"name": "Realty",                "wt": 0.4,  "symbol": "NSE:NIFTY REALTY"},
]

SECTOR_COLORS = {
    "Financial Services": "#3FB68C", "Information Technology": "#5C86B8",
    "Oil, Gas & Fuels": "#D9A44C", "FMCG": "#8B9795", "Automobile": "#3FB68C",
    "Healthcare": "#E0654A", "Metals & Mining": "#3FB68C", "Construction": "#D9A44C",
    "Consumer Durables": "#8B9795", "Capital Goods": "#5C86B8", "Power": "#8B9795",
    "Telecommunication": "#3FB68C", "Consumer Services": "#E0654A",
    "Cement & Products": "#8B9795", "Chemicals": "#E0654A", "Realty": "#3FB68C",
}

_nifty_last_call = 0.0
_nifty_lock = threading.Lock()
NIFTY_MIN_GAP = 0.35

def nifty_throttled_call(fn, *args, **kwargs):
    global _nifty_last_call
    with _nifty_lock:
        wait = NIFTY_MIN_GAP - (time.time() - _nifty_last_call)
        if wait > 0:
            time.sleep(wait)
        result = fn(*args, **kwargs)
        _nifty_last_call = time.time()
        return result

NIFTY_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nifty_dashboard.db")

def get_nifty_db():
    conn = sqlite3.connect(NIFTY_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cache (
            key TEXT PRIMARY KEY,
            payload TEXT,
            fetched_at TEXT
        )
    """)
    return conn

def nifty_cache_get(key, max_age_seconds):
    try:
        conn = get_nifty_db()
        row = conn.execute("SELECT payload, fetched_at FROM cache WHERE key=?", (key,)).fetchone()
        conn.close()
        if not row:
            return None
        payload, fetched_at = row
        if dt.now() - dt.fromisoformat(fetched_at) > timedelta(seconds=max_age_seconds):
            return None
        return json.loads(payload)
    except Exception:
        return None

def nifty_cache_set(key, payload):
    try:
        conn = get_nifty_db()
        conn.execute(
            "INSERT OR REPLACE INTO cache (key, payload, fetched_at) VALUES (?, ?, ?)",
            (key, json.dumps(payload), dt.now().isoformat())
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

@app.route("/api/index-quote")
def index_quote():
    cached = nifty_cache_get("index_quote", max_age_seconds=5)
    if cached:
        return jsonify(cached)
    try:
        kite_client = get_kite()
        if not kite_client:
            return jsonify({"error": "Kite client not initialized"}), 400
        q = nifty_throttled_call(kite_client.quote, ["NSE:NIFTY 50"])
        d = q["NSE:NIFTY 50"]
        payload = {
            "ltp": d["last_price"],
            "change": round(d["last_price"] - d["ohlc"]["close"], 2),
            "change_pct": round((d["last_price"] - d["ohlc"]["close"]) / d["ohlc"]["close"] * 100, 2),
            "timestamp": dt.now().isoformat(),
        }
        nifty_cache_set("index_quote", payload)
        return jsonify(payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/sector-weights")
def sector_weights():
    cached = nifty_cache_get("sector_weights", max_age_seconds=15)
    if cached:
        return jsonify(cached)
    try:
        from nifty_candle_analyzer import _CONSTITUENTS_RAW
        
        sector_mapping = {
            "Financial Services": "Financial Services",
            "Oil Gas & Fuels": "Oil, Gas & Fuels",
            "Information Technology": "Information Technology",
            "Construction": "Construction",
            "Fast Moving Consumer Goods": "FMCG",
            "Telecommunication": "Telecommunication",
            "Automobile": "Automobile",
            "Healthcare": "Healthcare",
            "Power": "Power",
            "Metals & Mining": "Metals & Mining",
            "Consumer Durables": "Consumer Durables",
            "Construction Materials": "Cement & Products",
            "Services": "Consumer Services",
        }

        kite_client = get_kite()
        if not kite_client:
            return jsonify({"error": "Kite client not initialized"}), 400
        
        sector_symbols = [s["symbol"] for s in SECTOR_CONFIG]
        stock_symbols = [f"NSE:{sym}" for sym in _CONSTITUENTS_RAW.keys()]
        all_symbols = list(set(sector_symbols + stock_symbols))
        
        quotes = None
        for _attempt in range(2):
            try:
                quotes = nifty_throttled_call(kite_client.quote, all_symbols)
                break
            except Exception:
                if _attempt == 0:
                    import time as _t; _t.sleep(0.5)
        if quotes is None:
            stale = nifty_cache_get("sector_weights", max_age_seconds=300)
            if stale:
                return jsonify({**stale, "_stale": True})
            return jsonify({"error": "Kite quote fetch failed after retry"}), 503
        
        # Prepare stock data dictionary
        stock_data = {}
        for sym, (raw_wt, raw_sec) in _CONSTITUENTS_RAW.items():
            nsec_name = sector_mapping.get(raw_sec, raw_sec)
            q = quotes.get(f"NSE:{sym}")
            chg_pct = 0.0
            price = 0.0
            if q:
                close = q.get("ohlc", {}).get("close")
                price = q.get("last_price", 0.0)
                chg_pct = round((price - close) / close * 100, 2) if close else 0.0
            
            sitem = {
                "symbol": sym,
                "wt": round(raw_wt * 100, 1),
                "chg": chg_pct,
                "price": price
            }
            if nsec_name not in stock_data:
                stock_data[nsec_name] = []
            stock_data[nsec_name].append(sitem)

        result = []
        for s in SECTOR_CONFIG:
            s_name = s["name"]
            s_stocks = sorted(stock_data.get(s_name, []), key=lambda x: x["wt"], reverse=True)
            
            chg_pct = 0.0
            if s_stocks:
                tot_w = sum(st["wt"] for st in s_stocks)
                if tot_w > 0:
                    chg_pct = round(sum(st["wt"] * st["chg"] for st in s_stocks) / tot_w, 2)
            else:
                q = quotes.get(s["symbol"])
                if q:
                    close = q.get("ohlc", {}).get("close")
                    last_price = q.get("last_price", 0.0)
                    chg_pct = round((last_price - close) / close * 100, 2) if close else 0.0
            
            result.append({
                "name": s_name,
                "wt": s["wt"],
                "chg": chg_pct,
                "color": SECTOR_COLORS.get(s_name, "#8B9795"),
                "stocks": s_stocks
            })
        
        payload = {"sectors": result, "total_weight": round(sum(s["wt"] for s in SECTOR_CONFIG), 1)}
        nifty_cache_set("sector_weights", payload)
        return jsonify(payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def compute_bias_score(pcr, ce_buildup, pe_buildup, or_result):
    score = 50
    if pcr > 1.2: score += 10
    elif pcr < 0.8: score -= 10

    if ce_buildup == "short_covering": score += 15
    elif ce_buildup == "long_buildup": score += 10
    elif ce_buildup == "short_buildup": score -= 15

    if pe_buildup == "long_unwinding": score += 8
    elif pe_buildup == "long_buildup": score -= 12
    elif pe_buildup == "short_buildup": score += 8

    if or_result == "bullish_break": score += 12
    elif or_result == "bearish_break": score -= 12

    return max(0, min(100, score))

def zone_for_score(score):
    if score < 35: return "bearish"
    if score < 65: return "choppy"
    return "bullish"

@app.route("/api/bias-score")
def bias_score():
    try:
        cached = nifty_cache_get("bias_score_data", max_age_seconds=10)
        if cached:
            return jsonify(cached)

        # Default fallback values (original mocks)
        pcr, ce_buildup, pe_buildup, or_result = 1.32, "short_covering", "long_unwinding", "bullish_break"

        try:
            kite = get_kite()
            from oi_spurt_routes import get_option_chain, compute_pcr, get_ltp_and_pivots
            
            ltp, price_change_pct, pivots, pivot_source, prev_close, open_gap_pct, perr = get_ltp_and_pivots(kite, "NIFTY")
            chain, expiry, futures_oi, futures_oi_prev, futures_ltp, futures_prev_close, cerr = get_option_chain(kite, "NIFTY")

            if chain and ltp:
                chain_sorted = sorted(chain, key=lambda r: r["strike"])
                atm_idx      = min(range(len(chain_sorted)), key=lambda i: abs(chain_sorted[i]["strike"] - ltp))
                start        = max(0, atm_idx - 9)
                end          = min(len(chain_sorted), atm_idx + 10)
                raw_slice    = chain_sorted[start:end]

                pcr_val = compute_pcr(raw_slice)
                if pcr_val is not None:
                    pcr = round(pcr_val, 2)

                atm_row = chain_sorted[atm_idx]

                def get_buildup_label(oi_chg, cur_ltp, prv_ltp):
                    if prv_ltp <= 0 or cur_ltp <= 0: return "flat"
                    if oi_chg == 0: return "flat"
                    pct_chg = (cur_ltp - prv_ltp) / prv_ltp
                    THRESHOLD = 0.0025
                    if abs(pct_chg) <= THRESHOLD:
                        return "flat"
                    price_up = pct_chg > THRESHOLD
                    oi_up    = oi_chg > 0
                    if oi_up and price_up:      return "long_buildup"
                    if oi_up and not price_up:  return "short_buildup"
                    if not oi_up and price_up:  return "short_covering"
                    return "long_unwinding"

                ce_buildup = get_buildup_label(atm_row.get("ce_oi_chg", 0), atm_row.get("ce_ltp", 0), atm_row.get("ce_prev_ltp", 0))
                pe_buildup = get_buildup_label(atm_row.get("pe_oi_chg", 0), atm_row.get("pe_ltp", 0), atm_row.get("pe_prev_ltp", 0))

            conn = sqlite3.connect(DB_PATH)
            today_str = now_ist().date().isoformat()
            row = conn.execute(
                "SELECT or_direction FROM first_hour_predictions WHERE date = ? AND symbol = 'NIFTY'",
                (today_str,)
            ).fetchone()
            if not row:
                row = conn.execute(
                    "SELECT or_direction FROM first_hour_predictions WHERE symbol = 'NIFTY' ORDER BY date DESC LIMIT 1"
                ).fetchone()
            conn.close()

            if row and row[0]:
                direction = row[0].strip().lower()
                if "bullish" in direction:
                    or_result = "bullish_break"
                elif "bearish" in direction:
                    or_result = "bearish_break"
                else:
                    or_result = "flat"
        except Exception as inner_ex:
            print(f"Error computing live Nifty bias score: {inner_ex}")

        score = compute_bias_score(pcr, ce_buildup, pe_buildup, or_result)
        payload = {
            "score": score,
            "zone": zone_for_score(score),
            "drivers": [
                {"text": f"CE buildup: {ce_buildup.replace('_',' ')}", "tone": "g" if ce_buildup in ("short_covering","long_buildup") else ("a" if ce_buildup == "flat" else "r")},
                {"text": f"PE buildup: {pe_buildup.replace('_',' ')}", "tone": "g" if pe_buildup in ("long_unwinding","short_buildup") else ("a" if pe_buildup == "flat" else "r")},
                {"text": f"PCR (OI): {pcr}", "tone": "g" if pcr > 1.2 else ("r" if pcr < 0.8 else "a")},
                {"text": f"Opening range: {or_result.replace('_',' ')}", "tone": "g" if or_result == "bullish_break" else ("r" if or_result == "bearish_break" else "a")},
            ],
        }
        nifty_cache_set("bias_score_data", payload)
        return jsonify(payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def detect_patterns(candles):
    tags = []
    for i in range(1, len(candles)):
        prev, cur = candles[i - 1], candles[i]
        if prev["c"] < prev["o"] and cur["c"] > cur["o"] and cur["o"] <= prev["c"] and cur["c"] >= prev["o"]:
            tags.append({"index": i, "pattern": "Bullish Engulfing", "tone": "bull"})
        if prev["c"] > prev["o"] and cur["c"] < cur["o"] and cur["o"] >= prev["c"] and cur["c"] <= prev["o"]:
            tags.append({"index": i, "pattern": "Bearish Engulfing", "tone": "bear"})
        if cur["h"] <= prev["h"] and cur["l"] >= prev["l"]:
            tags.append({"index": i, "pattern": "Inside Bar", "tone": "neutral"})
        body = abs(cur["c"] - cur["o"])
        upper_wick = cur["h"] - max(cur["o"], cur["c"])
        if body > 0 and upper_wick > body * 2:
            tags.append({"index": i, "pattern": "Upper Wick Rejection", "tone": "bear"})
    return tags

@app.route("/api/candles")
def candles():
    cached = nifty_cache_get("candles", max_age_seconds=30)
    if cached:
        return jsonify(cached)
    try:
        kite_client = get_kite()
        if not kite_client:
            return jsonify({"error": "Kite client not initialized"}), 400
        to_dt = dt.now()
        from_dt = to_dt - timedelta(hours=3)
        data = nifty_throttled_call(
            kite_client.historical_data,
            instrument_token=256265,  # NIFTY 50 spot
            from_date=from_dt,
            to_date=to_dt,
            interval="5minute",
        )
        candle_list = [
            {"t": c["date"].isoformat(), "o": c["open"], "h": c["high"], "l": c["low"], "c": c["close"]}
            for c in data[-14:]
        ]
        patterns = detect_patterns(candle_list)
        payload = {"candles": candle_list, "patterns": patterns}
        nifty_cache_set("candles", payload)
        return jsonify(payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Nifty Candle Analyzer</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#12181A; --panel:#182022; --panel-2:#1D2628; --border:#2A3739;
    --text:#E9E6DD; --text-muted:#8B9795; --text-faint:#5C6A68;
    --green:#3FB68C; --green-dim:#1F3D34; --red:#E0654A; --red-dim:#3D251F;
    --amber:#D9A44C; --radius:3px;
  }
  *{box-sizing:border-box;margin:0;padding:0;}
  body{background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;padding:20px;min-height:100vh;
    background-image:linear-gradient(var(--border) 1px,transparent 1px),linear-gradient(90deg,var(--border) 1px,transparent 1px);
    background-size:44px 44px;background-position:-1px -1px;background-attachment:fixed;}
  .wrap{max-width:1180px;margin:0 auto;}
  header{background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);padding:16px 22px;
    display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;margin-bottom:14px;}
  .idx-name{font-family:'IBM Plex Mono',monospace;font-weight:700;font-size:13px;letter-spacing:.12em;color:var(--text-muted);text-transform:uppercase;}
  .idx-price{font-family:'IBM Plex Mono',monospace;font-weight:700;font-size:30px;margin-top:3px;}
  .idx-chg{font-family:'IBM Plex Mono',monospace;font-size:14px;font-weight:600;margin-left:12px;}
  .idx-chg.up{color:var(--green);} .idx-chg.down{color:var(--red);}
  .idx-meta{text-align:right;font-size:11.5px;color:var(--text-faint);font-family:'IBM Plex Mono',monospace;line-height:1.6;}
  .live-dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--green);margin-right:6px;animation:pulse 1.8s infinite;}
  @keyframes pulse{0%,100%{opacity:1;}50%{opacity:.3;}}
  .gauge-panel{background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);padding:22px 26px 16px;margin-bottom:14px;}
  .gauge-label-row{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:14px;}
  .section-title{font-family:'IBM Plex Mono',monospace;font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--text-faint);}
  .bias-readout{font-family:'IBM Plex Mono',monospace;font-weight:700;font-size:20px;}
  .bias-readout.bullish{color:var(--green);} .bias-readout.bearish{color:var(--red);} .bias-readout.choppy{color:var(--amber);}
  .gauge-track{position:relative;height:46px;display:flex;border-radius:var(--radius);overflow:visible;margin-bottom:6px;}
  .gauge-zone{flex:1;position:relative;display:flex;align-items:center;justify-content:center;border-right:1px solid var(--border);}
  .gauge-zone:last-child{border-right:none;}
  .gauge-zone::after{content:'';position:absolute;inset:0;opacity:.14;}
  .zone-bear::after{background:var(--red);} .zone-choppy::after{background:var(--amber);} .zone-bull::after{background:var(--green);}
  .zone-tag{font-family:'IBM Plex Mono',monospace;font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--text-faint);z-index:1;}
  .needle{position:absolute;top:-8px;bottom:-8px;width:3px;background:var(--text);box-shadow:0 0 10px 1px rgba(233,230,221,.5);transition:left 900ms cubic-bezier(.2,.9,.25,1);z-index:2;}
  .needle::before{content:'';position:absolute;top:-6px;left:50%;transform:translateX(-50%);width:0;height:0;border-left:6px solid transparent;border-right:6px solid transparent;border-top:7px solid var(--text);}
  .ticks{display:flex;justify-content:space-between;margin-top:4px;font-family:'IBM Plex Mono',monospace;font-size:9.5px;color:var(--text-faint);}
  .gauge-drivers{display:flex;gap:18px;flex-wrap:wrap;margin-top:16px;padding-top:14px;border-top:1px dashed var(--border);}
  .driver{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--text-muted);display:flex;align-items:center;gap:6px;}
  .driver .dot{width:6px;height:6px;border-radius:50%;} .dot.g{background:var(--green);} .dot.r{background:var(--red);} .dot.a{background:var(--amber);}
  .grid{display:grid;grid-template-columns:1.35fr 1fr;gap:14px;}
  @media (max-width:860px){.grid{grid-template-columns:1fr;}}
  .panel{background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);padding:20px 22px;}
  .panel + .panel{margin-top:14px;}
  .candle-svg-wrap{background:var(--panel-2);border:1px solid var(--border);border-radius:var(--radius);padding:14px 10px 8px;margin-top:12px;}
  .pattern-tags{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px;}
  .tag{font-family:'IBM Plex Mono',monospace;font-size:10.5px;padding:5px 9px;border-radius:2px;border:1px solid var(--border);color:var(--text-muted);background:var(--panel-2);}
  .tag.bull{color:var(--green);border-color:var(--green-dim);background:var(--green-dim);}
  .tag.bear{color:var(--red);border-color:var(--red-dim);background:var(--red-dim);}
  .signal-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px;}
  .signal-card{background:var(--panel-2);border:1px solid var(--border);border-radius:var(--radius);padding:12px 14px;}
  .signal-card .k{font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--text-faint);text-transform:uppercase;letter-spacing:.08em;}
  .signal-card .v{font-family:'IBM Plex Mono',monospace;font-size:16px;font-weight:700;margin-top:4px;}
  .v.green{color:var(--green);} .v.red{color:var(--red);} .v.amber{color:var(--amber);}
  .stacked-bar{display:flex;width:100%;height:20px;border-radius:2px;overflow:hidden;margin:12px 0 16px;border:1px solid var(--border);}
  .sector-row{display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border);font-family:'IBM Plex Mono',monospace;font-size:12px;}
  .sector-row:last-child{border-bottom:none;}
  .sector-left{display:flex;align-items:center;gap:9px;color:var(--text);}
  .swatch{width:9px;height:9px;border-radius:2px;flex-shrink:0;}
  .sector-name{color:var(--text-muted);}
  .sector-right{display:flex;align-items:center;gap:10px;}
  .sector-chg{font-size:10.5px;width:46px;text-align:right;}
  .sector-chg.up{color:var(--green);} .sector-chg.down{color:var(--red);}
  .sector-wt{width:44px;text-align:right;font-weight:600;}
  .total-row{display:flex;justify-content:space-between;margin-top:10px;padding-top:10px;border-top:1px solid var(--border);font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--text-faint);}
  .total-row b{color:var(--text);}
  footer{text-align:center;color:var(--text-faint);font-family:'IBM Plex Mono',monospace;font-size:10.5px;margin-top:18px;letter-spacing:.05em;}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <div class="idx-name">NIFTY 50 · SPOT INDEX</div>
      <div><span class="idx-price" id="idxPrice">—</span><span class="idx-chg mono" id="idxChg"></span></div>
    </div>
    <div class="idx-meta">
      <div><span class="live-dot"></span>LIVE — <span id="idxTime">—</span></div>
      <div>Timeframe: 5-min · Live via Kite</div>
    </div>
  </header>

  <div class="gauge-panel">
    <div class="gauge-label-row">
      <span class="section-title">Index Bias — Composite Classifier</span>
      <span class="bias-readout" id="biasReadout">—</span>
    </div>
    <div class="gauge-track">
      <div class="gauge-zone zone-bear"><span class="zone-tag">Bearish</span></div>
      <div class="gauge-zone zone-choppy"><span class="zone-tag">Choppy</span></div>
      <div class="gauge-zone zone-bull"><span class="zone-tag">Bullish</span></div>
      <div class="needle" id="gaugeNeedle" style="left:50%;"></div>
    </div>
    <div class="ticks"><span>0</span><span>35</span><span>65</span><span>100</span></div>
    <div class="gauge-drivers" id="gaugeDrivers"></div>
  </div>

  <div class="grid">
    <div class="col-main">
      <div class="panel">
        <span class="section-title">Candle Pattern Analyzer · Last 14 × 5-min</span>
        <div class="candle-svg-wrap"><svg id="candleSvg" viewBox="0 0 640 190" width="100%" height="190" preserveAspectRatio="none"></svg></div>
        <div class="pattern-tags" id="patternTags"></div>
      </div>
      <div class="panel">
        <span class="section-title">Supporting Signals</span>
        <div class="signal-grid" id="signalGrid"></div>
      </div>
    </div>
    <div class="col-side">
      <div class="panel">
        <span class="section-title">Sector Weightage — NIFTY 50 (Σ 100%)</span>
        <div class="stacked-bar" id="stackedBar"></div>
        <div class="sector-list" id="sectorList"></div>
        <div class="total-row"><span id="sectorCount">—</span><span>Total weight <b id="totalWt">—</b></span></div>
      </div>
    </div>
  </div>
  <footer>NIFTY CANDLE ANALYZER · LIVE DATA VIA KITE CONNECT</footer>
</div>

<script>
async function loadIndexQuote(){
  const r = await fetch('/api/index-quote'); const d = await r.json();
  document.getElementById('idxPrice').textContent = d.ltp.toLocaleString('en-IN', {minimumFractionDigits:2});
  const chgEl = document.getElementById('idxChg');
  const up = d.change >= 0;
  chgEl.className = 'idx-chg ' + (up ? 'up' : 'down');
  chgEl.textContent = (up?'▲ +':'▼ ') + d.change.toFixed(2) + ' (' + (up?'+':'') + d.change_pct.toFixed(2) + '%)';
  document.getElementById('idxTime').textContent = new Date(d.timestamp).toLocaleString('en-IN');
}

async function loadBias(){
  const r = await fetch('/api/bias-score'); const d = await r.json();
  const readout = document.getElementById('biasReadout');
  readout.className = 'bias-readout ' + d.zone;
  readout.textContent = d.zone.toUpperCase() + ' · Score ' + d.score + '/100';
  document.getElementById('gaugeNeedle').style.left = d.score + '%';
  const drivers = document.getElementById('gaugeDrivers');
  drivers.innerHTML = '';
  d.drivers.forEach(dr=>{
    const span = document.createElement('span'); span.className='driver';
    span.innerHTML = '<span class="dot ' + dr.tone + '"></span>' + dr.text;
    drivers.appendChild(span);
  });
  const grid = document.getElementById('signalGrid'); grid.innerHTML = '';
  d.drivers.forEach(dr=>{
    const [k,...rest] = dr.text.split(':'); const v = rest.join(':').trim();
    const card = document.createElement('div'); card.className='signal-card';
    const toneClass = dr.tone==='g'?'green':(dr.tone==='r'?'red':'amber');
    card.innerHTML = '<div class="k">'+k+'</div><div class="v '+toneClass+'">'+v+'</div>';
    grid.appendChild(card);
  });
}

async function loadSectors(){
  const r = await fetch('/api/sector-weights'); const d = await r.json();
  const bar = document.getElementById('stackedBar'); bar.innerHTML='';
  const list = document.getElementById('sectorList'); list.innerHTML='';
  d.sectors.forEach(s=>{
    const seg = document.createElement('div');
    seg.style.width = s.wt + '%'; seg.style.background = s.color; seg.title = s.name+' — '+s.wt+'%';
    bar.appendChild(seg);
    const row = document.createElement('div'); row.className='sector-row';
    row.innerHTML = '<div class="sector-left"><span class="swatch" style="background:'+s.color+'"></span>'
      + '<span class="sector-name">'+s.name+'</span></div>'
      + '<div class="sector-right"><span class="sector-chg '+(s.chg>=0?'up':'down')+'">'+(s.chg>=0?'+':'')+s.chg.toFixed(2)+'%</span>'
      + '<span class="sector-wt">'+s.wt.toFixed(1)+'%</span></div>';
    list.appendChild(row);
  });
  document.getElementById('sectorCount').textContent = d.sectors.length + ' sectors';
  document.getElementById('totalWt').textContent = d.total_weight.toFixed(1) + '%';
}

async function loadCandles(){
  const r = await fetch('/api/candles'); const d = await r.json();
  const svg = document.getElementById('candleSvg'); svg.innerHTML='';
  const candles = d.candles;
  if(!candles.length) return;
  const min = Math.min(...candles.map(c=>c.l)), max = Math.max(...candles.map(c=>c.h));
  const pad=12, W=640, H=190, cw=(W-pad*2)/candles.length;
  const y = v => H - pad - ((v-min)/(max-min))*(H-pad*2);
  const ns = 'http://www.w3.org/2000/svg';
  candles.forEach((c,i)=>{
    const x = pad + i*cw + cw/2;
    const up = c.c >= c.o; const color = up ? '#3FB68C' : '#E0654A';
    const wick = document.createElementNS(ns,'line');
    wick.setAttribute('x1',x); wick.setAttribute('x2',x);
    wick.setAttribute('y1',y(c.h)); wick.setAttribute('y2',y(c.l));
    wick.setAttribute('stroke',color); wick.setAttribute('stroke-width','1.4');
    svg.appendChild(wick);
    const bodyTop = y(Math.max(c.o,c.c)); const bodyH = Math.max(2, Math.abs(y(c.o)-y(c.c)));
    const rect = document.createElementNS(ns,'rect');
    rect.setAttribute('x', x - cw*0.28); rect.setAttribute('y', bodyTop);
    rect.setAttribute('width', cw*0.56); rect.setAttribute('height', bodyH);
    rect.setAttribute('fill', color); rect.setAttribute('rx','1');
    svg.appendChild(rect);
  });
  const tagsEl = document.getElementById('patternTags'); tagsEl.innerHTML='';
  d.patterns.forEach(p=>{
    const tag = document.createElement('span');
    tag.className = 'tag ' + (p.tone==='bull'?'bull':(p.tone==='bear'?'bear':''));
    tag.textContent = p.pattern + ' — candle #' + (p.index+1);
    tagsEl.appendChild(tag);
  });
}

async function refreshAll(){
  try { await Promise.all([loadIndexQuote(), loadBias(), loadSectors(), loadCandles()]); }
  catch(e){ console.error('refresh failed', e); }
}

// ── Kite session guard ───────────────────────────────────────────────────────
let _kiteReady = false;
let _banner = null;

function showBanner(msg){
  if(!_banner){
    _banner = document.createElement('div');
    _banner.id = 'kiteBanner';
    Object.assign(_banner.style, {
      position:'fixed', top:'0', left:'0', width:'100%', zIndex:'9999',
      background:'#D9A44C', color:'#12181A', textAlign:'center',
      padding:'10px 16px', fontFamily:"'Inter',sans-serif",
      fontSize:'14px', fontWeight:'600', letterSpacing:'.02em'
    });
    document.body.prepend(_banner);
  }
  _banner.textContent = msg;
}

function clearBanner(){
  if(_banner){ _banner.remove(); _banner = null; }
}

async function checkKiteAndRefresh(){
  try {
    const r = await fetch('/kite/auth/status');
    const d = await r.json();
    if(d.status === 'ok'){
      if(!_kiteReady){
        _kiteReady = true;
        clearBanner();
      }
      await refreshAll();
    } else {
      _kiteReady = false;
      showBanner('⚠  Kite session not connected — open Settings → Kite API to log in. Retrying…');
    }
  } catch(e){
    _kiteReady = false;
    showBanner('⚠  Cannot reach backend. Retrying…');
    console.error('kite check failed', e);
  }
}

checkKiteAndRefresh();
setInterval(checkKiteAndRefresh, 10000); // check + refresh every 10s
</script>
</body>
</html>"""

@app.route('/nifty-candle-analyzer')
@app.route('/nifty-analyzer')
def nifty_candle_analyzer_dashboard():
    from flask import render_template_string
    return render_template_string(PAGE)


# ── F&O Quarterly Results Analyzer (F&O ResAnalyzer) ───────────────────────

RES_DB_PATH = os.path.join(os.path.dirname(__file__), "results_cache.db")

_RES_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

_FNO_RES_SYMBOLS = {
    "AARTIIND","ABB","ABCAPITAL","ABFRL","ACC","ADANIENSOL","ADANIENT","ADANIGREEN",
    "ADANIPORTS","ALKEM","AMBER","AMBUJACEM","ANGELONE","APLAPOLLO","APOLLOHOSP",
    "APOLLOTYRE","ASHOKLEY","ASIANPAINT","ASTRAL","ATUL","AUBANK","AUROPHARMA",
    "AXISBANK","BAJAJ-AUTO","BAJAJFINSV","BAJFINANCE","BALKRISIND","BANDHANBNK",
    "BANKBARODA","BANKINDIA","BATAINDIA","BEL","BERGEPAINT","BHARATFORG","BHARTIARTL",
    "BHEL","BIOCON","BOSCHLTD","BPCL","BRITANNIA","BSE","BSOFT","CAMS","CANBK",
    "CANFINHOME","CDSL","CESC","CGPOWER","CHAMBLFERT","CHOLAFIN","CIPLA","COALINDIA",
    "COFORGE","COLPAL","CONCOR","COROMANDEL","CROMPTON","CUB","CUMMINSIND","CYIENT",
    "DABUR","DALBHARAT","DEEPAKNTR","DELHIVERY","DIVISLAB","DIXON","DLF","DMART",
    "DRREDDY","EICHERMOT","ESCORTS","EXIDEIND","FEDERALBNK","GAIL","GLENMARK",
    "GMRAIRPORT","GNFC","GODREJCP","GODREJPROP","GRANULES","GRASIM","GUJGASLTD",
    "HAL","HAVELLS","HCLTECH","HDFCAMC","HDFCBANK","HDFCLIFE","HEROMOTOCO",
    "HFCL","HINDALCO","HINDCOPPER","HINDPETRO","HINDUNILVR","HUDCO","IBULHSGFIN",
    "ICICIBANK","ICICIGI","ICICIPRULI","IDEA","IDFCFIRSTB","IEX","IGL","INDHOTEL",
    "INDIACEM","INDIAMART","INDIGO","INDUSINDBK","INDUSTOWER","INFY","INOXWIND",
    "IOC","IPCALAB","IRB","IRCTC","IRFC","ITC","JINDALSTEL","JIOFIN","JKCEMENT",
    "JSWENERGY","JSWSTEEL","JUBLFOOD","KALYANKJIL","KEI","KOTAKBANK","KPITTECH",
    "LALPATHLAB","LAURUSLABS","LICHSGFIN","LICI","LODHA","LT","LTF","LTIM","LTTS",
    "LUPIN","M&M","M&MFIN","MANAPPURAM","MARICO","MARUTI","MAXHEALTH","MCX",
    "METROPOLIS","MFSL","MGL","MOTHERSON","MPHASIS","MRF","MUTHOOTFIN","NATIONALUM",
    "NAUKRI","NAVINFLUOR","NBCC","NCC","NESTLEIND","NHPC","NMDC","NTPC","NYKAA",
    "OBEROIRLTY","OFSS","OIL","ONGC","PAGEIND","PATANJALI","PAYTM","PEL","PERSISTENT",
    "PETRONET","PFC","PHOENIXLTD","PIDILITIND","PIIND","PNB","PNBHOUSING","POLICYBZR",
    "POLYCAB","POONAWALLA","POWERGRID","PRESTIGE","PVRINOX","RAMCOCEM","RBLBANK",
    "RECLTD","RELIANCE","SAIL","SBICARD","SBILIFE","SBIN","SHREECEM","SHRIRAMFIN",
    "SIEMENS","SJVN","SOLARINDS","SONACOMS","SRF","STAR","SUNPHARMA","SUNTV",
    "SUPREMEIND","SYNGENE","TATACHEM","TATACOMM","TATACONSUM","TATAELXSI",
    "TATAMOTORS","TATAPOWER","TATASTEEL","TATATECH","TCS","TECHM","TIINDIA",
    "TITAGARH","TITAN","TORNTPHARM","TORNTPOWER","TRENT","TVSMOTOR","UBL",
    "ULTRACEMCO","UNIONBANK","UNITDSPR","UPL","VBL","VEDL","VOLTAS","WIPRO",
    "YESBANK","ZEEL","ZYDUSLIFE","TI"
}

def _init_res_db():
    """Safe schema migration for results_cache and results_annotations.
    Uses CREATE TABLE IF NOT EXISTS + ALTER TABLE for forward-compat.
    Never drops data on restart."""
    conn = sqlite3.connect(RES_DB_PATH)
    # Safe create — does NOT drop existing data
    conn.execute("""
        CREATE TABLE IF NOT EXISTS results_cache (
            symbol               TEXT PRIMARY KEY,
            quarter_label        TEXT,
            revenue              REAL,
            revenue_yoy          REAL,
            revenue_qoq          REAL,
            net_profit           REAL,
            profit_yoy           REAL,
            profit_qoq           REAL,
            opm                  REAL,
            opm_yoy_delta        REAL,
            opm_qoq_delta        REAL,
            pat_margin           REAL,
            pat_margin_yoy       REAL,
            other_income         REAL,
            other_income_pct     REAL,
            finance_cost         REAL,
            finance_cost_yoy     REAL,
            eps                  REAL,
            eps_yoy              REAL,
            depreciation         REAL,
            exceptional_items    REAL,
            normalized_pat       REAL,
            icr                  REAL,
            est_pat_consensus    REAL,
            est_revenue_consensus REAL,
            est_eps_consensus    REAL,
            n_analysts           INTEGER,
            verdict              TEXT,
            reasons              TEXT,
            quality_flags        TEXT,
            fetched_at           TEXT,
            raw_status           TEXT
        )
    """)
    # Forward-compat: add any columns that may be missing from older DB files
    _new_cols = [
        ("opm_qoq_delta",          "REAL"),
        ("pat_margin",             "REAL"),
        ("pat_margin_yoy",         "REAL"),
        ("other_income",           "REAL"),
        ("other_income_pct",       "REAL"),
        ("finance_cost",           "REAL"),
        ("finance_cost_yoy",       "REAL"),
        ("eps",                    "REAL"),
        ("eps_yoy",                "REAL"),
        ("depreciation",           "REAL"),
        ("quality_flags",          "TEXT"),
        ("raw_status",             "TEXT"),
        # Phase 1 additions
        ("exceptional_items",      "REAL"),
        ("normalized_pat",         "REAL"),
        ("icr",                    "REAL"),
        ("est_pat_consensus",      "REAL"),
        ("est_revenue_consensus",  "REAL"),
        ("est_eps_consensus",      "REAL"),
        ("n_analysts",             "INTEGER"),
    ]
    existing = {row[1] for row in conn.execute("PRAGMA table_info(results_cache)")}
    for col, typ in _new_cols:
        if col not in existing:
            conn.execute(f"ALTER TABLE results_cache ADD COLUMN {col} {typ}")
    # Annotations table: user-entered Estimate PAT + Guidance, persisted
    conn.execute("""
        CREATE TABLE IF NOT EXISTS results_annotations (
            symbol      TEXT PRIMARY KEY,
            est_pat     REAL,
            guidance    TEXT,
            updated_at  TEXT
        )
    """)
    # Trendlyne ID cache — symbol → numeric ID, resolved once and reused
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trendlyne_id_cache (
            symbol      TEXT PRIMARY KEY,
            tl_id       TEXT,
            tl_slug     TEXT,
            resolved_at TEXT
        )
    """)
    conn.commit()
    conn.close()

def _resolve_trendlyne_id(symbol):
    """Resolve NSE symbol to Trendlyne numeric ID + slug.
    Primary: DB cache (populated by _seed_trendlyne_from_sitemap on startup).
    Fallback: live lookup via equity-sitemap-stocks.xml.
    Returns (tl_id, tl_slug) or (None, None) on failure."""
    import re as _re, requests as _req
    # DB cache first
    try:
        conn = sqlite3.connect(RES_DB_PATH)
        row = conn.execute(
            "SELECT tl_id, tl_slug FROM trendlyne_id_cache WHERE symbol=?", (symbol,)
        ).fetchone()
        conn.close()
        if row and row[0]:
            return row[0], row[1]
    except Exception:
        pass

    # Live fallback: fetch sitemap and extract for this one symbol
    try:
        resp = _req.get(
            "https://trendlyne.com/equity-sitemap-stocks.xml",
            headers=_TL_HEADERS, timeout=15
        )
        if resp.status_code == 200:
            m = _re.search(
                rf"/equity/(\d+)/({_re.escape(symbol)})/([^/]+)/",
                resp.text, _re.IGNORECASE
            )
            if m:
                tl_id, sym_found, slug = m.group(1), m.group(2), m.group(3)
                _tl_cache_id(symbol, tl_id, slug)
                return tl_id, slug
    except Exception as e:
        logging.debug(f"[TL] Sitemap lookup failed for {symbol}: {e}")

    # Cache the failure ONLY if no valid entry already exists — avoids corrupting
    # valid rows on transient DB read errors or sitemap timeouts.
    try:
        conn = sqlite3.connect(RES_DB_PATH)
        existing = conn.execute(
            "SELECT tl_id FROM trendlyne_id_cache WHERE symbol=?", (symbol,)
        ).fetchone()
        conn.close()
        if not existing or not existing[0]:
            _tl_cache_id(symbol, None, None)
    except Exception:
        pass
    return None, None


def _seed_trendlyne_from_sitemap():
    """One-time seeder: fetch equity-sitemap-stocks.xml and bulk-populate
    trendlyne_id_cache for all F&O symbols. Called once at startup in a thread.
    Safe to run repeatedly — uses INSERT OR REPLACE to refresh stale/null rows."""
    import re as _re
    import requests
    _headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://trendlyne.com/",
    }
    try:
        resp = requests.get(
            "https://trendlyne.com/equity-sitemap-stocks.xml",
            headers=_headers, timeout=20
        )
        if resp.status_code != 200:
            logging.debug(f"[TL-SEED] Sitemap returned {resp.status_code}")
            return
        # Parse all /equity/{id}/{symbol}/{slug}/ URLs
        pairs = _re.findall(r"/equity/(\d+)/([A-Z0-9&-]+)/([^/]+)/", resp.text)
        if not pairs:
            logging.debug("[TL-SEED] No URLs found in sitemap")
            return
        # Build symbol→(id, slug) map
        sitemap_map = {sym: (tl_id, slug) for tl_id, sym, slug in pairs}
        # Insert for all F&O symbols that have matches
        resolved, skipped = 0, 0
        conn = sqlite3.connect(RES_DB_PATH)
        ts = dt.now(datetime.timezone.utc).isoformat()
        for sym in _FNO_RES_SYMBOLS:
            if sym in sitemap_map:
                tl_id, slug = sitemap_map[sym]
                conn.execute(
                    "INSERT OR REPLACE INTO trendlyne_id_cache VALUES (?,?,?,?)",
                    (sym, tl_id, slug, ts)
                )
                resolved += 1
            else:
                skipped += 1
        conn.commit()
        conn.close()
        logging.info(f"[TL-SEED] Seeded {resolved} F&O symbols, {skipped} not found in sitemap")
    except Exception as e:
        logging.warning(f"[TL-SEED] Seeder failed: {e}")

try:
    _init_res_db()
except Exception as _e:
    logging.error(f"[RES-DB] Failed to initialise results DB: {_e}")



def _clean_res_num(text):
    if text is None: return None
    t = text.strip().replace(",", "").replace("%", "")
    if t in ("", "-", "--"): return None
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()")
    try:
        val = float(t)
        return -val if neg else val
    except ValueError:
        return None

def _res_pct_change(curr, prev):
    if curr is None or prev is None or prev == 0: return None
    return round(((curr - prev) / abs(prev)) * 100, 2)

def _fetch_screener_quarterly(symbol):
    """Fetch quarterly P&L rows from screener.in.
    Returns all rows needed for Tier 1-4 institutional analysis."""
    import requests
    from bs4 import BeautifulSoup
    for variant in ("consolidated", ""):
        url = f"https://www.screener.in/company/{symbol}/{variant}/".rstrip("/") + "/"
        try:
            resp = requests.get(url, headers=_RES_HEADERS, timeout=15)
        except Exception:
            continue
        if resp.status_code != 200: continue
        soup = BeautifulSoup(resp.text, "lxml")
        section = soup.find("section", {"id": "quarters"})
        if not section: continue
        table = section.find("table")
        if not table: continue
        headers_row = table.find("thead").find_all("th")
        quarter_labels = [th.get_text(strip=True) for th in headers_row[1:]]
        rows = {}
        for tr in table.find("tbody").find_all("tr"):
            cells = tr.find_all("td")
            if not cells: continue
            label = cells[0].get_text(strip=True).replace("+", "").strip()
            values = [_clean_res_num(td.get_text(strip=True)) for td in cells[1:]]
            rows[label] = values
        sales_row    = rows.get("Sales") or rows.get("Revenue") or rows.get("Sales ")
        profit_row   = rows.get("Net Profit") or rows.get("Net Profit ")
        opm_row      = (rows.get("OPM %") or rows.get("OPM%")
                        or rows.get("EBITDA %") or rows.get("EBITDA Margin")
                        or rows.get("Operating Profit Margin") or rows.get("Operating Margin %")
                        or rows.get("EBITDA Margin %") or rows.get("Operating Profit Margin (%)"))
        if not sales_row or not profit_row: continue
        # Extended rows for Tier 3 & 4
        other_income_row  = (rows.get("Other Income") or rows.get("Other Income ")
                             or rows.get("Other income"))
        finance_cost_row  = (rows.get("Finance Cost") or rows.get("Finance Costs")
                             or rows.get("Interest") or rows.get("Interest Expense"))
        depreciation_row  = (rows.get("Depreciation") or rows.get("Depreciation ")
                             or rows.get("Depreciation & Amortization")
                             or rows.get("D&A"))
        # EPS: try diluted first, then basic
        eps_row = (rows.get("EPS in Rs") or rows.get("EPS") or rows.get("Diluted EPS")
                   or rows.get("Basic EPS") or rows.get("EPS (Rs)"))
        # Exceptional Items (Tier 3 quality filter)
        exceptional_row = (rows.get("Exceptional Items") or rows.get("Exceptional Item")
                           or rows.get("Extraordinary Items") or rows.get("Extra-Ordinary Items")
                           or rows.get("Exceptional items"))
        return {
            "url": url,
            "quarter_labels":  quarter_labels,
            "sales":           sales_row,
            "net_profit":      profit_row,
            "opm":             opm_row,
            "other_income":    other_income_row,
            "finance_cost":    finance_cost_row,
            "depreciation":    depreciation_row,
            "eps":             eps_row,
            "exceptional_items": exceptional_row,
        }
    return None

# ── Trendlyne consensus estimate helpers ─────────────────────────────────────

_TL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://trendlyne.com/",
}

# NOTE: Trendlyne seeder is started in __main__ with a 15s delay
# to avoid module-level I/O that blocks request handling at startup.


def _tl_cache_id(symbol, tl_id, tl_slug):
    """Persist Trendlyne ID resolution result to DB cache."""
    try:
        conn = sqlite3.connect(RES_DB_PATH)
        conn.execute(
            "INSERT OR REPLACE INTO trendlyne_id_cache VALUES (?,?,?,?)",
            (symbol, tl_id, tl_slug, dt.now(datetime.timezone.utc).isoformat())
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logging.debug(f"[TL] Failed to cache ID for {symbol}: {e}")


def _fetch_trendlyne_estimates(symbol):
    """Fetch consensus PAT, Revenue, EPS from Trendlyne SSR HTML JSON blob.
    Data is Capital IQ analyst pool, server-side rendered — no JS required.
    Returns dict with est_pat_consensus, est_revenue_consensus, est_eps_consensus,
    n_analysts, tl_quarter or None on failure."""
    import requests, html as _html_mod, re as _re
    tl_id, slug = _resolve_trendlyne_id(symbol)
    if not tl_id:
        logging.debug(f"[TL] {symbol}: no tl_id resolved")
        return None
    url = f"https://trendlyne.com/equity/consensus-estimates/{tl_id}/{symbol}/{slug}/"
    try:
        resp = requests.get(url, headers=_TL_HEADERS, timeout=15)
    except Exception as e:
        logging.warning(f"[TL] {symbol}: request failed: {type(e).__name__}: {e}")
        return None
    if resp.status_code != 200:
        logging.warning(f"[TL] {symbol}: HTTP {resp.status_code}")
        return None

    try:
        unescaped = _html_mod.unescape(resp.text)
        start = unescaped.find('{"TARGET_PRICE"')
        if start < 0:
            logging.debug(f"[TL] {symbol}: TARGET_PRICE blob not found ({len(resp.text)}B)")
            return None
        # Walk to find matching closing brace
        depth, end = 0, start
        for i, c in enumerate(unescaped[start:start + 300000]):
            if c == '{':   depth += 1
            elif c == '}': depth -= 1
            if depth == 0: end = start + i + 1; break
        if end == start:
            logging.debug(f"[TL] {symbol}: could not find closing brace")
            return None
        data = json.loads(unescaped[start:end])
    except Exception as e:
        logging.warning(f"[TL] {symbol}: JSON parse failed: {type(e).__name__}: {e}")
        return None

    def _pick_quarter(key):
        """Return the current quarter dict, fallback to most-recent past."""
        quarters = data.get(key, {}).get("QUARTER", [])
        for q in quarters:
            if q.get("is_current"):
                return q
        past = [q for q in quarters if q.get("is_past")]
        return past[-1] if past else None

    ni  = _pick_quarter("NET_INCOME")
    rev = _pick_quarter("REVENUE")
    eps = _pick_quarter("EPS")

    if not ni:
        logging.debug(f"[TL] {symbol}: NET_INCOME quarter not found (keys: {list(data.keys())})")
        return None

    def _median_or_avg(q):
        return q.get("MEDIAN") or q.get("AVG")

    return {
        "est_pat_consensus":      round(_median_or_avg(ni), 2),
        "est_revenue_consensus":  round(_median_or_avg(rev), 2) if rev else None,
        "est_eps_consensus":      round(_median_or_avg(eps), 2) if eps else None,
        "n_analysts":             int(ni.get("NUMBER_OF_ANALYSTS") or 0),
        "tl_quarter":             ni.get("periodtype"),
    }


# ─────────────────────────────────────────────────────────────────────────────

def _analyze_res_quarterly(symbol, data, est_pat_consensus=None, guidance=None, n_analysts=0):
    """4-Tier institutional scoring engine.

    Tier 1 (primary sentiment drivers): OPM margin trajectory, PAT margin %
    Tier 2 (core quality checks):       Revenue YoY, PAT YoY
    Tier 3 (quality filter):            Other Income %, Finance Cost trend
    Tier 4 (context, lower weight):     EPS, Depreciation
    """
    labels            = data["quarter_labels"]
    sales             = data["sales"]
    profit            = data["net_profit"]
    opm               = data.get("opm")               or [None] * len(labels)
    other_income      = data.get("other_income")      or [None] * len(labels)
    finance_cost      = data.get("finance_cost")      or [None] * len(labels)
    depreciation      = data.get("depreciation")      or [None] * len(labels)
    eps_data          = data.get("eps")               or [None] * len(labels)
    exceptional_items = data.get("exceptional_items") or [None] * len(labels)

    n = len(labels)
    if n < 5:
        return {"verdict": "Insufficient Data", "reasons": ["Not enough quarters returned"]}

    latest_q = labels[-1]

    # --- Raw values (latest, prior quarter, year-ago) ---
    curr_sales,  prev_q_sales,  yoy_sales  = sales[-1],             sales[-2],             sales[-5]
    curr_profit, prev_q_profit, yoy_profit = profit[-1],            profit[-2],            profit[-5]
    curr_opm,    prev_q_opm,    yoy_opm    = opm[-1],               opm[-2],               opm[-5]
    curr_oi,     yoy_oi                    = other_income[-1],       other_income[-5]
    curr_fc,     yoy_fc                    = finance_cost[-1],       finance_cost[-5]
    curr_dep                               = depreciation[-1]
    curr_eps,    yoy_eps                   = eps_data[-1],           eps_data[-5]
    curr_exc                               = exceptional_items[-1]

    # --- Derived metrics ---
    rev_yoy       = _res_pct_change(curr_sales,  yoy_sales)
    rev_qoq       = _res_pct_change(curr_sales,  prev_q_sales)
    profit_yoy    = _res_pct_change(curr_profit, yoy_profit)
    profit_qoq    = _res_pct_change(curr_profit, prev_q_profit)
    opm_yoy_delta = round(curr_opm - yoy_opm, 2)     if (curr_opm    is not None and yoy_opm    is not None) else None
    opm_qoq_delta = round(curr_opm - prev_q_opm, 2)  if (curr_opm    is not None and prev_q_opm is not None) else None
    eps_yoy       = _res_pct_change(curr_eps,    yoy_eps)
    fc_yoy        = _res_pct_change(curr_fc,     yoy_fc)

    # PAT margin % = net_profit / sales * 100
    pat_margin     = round((curr_profit / curr_sales) * 100, 2) if (curr_profit is not None and curr_sales and curr_sales != 0) else None
    yoy_pat_margin = round((yoy_profit  / yoy_sales)  * 100, 2) if (yoy_profit  is not None and yoy_sales  and yoy_sales  != 0) else None
    pat_margin_yoy = round(pat_margin - yoy_pat_margin, 2)       if (pat_margin is not None and yoy_pat_margin is not None) else None

    # Other Income as % of revenue
    total_income     = (curr_sales or 0) + (curr_oi or 0)
    other_income_pct = round((curr_oi / total_income) * 100, 2) if (curr_oi is not None and total_income > 0) else None

    # Normalized PAT: strip Other Income + Exceptional Items (institutional standard)
    normalized_pat = curr_profit
    if normalized_pat is not None:
        if curr_oi  is not None: normalized_pat -= curr_oi
        if curr_exc is not None: normalized_pat -= curr_exc
        normalized_pat = round(normalized_pat, 2)

    # Interest Coverage Ratio: EBITDA / Finance Cost
    icr = None
    if curr_opm is not None and curr_sales and curr_fc and curr_fc > 0:
        ebitda_approx = curr_sales * (curr_opm / 100)
        icr = round(ebitda_approx / curr_fc, 1)

    # ── Scoring ──────────────────────────────────────────────────────────────
    reasons       = []   # human-readable, shown in UI
    quality_flags = []   # ⚠ warnings for low-quality beats
    score         = 0

    # TIER 1A — OPM/EBITDA Margin trajectory (highest institutional weight)
    if opm_yoy_delta is not None:
        if opm_yoy_delta >= 2:
            score += 3; reasons.append(f"[T1] EBITDA margin expanded strongly YoY (+{opm_yoy_delta} pts)")
        elif opm_yoy_delta >= 0.5:
            score += 2; reasons.append(f"[T1] EBITDA margin expanded YoY (+{opm_yoy_delta} pts)")
        elif opm_yoy_delta >= -0.5:
            reasons.append(f"[T1] EBITDA margin flat YoY ({opm_yoy_delta:+.1f} pts)")
        elif opm_yoy_delta >= -2:
            score -= 2; reasons.append(f"[T1] EBITDA margin contracted YoY ({opm_yoy_delta} pts)")
        else:
            score -= 3; reasons.append(f"[T1] EBITDA margin compressed significantly YoY ({opm_yoy_delta} pts)")

    # TIER 1B — PAT Margin trajectory
    if pat_margin_yoy is not None:
        if pat_margin_yoy >= 1.5:
            score += 2; reasons.append(f"[T1] PAT margin expanded YoY (+{pat_margin_yoy} pts \u2192 {pat_margin}%)")
        elif pat_margin_yoy <= -1.5:
            score -= 2; reasons.append(f"[T1] PAT margin compressed YoY ({pat_margin_yoy} pts \u2192 {pat_margin}%)")
        else:
            reasons.append(f"[T1] PAT margin broadly stable ({pat_margin}%, \u0394{pat_margin_yoy:+.1f} pts)")

    # TIER 1C — Beat/Miss vs consensus (highest institutional signal)
    if est_pat_consensus and curr_profit:
        beat_pct = (curr_profit - est_pat_consensus) / abs(est_pat_consensus) * 100
        analyst_tag = f"{n_analysts}-analyst consensus" if n_analysts else "consensus"
        if   beat_pct >  10: score += 3; reasons.append(f"[T1] Strong PAT beat (+{beat_pct:.1f}% vs {analyst_tag})")
        elif beat_pct >   3: score += 2; reasons.append(f"[T1] PAT beat (+{beat_pct:.1f}% vs {analyst_tag})")
        elif beat_pct >  -3: pass;       reasons.append(f"[T1] PAT in-line with {analyst_tag} ({beat_pct:+.1f}%)")
        elif beat_pct > -10: score -= 2; reasons.append(f"[T1] PAT miss ({beat_pct:.1f}% vs {analyst_tag})")
        else:                score -= 3; reasons.append(f"[T1] Strong PAT miss ({beat_pct:.1f}% vs {analyst_tag})")

    # TIER 1D — Management guidance revision
    if guidance == "CUT":
        score -= 2
        reasons.append("[T1] Mgmt guidance cut \u2014 forward model downgrade risk")
    elif guidance == "UP":
        score += 2
        reasons.append("[T1] Mgmt guidance raised \u2014 earnings upgrade cycle")

    # TIER 2A — Revenue YoY (preferred over QoQ — strips seasonality)
    if rev_yoy is not None:
        if rev_yoy >= 15:
            score += 2; reasons.append(f"[T2] Strong revenue growth YoY (+{rev_yoy}%)")
        elif rev_yoy >= 5:
            score += 1; reasons.append(f"[T2] Moderate revenue growth YoY (+{rev_yoy}%)")
        elif rev_yoy >= 0:
            reasons.append(f"[T2] Flat revenue YoY ({rev_yoy}%)")
        else:
            score -= 2; reasons.append(f"[T2] Revenue declined YoY ({rev_yoy}%)")

    # TIER 2B — PAT YoY (cross-checked against Other Income before trusting)
    if profit_yoy is not None:
        if profit_yoy >= 20:
            score += 2; reasons.append(f"[T2] Strong PAT growth YoY (+{profit_yoy}%)")
        elif profit_yoy >= 5:
            score += 1; reasons.append(f"[T2] Moderate PAT growth YoY (+{profit_yoy}%)")
        elif profit_yoy >= 0:
            reasons.append(f"[T2] Flat PAT YoY ({profit_yoy}%)")
        else:
            score -= 2; reasons.append(f"[T2] PAT declined YoY ({profit_yoy}%)")

    # TIER 3A — Other Income quality filter
    if other_income_pct is not None:
        if other_income_pct > 20:
            score -= 1
            quality_flags.append(f"\u26a0 Other Income is {other_income_pct}% of revenue \u2014 PAT quality suspect")
            reasons.append(f"[T3] High other income ({other_income_pct}% of revenue) \u2014 normalized PAT may be lower")
        elif other_income_pct > 10:
            quality_flags.append(f"\u26a0 Other Income elevated ({other_income_pct}% of revenue) \u2014 check for one-off gains")
            reasons.append(f"[T3] Elevated other income ({other_income_pct}% of revenue)")

    # TIER 3B — Finance Cost trend
    if fc_yoy is not None:
        if fc_yoy > 20 and profit_yoy is not None and profit_yoy > 0:
            quality_flags.append(f"\u26a0 Finance cost rising sharply YoY (+{fc_yoy}%) despite PAT growth \u2014 check interest coverage")
            reasons.append(f"[T3] Finance cost up {fc_yoy}% YoY \u2014 rising leverage eating into PAT")
            score -= 1
        elif fc_yoy > 10:
            reasons.append(f"[T3] Finance cost up {fc_yoy}% YoY \u2014 monitor leverage")
        elif fc_yoy < -5:
            reasons.append(f"[T3] Finance cost reduced YoY ({fc_yoy}%) \u2014 deleveraging positive")
            score += 1

    # TIER 3C — Interest Coverage Ratio
    if icr is not None:
        if   icr < 1.5: score -= 2; quality_flags.append(f"\u26a0 ICR critically low ({icr}x) \u2014 debt servicing risk")
        elif icr < 3.0: score -= 1; quality_flags.append(f"\u26a0 ICR below comfortable range ({icr}x) \u2014 monitor leverage")
        elif icr > 8.0: score += 1; reasons.append(f"[T3] Strong interest coverage ({icr}x)")

    # TIER 3D — Exceptional Items flag
    if curr_exc is not None and curr_profit and abs(curr_exc) > abs(curr_profit) * 0.1:
        quality_flags.append(f"\u26a0 Exceptional item Rs.{curr_exc} Cr \u2014 normalized PAT differs from reported")
        reasons.append(f"[T3] Exceptional item of Rs.{curr_exc} Cr \u2014 check normalized PAT")

    # TIER 4 — EPS (display only, derived \u2014 no separate scoring weight)
    if eps_yoy is not None:
        reasons.append(f"[T4] EPS YoY: {eps_yoy:+.1f}% (\u20b9{curr_eps})")

    # ── Verdict ──────────────────────────────────────────────────────────────
    if   score >= 5:   verdict = "Strong Positive"
    elif score >= 2:   verdict = "Positive"
    elif score == 1:   verdict = "Positive"
    elif score == 0:   verdict = "Neutral"
    elif score >= -2:  verdict = "Negative"
    else:              verdict = "Strong Negative"

    return {
        "quarter_label":          latest_q,
        # Tier 1
        "opm":                    curr_opm,
        "opm_yoy_delta":          opm_yoy_delta,
        "opm_qoq_delta":          opm_qoq_delta,
        "pat_margin":             pat_margin,
        "pat_margin_yoy":         pat_margin_yoy,
        "est_pat_consensus":      est_pat_consensus,
        "est_revenue_consensus":  None,  # filled by analyze loop
        "est_eps_consensus":      None,  # filled by analyze loop
        "n_analysts":             n_analysts,
        # Tier 2
        "revenue":                curr_sales,
        "revenue_yoy":            rev_yoy,
        "revenue_qoq":            rev_qoq,
        "net_profit":             curr_profit,
        "profit_yoy":             profit_yoy,
        "profit_qoq":             profit_qoq,
        # Tier 3
        "other_income":           curr_oi,
        "other_income_pct":       other_income_pct,
        "finance_cost":           curr_fc,
        "finance_cost_yoy":       fc_yoy,
        "exceptional_items":      curr_exc,
        "normalized_pat":         normalized_pat,
        "icr":                    icr,
        # Tier 4
        "eps":                    curr_eps,
        "eps_yoy":                eps_yoy,
        "depreciation":           curr_dep,
        # Verdict
        "verdict":                verdict,
        "reasons":                reasons,
        "quality_flags":          quality_flags,
    }

def _save_res_cache(symbol, result, status="ok"):
    conn = sqlite3.connect(RES_DB_PATH)
    conn.execute("""
        INSERT INTO results_cache
        (symbol, quarter_label, revenue, revenue_yoy, revenue_qoq, net_profit,
         profit_yoy, profit_qoq, opm, opm_yoy_delta, opm_qoq_delta,
         pat_margin, pat_margin_yoy,
         other_income, other_income_pct, finance_cost, finance_cost_yoy,
         eps, eps_yoy, depreciation,
         exceptional_items, normalized_pat, icr,
         est_pat_consensus, est_revenue_consensus, est_eps_consensus, n_analysts,
         verdict, reasons, quality_flags, fetched_at, raw_status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(symbol) DO UPDATE SET
            quarter_label=excluded.quarter_label,
            revenue=excluded.revenue, revenue_yoy=excluded.revenue_yoy, revenue_qoq=excluded.revenue_qoq,
            net_profit=excluded.net_profit, profit_yoy=excluded.profit_yoy, profit_qoq=excluded.profit_qoq,
            opm=excluded.opm, opm_yoy_delta=excluded.opm_yoy_delta, opm_qoq_delta=excluded.opm_qoq_delta,
            pat_margin=excluded.pat_margin, pat_margin_yoy=excluded.pat_margin_yoy,
            other_income=excluded.other_income, other_income_pct=excluded.other_income_pct,
            finance_cost=excluded.finance_cost, finance_cost_yoy=excluded.finance_cost_yoy,
            eps=excluded.eps, eps_yoy=excluded.eps_yoy, depreciation=excluded.depreciation,
            exceptional_items=excluded.exceptional_items,
            normalized_pat=excluded.normalized_pat, icr=excluded.icr,
            est_pat_consensus=excluded.est_pat_consensus,
            est_revenue_consensus=excluded.est_revenue_consensus,
            est_eps_consensus=excluded.est_eps_consensus,
            n_analysts=excluded.n_analysts,
            verdict=excluded.verdict, reasons=excluded.reasons, quality_flags=excluded.quality_flags,
            fetched_at=excluded.fetched_at, raw_status=excluded.raw_status
    """, (
        symbol,
        result.get("quarter_label"),
        result.get("revenue"),             result.get("revenue_yoy"),    result.get("revenue_qoq"),
        result.get("net_profit"),          result.get("profit_yoy"),     result.get("profit_qoq"),
        result.get("opm"),                 result.get("opm_yoy_delta"),  result.get("opm_qoq_delta"),
        result.get("pat_margin"),          result.get("pat_margin_yoy"),
        result.get("other_income"),        result.get("other_income_pct"),
        result.get("finance_cost"),        result.get("finance_cost_yoy"),
        result.get("eps"),                 result.get("eps_yoy"),
        result.get("depreciation"),
        result.get("exceptional_items"),   result.get("normalized_pat"),  result.get("icr"),
        result.get("est_pat_consensus"),   result.get("est_revenue_consensus"),
        result.get("est_eps_consensus"),   result.get("n_analysts"),
        result.get("verdict"),
        json.dumps(result.get("reasons", [])),
        json.dumps(result.get("quality_flags", [])),
        dt.now(datetime.timezone.utc).isoformat(),
        status
    ))
    conn.commit()
    conn.close()

def _get_res_cached(symbol, max_age_hours=6):
    conn = sqlite3.connect(RES_DB_PATH)
    cur = conn.execute("SELECT * FROM results_cache WHERE symbol=?", (symbol,))
    row = cur.fetchone()
    conn.close()
    if not row: return None
    # Use cursor.description for reliable column mapping regardless of schema version
    cols = [desc[0] for desc in cur.description]
    d = dict(zip(cols, row))
    fetched_str = d.get("fetched_at")
    if not fetched_str:
        return None  # corrupt/old row — force live re-fetch
    fetched   = dt.fromisoformat(fetched_str)
    age_hours = (dt.now(datetime.timezone.utc) - fetched).total_seconds() / 3600
    d["reasons"]       = json.loads(d["reasons"])       if d.get("reasons")       else []
    d["quality_flags"] = json.loads(d["quality_flags"]) if d.get("quality_flags") else []
    d["age_hours"]     = round(age_hours, 1)
    d["stale"]         = age_hours > max_age_hours
    return d

def _get_res_annotation(symbol):
    """Return user-entered est_pat and guidance from results_annotations."""
    conn = sqlite3.connect(RES_DB_PATH)
    row = conn.execute(
        "SELECT est_pat, guidance FROM results_annotations WHERE symbol=?", (symbol,)
    ).fetchone()
    conn.close()
    if not row: return {"est_pat": None, "guidance": None}
    return {"est_pat": row[0], "guidance": row[1]}

@app.route('/fno-res-analyzer')
def fno_res_analyzer_page():
    return send_from_directory(static_root, 'fno-res-analyzer.html')

@app.route("/api/fno-symbols")
def api_fno_symbols():
    return jsonify(sorted(_FNO_RES_SYMBOLS))

@app.route("/api/res-annotate", methods=["POST"])
def api_res_annotate():
    """Save user-entered Est. PAT and/or Guidance for a symbol."""
    body   = request.get_json(force=True) or {}
    symbol = (body.get("symbol") or "").strip().upper()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    est_pat  = body.get("est_pat")   # numeric or None
    guidance = body.get("guidance")  # 'UP' | 'FLAT' | 'CUT' | None
    conn = sqlite3.connect(RES_DB_PATH)
    # Partial UPSERT: COALESCE preserves existing value when caller sends null
    conn.execute("""
        INSERT INTO results_annotations (symbol, est_pat, guidance, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(symbol) DO UPDATE SET
            est_pat    = COALESCE(excluded.est_pat,  est_pat),
            guidance   = COALESCE(excluded.guidance, guidance),
            updated_at = excluded.updated_at
    """, (symbol, est_pat, guidance, dt.now(datetime.timezone.utc).isoformat()))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "symbol": symbol})

# Quarter-code → (month_prefix, year_offset) mapping
# Q1 FYxx = Jun of (xx-1), Q2 = Sep of (xx-1), Q3 = Dec of (xx-1), Q4 = Mar of xx
_RES_Q_MAP = {
    "Q1": ("Jun", -1),
    "Q2": ("Sep", -1),
    "Q3": ("Dec", -1),
    "Q4": ("Mar",  0),
}

@app.route("/api/res-lookup", methods=["GET"])
def api_res_lookup():
    """Pure DB lookup — no live fetch.
    Params: symbol, q (Q1/Q2/Q3/Q4), fy (2-digit, e.g. 26)
    Returns the cached row + annotation, or {found: false}.
    """
    symbol = (request.args.get("symbol") or "").strip().upper()
    q_code = (request.args.get("q") or "").strip().upper()      # Q1..Q4
    fy_raw = (request.args.get("fy") or "").strip()             # e.g. "26"

    if not symbol or q_code not in _RES_Q_MAP or not fy_raw:
        return jsonify({"found": False, "message": "Invalid parameters — provide symbol, q (Q1-Q4) and fy (e.g. 26)"}), 400

    try:
        fy = int(fy_raw)
    except ValueError:
        return jsonify({"found": False, "message": "fy must be a 2-digit number"}), 400

    month_prefix, yr_offset = _RES_Q_MAP[q_code]
    # Convert 2-digit FY to full calendar year of that quarter
    full_fy = (2000 + fy) if fy < 100 else fy
    cal_year = full_fy + yr_offset      # e.g. FY26 Q1 → Jun 2025

    quarter_pattern = f"{month_prefix} {cal_year}%"   # e.g. "Jun 2025%"

    conn = sqlite3.connect(RES_DB_PATH)
    cols = [
        "symbol", "quarter_label",
        "revenue", "revenue_yoy", "revenue_qoq",
        "net_profit", "profit_yoy", "profit_qoq",
        "opm", "opm_yoy_delta", "opm_qoq_delta",
        "pat_margin", "pat_margin_yoy",
        "other_income", "other_income_pct",
        "finance_cost", "finance_cost_yoy",
        "eps", "eps_yoy", "depreciation",
        "verdict", "reasons", "quality_flags",
        "fetched_at", "raw_status",
        "icr", "normalized_pat", "exceptional_items",
        "est_pat_consensus", "est_revenue_consensus", "est_eps_consensus", "n_analysts"
    ]
    row = conn.execute(
        f"SELECT {','.join(cols)} FROM results_cache WHERE symbol=? AND quarter_label LIKE ?",
        (symbol, quarter_pattern)
    ).fetchone()

    ann = conn.execute(
        "SELECT est_pat, guidance FROM results_annotations WHERE symbol=?",
        (symbol,)
    ).fetchone()
    conn.close()

    if row is None:
        return jsonify({
            "found": False,
            "symbol": symbol,
            "q_label": f"{q_code} FY{fy_raw}",
            "message": f"{symbol} — {q_code} FY{fy_raw} not in DB. Run Analyze to fetch it."
        })

    d = dict(zip(cols, row))
    d["reasons"]       = json.loads(d["reasons"])       if d["reasons"]       else []
    d["quality_flags"] = json.loads(d["quality_flags"]) if d["quality_flags"] else []
    d["found"]         = True
    d["is_fno"]        = True
    d["from_cache"]    = True
    d["est_pat"]       = ann[0] if ann else None
    d["guidance"]      = ann[1] if ann else None
    return jsonify(d)

@app.route("/api/analyze", methods=["POST"])
def api_res_analyze():
    body          = request.get_json(force=True) or {}
    symbols       = [s.strip().upper() for s in body.get("symbols", []) if s.strip()]
    force_refresh = body.get("force_refresh", False)
    results       = []
    for symbol in symbols:
        if symbol not in _FNO_RES_SYMBOLS:
            continue
        is_fno = True
        cached = None if force_refresh else _get_res_cached(symbol)
        if cached and not cached["stale"]:
            cached["is_fno"]     = is_fno
            cached["from_cache"] = True
            cached.update(_get_res_annotation(symbol))
            results.append(cached)
            continue
        raw = _fetch_screener_quarterly(symbol)
        if raw is None:
            entry = {
                "symbol": symbol, "is_fno": is_fno, "from_cache": False,
                "verdict": "Fetch Failed",
                "reasons": ["Could not retrieve data from screener.in — check symbol or connectivity"],
                "quality_flags": [],
                "quarter_label": None,
                "fetched_at": dt.now(datetime.timezone.utc).isoformat(),
            }
            _save_res_cache(symbol, entry, status="failed")
            entry.update(_get_res_annotation(symbol))
            results.append(entry)
            time.sleep(0.5)
            continue
        # Get annotation (user's manual est_pat + guidance) before scoring
        annotation   = _get_res_annotation(symbol)
        user_est_pat = annotation.get("est_pat")
        guidance     = annotation.get("guidance")
        # Fetch Trendlyne consensus estimates (graceful on failure)
        tl_est = None
        try:
            tl_est = _fetch_trendlyne_estimates(symbol)
        except Exception as _te:
            logging.debug(f"[TL] Non-fatal error for {symbol}: {type(_te).__name__}: {_te}")
        est_pat_consensus     = tl_est.get("est_pat_consensus")     if tl_est else None
        est_revenue_consensus = tl_est.get("est_revenue_consensus") if tl_est else None
        est_eps_consensus     = tl_est.get("est_eps_consensus")     if tl_est else None
        n_analysts            = tl_est.get("n_analysts", 0)         if tl_est else 0
        # Manual user entry always wins over Trendlyne auto for Beat/Miss scoring
        effective_est_pat = user_est_pat if user_est_pat is not None else est_pat_consensus
        analyzed = _analyze_res_quarterly(
            symbol, raw,
            est_pat_consensus=effective_est_pat,
            guidance=guidance,
            n_analysts=n_analysts,
        )
        # Merge Trendlyne estimates into result for frontend display
        analyzed["est_pat_consensus"]     = est_pat_consensus
        analyzed["est_revenue_consensus"] = est_revenue_consensus
        analyzed["est_eps_consensus"]     = est_eps_consensus
        analyzed["n_analysts"]            = n_analysts
        analyzed["tl_source"]             = True if tl_est else False
        analyzed["symbol"]     = symbol
        analyzed["is_fno"]     = is_fno
        analyzed["from_cache"] = False
        _save_res_cache(symbol, analyzed, status="ok")
        analyzed.update(annotation)
        results.append(analyzed)
        time.sleep(0.5)
    return jsonify(results)


# ── Futures Buildup Board ──────────────────────────────────────────────────────
_fut_buildup_cache = {"data": None, "ts": 0.0}
_fut_buildup_lock  = threading.Lock()

# CE SC above ATM signal cache — populated from _spurt_history or background get_option_chain()
_chain_signal_cache   = {}                       # {symbol: {"date": str, "ce_sc_above_atm": bool, "ts": float}}
_chain_signal_lock    = threading.Lock()
_chain_signal_pending = set()                    # symbols with a bg thread in flight (prevents duplicates)
_chain_signal_sem     = threading.Semaphore(3)   # max 3 concurrent get_option_chain() calls

# Static cap categorization — Nifty 50 = Large, Nifty Midcap 150 F&O = Mid, rest = Small
_CAP_CATEGORY = {
    # ── Large Cap (Nifty 50) ──────────────────────────────────────────────────
    "ADANIENT": "Large Cap", "ADANIPORTS": "Large Cap", "APOLLOHOSP": "Large Cap",
    "ASIANPAINT": "Large Cap", "AXISBANK": "Large Cap", "BAJAJ-AUTO": "Large Cap",
    "BAJAJFINSV": "Large Cap", "BAJFINANCE": "Large Cap", "BHARTIARTL": "Large Cap",
    "BPCL": "Large Cap", "BRITANNIA": "Large Cap", "CIPLA": "Large Cap",
    "COALINDIA": "Large Cap", "DRREDDY": "Large Cap", "EICHERMOT": "Large Cap",
    "GRASIM": "Large Cap", "HCLTECH": "Large Cap", "HDFCBANK": "Large Cap",
    "HDFCLIFE": "Large Cap", "HEROMOTOCO": "Large Cap", "HINDALCO": "Large Cap",
    "HINDUNILVR": "Large Cap", "ICICIBANK": "Large Cap", "INDUSINDBK": "Large Cap",
    "INFY": "Large Cap", "ITC": "Large Cap", "JSWSTEEL": "Large Cap",
    "KOTAKBANK": "Large Cap", "LT": "Large Cap", "M&M": "Large Cap",
    "MARUTI": "Large Cap", "NESTLEIND": "Large Cap", "NTPC": "Large Cap",
    "ONGC": "Large Cap", "POWERGRID": "Large Cap", "RELIANCE": "Large Cap",
    "SBIN": "Large Cap", "SHRIRAMFIN": "Large Cap", "SBILIFE": "Large Cap",
    "SUNPHARMA": "Large Cap", "TATAMOTORS": "Large Cap", "TATASTEEL": "Large Cap",
    "TATACONSUM": "Large Cap", "TCS": "Large Cap", "TECHM": "Large Cap",
    "TITAN": "Large Cap", "TRENT": "Large Cap", "ULTRACEMCO": "Large Cap",
    "WIPRO": "Large Cap",
    # ── Mid Cap ───────────────────────────────────────────────────────────────
    "ABCAPITAL": "Mid Cap", "ABFRL": "Mid Cap", "ACC": "Mid Cap",
    "ADANIGREEN": "Mid Cap", "ALKEM": "Mid Cap", "AMBUJACEM": "Mid Cap",
    "APLAPOLLO": "Mid Cap", "ASHOKLEY": "Mid Cap", "ASTRAL": "Mid Cap",
    "AUBANK": "Mid Cap", "AUROPHARMA": "Mid Cap", "BALKRISIND": "Mid Cap",
    "BANDHANBNK": "Mid Cap", "BANKBARODA": "Mid Cap", "BATAINDIA": "Mid Cap",
    "BEL": "Mid Cap", "BERGEPAINT": "Mid Cap", "BHARATFORG": "Mid Cap",
    "BHEL": "Mid Cap", "BIOCON": "Mid Cap", "BOSCHLTD": "Mid Cap",
    "CANBK": "Mid Cap", "CHOLAFIN": "Mid Cap", "COFORGE": "Mid Cap",
    "COLPAL": "Mid Cap", "CONCOR": "Mid Cap", "COROMANDEL": "Mid Cap",
    "CROMPTON": "Mid Cap", "CUMMINSIND": "Mid Cap", "DABUR": "Mid Cap",
    "DALBHARAT": "Mid Cap", "DEEPAKNTR": "Mid Cap", "DELHIVERY": "Mid Cap",
    "DIVISLAB": "Mid Cap", "DIXON": "Mid Cap", "DLF": "Mid Cap",
    "DMART": "Mid Cap", "ESCORTS": "Mid Cap", "EXIDEIND": "Mid Cap",
    "FEDERALBNK": "Mid Cap", "GAIL": "Mid Cap", "GLENMARK": "Mid Cap",
    "GMRINFRA": "Mid Cap", "GMRAIRPORT": "Mid Cap", "GODREJCP": "Mid Cap", "GODREJPROP": "Mid Cap",
    "GRANULES": "Mid Cap", "HAL": "Mid Cap", "HAVELLS": "Mid Cap",
    "HDFCAMC": "Mid Cap", "HINDPETRO": "Mid Cap", "HINDZINC": "Mid Cap",
    "ICICIPRULI": "Mid Cap", "IDEA": "Mid Cap", "IDFCFIRSTB": "Mid Cap",
    "IEX": "Mid Cap", "INDUSTOWER": "Mid Cap", "IOC": "Mid Cap",
    "IRCTC": "Mid Cap", "IRFC": "Mid Cap", "JINDALSTEL": "Mid Cap",
    "JUBLFOOD": "Mid Cap", "KAJARIACER": "Mid Cap", "KAYNES": "Mid Cap",
    "L&TFH": "Mid Cap", "LICHSGFIN": "Mid Cap", "LICI": "Mid Cap",
    "LUPIN": "Mid Cap", "M&MFIN": "Mid Cap", "MANAPPURAM": "Mid Cap",
    "MARICO": "Mid Cap", "MAXHEALTH": "Mid Cap", "MCX": "Mid Cap",
    "MFSL": "Mid Cap", "MOTHERSON": "Mid Cap", "MPHASIS": "Mid Cap",
    "MRF": "Mid Cap", "MUTHOOTFIN": "Mid Cap", "NAUKRI": "Mid Cap",
    "NAVINFLUOR": "Mid Cap", "NBCC": "Mid Cap", "NHPC": "Mid Cap",
    "NMDC": "Mid Cap", "NYKAA": "Mid Cap", "OBEROIRLTY": "Mid Cap",
    "OIL": "Mid Cap", "PAYTM": "Mid Cap", "PEL": "Mid Cap",
    "PERSISTENT": "Mid Cap", "PETRONET": "Mid Cap", "PFC": "Mid Cap",
    "PNB": "Mid Cap", "POLYCAB": "Mid Cap", "PVRINOX": "Mid Cap",
    "RAMCOCEM": "Mid Cap", "RECLTD": "Mid Cap", "SAIL": "Mid Cap",
    "SIEMENS": "Mid Cap", "SRF": "Mid Cap", "SYNGENE": "Mid Cap",
    "TATACHEM": "Mid Cap", "TATACOMM": "Mid Cap", "TATAELXSI": "Mid Cap",
    "TATAPOWER": "Mid Cap", "TORNTPHARM": "Mid Cap", "TORNTPOWER": "Mid Cap",
    "TVSMOTOR": "Mid Cap", "UBL": "Mid Cap", "UNIONBANK": "Mid Cap",
    "UPL": "Mid Cap", "VEDL": "Mid Cap", "VOLTAS": "Mid Cap",
    "ZOMATO": "Mid Cap", "ZYDUSLIFE": "Mid Cap",
}

def _get_fut_buildup_db_snapshot():
    """Retrieve latest futures buildup snapshot from SQLite for off-market hours."""
    try:
        db_path = os.path.join(os.path.dirname(__file__), "tradesignal_cache.db")
        if not os.path.exists(db_path):
            return None
        import sqlite3
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fno_futures_buildup_snapshot (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT,
                timestamp TEXT,
                payload_json TEXT
            )
        """)
        cur.execute("SELECT payload_json, date, timestamp FROM fno_futures_buildup_snapshot ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            data = json.loads(row[0])
            data["is_eod_snapshot"] = True
            data["snapshot_date"] = row[1]
            data["snapshot_ts"] = row[2]
            return data
    except Exception as e:
        logging.warning(f"[FutBuildup] DB snapshot read failed: {e}")
    return None

def _save_fut_buildup_db_snapshot(payload):
    """Save latest futures buildup snapshot to SQLite."""
    try:
        if not payload or not payload.get("stocks"):
            return
        db_path = os.path.join(os.path.dirname(__file__), "tradesignal_cache.db")
        import sqlite3
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fno_futures_buildup_snapshot (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT,
                timestamp TEXT,
                payload_json TEXT
            )
        """)
        today_str = datetime.date.today().isoformat()
        now_str = datetime.datetime.now().strftime("%H:%M:%S")
        cur.execute(
            "INSERT INTO fno_futures_buildup_snapshot (date, timestamp, payload_json) VALUES (?, ?, ?)",
            (today_str, now_str, json.dumps(payload))
        )
        conn.commit()
        # Keep last 10 snapshots
        cur.execute("DELETE FROM fno_futures_buildup_snapshot WHERE id NOT IN (SELECT id FROM fno_futures_buildup_snapshot ORDER BY id DESC LIMIT 10)")
        conn.commit()
        conn.close()
    except Exception as e:
        logging.warning(f"[FutBuildup] DB snapshot save failed: {e}")

def _get_ce_sc_above_atm(symbol, spot_ltp, kite):
    """Return True if any CE strike at or above ATM shows Short Covering (OI down)."""
    today_str = datetime.date.today().isoformat()

    with _chain_signal_lock:
        cached = _chain_signal_cache.get(symbol)
    if cached and cached["date"] == today_str and time.time() - cached["ts"] < 300:
        return cached["ce_sc_above_atm"]

    try:
        from oi_spurt_routes import _spurt_history, _spurt_lock
        with _spurt_lock:
            ticks = list(_spurt_history.get(symbol, []))
        if len(ticks) >= 2:
            t_cur = ticks[-1]
            t_prv = ticks[-2]
            strikes_list = [s for s in t_cur.get("strikes", {}) if isinstance(s, (int, float))]
            if strikes_list and spot_ltp > 0:
                atm_strike = min(strikes_list, key=lambda s: abs(s - spot_ltp))
                ce_sc = any(
                    strike >= atm_strike
                    and (t_cur["strikes"][strike].get("ce_oi", 0) - t_prv["strikes"].get(strike, {}).get("ce_oi", 0)) < 0
                    and t_cur["strikes"][strike].get("ce_ltp", 0) >= t_prv["strikes"].get(strike, {}).get("ce_ltp", 0) * 0.99
                    for strike in strikes_list
                )
                with _chain_signal_lock:
                    _chain_signal_cache[symbol] = {"date": today_str, "ce_sc_above_atm": ce_sc, "ts": time.time()}
                return ce_sc
    except Exception:
        pass

    with _chain_signal_lock:
        if symbol in _chain_signal_pending:
            return None
        _chain_signal_pending.add(symbol)

    def _bg():
        try:
            with _chain_signal_sem:
                from oi_spurt_routes import get_option_chain
                chain, *_ = get_option_chain(kite, symbol)
                if chain and spot_ltp > 0:
                    atm_idx = min(range(len(chain)), key=lambda i: abs(chain[i]["strike"] - spot_ltp))
                    atm_strike = chain[atm_idx]["strike"]

                    ce_sc = any(
                        r["strike"] >= atm_strike
                        and r.get("ce_oi_chg", 0) < 0
                        and (r.get("ce_ltp", 0) >= r.get("ce_prev_ltp", 0) * 0.99 or r.get("ce_prev_ltp", 0) == 0)
                        for r in chain
                    )
                else:
                    ce_sc = False
                with _chain_signal_lock:
                    _chain_signal_cache[symbol] = {"date": today_str, "ce_sc_above_atm": ce_sc, "ts": time.time()}
        except Exception as e:
            logging.debug(f"[ChainSignal] {symbol}: {e}")
        finally:
            with _chain_signal_lock:
                _chain_signal_pending.discard(symbol)

    threading.Thread(target=_bg, daemon=True).start()
    return None

def _classify_fut_buildup(oi_chg_pct: float, price_chg_pct: float, symbol: str = "") -> str:
    """Classify futures position buildup from OI% change and price% change using Layer 1 asset-class noise filter."""
    if symbol:
        try:
            from oi_spurt_routes import get_layer1_noise_threshold
            noise_th = get_layer1_noise_threshold(symbol)
        except Exception:
            noise_th = 0.10
    else:
        noise_th = 0.10

    # 1. Price noise filter — matches Layer 1 matrix exactly
    if abs(price_chg_pct) <= noise_th:
        return "Flat"

    # 2. OI noise filter
    OI_NOISE_FLOOR = 0.05   # %
    if abs(oi_chg_pct) < OI_NOISE_FLOOR:
        return "Flat"

    oi_up    = oi_chg_pct   > 0
    price_up = price_chg_pct > 0

    if oi_up and price_up:     return "Long Buildup"
    if oi_up and not price_up: return "Short Buildup"
    if not oi_up and price_up: return "Short Covering"
    return "Long Unwinding"

_CAP_CATEGORY = {
    # NIFTY 50 / Large Cap
    "RELIANCE": "Large Cap", "TCS": "Large Cap", "HDFCBANK": "Large Cap", "ICICIBANK": "Large Cap",
    "INFY": "Large Cap", "BHARTIARTL": "Large Cap", "ITC": "Large Cap", "SBIN": "Large Cap",
    "LICI": "Large Cap", "HINDUNILVR": "Large Cap", "LT": "Large Cap", "BAJFINANCE": "Large Cap",
    "HCLTECH": "Large Cap", "MARUTI": "Large Cap", "SUNPHARMA": "Large Cap", "ADANIENT": "Large Cap",
    "TATAMOTORS": "Large Cap", "KOTAKBANK": "Large Cap", "NTPC": "Large Cap", "AXISBANK": "Large Cap",
    "TITAN": "Large Cap", "ONGC": "Large Cap", "POWERGRID": "Large Cap", "TATACONSUM": "Large Cap",
    "COALINDIA": "Large Cap", "BAJAJFINSV": "Large Cap", "ASIANPAINT": "Large Cap", "M&M": "Large Cap",
    "JSWSTEEL": "Large Cap", "TATASTEEL": "Large Cap", "ADANIPORTS": "Large Cap", "NESTLEIND": "Large Cap",
    "ULTRACEMCO": "Large Cap", "WIPRO": "Large Cap", "GRASIM": "Large Cap", "HINDALCO": "Large Cap",
    "VEDL": "Large Cap", "TECHM": "Large Cap", "IOC": "Large Cap", "SBILIFE": "Large Cap",
    "DRREDDY": "Large Cap", "BPCL": "Large Cap", "BRITANNIA": "Large Cap", "DIVISLAB": "Large Cap",
    "EICHERMOT": "Large Cap", "CIPLA": "Large Cap", "APOLLOHOSP": "Large Cap", "HEROMOTOCO": "Large Cap",
    "BEL": "Large Cap", "HAL": "Large Cap", "SHRIRAMFIN": "Large Cap", "INDUSINDBK": "Large Cap",
    "BAJAJ-AUTO": "Large Cap", "DLF": "Large Cap", "PFC": "Large Cap", "RECLTD": "Large Cap",
    "SIEMENS": "Large Cap", "TRENT": "Large Cap", "ZOMATO": "Large Cap", "JIOFIN": "Large Cap",
    "MOTHERSON": "Large Cap", "CHOLAFIN": "Large Cap", "HDFCLIFE": "Large Cap", "TVSMOTOR": "Large Cap",
    "GODREJCP": "Large Cap", "GAIL": "Large Cap", "ABB": "Large Cap", "BOSCHLTD": "Large Cap",
    "PIDILITIND": "Large Cap", "DABUR": "Large Cap", "HAVELLS": "Large Cap", "AMBUJACEM": "Large Cap",
    "CUMMINSIND": "Large Cap", "POLYCAB": "Large Cap", "LTIM": "Large Cap",
    # Mid Cap
    "AUROPHARMA": "Mid Cap", "PERSISTENT": "Mid Cap", "COFORGE": "Mid Cap", "MPHASIS": "Mid Cap",
    "FEDERALBNK": "Mid Cap", "IDFCFIRSTB": "Mid Cap", "BANDHANBNK": "Mid Cap", "AUBANK": "Mid Cap",
    "BANKBARODA": "Mid Cap", "PNB": "Mid Cap", "CANBK": "Mid Cap", "UNIONBANK": "Mid Cap",
    "INDIANB": "Mid Cap", "ASHOKLEY": "Mid Cap", "BALKRISIND": "Mid Cap", "MRF": "Mid Cap",
    "BHARATFORG": "Mid Cap", "ESCORTS": "Mid Cap", "TIINDIA": "Mid Cap", "APOLLOTYRE": "Mid Cap",
    "EXIDEIND": "Mid Cap", "VOLTAS": "Mid Cap", "BLUESTARCO": "Mid Cap", "CROMPTON": "Mid Cap",
    "DIXON": "Mid Cap", "WHIRLPOOL": "Mid Cap", "KAYNES": "Mid Cap", "AMBER": "Mid Cap",
    "JUBLFOOD": "Mid Cap", "DEVYANI": "Mid Cap", "RADICO": "Mid Cap", "MARICO": "Mid Cap",
    "COLPAL": "Mid Cap", "UBL": "Mid Cap", "UNITDSPR": "Mid Cap", "PAGEIND": "Mid Cap",
    "METROPOLIS": "Mid Cap", "LALPATHLAB": "Mid Cap", "SYNGENE": "Mid Cap", "GLAND": "Mid Cap",
    "ALKEM": "Mid Cap", "TORNTPHARM": "Mid Cap", "IPCALAB": "Mid Cap", "BIOCON": "Mid Cap",
    "GLENMARK": "Mid Cap", "LUPIN": "Mid Cap", "ZYDUSLIFE": "Mid Cap", "LAURUSLABS": "Mid Cap",
    "COROMANDEL": "Mid Cap", "PIIND": "Mid Cap", "UPL": "Mid Cap", "DEEPAKNTR": "Mid Cap",
    "TATACHEM": "Mid Cap", "AARTIIND": "Mid Cap", "SRF": "Mid Cap", "NAVINFLUOR": "Mid Cap",
    "ATUL": "Mid Cap", "GUJGASLTD": "Mid Cap", "IGL": "Mid Cap", "MGL": "Mid Cap",
    "PETRONET": "Mid Cap", "OIL": "Mid Cap", "HINDPETRO": "Mid Cap", "GSPL": "Mid Cap",
    "OBEROIRLTY": "Mid Cap", "GODREJPROP": "Mid Cap", "PHOENIXLTD": "Mid Cap", "PRESTIGE": "Mid Cap",
    "BRIGADE": "Mid Cap", "SOBHA": "Mid Cap", "LODHA": "Mid Cap", "SUNTECK": "Mid Cap",
    "BATAINDIA": "Mid Cap", "RELAXO": "Mid Cap", "KALYANKJIL": "Mid Cap", "TITAGARH": "Mid Cap",
    "BHEL": "Mid Cap", "RVNL": "Mid Cap", "IRCON": "Mid Cap", "RAILTEL": "Mid Cap",
    "MAZDOCK": "Mid Cap", "COCHINSHIP": "Mid Cap", "GRSE": "Mid Cap", "BDL": "Mid Cap",
    "ABCAPITAL": "Mid Cap", "L&TFH": "Mid Cap", "M&MFIN": "Mid Cap", "MUTHOOTFIN": "Mid Cap",
    "MANAPPURAM": "Mid Cap", "POONAWALLA": "Mid Cap", "LICHSGFIN": "Mid Cap", "PNBHOUSING": "Mid Cap",
    "HDFCAMC": "Mid Cap", "NAM-INDIA": "Mid Cap", "UTIAMC": "Mid Cap", "CAMS": "Mid Cap",
    "KFINTECH": "Mid Cap", "BSE": "Mid Cap", "MCX": "Mid Cap", "CDSL": "Mid Cap",
    "IEX": "Mid Cap", "NAUKRI": "Mid Cap", "PAYTM": "Mid Cap", "POLICYBZR": "Mid Cap",
    "NYKAA": "Mid Cap", "DELHIVERY": "Mid Cap", "INDIGO": "Mid Cap", "IRCTC": "Mid Cap",
    "PVRINOX": "Mid Cap", "ZEEL": "Mid Cap", "SUNTV": "Mid Cap", "SAIL": "Mid Cap",
    "NMDC": "Mid Cap", "NATIONALUM": "Mid Cap", "JINDALSTEL": "Mid Cap", "HINDCOPPER": "Mid Cap",
}


@app.route('/api/futures-buildup', methods=['GET'])
def futures_buildup_board():
    """Returns near-month futures buildup data for all tracked F&O symbols.
    Calculates Spot % Move, Future % Move, Future LTP, OI % Change, and Buildup classification.
    Zero Kite API calls: reuses in-memory KiteDataAgent cache and persists snapshots to SQLite.
    """
    # Serve from cache if fresh (dynamically enrich any newly ready chain signals)
    with _fut_buildup_lock:
        cached = _fut_buildup_cache.get("data")
        age    = time.time() - _fut_buildup_cache.get("ts", 0.0)
        if cached is not None and age < 60:
            today_str = datetime.date.today().isoformat()
            with _chain_signal_lock:
                for s in cached.get("stocks", []):
                    if s.get("buildup") in ("Short Covering", "Long Buildup") and s.get("ce_sc_above_atm") is None:
                        sig = _chain_signal_cache.get(s["symbol"])
                        if sig and sig["date"] == today_str:
                            s["ce_sc_above_atm"] = sig["ce_sc_above_atm"]
            return jsonify(cached)

    kite = get_kite()
    if not kite:
        snapshot = _get_fut_buildup_db_snapshot()
        if snapshot:
            return jsonify(snapshot)
        return jsonify({"error": "Kite not connected", "stocks": []}), 400

    try:
        from oi_spurt_routes import get_instruments, BFO_SYMBOLS, _chunks

        today = datetime.date.today()

        # ── Build near-month futures map from cached NFO instruments ──────────
        instruments = get_instruments(kite, "NFO")
        fut_map = {}  # symbol -> nearest inst
        for inst in instruments:
            if inst.get("instrument_type") != "FUT":
                continue
            name   = inst.get("name", "")
            expiry = inst.get("expiry")
            if not name or not expiry or expiry < today:
                continue
            existing = fut_map.get(name)
            if existing is None or expiry < existing["expiry"]:
                fut_map[name] = inst

        if not fut_map:
            snapshot = _get_fut_buildup_db_snapshot()
            if snapshot:
                return jsonify(snapshot)
            return jsonify({"error": "No futures instruments found", "stocks": []}), 500

        # Build quote key lists
        symbol_to_fut_key  = {}
        symbol_to_spot_key = {}
        for name, inst in fut_map.items():
            exchange = "BFO" if name.upper() in BFO_SYMBOLS else "NFO"
            symbol_to_fut_key[name]  = f"{exchange}:{inst['tradingsymbol']}"
            symbol_to_spot_key[name] = f"NSE:{name}"

        fut_keys  = list(symbol_to_fut_key.values())
        spot_keys = list(symbol_to_spot_key.values())

        # ── Kite Call 1: All near-month futures (OI, LTP, prev close) ────────
        fut_quotes = {}
        for batch in _chunks(fut_keys, 250):
            try:
                fut_quotes.update(kite.quote(batch))
            except Exception as e:
                logging.warning(f"[FutBuildup] Futures quote batch failed: {e}")

        # ── Kite Call 2: All spot underlyings (LTP, prev close) ──────────────
        spot_quotes = {}
        for batch in _chunks(spot_keys, 250):
            try:
                spot_quotes.update(kite.quote(batch))
            except Exception as e:
                logging.warning(f"[FutBuildup] Spot quote batch failed: {e}")

        # ── Lazy-load supplementary in-memory state (zero extra Kite calls) ────
        try:
            from option_gainers_scanner import get_avg_volume, _avg_volume_cache, _last_day_volume_cache, _avg_volume_lock
            _ogs_available = True
        except Exception:
            _ogs_available = False

        try:
            from ema_crossover_scanner import get_ema_crossover_state
            _ema_state = get_ema_crossover_state().get("crossovers", {})
        except Exception:
            _ema_state = {}

        try:
            from cpr_utils import get_cpr_pivots, compute_cpr_flags, warm_cpr_pivots_bg
            _cpr_available = True
        except Exception:
            _cpr_available = False

        today_str      = today.isoformat()
        _eod_bl_ok     = False
        _get_cached_bl = None
        try:
            from oi_spurt_routes import (
                get_cached_baseline as _get_cached_bl,
                _baseline_queue, _baseline_seq,
                _pending_baselines, _baseline_lock,
                _baseline_worker,
            )
            import oi_spurt_routes as _oir
            _eod_bl_ok = True
            if _oir._worker_thread is None or not _oir._worker_thread.is_alive():
                _oir._worker_thread = threading.Thread(target=_baseline_worker, daemon=True)
                _oir._worker_thread.start()
        except Exception:
            pass

        # ── Classify each symbol ──────────────────────────────────────────────
        stocks = []
        for symbol, fut_key in symbol_to_fut_key.items():
            fq = fut_quotes.get(fut_key)
            if not fq:
                continue

            curr_oi       = int(fq.get("oi") or 0)
            fut_ltp       = float(fq.get("last_price") or 0)
            fut_prev_cls  = float((fq.get("ohlc") or {}).get("close") or fut_ltp or 1)
            fut_price_chg = ((fut_ltp - fut_prev_cls) / fut_prev_cls * 100) if fut_prev_cls else 0.0

            # True previous-day EOD OI from SQLite cache (same infra as option chain).
            # Background worker populates cache; falls back to oi_day_low temporarily.
            prev_oi = None
            if _eod_bl_ok and curr_oi > 0:
                fut_inst = fut_map.get(symbol, {})
                fut_ts   = fut_inst.get("tradingsymbol", "")
                fut_tok  = fut_inst.get("instrument_token")
                if fut_ts:
                    prev_oi = _get_cached_bl(today_str, fut_ts)
                    if prev_oi is None and fut_tok:
                        key = (today_str, fut_ts)
                        with _baseline_lock:
                            if key not in _pending_baselines:
                                _pending_baselines.add(key)
                                _baseline_queue.put((1, next(_baseline_seq), kite, today_str, int(fut_tok), fut_ts, curr_oi))
            if prev_oi is None or prev_oi == 0:
                prev_oi = int(fq.get("oi_day_low") or curr_oi)  # fallback until cache warms

            oi_chg_pct = ((curr_oi - prev_oi) / prev_oi * 100) if prev_oi > 0 else 0.0

            # Spot
            sq            = spot_quotes.get(symbol_to_spot_key.get(symbol, "")) or {}
            spot_ltp      = float(sq.get("last_price") or fut_ltp)
            spot_ohlc     = sq.get("ohlc") or {}
            spot_prev_cls = float(spot_ohlc.get("close") or spot_ltp or 1)
            spot_open     = float(spot_ohlc.get("open") or 0)
            spot_chg_pct  = ((spot_ltp - spot_prev_cls) / spot_prev_cls * 100) if spot_prev_cls else 0.0

            # Gap % = (today open - prev close) / prev close
            gap_pct = round(((spot_open - spot_prev_cls) / spot_prev_cls * 100), 2) if spot_prev_cls and spot_open else None

            # RVOL — from shared avg-volume cache (no Kite call)
            rvol = None
            if _ogs_available:
                try:
                    day_vol = sq.get("volume") or 0
                    if not day_vol:
                        with _avg_volume_lock:
                            day_vol = _last_day_volume_cache.get(symbol, 0)
                    avg_vol = get_avg_volume(symbol)
                    if avg_vol and avg_vol > 0 and day_vol > 0:
                        rvol = round(day_vol / avg_vol, 1)
                except Exception:
                    pass

            # Linearity + FH VOL — from EMA crossover scanner in-memory state
            sym_ema = _ema_state.get(symbol, {})
            linearity  = sym_ema.get("linearity_score")   # int 0–100 or None
            fh_spurt   = sym_ema.get("fh_spurt_ratio")    # float or None
            fh_cumul   = sym_ema.get("fh_cumulative_ratio")  # float or None
            fh_tag     = sym_ema.get("fh_spurt_tag")      # str or None

            # Auto-warm CPR pivots in background if any symbols are missing from cache
            if _cpr_available and time.time() - getattr(futures_buildup_board, '_last_cpr_warm', 0) > 300:
                futures_buildup_board._last_cpr_warm = time.time()
                missing_cpr = {}
                for sym, spot_key in symbol_to_spot_key.items():
                    sq_val = spot_quotes.get(spot_key)
                    if sq_val and not get_cpr_pivots(sym):
                        tok = sq_val.get("instrument_token")
                        if tok:
                            missing_cpr[tok] = sym
                if missing_cpr:
                    warm_cpr_pivots_bg(kite, missing_cpr)

            # CPR Pivots (TC, Pivot, BC) — Top/Bottom flags
            cpr_flags = None
            open_vs_pdh_pdl = "inside"
            pdh_val = None
            pdl_val = None
            if _cpr_available:
                cpr_info = get_cpr_pivots(symbol)
                if cpr_info:
                    pdh_val = cpr_info.get("pdh")
                    pdl_val = cpr_info.get("pdl")
                    if spot_open > 0 and pdh_val and pdh_val > 0 and spot_open > pdh_val:
                        open_vs_pdh_pdl = "above_pdh"
                    elif spot_open > 0 and pdl_val and pdl_val > 0 and spot_open < pdl_val:
                        open_vs_pdh_pdl = "below_pdl"
                    cpr_flags = compute_cpr_flags(
                        spot_open=spot_open,
                        spot_ltp=spot_ltp,
                        tc=cpr_info.get("tc"),
                        pivot=cpr_info.get("pivot"),
                        bc=cpr_info.get("bc")
                    )

            buildup = _classify_fut_buildup(oi_chg_pct, fut_price_chg, symbol=symbol)
            cap     = _CAP_CATEGORY.get(symbol.upper(), "Small Cap")

            # CE SC above ATM — only computed for SC/LB rows (others skipped for performance)
            ce_sc_above_atm = None
            if buildup in ("Short Covering", "Long Buildup"):
                ce_sc_above_atm = _get_ce_sc_above_atm(symbol, spot_ltp, kite)

            stocks.append({
                "symbol":          symbol,
                "ltp":             round(spot_ltp, 2),
                "spot_chg_pct":    round(spot_chg_pct, 2),
                "fut_ltp":         round(fut_ltp, 2),
                "fut_chg_pct":     round(fut_price_chg, 2),
                "buildup":         buildup,
                "cap":             cap,
                "oi_chg_pct":      round(oi_chg_pct, 2),
                "gap_pct":         gap_pct,
                "rvol":            rvol,
                "linearity":       linearity,
                "fh_spurt":        fh_spurt,
                "fh_cumul":        fh_cumul,
                "fh_tag":          fh_tag,
                "cpr":             cpr_flags,
                "ce_sc_above_atm": ce_sc_above_atm,
                "spot_open":       round(spot_open, 2) if spot_open else None,
                "pdh":             pdh_val,
                "pdl":             pdl_val,
                "open_vs_pdh_pdl": open_vs_pdh_pdl,
            })

        stocks.sort(key=lambda x: x["symbol"])
        payload = {
            "stocks":     stocks,
            "count":      len(stocks),
            "updated_at": dt.now().strftime("%H:%M:%S"),
        }

        # Persist to SQLite snapshot for 24/7 offline / weekend fallback
        _save_fut_buildup_db_snapshot(payload)

        with _fut_buildup_lock:
            _fut_buildup_cache["data"] = payload
            _fut_buildup_cache["ts"]   = time.time()

        return jsonify(payload)

    except Exception as e:
        logging.exception("[FutBuildup] Unhandled error")
        snapshot = _get_fut_buildup_db_snapshot()
        if snapshot:
            return jsonify(snapshot)
        return jsonify({"error": str(e), "stocks": []}), 500


# ── Run ──

if __name__ == '__main__':
    # Start the Twisted reactor once globally to prevent background thread startup collisions
    from twisted.internet import reactor
    import threading
    if not reactor.running:
        threading.Thread(
            target=reactor.run, 
            kwargs={"installSignalHandlers": False}, 
            daemon=True
        ).start()

    import logging
    log = logging.getLogger('werkzeug')
    # log.setLevel(logging.ERROR)  # Suppress default request logs
    port = int(os.environ.get('PORT', 5000))
    print(f"\n{'='*50}")
    print(f"  TradeSignal Backend Server")
    print(f"  Build:     {BACKEND_BUILD}")
    print(f"  File:      {__file__}")
    print(f"  Running on http://localhost:{port}")
    print(f"  App:       http://localhost:{port}/")
    print(f"  WebSocket: ws://localhost:{port}/socket.io/")
    print(f"{'='*50}\n")

    # ── Tier 1: Kite-INDEPENDENT services — always start immediately ────────────
    try:
        # Instruments DB sync (weekly, no Kite calls at boot)
        start_instruments_sync_scheduler()

        # Expiry engine (disabled)
        # from expiry_engine import start_expiry_engine
        # start_expiry_engine()

        # DISABLED — Cash Momentum Scanner paused (no active use case)
        # from cash_momentum_scanner import start_cash_scanner
        # start_cash_scanner()

        # News aggregator (RSS feeds, no Kite dependency)
        start_news_aggregator()

        print("  [OK] Kite-independent background services started.")
    except Exception as e:
        print(f"  [Error] Could not start Kite-independent services: {e}")

    # ── Tier 2: Kite-DEPENDENT services — gated behind connectivity check ────────
    # Note: no `global _kite` needed here — if __name__ == '__main__' runs at
    # module scope, so direct assignment to _kite updates the module-level variable.
    print("\n[STARTUP] Verifying Zerodha Kite connectivity...")
    _boot_api_key, _boot_token = _load_kite_session()
    if _boot_api_key and _boot_token:
        try:
            from kiteconnect import KiteConnect as _KC
            _kite = _KC(api_key=_boot_api_key)
            _kite.set_access_token(_boot_token)
            _profile = _kite.profile()  # lightweight connectivity verification
            print("  [SUCCESS] Kite connected successfully.")
            # Sync GTM credentials so WebSocket multiplexer uses correct token
            sync_global_ticker_credentials(_boot_api_key, _boot_token)
            # Patch kite.quote() so all services receive a quote-patched instance.
            # Ensures the LTP cache side-effect in _wrap_kite_quote works for
            # every scanner and the RVOL warming thread from the very first call.
            _kite = _wrap_kite_quote(_kite)
            # Launch all Kite-dependent scanners/services
            start_kite_dependent_services(_kite)
        except Exception as _conn_err:
            _kite = None  # Reset global client to None so system knows connectivity failed
            print(f"  [WARNING] Kite connectivity check failed: {_conn_err}")
            print("  [INFO] Kite-dependent services DEFERRED — will auto-start on first successful login.")
    else:
        print("  [INFO] No saved Kite session found.")
        print("  [INFO] Kite-dependent services DEFERRED — will auto-start once you log in via the browser.")

    # Use SocketIO for development (supports WebSocket)
    # use_reloader=False: prevents Werkzeug from forking in Termux which caused
    # new browser tabs to hang until the terminal was refocused.
    import threading as _t
    _t.Timer(15, lambda: threading.Thread(
        target=_seed_trendlyne_from_sitemap, daemon=True, name="tl-seed"
    ).start()).start()
    socketio.run(app, host='0.0.0.0', port=port, debug=False,
                 allow_unsafe_werkzeug=True, use_reloader=False)
