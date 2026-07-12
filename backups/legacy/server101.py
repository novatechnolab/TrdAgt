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
from datetime import datetime as dt, timedelta
import pandas as pd
import numpy as np
from flask import Flask, request, jsonify, send_from_directory, g, Response
from flask_compress import Compress
from flask_cors import CORS
from flask_socketio import SocketIO, emit
from dotenv import load_dotenv

# Load .env from multiple candidate paths so it works regardless of
# what directory the server is launched from (important for Termux).
_server_dir = os.path.dirname(os.path.abspath(__file__))
_env_candidates = [
    os.path.join(_server_dir, '../../.env'),          # launched from app/backend
    os.path.join(_server_dir, '../.env'),             # launched from app/
    os.path.join(_server_dir, '.env'),                # launched from project root
    os.path.join(os.getcwd(), '.env'),                # current working dir
    os.path.expanduser('~/TradeSignal/.env'),         # Termux home fallback
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

app = Flask(__name__, static_folder='..', static_url_path='')
# Security: use env variable or generate a random key — never a hardcoded default
app.secret_key = os.environ.get('APP_SECRET_KEY') or os.urandom(32).hex()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=False,  # Set to False for local HTTP (important!)
    SESSION_COOKIE_SAMESITE='Lax',
)
CORS(app)
Compress(app)
socketio = SocketIO(app, cors_allowed_origins="*")
from flask import session

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

# ── Database path ──
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
    ''')
    conn.commit()
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


# ══════════════════════════════════════════════════════════════
# ── Kite client helper ──
# ══════════════════════════════════════════════════════════════

def get_kite():
    """Get the global KiteConnect instance or reconstruct from session."""
    global _kite
    
    # Try to get from session if global is missing
    if _kite is None:
        api_key = session.get('kite_api_key') or os.environ.get('KITE_API_KEY')
        access_token = session.get('kite_access_token')
        if api_key and access_token:
            from kiteconnect import KiteConnect
            _kite = KiteConnect(api_key=api_key)
            _kite.set_access_token(access_token)
            
    return _kite


# ══════════════════════════════════════════════════════════════
# ── Technical Indicators ──
# ══════════════════════════════════════════════════════════════

def tv_rma(series, length):
    """TradingView equivalent RMA (used in ATR/ADX/RSI). Uses SMA for the first valid value."""
    res = series.copy()
    res.iloc[:] = np.nan
    valid_idx = series.first_valid_index()
    if valid_idx is None:
        return res
    start_pos = series.index.get_loc(valid_idx)
    sma_pos = start_pos + length - 1
    if sma_pos >= len(series):
        return res
    
    first_sma = series.iloc[start_pos : sma_pos + 1].mean()
    res.iloc[sma_pos] = first_sma
    res.iloc[sma_pos + 1:] = series.iloc[sma_pos + 1:]
    return res.ewm(alpha=1/length, adjust=False).mean()

def tv_ema(series, length):
    """TradingView equivalent EMA. Uses SMA for the first valid value."""
    res = series.copy()
    res.iloc[:] = np.nan
    valid_idx = series.first_valid_index()
    if valid_idx is None:
        return res
    start_pos = series.index.get_loc(valid_idx)
    sma_pos = start_pos + length - 1
    if sma_pos >= len(series):
        return res
    
    first_sma = series.iloc[start_pos : sma_pos + 1].mean()
    res.iloc[sma_pos] = first_sma
    res.iloc[sma_pos + 1:] = series.iloc[sma_pos + 1:]
    return res.ewm(span=length, adjust=False).mean()

def ema(series, period):
    """Exponential Moving Average"""
    return tv_ema(series, period)


def vwap(df):
    """Volume Weighted Average Price"""
    df = df.copy()
    df['tp'] = (df['high'] + df['low'] + df['close']) / 3

    df['cum_vol'] = df.groupby(df.index.date)['volume'].cumsum()
    df['cum_pv'] = (df['tp'] * df['volume']).groupby(df.index.date).cumsum()

    df['vwap'] = df['cum_pv'] / df['cum_vol']
    return df['vwap']


def atr(df, period=14):
    """Average True Range"""
    high = df['high']
    low = df['low']
    close = df['close']

    prev_close = close.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)

    return tv_rma(tr, period)


def adx(df, period=14):
    """Average Directional Index"""
    high = df['high']
    low = df['low']
    close = df['close']

    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    plus_dm = high - prev_high
    minus_dm = prev_low - low

    plus_dm = np.where((plus_dm > minus_dm) & (plus_dm > 0), plus_dm, 0)
    minus_dm = np.where((minus_dm > plus_dm) & (minus_dm > 0), minus_dm, 0)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)

    atr_ = tv_rma(tr, period)

    plus_di = 100 * (tv_rma(pd.Series(plus_dm, index=df.index), period) / atr_)
    minus_di = 100 * (tv_rma(pd.Series(minus_dm, index=df.index), period) / atr_)

    dx = 100 * (abs(plus_di - minus_di) / (plus_di + minus_di))

    adx_val = tv_rma(dx, period)

    return adx_val, plus_di, minus_di


def macd(close):
    """MACD (Moving Average Convergence Divergence)"""
    ema12 = tv_ema(close, 12)
    ema26 = tv_ema(close, 26)

    macd_line = ema12 - ema26
    signal = tv_ema(macd_line, 9)
    hist = macd_line - signal

    return macd_line, signal, hist


def rsi(close, period=14):
    """Relative Strength Index"""
    delta = close.diff()
    gain = tv_rma(delta.where(delta > 0, 0), period)
    loss = tv_rma(-delta.where(delta < 0, 0), period)
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def get_indicators_at_time(instrument_token, interval, target_date, target_time, kite_client, is_index=False):
    """
    Calculate technical indicators at exact candle timestamp
    """
    # Fetch sufficient historical data before target date for indicator warm-up
    to_date = dt.strptime(target_date, "%Y-%m-%d") + timedelta(days=1)
    from_date = to_date - timedelta(days=90)

    data = kite_client.historical_data(
        instrument_token,
        from_date,
        to_date,
        interval
    )

    df = pd.DataFrame(data)
    df.set_index("date", inplace=True)

    # Calculate indicators
    df['ema9'] = ema(df['close'], 9)
    df['ema21'] = ema(df['close'], 21)
    
    if not is_index:
        df['vwap'] = vwap(df)
    else:
        df['vwap'] = None
        
    df['atr'] = atr(df)
    df['adx'], df['plus_di'], df['minus_di'] = adx(df)
    df['macd'], df['signal'], df['hist'] = macd(df['close'])
    df['rsi'] = rsi(df['close'])

    # Find target row
    target_dt = pd.to_datetime(f"{target_date} {target_time}")

    # Match timezone awareness between the requested timestamp and the candle index.
    if hasattr(df.index, 'tz') and df.index.tz is not None and target_dt.tzinfo is None:
        target_dt = target_dt.tz_localize(df.index.tz)
    elif (not hasattr(df.index, 'tz') or df.index.tz is None) and target_dt.tzinfo is not None:
        target_dt = target_dt.tz_convert(None)

    if target_dt not in df.index:
        # Try to find the closest candle
        closest_idx = df.index.get_indexer([target_dt], method='nearest')[0]
        if closest_idx >= 0:
            target_dt = df.index[closest_idx]
        else:
            raise ValueError("No suitable candle found near target timestamp")

    row = df.loc[target_dt]

    def safe_float(val):
        return None if pd.isna(val) else float(val)

    return {
        "close": safe_float(row['close']),
        "ema9": safe_float(row['ema9']),
        "ema21": safe_float(row['ema21']),
        "vwap": safe_float(row['vwap']),
        "volume": int(row['volume']) if not pd.isna(row['volume']) else 0,
        "atr": safe_float(row['atr']),
        "adx": safe_float(row['adx']),
        "+DI": safe_float(row['plus_di']),
        "-DI": safe_float(row['minus_di']),
        "macd": safe_float(row['macd']),
        "signal": safe_float(row['signal']),
        "histogram": safe_float(row['hist']),
        "rsi": safe_float(row['rsi']),
        "timestamp": target_dt.isoformat()
    }


# ══════════════════════════════════════════════════════════════
# ── Routes ──
# ══════════════════════════════════════════════════════════════

# ── Static Files ──
@app.route('/')
def index():
    resp = send_from_directory('..', 'index.html')
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return resp


@app.route('/<path:path>')
def static_files(path):
    # Don't serve api paths as static
    if path.startswith('api/'):
        return jsonify({'error': 'Not found'}), 404
    resp = send_from_directory('..', path)
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

        token = None
        global _instruments_cache
        if _instruments_cache:
            for inst in _instruments_cache:
                if inst.get('tradingsymbol') == symbol and inst.get('exchange') == 'NSE':
                    token = inst.get('instrument_token')
                    break

        if not token:
            # Fallback: try SQLite instruments cache
            row = db.execute(
                'SELECT instrument_token FROM instruments '
                'WHERE tradingsymbol = ? AND exchange = ? LIMIT 1',
                (symbol, 'NSE')
            ).fetchone()
            if row:
                token = row['instrument_token']

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
                        nfo_nifty = [i for i in _instruments_cache if i.get('name') == 'NIFTY' and i.get('segment') == 'NFO-FUT']
                        if nfo_nifty:
                            nfo_nifty.sort(key=lambda x: x.get('expiry', ''))
                            fut_symbol = f"NFO:{nfo_nifty[0]['tradingsymbol']}"
                            nifty_fut_q = kite.quote([fut_symbol])
                            nifty_fut_ltp = nifty_fut_q.get(fut_symbol, {}).get('last_price', 0)
                            if nifty_fut_ltp:
                                premium_pct = ((nifty_fut_ltp - nifty_spot) / nifty_spot) * 100
                                snapshot['gift_nifty_premium'] = premium_pct
                except Exception:
                    snapshot['gift_nifty_premium'] = 0

                # Futures (nearest month)
                fut_sym = None
                if _instruments_cache:
                    futs = [i for i in _instruments_cache
                            if i.get('name') == symbol and i.get('instrument_type') == 'FUT'
                            and i.get('exchange') == 'NFO']
                    futs.sort(key=lambda x: x.get('expiry', ''))
                    if futs:
                        fut_sym = f"NFO:{futs[0]['tradingsymbol']}"

                if fut_sym:
                    fq = kite.quote([fut_sym])
                    fdata = fq.get(fut_sym, {})
                    spot = snapshot['ltp'] or 1
                    fut_ltp = fdata.get('last_price', 0)
                    snapshot['futures'] = {
                        'ltp': fut_ltp,
                        'oi': fdata.get('oi', 0),
                        'oi_change': fdata.get('oi_day_change', 0),
                        'premium': fut_ltp - spot,
                        'premium_pct': round((fut_ltp - spot) / spot * 100, 3) if spot else 0
                    }
            except Exception as e:
                result['snapshot_error'] = str(e)

        result['snapshot'] = snapshot

        # ── 3. Analyst Rating (best-effort from NSE) ──
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
        with urllib.request.urlopen(req, timeout=4) as resp:
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
        exchange = 'NSE' if 'BSE' not in symbol.upper() else 'BSE'
        token = None

        # 1) Prefer fresh instruments directly from Kite for exact symbol match.
        # This avoids stale cached instrument metadata or misleading LTP token mappings.
        try:
            fresh_instruments = kite.instruments(exchange)
            exact_matches = [
                inst for inst in fresh_instruments
                if inst.get('tradingsymbol') == symbol and inst.get('exchange') == exchange
            ]
            exact_matches.sort(key=lambda inst: (
                0 if inst.get('segment') == exchange else 1,
                0 if inst.get('instrument_type') in (None, '', 'EQ') else 1,
            ))
            if exact_matches:
                token = exact_matches[0].get('instrument_token')
                logging.warning(f'[validate-entry] Fresh instruments token resolved: {token}')
        except Exception as e:
            logging.warning(f'[validate-entry] Fresh instruments lookup failed for {symbol}: {e}')

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

        # Get live snapshot (best-effort)
        snapshot = {}
        try:
            q = kite.quote(f'{exchange}:{symbol}')
            key = f'{exchange}:{symbol}'
            if key in q:
                qd = q[key]
                snapshot = {
                    'ltp': float(qd.get('last_price', 0)),
                    'vwap': float(qd.get('average_price', 0)),
                    'volume': int(qd.get('volume', 0)),
                    'symbol': symbol,
                }
        except Exception:
            # Snapshot is optional; ignore failures
            pass

        logging.warning(
            f'[validate-entry] ✓ Response: {len(candles)} candles | '
            f'First={candles[0]["close"]} | Last={candles[-1]["close"]}'
        )

        return jsonify({
            'success': True,
            'candles': candles,
            'snapshot': snapshot,
            'symbol': symbol,
            'price': price,
            'direction': direction,
            'interval': interval,
            'count': len(candles),
            'data_source': data_source,
        })

    except Exception as e:
        logging.error(f'[validate-entry] Exception: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


# ── Get Indicators at Time ──
@app.route('/api/indicators', methods=['POST'])
def get_indicators():
    """Calculate technical indicators at exact candle timestamp."""
    body = request.get_json(silent=True) or {}
    symbol = body.get('symbol', '').strip()
    date = body.get('date', '').strip()
    time = body.get('time', '').strip()

    if not symbol or not date or not time:
        return jsonify({'error': 'symbol, date, and time are required'}), 400

    nse_symbol_map = {'NIFTY': 'NIFTY 50', 'BANKNIFTY': 'NIFTY BANK', 'FINNIFTY': 'NIFTY FIN SERVICE'}
    symbol = nse_symbol_map.get(symbol.upper(), symbol)

    logging.warning(f'\n=== GET INDICATORS: {symbol} {date} {time} ===\n')

    try:
        # Get Kite connection
        kite = get_kite()
        if not kite:
            return jsonify({'error': 'Kite not initialized'}), 500

        # Resolve instrument token
        exchange = 'NSE' if 'BSE' not in symbol.upper() else 'BSE'
        token = None

        # 1) Prefer fresh instruments directly from Kite for exact symbol match.
        try:
            fresh_instruments = kite.instruments(exchange)
            exact_matches = [
                inst for inst in fresh_instruments
                if inst.get('tradingsymbol') == symbol and inst.get('exchange') == exchange
            ]
            exact_matches.sort(key=lambda inst: (
                0 if inst.get('segment') == exchange else 1,
                0 if inst.get('instrument_type') in (None, '', 'EQ') else 1,
            ))
            if exact_matches:
                token = exact_matches[0].get('instrument_token')
                logging.warning(f'[indicators] Fresh instruments token resolved: {token}')
        except Exception as e:
            logging.warning(f'[indicators] Fresh instruments lookup failed for {symbol}: {e}')

        # 2) Fallback to LTP token
        if not token:
            try:
                ltp_data = kite.ltp([f'{exchange}:{symbol}'])
                key = f'{exchange}:{symbol}'
                if key in ltp_data:
                    token = ltp_data[key].get('instrument_token') or token
                    logging.warning(f'[indicators] LTP token resolved: {token}')
            except Exception as e:
                logging.warning(f'[indicators] LTP token lookup failed for {symbol}: {e}')

        # 3) Final fallback: cached instruments
        if not token:
            instruments = cache_get_instruments(get_db())
            for inst in instruments:
                if inst.get('tradingsymbol') == symbol and inst.get('exchange') == exchange:
                    token = inst.get('instrument_token')
                    logging.warning(f'[indicators] Cached instruments token resolved: {token}')
                    break

        if not token:
            return jsonify({'error': f'Token not found: {symbol}'}), 404

        logging.warning(f'[indicators] Token resolved: {token}')

        # Calculate indicators
        is_index = any(opt in symbol.upper() for opt in ['NIFTY', 'SENSEX', 'BANKEX', 'VIX'])
        result = get_indicators_at_time(token, '5minute', date, time, kite, is_index=is_index)

        logging.warning(f'[indicators] ✓ Calculated indicators for {symbol} at {date} {time}')

        return jsonify({
            'success': True,
            'symbol': symbol,
            'date': date,
            'time': time,
            'indicators': result,
        })

    except Exception as e:
        logging.error(f'[indicators] Exception: {e}', exc_info=True)
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
        api_key = re.sub(r'\s+', '', str(data.get('api_key') or env_key or ''))
        request_token = re.sub(r'\s+', '', str(data.get('request_token') or ''))
        api_secret = re.sub(r'\s+', '', str(data.get('api_secret') or env_secret or ''))
        
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
@app.route('/api/instruments')
def instruments():
    try:
        db = get_db()

        # Check if cached instruments are fresh
        if cache_instruments_fresh(db):
            cached = cache_get_instruments(db)
            if cached:
                global _instruments_cache
                _instruments_cache = cached
                return jsonify({'instruments': cached, 'source': 'cache'})

        # Fetch from Kite API
        kite = get_kite()
        if not kite:
            # Fall back to stale cache if available
            cached = cache_get_instruments(db)
            if cached:
                _instruments_cache = cached
                return jsonify({'instruments': cached, 'source': 'stale_cache'})
            return jsonify({'error': 'Not connected'}), 401

        nse = kite.instruments('NSE')
        nfo = kite.instruments('NFO')
        try:
            bse = kite.instruments('BSE')
        except Exception:
            bse = []
        all_instruments = nse + nfo + bse
        _instruments_cache = all_instruments

        # Store in cache
        cache_store_instruments(db, all_instruments)

        return jsonify({'instruments': all_instruments, 'source': 'api'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Equity List (F&O stocks only) ──
@app.route('/api/equity-list')
def equity_list():
    try:
        kite = get_kite()
        if not kite:
            return jsonify({'error': 'Not connected'}), 401

        global _instruments_cache
        if not _instruments_cache:
            _instruments_cache = kite.instruments('NSE') + kite.instruments('NFO')

        fno_names = set()
        for i in _instruments_cache:
            if i.get('segment') in ('NFO-FUT', 'NFO-OPT'):
                fno_names.add(i.get('name', ''))

        equity = [i for i in _instruments_cache
                  if i.get('exchange') == 'NSE' and i.get('name') in fno_names and i.get('segment') == 'NSE']

        return jsonify({'stocks': equity})
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

        # ── CACHE STRATEGY ──
        # For intraday intervals: always fetch fresh from API (data changes all day)
        # For daily/weekly: use cache with incremental gap-fill

        cached_candles = []
        api_candles = []
        source = 'cache'

        if is_intraday:
            # Intraday: always fresh from API to get latest minutes, but use cache if offline
            kite = get_kite()
            if not kite:
                stale = cache_get_ohlcv(db, token, from_date_str, to_date_str, interval)
                if stale:
                    return jsonify({'candles': stale, 'source': 'stale_cache'})
                return jsonify({'error': 'Not connected — intraday data requires live API'}), 401
            # Extend to_date to end-of-day so today's intraday bars (9:15–15:30) are included.
            # Without this, to_date = midnight → Kite excludes all of today's bars.
            to_date_intraday = to_date.replace(hour=23, minute=59, second=59)
            data = kite.historical_data(token, from_date, to_date_intraday, interval)
            cache_store_ohlcv(db, token, data, interval)
            for candle in data:
                c = dict(candle)
                if 'date' in c and hasattr(c['date'], 'isoformat'):
                    c['date'] = c['date'].isoformat()
                api_candles.append(c)
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
                    return jsonify({'error': 'Not connected and no cached data'}), 401

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
                return jsonify({'error': 'Not connected and no cached data'}), 401

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


# ── Option Chain ──
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
            _instruments_cache = kite.instruments('NSE') + kite.instruments('NFO')

        options = [i for i in _instruments_cache
                   if i.get('name') == symbol and i.get('segment') == 'NFO-OPT']

        if expiry:
            # Normalize expiry to string for comparison (handles both
            # datetime.date objects from live API and strings from cache)
            exp_str = expiry.strip()
            def expiry_matches(inst_expiry):
                if inst_expiry is None:
                    return False
                if hasattr(inst_expiry, 'isoformat'):
                    return inst_expiry.isoformat() == exp_str
                return str(inst_expiry).split('T')[0] == exp_str
            options = [i for i in options if expiry_matches(i.get('expiry'))]

        if not options:
            return jsonify({'chain': [], 'spot_price': 0})

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
            strikes[strike][side] = {
                'oi': q.get('oi', 0),
                'oiChange': q.get('oi_day_change', 0),
                'volume': q.get('volume', 0),
                'iv': round(q.get('implied_volatility', 0) or 0, 1),
                'ltp': q.get('last_price', 0),
                'delta': 0
            }

        chain = sorted(strikes.values(), key=lambda x: x['strike'])
        return jsonify({'chain': chain, 'spot_price': spot})

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
            _instruments_cache = kite.instruments('NSE') + kite.instruments('NFO')

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
            _instruments_cache = kite.instruments('NSE') + kite.instruments('NFO')

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
            _instruments_cache = kite.instruments('NSE') + kite.instruments('NFO')

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
        return jsonify({'error': 'Not connected'}), 401

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

@app.route('/api/news')
def news_feed():
    import time
    symbol = request.args.get('symbol', '').upper()
    cache_key = symbol or '__all__'

    # Serve cached if < 5 min old
    cached = _news_cache.get(cache_key)
    if cached and time.time() - cached['ts'] < 300:
        return jsonify({'headlines': cached['data']})

    try:
        import feedparser
    except ImportError:
        return jsonify({'error': 'feedparser not installed. Run: pip install feedparser'}), 500

    feeds = [
        ('Livemint', 'https://www.livemint.com/rss/markets'),
        ('ET Markets', 'https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms'),
        ('MoneyControl', 'https://www.moneycontrol.com/rss/marketreports.xml'),
    ]

    headlines = []
    for source, url in feeds:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:15]:
                title = entry.get('title', '')
                link = entry.get('link', '')
                published = entry.get('published', entry.get('updated', ''))

                # Simple rule-based sentiment
                title_lower = title.lower()
                bullish_words = ['surge', 'rally', 'gain', 'jump', 'soar', 'rise', 'bull',
                                 'high', 'up ', 'profit', 'record', 'boost', 'strong',
                                 'positive', 'buy', 'outperform', 'upgrade']
                bearish_words = ['fall', 'crash', 'drop', 'slip', 'sink', 'plunge', 'bear',
                                 'low', 'down ', 'loss', 'weak', 'sell', 'cut', 'decline',
                                 'negative', 'underperform', 'downgrade', 'warning']

                bull_count = sum(1 for w in bullish_words if w in title_lower)
                bear_count = sum(1 for w in bearish_words if w in title_lower)

                if bull_count > bear_count:
                    sentiment = 'bullish'
                    score = min(bull_count / 3, 1.0)
                elif bear_count > bull_count:
                    sentiment = 'bearish'
                    score = -min(bear_count / 3, 1.0)
                else:
                    sentiment = 'neutral'
                    score = 0

                # If symbol filter, only include if symbol in title
                if symbol and symbol not in title.upper():
                    continue

                headlines.append({
                    'title': title,
                    'source': source,
                    'url': link,
                    'sentiment': sentiment,
                    'score': round(score, 2),
                    'time': published,
                })
        except Exception:
            continue  # skip broken feeds

    # Sort by time (newest first), deduplicate by title
    seen = set()
    unique = []
    for h in headlines:
        if h['title'] not in seen:
            seen.add(h['title'])
            unique.append(h)

    _news_cache[cache_key] = {'data': unique[:50], 'ts': time.time()}
    return jsonify({'headlines': unique[:50]})


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
            # Fallback: use equity list endpoint logic
            instruments = kite.instruments('NSE')
            nfo_instruments = kite.instruments('NFO')
            nfo_syms = set(i['name'] for i in nfo_instruments if i.get('instrument_type') == 'FUT')
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
            _instruments_cache = kite.instruments('NSE') + kite.instruments('NFO')

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
        row = db.execute(
            "SELECT instrument_token FROM instruments WHERE tradingsymbol=? AND exchange='NSE' LIMIT 1",
            (symbol,)
        ).fetchone()
        if not row:
            return jsonify({'error': f'Instrument token not found for {symbol}'}), 404
        token = row[0]

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

        # Compute indicators
        def ema(data_arr, period):
            k = 2 / (period + 1)
            result = [None] * (period - 1)
            e = sum(data_arr[:period]) / period
            result.append(e)
            for i in range(period, len(data_arr)):
                e = data_arr[i] * k + e * (1 - k)
                result.append(e)
            return result

        def rsi(data_arr, period=14):
            result = [None] * period
            gains, losses = 0, 0
            for i in range(1, period + 1):
                d = data_arr[i] - data_arr[i - 1]
                if d > 0: gains += d
                else: losses -= d
            ag, al = gains / period, losses / period
            result.append(100 if al == 0 else 100 - 100 / (1 + ag / al))
            for i in range(period + 1, len(data_arr)):
                d = data_arr[i] - data_arr[i - 1]
                ag = (ag * (period - 1) + max(d, 0)) / period
                al = (al * (period - 1) + max(-d, 0)) / period
                result.append(100 if al == 0 else 100 - 100 / (1 + ag / al))
            return result

        def atr_calc(h, l, c, period=14):
            result = [h[0] - l[0]]
            for i in range(1, len(c)):
                tr = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
                result.append(result[-1] * (period - 1) / period + tr / period)
            return result

        ema9 = ema(closes, 9)
        ema21 = ema(closes, 21)
        rsi_vals = rsi(closes, 14)
        atr_vals = atr_calc(highs, lows, closes, 14)

        # MACD
        emaF = ema(closes, 12)
        emaS = ema(closes, 26)
        macd_line = [emaF[i] - emaS[i] if emaF[i] is not None and emaS[i] is not None else None for i in range(len(closes))]
        valid_macd = [v for v in macd_line if v is not None]
        sig_line = ema(valid_macd, 9) if valid_macd else []
        macd_result = [None] * len(macd_line)
        si = 0
        for i in range(len(macd_line)):
            if macd_line[i] is not None and si < len(sig_line) and sig_line[si] is not None:
                macd_result[i] = {'macd': macd_line[i], 'signal': sig_line[si], 'hist': macd_line[i] - sig_line[si]}
                si += 1

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
        token = None
        global _instruments_cache
        if _instruments_cache:
            for inst in _instruments_cache:
                if inst.get('tradingsymbol') == symbol and inst.get('exchange') == 'NSE':
                    token = inst.get('instrument_token')
                    break
        if not token:
            row = db.execute(
                'SELECT instrument_token FROM instruments '
                'WHERE tradingsymbol = ? AND exchange = ? LIMIT 1',
                (symbol, 'NSE')
            ).fetchone()
            if row:
                token = row['instrument_token']

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
                if cached and len(cached) >= 10:
                    result[f'ohlcv_{interval}'] = cached
                elif kite:
                    missing_intervals.append((interval, from_date, to_date))
                else:
                    result[f'ohlcv_{interval}'] = []
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
        if _instruments_cache:
            for i in _instruments_cache:
                if i.get('segment') in ('NFO-FUT', 'NFO-OPT'):
                    fno_names.add(i.get('name', ''))
        else:
            # Fallback: try SQLite cache
            rows = db.execute(
                "SELECT DISTINCT name FROM instruments WHERE segment IN ('NFO-FUT', 'NFO-OPT')"
            ).fetchall()
            fno_names = set(r['name'] for r in rows)

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
    """Fetch upcoming board meetings/results from NSE corporate actions."""
    try:
        import urllib.request
        today = dt.now().strftime('%d-%m-%Y')
        future = (dt.now() + timedelta(days=30)).strftime('%d-%m-%Y')
        url = f'https://www.nseindia.com/api/corporate-announcements?index=equities&from_date={today}&to_date={future}'
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
            'Accept': 'application/json',
            'Referer': 'https://www.nseindia.com',
        })
        with urllib.request.urlopen(req, timeout=8) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode())
                results = []
                for item in data:
                    subject = (item.get('subject', '') or '').lower()
                    if any(kw in subject for kw in ['board meeting', 'financial result', 'quarterly result', 'annual result']):
                        symbol = item.get('symbol', '')
                        date_str = item.get('an_dt', '') or item.get('date', '')
                        purpose = item.get('subject', '')
                        if symbol and date_str:
                            results.append({
                                'symbol': symbol,
                                'date': date_str,
                                'purpose': purpose
                            })
                return results
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
                    try:
                        # Try dd-Mon-yyyy format (NSE format)
                        parsed = dt.strptime(date_val, '%d-%b-%Y')
                        date_val = parsed.strftime('%Y-%m-%d')
                    except ValueError:
                        try:
                            parsed = dt.strptime(date_val, '%d-%m-%Y')
                            date_val = parsed.strftime('%Y-%m-%d')
                        except ValueError:
                            pass  # Keep as-is

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
            token = None
            global _instruments_cache
            if _instruments_cache:
                for inst in _instruments_cache:
                    if inst.get('tradingsymbol') == symbol and inst.get('exchange') == 'NSE':
                        token = inst.get('instrument_token')
                        break
            if not token:
                row = db.execute(
                    'SELECT instrument_token FROM instruments WHERE tradingsymbol = ? AND exchange = ? LIMIT 1',
                    (symbol, 'NSE')
                ).fetchone()
                if row:
                    token = row['instrument_token']

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

        now = dt.now()
        ist_now = now  # Server should be in IST or we use naive UTC
        reco_date = ist_now.strftime('%Y-%m-%d')
        reco_time = ist_now.strftime('%H:%M:%S')
        created_at = now.isoformat()

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

        # Create Kite WebSocket connection
        from kiteconnect import KiteTicker
        kws = KiteTicker(api_key, access_token)

        def on_ticks(ws, ticks):
            """Forward ticks to the client."""
            try:
                # Convert Kite tick format to our format
                formatted_ticks = []
                for tick in ticks:
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

                # Send to client via SocketIO
                socketio.emit('ticks', formatted_ticks, to=request.sid)
            except Exception as e:
                print(f"Error forwarding ticks: {e}")

        def on_connect(ws, response):
            """Subscribe to tokens once connected."""
            try:
                ws.subscribe(tokens)
                ws.set_mode(ws.MODE_LTP, tokens)  # LTP mode for basic price updates
                print(f"Kite WS connected and subscribed to {len(tokens)} tokens for client {request.sid}")
                socketio.emit('subscribed', {'message': f'Subscribed to {len(tokens)} instruments'}, to=request.sid)
            except Exception as e:
                print(f"Error subscribing to tokens: {e}")
                socketio.emit('error', {'message': f'Subscription failed: {str(e)}'}, to=request.sid)

        def on_error(ws, code, reason):
            """Handle Kite WS errors."""
            print(f"Kite WS error for client {request.sid}: {code} - {reason}")
            socketio.emit('error', {'message': f'Kite WS error: {reason}'}, to=request.sid)

        def on_close(ws, code, reason):
            """Handle Kite WS close."""
            print(f"Kite WS closed for client {request.sid}: {code} - {reason}")
            socketio.emit('disconnected', {'reason': reason}, to=request.sid)

        # Set up callbacks
        kws.on_ticks = on_ticks
        kws.on_connect = on_connect
        kws.on_error = on_error
        kws.on_close = on_close

        # Connect to Kite WebSocket (non-blocking)
        try:
            kws.connect(threaded=True)
            # Store the connection
            _kite_ws_connections[request.sid] = kws
            print(f"WebSocket subscription initiated for client {request.sid}")
        except Exception as e:
            print(f"Failed to connect to Kite WS: {e}")
            socketio.emit('error', {'message': f'Connection failed: {str(e)}'}, to=request.sid)

    except Exception as e:
        print(f"WebSocket subscription error for client {request.sid}: {e}")
        socketio.emit('error', {'message': str(e)}, to=request.sid)


# ── Run ──
if __name__ == '__main__':
    import logging
    log = logging.getLogger('werkzeug')
    # log.setLevel(logging.ERROR)  # Suppress default request logs
    port = int(os.environ.get('PORT', 5000))
    print(f"\n{'='*50}")
    print(f"  TradeSignal Backend Server")
    print(f"  Running on http://localhost:{port}")
    print(f"  App:       http://localhost:{port}/")
    print(f"  WebSocket: ws://localhost:{port}/socket.io/")
    print(f"{'='*50}\n")

    # Use SocketIO for development (supports WebSocket)
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
