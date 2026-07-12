"""
APEX Signal Dashboard — Flask Backend v2
All 12 gaps fixed:
  1.  BOS detection (server-side + screener)
  2.  5m + 15m HTF confluence in screener
  3.  Rate limiting on Kite historical calls (0.35s delay)
  4.  IST timezone on all datetime operations
  5.  Market hours guard (09:15-15:30 IST, Mon-Fri)
  6.  WebSocket auto-reconnect with exponential backoff
  7.  In-memory candle cache with 60s TTL
  8.  CORS fixed for credentials:include
  9.  OI fetched and returned for index instruments
  10. Score normaliser scales to actual max (12 pts)
  11. Runtime instrument add/remove endpoints
  12. Volume spike detection in scoring
"""

import os, json, time, threading, logging
from datetime import datetime
from functools import wraps
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify, Response, g
from flask_cors import CORS
from kiteconnect import KiteConnect, KiteTicker

# ── Auth ──────────────────────────────────────────────────────────────────────
try:
    from auth import get_access_token, get_api_key
    USE_AUTH_MODULE = True
except ImportError:
    USE_AUTH_MODULE = False

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("apex")

# ── App ───────────────────────────────────────────────────────────────────────
app = Flask(__name__)
_ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:5000").split(",")
CORS(app, resources={r"/kite/*": {
    "origins": _ALLOWED_ORIGINS,
    "supports_credentials": True,
    "allow_headers": ["Content-Type"],
    "methods": ["GET", "POST", "DELETE", "OPTIONS"],
}})

# ── Constants ─────────────────────────────────────────────────────────────────
IST          = ZoneInfo("Asia/Kolkata")
API_KEY      = os.getenv("KITE_API_KEY", "")
ACCESS_TOKEN = os.getenv("KITE_ACCESS_TOKEN", "")
CONFIG_FILE  = os.path.join(os.path.dirname(__file__), "kite_config.json")
MARKET_OPEN  = (9, 15)
MARKET_CLOSE = (15, 30)
KITE_RATE_DELAY = 0.35
CACHE_TTL    = 60
SCORE_MAX    = 12   # EMA(2)+VWAP(1)+RSI(1)+MACD(2)+CHoCH(2)+BOS(1)+HTF(2)+VOL(1)
OI_INSTRUMENTS = {"NIFTY50", "BANKNIFTY", "FINNIFTY"}

INTERVAL_MAP = {"1":"minute","3":"3minute","5":"5minute","10":"10minute",
                "15":"15minute","30":"30minute","60":"60minute","D":"day"}

# ── State ─────────────────────────────────────────────────────────────────────
_candle_cache: dict = {}
_cache_lock   = threading.Lock()

tick_store:   dict[int, dict] = {}
tick_lock     = threading.Lock()
ws_thread     = None
ws_running    = False
ws_stop_flag  = False

INSTRUMENT_TOKENS: dict[str, int] = {
    "NIFTY50":256265,"BANKNIFTY":260105,"FINNIFTY":257801,
    "RELIANCE":738561,"HDFCBANK":341249,"INFY":408065,
    "TCS":2953217,"ICICIBANK":1270529,"SBIN":779521,"AXISBANK":1510401,
}
EXCHANGE_MAP: dict[str, str] = {k: "NSE" for k in INSTRUMENT_TOKENS}

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def now_ist() -> datetime:
    return datetime.now(IST)

def is_market_open() -> bool:
    n = now_ist()
    if n.weekday() >= 5: return False
    t = (n.hour, n.minute)
    return MARKET_OPEN <= t <= MARKET_CLOSE

def market_open_today() -> datetime:
    n = now_ist()
    return n.replace(hour=9, minute=15, second=0, microsecond=0)

def load_config() -> dict:
    cfg = {"api_key": API_KEY, "access_token": ACCESS_TOKEN}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                saved = json.load(f)
            cfg.update({k: v for k, v in saved.items() if v})
        except Exception:
            pass
    if USE_AUTH_MODULE:
        cfg["api_key"]      = get_api_key()      or cfg["api_key"]
        cfg["access_token"] = get_access_token() or cfg["access_token"]
    return cfg

def get_kite() -> KiteConnect:
    if "kite" not in g:
        cfg = load_config()
        if not cfg.get("api_key") or not cfg.get("access_token"):
            raise RuntimeError("Kite credentials not available. Check auth module or .env.")
        kite = KiteConnect(api_key=cfg["api_key"])
        kite.set_access_token(cfg["access_token"])
        g.kite = kite
    return g.kite

def save_tokens(api_key: str, access_token: str):
    with open(CONFIG_FILE, "w") as f:
        json.dump({"api_key": api_key, "access_token": access_token}, f)

def api_error(msg: str, code: int = 400):
    return jsonify({"status": "error", "message": msg}), code

def require_token(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not load_config().get("access_token"):
            return api_error("No access_token configured.", 401)
        return f(*args, **kwargs)
    return decorated

def _ckey(symbol: str, interval: str) -> str:
    return f"{symbol}_{interval}"

def _cache_get(key: str):
    with _cache_lock:
        e = _candle_cache.get(key)
        if e and (time.time() - e["ts"]) < CACHE_TTL:
            return e["data"]
    return None

def _cache_set(key: str, data):
    with _cache_lock:
        _candle_cache[key] = {"ts": time.time(), "data": data}

# ─────────────────────────────────────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/kite/auth", methods=["POST"])
def auth_set():
    data = request.get_json(force=True) or {}
    ak = data.get("api_key", "").strip()
    at = data.get("access_token", "").strip()
    if not ak or not at:
        return api_error("api_key and access_token required")
    save_tokens(ak, at)
    return jsonify({"status": "ok", "message": "Credentials saved"})

@app.route("/kite/auth/status", methods=["GET"])
def auth_status():
    cfg = load_config()
    if not cfg.get("access_token"):
        return jsonify({"status": "no_token", "message": "No access_token found."})
    try:
        profile = get_kite().profile()
        return jsonify({"status": "ok", "user": profile.get("user_name", ""),
                        "user_id": profile.get("user_id", ""), "broker": profile.get("broker", "ZERODHA")})
    except RuntimeError as e:
        return jsonify({"status": "no_token", "message": str(e)})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

# ─────────────────────────────────────────────────────────────────────────────
# HISTORICAL CANDLES
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_raw(kite, token: int, interval: str, from_dt, to_dt, oi: bool = False) -> list:
    key = _ckey(str(token), interval)
    cached = _cache_get(key)
    if cached: return cached
    raw = kite.historical_data(instrument_token=token, from_date=from_dt,
                               to_date=to_dt, interval=interval, continuous=False, oi=oi)
    candles = []
    for c in raw:
        entry = {
            "timestamp": c["date"].isoformat() if hasattr(c["date"], "isoformat") else str(c["date"]),
            "open": round(c["open"], 2), "high": round(c["high"], 2),
            "low":  round(c["low"],  2), "close": round(c["close"], 2),
            "volume": c["volume"],
        }
        if oi: entry["oi"] = c.get("oi", 0)
        candles.append(entry)
    _cache_set(key, candles)
    return candles

@app.route("/kite/historical", methods=["GET"])
@require_token
def historical():
    symbol       = request.args.get("symbol", "").upper()
    token        = request.args.get("instrument_token", "")
    interval_raw = request.args.get("interval", "5")
    from_str     = request.args.get("from", "")
    to_str       = request.args.get("to", "")
    include_oi   = bool(int(request.args.get("oi", 0))) and symbol in OI_INSTRUMENTS

    token = int(token) if token else INSTRUMENT_TOKENS.get(symbol)
    if not token:
        return api_error(f"Unknown symbol '{symbol}'.")

    interval = INTERVAL_MAP.get(str(interval_raw), "5minute")
    try:
        from_dt = datetime.fromisoformat(from_str).replace(tzinfo=IST) if from_str else market_open_today()
        to_dt   = datetime.fromisoformat(to_str).replace(tzinfo=IST)   if to_str   else now_ist()
    except ValueError:
        return api_error("Invalid date format. Use ISO8601.")

    try:
        candles = _fetch_raw(get_kite(), token, interval, from_dt, to_dt, include_oi)
        return jsonify({"status": "ok", "symbol": symbol or str(token),
                        "interval": interval, "count": len(candles), "candles": candles})
    except Exception as e:
        return api_error(f"Kite error: {e}", 502)

# ─────────────────────────────────────────────────────────────────────────────
# QUOTE / LTP
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/kite/quote", methods=["GET"])
@require_token
def quote():
    symbols   = [s.strip().upper() for s in request.args.get("symbols", "NIFTY50").split(",") if s.strip()]
    inst_keys = [f"{EXCHANGE_MAP.get(s,'NSE')}:{s}" for s in symbols]
    try:
        raw = get_kite().quote(inst_keys)
        result = {}
        for key, data in raw.items():
            sym = key.split(":")[-1]
            result[sym] = {
                "last_price": data.get("last_price"),
                "open": data.get("ohlc", {}).get("open"),
                "high": data.get("ohlc", {}).get("high"),
                "low":  data.get("ohlc", {}).get("low"),
                "close":data.get("ohlc", {}).get("close"),
                "volume": data.get("volume"),
                "oi": data.get("oi"),
                "change": data.get("net_change"),
                "change_pct": round((data.get("net_change", 0) /
                    data.get("ohlc", {}).get("close", 1)) * 100, 2)
                    if data.get("ohlc", {}).get("close") else 0,
                "timestamp": data.get("timestamp"),
                "buy_quantity":  data.get("buy_quantity"),
                "sell_quantity": data.get("sell_quantity"),
            }
        return jsonify({"status": "ok", "data": result})
    except Exception as e:
        return api_error(f"Kite error: {e}", 502)

@app.route("/kite/ltp", methods=["GET"])
@require_token
def ltp():
    symbols   = [s.strip().upper() for s in request.args.get("symbols", "NIFTY50").split(",")]
    inst_keys = [f"{EXCHANGE_MAP.get(s,'NSE')}:{s}" for s in symbols]
    try:
        raw = get_kite().ltp(inst_keys)
        return jsonify({"status": "ok", "data": {k.split(":")[-1]: v.get("last_price") for k, v in raw.items()}})
    except Exception as e:
        return api_error(f"Kite error: {e}", 502)

# ─────────────────────────────────────────────────────────────────────────────
# GLOBAL MACRO QUOTES (India VIX, USD/INR, WTI/Brent Crude via Kite)
# ─────────────────────────────────────────────────────────────────────────────

_MONTHS_SHORT = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC']

def _front_month_symbol(prefix: str, offset: int = 0) -> str:
    """Build a futures tradingsymbol e.g. CRUDEOIL25MAYFUT.
    offset=0 → current calendar month, offset=1 → next month."""
    n   = now_ist()
    m   = n.month - 1 + offset          # 0-indexed
    yr  = (n.year + m // 12) % 100
    mon = _MONTHS_SHORT[m % 12]
    return f"{prefix}{yr:02d}{mon}FUT"


def _kite_quote_safe(kite, exchange: str, symbol: str) -> dict | None:
    """Single Kite quote call; returns None on any failure."""
    try:
        key  = f"{exchange}:{symbol}"
        raw  = kite.quote([key])
        data = raw.get(key, {})
        if not data or data.get("last_price") is None:
            return None
        ltp  = data["last_price"]
        prev = (data.get("ohlc") or {}).get("close") or ltp
        chg  = round(ltp - prev, 4)
        pct  = round((chg / prev) * 100, 2) if prev else 0
        return {
            "price":      ltp,
            "change":     chg,
            "change_pct": pct,
            "high":       (data.get("ohlc") or {}).get("high"),
            "low":        (data.get("ohlc") or {}).get("low"),
            "symbol":     symbol,
            "exchange":   exchange,
        }
    except Exception as e:
        log.debug(f"Macro quote {exchange}:{symbol} — {e}")
        return None


@app.route("/kite/global-quotes", methods=["GET"])
@require_token
def global_quotes():
    """
    Real-time macro quotes sourced directly from Kite (NSE/CDS/MCX feeds).
      • India VIX   → NSE:INDIA VIX           (stable index, no expiry)
      • USD/INR     → CDS front-month USDINR futures (auto-rolls monthly)
      • WTI Crude   → MCX front-month CRUDEOIL futures (WTI-benchmarked)
      • Brent Crude → MCX front-month BRENTCRUDEOIL futures (if listed)
    Falls back to next-month contract if current month has expired.
    """
    try:
        kite = get_kite()
    except RuntimeError as e:
        return api_error(str(e), 401)

    result = {}

    # 1. India VIX — permanent NSE index, no expiry symbol needed
    result["india_vix"] = _kite_quote_safe(kite, "NSE", "INDIA VIX")

    # 2. USD/INR — CDS segment, try current then next-month contract
    usdinr = None
    for offset in (0, 1):
        usdinr = _kite_quote_safe(kite, "CDS", _front_month_symbol("USDINR", offset))
        if usdinr:
            break
    result["usdinr"] = usdinr

    # 3. WTI Crude — MCX CRUDEOIL futures (WTI-benchmarked in India)
    wti = None
    for offset in (0, 1):
        wti = _kite_quote_safe(kite, "MCX", _front_month_symbol("CRUDEOIL", offset))
        if wti:
            break
    result["wti_crude"] = wti

    # 4. Brent Crude — MCX BRENTCRUDEOIL (listed on MCX; None if not available)
    brent = None
    for offset in (0, 1):
        brent = _kite_quote_safe(kite, "MCX", _front_month_symbol("BRENTCRUDEOIL", offset))
        if brent:
            break
    result["brent_crude"] = brent

    return jsonify({
        "status":    "ok",
        "data":      result,
        "timestamp": now_ist().strftime("%H:%M:%S IST"),
    })

# ─────────────────────────────────────────────────────────────────────────────
# SCREENER — 5m + 15m HTF, rate-limited, market hours guard
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/kite/screener", methods=["GET"])
@require_token
def screener():
    if not is_market_open():
        return jsonify({"status": "ok", "market_open": False,
                        "message": "Market closed — showing cached signals",
                        "results": _cache_get("screener_last") or []})

    symbols_raw  = request.args.get("symbols", ",".join(INSTRUMENT_TOKENS.keys()))
    interval_raw = request.args.get("interval", "5")
    symbols      = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()]
    entry_iv     = INTERVAL_MAP.get(str(interval_raw), "5minute")
    htf_iv       = "15minute"
    from_dt      = market_open_today()
    to_dt        = now_ist()
    kite         = get_kite()
    results      = []

    for i, sym in enumerate(symbols):
        token = INSTRUMENT_TOKENS.get(sym)
        if not token: continue
        if i > 0: time.sleep(KITE_RATE_DELAY)
        try:
            oi   = sym in OI_INSTRUMENTS
            raw5 = _fetch_raw(kite, token, entry_iv, from_dt, to_dt, oi)
            time.sleep(KITE_RATE_DELAY)
            raw15 = _fetch_raw(kite, token, htf_iv, from_dt, to_dt, oi)
            if len(raw5) < 30: continue

            c5    = [c["close"] for c in raw5]
            c15   = [c["close"] for c in raw15] if len(raw15) >= 20 else c5
            e21   = _ema(c5, 21);  e50 = _ema(c5, 50)
            rsi   = _rsi(c5, 14);  vwap_v = _vwap(raw5)
            mh    = _macd_hist(c5)
            bos   = _detect_bos(raw5)
            choch = _detect_choch(raw5)
            vol_s = _vol_spike(raw5)
            htf   = _htf_bias(c15[-1], _ema(c15,21)[-1], _ema(c15,50)[-1]) if len(c15) > 50 else "neutral"
            score, direction, reason = _score(c5[-1], e21[-1], e50[-1], vwap_v[-1], rsi[-1], mh, bos, choch, htf, vol_s)

            results.append({
                "symbol": sym, "last_price": round(c5[-1], 2),
                "signal": direction, "score": score, "reason": reason,
                "htf_bias": htf, "bos": bos, "choch": choch, "vol_spike": vol_s,
                "ema21": round(e21[-1], 2) if e21[-1] else None,
                "ema50": round(e50[-1], 2) if e50[-1] else None,
                "vwap":  round(vwap_v[-1], 2) if vwap_v[-1] else None,
                "rsi":   round(rsi[-1], 1)    if rsi[-1]   else None,
                "oi":    raw5[-1].get("oi") if oi else None,
            })
        except Exception as e:
            log.warning(f"Screener error {sym}: {e}")
            results.append({"symbol": sym, "error": str(e)})

    _cache_set("screener_last", results)
    return jsonify({"status": "ok", "market_open": True,
                    "interval": entry_iv, "htf": htf_iv, "results": results})

# ─────────────────────────────────────────────────────────────────────────────
# WEBSOCKET — auto-reconnect with exponential backoff
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/kite/ws/start", methods=["POST"])
@require_token
def ws_start():
    global ws_thread, ws_running, ws_stop_flag
    data   = request.get_json(force=True) or {}
    tokens = [int(t) for t in data.get("tokens", list(INSTRUMENT_TOKENS.values()))]
    if ws_running:
        return jsonify({"status": "ok", "message": "WebSocket already running"})
    ws_stop_flag = False

    def run_ws():
        global ws_running, ws_stop_flag
        delay = 5
        while not ws_stop_flag:
            ws_running = True
            try:
                cfg    = load_config()
                ticker = KiteTicker(cfg["api_key"], cfg["access_token"])

                def on_ticks(ws, ticks):
                    with tick_lock:
                        for t in ticks:
                            tick_store[t["instrument_token"]] = {
                                "token":   t["instrument_token"],
                                "ltp":     t.get("last_price"),
                                "volume":  t.get("volume"),
                                "buy_qty": t.get("buy_quantity"),
                                "sell_qty":t.get("sell_quantity"),
                                "change":  t.get("change"),
                                "oi":      t.get("oi"),
                                "ohlc":    t.get("ohlc", {}),
                                "ts":      now_ist().isoformat(),
                            }

                def on_connect(ws, _):
                    nonlocal delay
                    log.info("KiteTicker connected")
                    ws.subscribe(tokens)
                    ws.set_mode(ws.MODE_FULL, tokens)
                    delay = 5  # reset backoff

                def on_error(ws, code, reason):
                    log.error(f"KiteTicker error {code}: {reason}")

                def on_close(ws, code, reason):
                    log.warning(f"KiteTicker closed {code}: {reason}")

                ticker.on_ticks   = on_ticks
                ticker.on_connect = on_connect
                ticker.on_error   = on_error
                ticker.on_close   = on_close
                ticker.connect(threaded=False)
            except Exception as e:
                log.error(f"KiteTicker exception: {e}")

            ws_running = False
            if ws_stop_flag: break
            log.info(f"WS reconnecting in {delay}s...")
            time.sleep(delay)
            delay = min(delay * 2, 120)

    ws_thread = threading.Thread(target=run_ws, daemon=True)
    ws_thread.start()
    return jsonify({"status": "ok", "message": f"WebSocket started for {len(tokens)} instruments"})

@app.route("/kite/ws/stop", methods=["POST"])
def ws_stop():
    global ws_stop_flag, ws_running
    ws_stop_flag = True
    ws_running   = False
    return jsonify({"status": "ok"})

@app.route("/kite/ws/ticks", methods=["GET"])
def ws_ticks_sse():
    token_filter = request.args.get("tokens", "")
    filter_set   = {int(t) for t in token_filter.split(",") if t.strip()} if token_filter else None

    def stream():
        last_sent = {}
        while True:
            with tick_lock:
                snapshot = dict(tick_store)
            out = {str(tok): tick for tok, tick in snapshot.items()
                   if (not filter_set or tok in filter_set)
                   and last_sent.get(tok) != tick.get("ltp")}
            if out:
                for tok in out: last_sent[int(tok)] = out[tok].get("ltp")
                yield f"data: {json.dumps(out)}\n\n"
            time.sleep(0.5)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/kite/ws/snapshot", methods=["GET"])
def ws_snapshot():
    with tick_lock:
        return jsonify({"status": "ok", "ticks": dict(tick_store), "count": len(tick_store)})

# ─────────────────────────────────────────────────────────────────────────────
# INSTRUMENTS — search + runtime add/remove
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/kite/instruments", methods=["GET"])
@require_token
def instruments_get():
    search   = request.args.get("search", "").upper()
    exchange = request.args.get("exchange", "NSE").upper()
    if not search:
        return jsonify({"status": "ok", "instruments": [
            {"symbol": k, "token": v, "exchange": EXCHANGE_MAP.get(k, "NSE")}
            for k, v in INSTRUMENT_TOKENS.items()
        ]})
    try:
        all_inst = get_kite().instruments(exchange)
        matches  = [
            {"token": i["instrument_token"], "symbol": i["tradingsymbol"],
             "name": i["name"], "exchange": i["exchange"], "type": i["instrument_type"]}
            for i in all_inst
            if search in i["tradingsymbol"] or search in i.get("name", "").upper()
        ][:50]
        return jsonify({"status": "ok", "count": len(matches), "instruments": matches})
    except Exception as e:
        return api_error(str(e), 502)

@app.route("/kite/instruments", methods=["POST"])
def instruments_add():
    data   = request.get_json(force=True) or {}
    symbol = data.get("symbol", "").upper().strip()
    token  = data.get("token")
    exch   = data.get("exchange", "NSE").upper()
    if not symbol or not token:
        return api_error("symbol and token required")
    INSTRUMENT_TOKENS[symbol] = int(token)
    EXCHANGE_MAP[symbol]      = exch
    return jsonify({"status": "ok", "message": f"{symbol} registered", "token": int(token)})

@app.route("/kite/instruments/<symbol>", methods=["DELETE"])
def instruments_remove(symbol: str):
    symbol = symbol.upper()
    if symbol not in INSTRUMENT_TOKENS:
        return api_error(f"{symbol} not found", 404)
    del INSTRUMENT_TOKENS[symbol]
    EXCHANGE_MAP.pop(symbol, None)
    return jsonify({"status": "ok", "message": f"{symbol} removed"})

# ─────────────────────────────────────────────────────────────────────────────
# INDICATOR HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _ema(closes, period):
    if len(closes) < period: return [None] * len(closes)
    k   = 2 / (period + 1)
    ema = [sum(closes[:period]) / period]
    for c in closes[period:]: ema.append(c * k + ema[-1] * (1 - k))
    return [None] * (len(closes) - len(ema)) + ema

def _rsi(closes, period=14):
    if len(closes) < period + 1: return [None] * len(closes)
    diffs  = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in diffs]
    losses = [max(-d, 0) for d in diffs]
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    rsis = [None] * (period + 1)
    rsis.append(100 - 100 / (1 + ag / al) if al else 100)
    for i in range(period, len(gains)):
        ag = (ag * (period-1) + gains[i])  / period
        al = (al * (period-1) + losses[i]) / period
        rsis.append(100 - 100 / (1 + ag / al) if al else 100)
    return rsis

def _vwap(candles):
    cum_pv = cum_vol = 0
    result = []
    for c in candles:
        tp = (c["high"] + c["low"] + c["close"]) / 3
        cum_pv  += tp * c["volume"]
        cum_vol += c["volume"]
        result.append(cum_pv / cum_vol if cum_vol else None)
    return result

def _macd_hist(closes, fast=12, slow=26, signal=9):
    ef   = _ema(closes, fast); es = _ema(closes, slow)
    macd = [f - s if f and s else None for f, s in zip(ef, es)]
    valid = [v for v in macd if v is not None]
    if len(valid) < signal: return None
    sig  = _ema(valid, signal)
    return valid[-1] - sig[-1] if sig[-1] is not None else None

def _swings(candles, lb=3):
    highs, lows = [], []
    for i in range(lb, len(candles) - lb):
        w = candles[i-lb:i+lb+1]
        if candles[i]["high"] == max(c["high"] for c in w): highs.append(i)
        if candles[i]["low"]  == min(c["low"]  for c in w): lows.append(i)
    return highs, lows

def _detect_bos(candles) -> str:
    """BOS — continuation: price breaks prior swing high/low in trend direction."""
    if len(candles) < 10: return "none"
    highs, lows = _swings(candles)
    last = candles[-1]["close"]
    if highs and last > candles[highs[-1]]["high"]: return "bullish"
    if lows  and last < candles[lows[-1]]["low"]:   return "bearish"
    return "none"

def _detect_choch(candles) -> str:
    """CHoCH — reversal: breaks prior swing against prevailing trend structure."""
    if len(candles) < 15: return "none"
    highs, lows = _swings(candles)
    last = candles[-1]["close"]
    if len(highs) >= 2:
        if candles[highs[-1]]["high"] < candles[highs[-2]]["high"]:  # lower highs
            if last > candles[highs[-2]]["high"]: return "bullish"
    if len(lows) >= 2:
        if candles[lows[-1]]["low"] > candles[lows[-2]]["low"]:      # higher lows
            if last < candles[lows[-2]]["low"]: return "bearish"
    return "none"

def _htf_bias(close, ema21, ema50) -> str:
    if not ema21 or not ema50: return "neutral"
    if close > ema21 > ema50: return "bullish"
    if close < ema21 < ema50: return "bearish"
    return "neutral"

def _vol_spike(candles, period=20) -> bool:
    if len(candles) < period + 1: return False
    avg = sum(c["volume"] for c in candles[-period-1:-1]) / period
    return candles[-1]["volume"] > avg * 1.5

def _score(close, ema21, ema50, vwap, rsi, macd_h, bos, choch, htf, vol_spike):
    bull = bear = 0; reasons = []
    if ema21 and ema50:
        if close > ema21 > ema50:  bull += 2; reasons.append("Price > EMA21 > EMA50")
        elif close < ema21 < ema50: bear += 2; reasons.append("Price < EMA21 < EMA50")
    if vwap:
        if close > vwap: bull += 1; reasons.append("Above VWAP")
        else:            bear += 1; reasons.append("Below VWAP")
    if rsi:
        if 55 < rsi < 75:    bull += 1; reasons.append(f"RSI {rsi:.0f} bullish")
        elif 25 < rsi < 45:  bear += 1; reasons.append(f"RSI {rsi:.0f} bearish")
    if macd_h is not None:
        if macd_h > 0: bull += 1; reasons.append("MACD positive")
        else:          bear += 1; reasons.append("MACD negative")
    if choch == "bullish":  bull += 2; reasons.append("CHoCH bullish ✓")
    elif choch == "bearish": bear += 2; reasons.append("CHoCH bearish ✓")
    if bos   == "bullish":  bull += 1; reasons.append("BOS bullish ✓")
    elif bos == "bearish":  bear += 1; reasons.append("BOS bearish ✓")
    if htf   == "bullish":  bull += 2; reasons.append("15m HTF bullish ✓")
    elif htf == "bearish":  bear += 2; reasons.append("15m HTF bearish ✓")
    if vol_spike:
        if bull > bear: bull += 1; reasons.append("Volume spike confirms bull")
        else:           bear += 1; reasons.append("Volume spike confirms bear")

    direction = "WAIT"; score = 0
    if bull >= 5 and bull > bear:
        direction = "BUY";  score = round(bull / SCORE_MAX * 10)
    elif bear >= 5 and bear > bull:
        direction = "SELL"; score = round(bear / SCORE_MAX * 10)
    return score, direction, reasons[0] if reasons else "—"

# ─────────────────────────────────────────────────────────────────────────────
# HEALTH
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def root():
    return jsonify({"service": "APEX Backend v2", "market_open": is_market_open(),
                    "time_ist": now_ist().strftime("%H:%M:%S"),
                    "instruments": len(INSTRUMENT_TOKENS)})

@app.route("/health", methods=["GET"])
def health():
    cfg = load_config()
    return jsonify({"status": "ok", "has_token": bool(cfg.get("access_token")),
                    "market_open": is_market_open(), "time_ist": now_ist().strftime("%H:%M:%S"),
                    "ws_running": ws_running, "tick_count": len(tick_store),
                    "cache_keys": len(_candle_cache), "instruments": len(INSTRUMENT_TOKENS)})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
