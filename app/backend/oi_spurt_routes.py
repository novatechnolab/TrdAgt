"""
oi_spurt_routes.py  –  Flask Blueprint: OI Spurt Scanner
=========================================================
Routes (mounted at /api/oi):
    GET  /api/oi/spurt?min_pct=5   →  NSE OI spurt list
    GET  /api/oi/symbol/<symbol>   →  Full detail: pivots, PCR, max pain, strikes

Kite connectivity reuses the existing get_kite() from server.py —
same session, same API key, no separate login needed.

FIXES APPLIED (2026-05-08):
  - Session-start OI baseline cache (replaces oi_day_high proxy)
  - net_change used for price direction in buildup classification
  - timedelta.total_seconds() fix (was .seconds — never refreshed NSE session)
  - Previous-day OHLC for correct pivot levels (was using today's intraday)
  - price_change populated from NSE pChange field
  - ATM-proximal CE/PE walls (highest OI at/above LTP for CE, at/below for PE)
  - NIFTYIT + BANKEX added to EXCHANGE_MAP
  - is_index flag in response for frontend PCR threshold selection
  - Trap labels: 'CE Writer Squeeze' / 'PE Writer Squeeze' (industry standard)
  - ce_wall / pe_wall returned separately from top_ce/top_pe
"""

import requests
import datetime
import threading
import time
import queue
import pytz

from collections import defaultdict
from session_utils import is_market_hours
from flask import Blueprint, jsonify, request
from oi_scanner_routes import implied_vol, trading_time_to_expiry, RISK_FREE_RATE

oi_spurt_bp = Blueprint("oi_spurt", __name__, url_prefix="/api/oi")

# Persistent tick history cache for Spurt Scanner (symbol -> list of last 4 ticks)
_spurt_history = {}
_spurt_lock = threading.Lock()

# EOD Spurt List Cache for off-market hours: {(date_str, min_pct): {"data": [...], "source": str, "fetched_at": iso_str}}
_spurt_eod_cache = {}
_spurt_eod_lock = threading.Lock()

_housekeeping_done = False
_housekeeping_lock = threading.Lock()

# ── Kite helper ────────────────────────────────────────────────────────────────
def _get_kite():
    from server import get_kite
    kite = get_kite()
    if kite is None:
        raise RuntimeError("Kite session not connected. Configure via Settings → Kite API.")
    return kite

def get_premium_threshold(prv_ltp, sym):
    if prv_ltp <= 0:
        return 0.025
    sym_upper = sym.upper() if sym else ""
    # Indices: NIFTY, BANKNIFTY, etc.
    if sym_upper in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX", "NIFTYIT", "NIFTYNXT50"):
        base = 0.0250  # 2.50%
    # Large Caps
    elif sym_upper in ("RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "BHARTIARTL", "ITC", "SBIN", "LT", "AXISBANK", "MARUTI", "HINDUNILVR", "KOTAKBANK", "TATASTEEL", "M&M", "TRENT"):
        base = 0.0200  # 2.00%
    # Mid/Small Caps
    else:
        base = 0.0300  # 3.00%
        
    # Minimum 2-tick change barrier (0.10 rupees) to filter spread noise
    tick_barrier = 0.10 / prv_ltp
    return max(base, tick_barrier)


def classify_dual_side_addition(ce_oi_chg, pe_oi_chg, ce_prem_chg, pe_prem_chg, 
                                  spot_pct_chg, spot_threshold, oi_add_threshold):


    spot_flat = abs(spot_pct_chg) <= spot_threshold
    both_oi_up = ce_oi_chg > oi_add_threshold and pe_oi_chg > oi_add_threshold
    
    if not spot_flat:
        return {
            "state": "TRENDING",
            "signal": f"Spot is trending ({spot_pct_chg:+.2f}%). Focus on standard directional buildup signals.",
            "bias": "DIRECTIONAL"
        }
        
    if not both_oi_up:
        return {
            "state": "CONSOLIDATION",
            "signal": "Spot is flat. Low range writing activity. Option premiums stable.",
            "bias": "NEUTRAL"
        }

    both_premium_down = ce_prem_chg < 0 and pe_prem_chg < 0
    both_premium_up = ce_prem_chg > 0 and pe_prem_chg > 0

    if both_premium_down:
        return {
            "state": "RANGE_PINNING",
            "signal": "Writers adding both legs, premium eroding — range expected, IV likely to decay",
            "bias": "NEUTRAL_THETA_FAVORABLE"
        }
    elif both_premium_up:
        return {
            "state": "VOL_COILING",
            "signal": "Both legs bought despite flat spot — IV expansion ahead of possible breakout",
            "bias": "NEUTRAL_BREAKOUT_WATCH"
        }
    else:
        return {
            "state": "MIXED_DUAL_ADD",
            "signal": "OI adding both sides but premium legs diverging — check individually",
            "bias": "UNCLEAR"
        }


# ── NSE scrape ─────────────────────────────────────────────────────────────────
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/market-data/oi-spurts",
}

_nse_sess      = None
_nse_sess_time = None
_nse_sess_lock = threading.Lock()


def nse_session():
    """Reuse NSE session; refresh every 25 min. FIXED: use total_seconds()."""
    global _nse_sess, _nse_sess_time
    now = datetime.datetime.now()
    # FIX: .total_seconds() not .seconds (was never refreshing — .seconds returns
    # only the seconds component of the timedelta, max 59)
    if _nse_sess is not None and (now - _nse_sess_time).total_seconds() <= 1500:
        return _nse_sess
    with _nse_sess_lock:
        if _nse_sess is None or (now - _nse_sess_time).total_seconds() > 1500:
            s = requests.Session()
            s.headers.update(NSE_HEADERS)
            try:
                s.get("https://www.nseindia.com", timeout=10)
                s.get("https://www.nseindia.com/market-data/oi-spurts", timeout=10)
            except Exception:
                pass
            _nse_sess      = s
            _nse_sess_time = now
    return _nse_sess


def fetch_oi_spurt(min_pct: float = 5.0):
    """Fetch NSE OI spurt list from NSE scraping (primary).
    If that fails or gets blocked (e.g. 403), fall back to calculation via Zerodha Kite API."""
    import logging
    log = logging.getLogger(__name__)

    # ── Step 1: Try scraping NSE website using curl_cffi ───────────────────
    url = "https://www.nseindia.com/api/live-analysis-oi-spurts-underlyings"
    scrape_error = None
    try:
        resp = nse_session().get(url, timeout=10)
        resp.raise_for_status()
        raw = resp.json().get("data", [])
        
        result = []
        for row in raw:
            try:
                oi_chg = float(row.get("avgInOI", row.get("perChange", row.get("oiChange", 0))))
            except (TypeError, ValueError):
                oi_chg = 0.0
            if oi_chg < min_pct:
                continue

            curr_oi = int(row.get("latestOI",  row.get("currentOI", row.get("openInterest", 0))))
            prev_oi = int(row.get("prevOI", 0))
            ltp     = float(row.get("underlyingValue", row.get("lastPrice", 0)))
            volume  = int(row.get("volume", row.get("tradedVolume", 0)))

            try:
                price_change = float(row.get("pChange", row.get("change", 0)))
            except (TypeError, ValueError):
                price_change = 0.0

            result.append({
                "symbol":        row.get("symbol", ""),
                "series":        row.get("series", "FUT"),
                "ltp":           ltp,
                "prev_oi":       prev_oi,
                "curr_oi":       curr_oi,
                "oi_change_pct": round(oi_chg, 2),
                "volume":        volume,
                "price_change":  round(price_change, 2),
            })

        result.sort(key=lambda x: x["oi_change_pct"], reverse=True)
        log.info(f"[OISpurt] Successfully scraped NSE website ({len(result)} items)")
        return result, "nse", None

    except Exception as e:
        scrape_error = str(e)
        log.warning(f"[OISpurt] NSE scrape failed: {scrape_error}. Falling back to Kite API...")

    # ── Step 2: Fall back to calculation via Zerodha Kite API ──────────────
    try:
        kite = _get_kite()
    except Exception as e:
        return [], "kite", f"Scrape failed ({scrape_error}) and Kite fallback not connected: {e}"

    today = datetime.date.today()
    try:
        instruments = get_instruments(kite, "NFO")
        try:
            bfo_instruments = get_instruments(kite, "BFO")
            all_instruments = instruments + bfo_instruments
        except Exception:
            all_instruments = instruments
    except Exception as e:
        return [], "kite", f"Scrape failed ({scrape_error}) and Kite instruments load failed: {e}"

    # Find the near-month futures contract for each F&O symbol
    fut_candidates = defaultdict(list)
    for inst in all_instruments:
        itype = inst.get("instrument_type")
        name = inst.get("name")
        expiry = inst.get("expiry")
        if itype == "FUT" and name and expiry and expiry >= today:
            fut_candidates[name].append(inst)

    symbol_to_fut_key = {}
    for name, insts in fut_candidates.items():
        nearest_inst = min(insts, key=lambda i: i["expiry"])
        exchange = "BFO" if name.upper() in BFO_SYMBOLS else "NFO"
        symbol_to_fut_key[name] = f"{exchange}:{nearest_inst['tradingsymbol']}"

    if not symbol_to_fut_key:
        return [], "kite", f"Scrape failed ({scrape_error}) and no F&O futures discovered for Kite fallback"

    fut_keys = list(symbol_to_fut_key.values())
    quotes = {}
    try:
        for batch in _chunks(fut_keys, 250):
            quotes.update(kite.quote(batch))
    except Exception as e:
        return [], "kite", f"Scrape failed ({scrape_error}) and Kite quote fetch failed: {e}"

    result = []
    today_str = datetime.date.today().isoformat()
    for symbol, fut_key in symbol_to_fut_key.items():
        q = quotes.get(fut_key)
        if not q:
            continue
        curr_oi = int(q.get("oi") or 0)
        # BUG 1 FIX: Use SQLite EOD baseline for prev_oi instead of oi_day_low.
        # oi_day_low is the intraday tick low (near 0 early session) → wildly inflated oi_chg%.
        # Falls back to oi_day_low only if no baseline exists yet (first run / cold start).
        tradingsymbol = fut_key.split(":", 1)[1] if ":" in fut_key else fut_key
        cached_eod = get_cached_baseline(today_str, tradingsymbol)
        if cached_eod is not None and cached_eod > 0:
            prev_oi = cached_eod
        else:
            prev_oi = int(q.get("oi_day_low") or 0)
        oi_chg = ((curr_oi - prev_oi) / prev_oi * 100) if prev_oi > 0 else 0.0

        if abs(oi_chg) < min_pct:
            continue

        ltp = float(q.get("last_price") or 0)
        ohlc = q.get("ohlc") or {}
        close = float(ohlc.get("close") or ltp or 1)
        price_change = ((ltp - close) / close * 100) if close else 0.0
        volume = int(q.get("volume") or 0)

        result.append({
            "symbol":        symbol,
            "series":        "FUT",
            "ltp":           ltp,
            "prev_oi":       prev_oi,
            "curr_oi":       curr_oi,
            "oi_change_pct": round(oi_chg, 2),
            "volume":        volume,
            "price_change":  round(price_change, 2),
        })

    result.sort(key=lambda x: x["oi_change_pct"], reverse=True)
    log.info(f"[OISpurt] Loaded via Kite fallback calculation ({len(result)} items)")
    return result, "kite", None

# ── Option chain ───────────────────────────────────────────────────────────────
def _chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i+n]


# ── Pivot cache (prev-day OHLC → pivots don't change intraday) ────────────────
_pivot_cache      = {}  # {(symbol_upper, date_str): {pivots, pivot_source, prev_close, open_gap_pct}}
_pivot_cache_lock = threading.Lock()

# ── Max Pain cache (3-min TTL — max pain changes slowly) ──────────────────────
_max_pain_cache = {}  # {(symbol, expiry_str): (value, expires_at)}

def compute_max_pain_cached(symbol, expiry, chain):
    """Cached wrapper for compute_max_pain(). Avoids O(n²) on every 15s refresh."""
    key = (symbol, str(expiry))
    cached = _max_pain_cache.get(key)
    if cached and time.time() < cached[1]:
        return cached[0]
    val = compute_max_pain(chain)
    _max_pain_cache[key] = (val, time.time() + 180)  # 3-min TTL
    return val


# ── Session-start OI baseline cache ───────────────────────────────────────────
# FIX: Replaces oi_day_high proxy. On first fetch per session, stores OI as
# baseline. OI change = current_oi - session_baseline (true intraday delta).
_session_oi      = {}   # {(symbol, strike, side): int}
_session_date    = None
_session_oi_lock = threading.Lock()


def _get_session_oi_baseline(symbol, strike, side, current_oi):
    """Return session-start OI for this strike/side. First call sets the baseline."""
    global _session_oi, _session_date
    today = datetime.date.today()
    with _session_oi_lock:
        if _session_date != today:
            _session_oi  = {}
            _session_date = today
        key = (symbol, strike, side)
        if key not in _session_oi:
            _session_oi[key] = current_oi  # set baseline on first read
        return _session_oi[key]


# Symbols that trade on BFO (BSE F&O) instead of NFO (NSE F&O)
BFO_SYMBOLS = {"SENSEX", "BANKEX"}

# ── Instruments cache (NFO + BFO) ─────────────────────────────────────────────
_instr_cache      = {}    # {exchange: list}
_instr_cache_time = {}    # {exchange: float}
_instr_lock       = threading.Lock()
INSTR_TTL         = 600   # 10 minutes


def get_instruments(kite, exchange="NFO"):
    """Return instruments list for given exchange (NFO or BFO) from local DB cache."""
    from db_instruments import get_cached_instruments
    return get_cached_instruments(exchange)


import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tradesignal_cache.db')

_pending_baselines = set()
_baseline_lock = threading.Lock()
_baseline_queue = queue.PriorityQueue()  # Priority 0 = user-triggered (highest), higher = background
_baseline_seq = __import__('itertools').count()  # Monotonic tie-breaker — avoids comparing KiteConnect objects
_worker_thread = None

def _fetch_and_cache_baseline_sync(kite, date_str: str, instrument_token: int, tradingsymbol: str, current_oi: int):
    """Fetch historical EOD baseline synchronously (called inside worker thread)."""
    try:
        today = datetime.date.today()
        from_dt = today - datetime.timedelta(days=7)
        to_dt = today - datetime.timedelta(days=1)
        
        hist = kite.historical_data(instrument_token, from_dt, to_dt, "day")
        val = current_oi
        if hist:
            val = int(hist[-1].get("oi", current_oi) or current_oi)
            
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS options_eod_baseline (
                date TEXT NOT NULL,
                tradingsymbol TEXT NOT NULL,
                eod_oi INTEGER NOT NULL,
                PRIMARY KEY (date, tradingsymbol)
            )
        """)
        conn.execute(
            "INSERT OR REPLACE INTO options_eod_baseline (date, tradingsymbol, eod_oi) VALUES (?, ?, ?)",
            (date_str, tradingsymbol, val)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"BG baseline fetch failed for {tradingsymbol}: {e}")
    finally:
        with _baseline_lock:
            _pending_baselines.discard((date_str, tradingsymbol))

def _baseline_worker():
    """Background worker processing baseline fetch requests sequentially with pacing."""
    while True:
        got_item = False
        try:
            priority, _seq, kite, date_str, token, symbol, current_oi = _baseline_queue.get()
            got_item = True
            if kite is None:  # Sentinel: None kite signals shutdown
                break
            _fetch_and_cache_baseline_sync(kite, date_str, token, symbol, current_oi)
            # Pacing delay: Sleep 0.35s to respect Zerodha's 3 requests/sec historical limit
            time.sleep(0.35)
        except Exception as ex:
            import logging
            logging.getLogger(__name__).error(f"Error in baseline worker loop: {ex}")
        finally:
            if got_item:
                _baseline_queue.task_done()


def get_cached_baseline(date_str: str, tradingsymbol: str) -> int | None:
    """Retrieve EOD OI from local cache if it exists."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS options_eod_baseline (
                date TEXT NOT NULL,
                tradingsymbol TEXT NOT NULL,
                eod_oi INTEGER NOT NULL,
                PRIMARY KEY (date, tradingsymbol)
            )
        """)
        conn.commit()
        
        cursor = conn.cursor()
        cursor.execute(
            "SELECT eod_oi FROM options_eod_baseline WHERE date = ? AND tradingsymbol = ?",
            (date_str, tradingsymbol)
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            return row[0]
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Error checking option baseline cache: {e}")
    return None


# ── Persistent Spurt Timestamp Tracker ─────────────────────────────────────────
_spurt_time_memory = {}  # {(date_str, symbol): (spurt_time_str, oi_change_pct)}
_spurt_time_lock = threading.Lock()

def sync_spurt_timestamps(spurt_list):
    """Assigns persistent discovery/update timestamps to each stock in spurt_list.
    Persists to SQLite (tradesignal_cache.db) and RAM.
    Updates spurt_time ONLY when OI% changes; retains existing timestamp if unchanged."""
    if not spurt_list:
        return spurt_list

    today_str = datetime.date.today().isoformat()
    now_time_str = datetime.datetime.now().strftime("%I:%M:%S %p").lower()

    with _spurt_time_lock:
        # Purge stale dates from RAM memory
        stale = [k for k in _spurt_time_memory.keys() if k[0] != today_str]
        for k in stale:
            del _spurt_time_memory[k]

        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS oi_spurt_log (
                    date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    spurt_time TEXT NOT NULL,
                    oi_change_pct REAL NOT NULL,
                    PRIMARY KEY (date, symbol)
                )
            """)
            conn.commit()

            cursor = conn.cursor()
            cursor.execute("SELECT symbol, spurt_time, oi_change_pct FROM oi_spurt_log WHERE date = ?", (today_str,))
            db_rows = {r[0]: (r[1], r[2]) for r in cursor.fetchall()}

            db_updates = []
            for item in spurt_list:
                sym = item.get("symbol")
                if not sym:
                    continue
                pct = item.get("oi_change_pct", 0.0)
                mem_key = (today_str, sym)

                if mem_key in _spurt_time_memory:
                    existing_time, existing_pct = _spurt_time_memory[mem_key]
                    if abs(pct - existing_pct) > 0.01:
                        _spurt_time_memory[mem_key] = (now_time_str, pct)
                        db_updates.append((today_str, sym, now_time_str, pct))
                        item["spurt_time"] = now_time_str
                    else:
                        item["spurt_time"] = existing_time
                elif sym in db_rows:
                    existing_time, existing_pct = db_rows[sym]
                    if abs(pct - existing_pct) > 0.01:
                        _spurt_time_memory[mem_key] = (now_time_str, pct)
                        db_updates.append((today_str, sym, now_time_str, pct))
                        item["spurt_time"] = now_time_str
                    else:
                        _spurt_time_memory[mem_key] = (existing_time, existing_pct)
                        item["spurt_time"] = existing_time
                else:
                    _spurt_time_memory[mem_key] = (now_time_str, pct)
                    db_updates.append((today_str, sym, now_time_str, pct))
                    item["spurt_time"] = now_time_str

            if db_updates:
                cursor.executemany(
                    "INSERT OR REPLACE INTO oi_spurt_log (date, symbol, spurt_time, oi_change_pct) VALUES (?, ?, ?, ?)",
                    db_updates
                )
                conn.commit()
            conn.close()
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Error syncing spurt timestamps: {e}")
            for item in spurt_list:
                if "spurt_time" not in item:
                    item["spurt_time"] = now_time_str

    return spurt_list



def get_option_chain(kite, symbol: str):
    """Nearest-expiry option chain from Kite.
    Automatically routes SENSEX/BANKEX to BFO exchange, all others to NFO.
    FIXES:
      - Uses session-start OI baseline (not oi_day_high) for oi_chg
      - Stores net_change per strike/side for correct buildup direction
    Returns (rows, expiry_str, futures_oi, futures_oi_prev, futures_ltp, futures_prev_close, error)."""
    global _worker_thread
    # FIX: BSE F&O instruments (SENSEX, BANKEX) live on BFO exchange, not NFO
    exchange = "BFO" if symbol.upper() in BFO_SYMBOLS else "NFO"
    try:
        instruments = get_instruments(kite, exchange)
    except Exception as e:
        return [], None, 0, 0, 0, 0, str(e)

    opts = [i for i in instruments
            if i["name"] == symbol and i["instrument_type"] in ("CE", "PE")]
    if not opts:
        return [], None, 0, 0, 0, 0, f"No NFO instruments found for {symbol}"

    # Always skip expired expiries — on weekly rollover days (e.g. Thursday post-expiry)
    # the DB still contains the just-expired contract; sorted()[0] would pick it and
    # return all-zero quotes from Kite. This guard works for:
    #   • NIFTY / FINNIFTY  — weekly Thursday expiry
    #   • SENSEX / BANKEX   — weekly Friday expiry (BFO)
    #   • Stock F&O         — monthly last-Thursday expiry
    today_d = datetime.date.today()
    valid_expiries = sorted(
        e for e in set(i["expiry"] for i in opts) if e >= today_d
    )
    if not valid_expiries:
        return [], None, 0, 0, 0, 0, f"No active expiry found for {symbol} (all contracts expired)"
    nearest     = valid_expiries[0]
    chain_instr = [i for i in opts if i["expiry"] == nearest]
    
    # Futures: always select NEAREST valid expiry.
    # instruments list is unordered (no SQLite ORDER BY), so a simple for…break
    # randomly picks July/Aug futures instead of the June near-month contract,
    # producing wrong LTP, wrong prev_close, and wrong buildup label.
    today = datetime.date.today()
    fut_candidates = [
        i for i in instruments
        if i["name"] == symbol
        and i["instrument_type"] == "FUT"
        and i.get("expiry") and i["expiry"] >= today
    ]
    fut_symbol = None
    if fut_candidates:
        nearest_fut = min(fut_candidates, key=lambda i: i["expiry"])
        fut_symbol = f"{exchange}:{nearest_fut['tradingsymbol']}"

    # FIX: use correct exchange prefix for quote (BFO: for SENSEX/BANKEX, NFO: for others)
    ts_list     = [f"{exchange}:{i['tradingsymbol']}" for i in chain_instr]
    if fut_symbol:
        ts_list.append(fut_symbol)

    quotes = {}
    try:
        for batch in _chunks(ts_list, 250):
            quotes.update(kite.quote(batch))
    except Exception:
        try:
            time.sleep(1)
            for batch in _chunks(ts_list, 250):
                quotes.update(kite.quote(batch))
        except Exception as e2:
            return [], str(nearest), 0, 0, 0, 0, str(e2)

    # Extract Futures details
    futures_oi = 0
    futures_oi_prev = 0
    futures_ltp = 0
    futures_prev_close = 0
    today_str = datetime.date.today().isoformat()
    if fut_symbol and fut_symbol in quotes:
        fq = quotes[fut_symbol]
        futures_oi   = int(fq.get("oi", 0) or 0)
        futures_ltp  = float(fq.get("last_price", 0) or 0)
        futures_prev_close = float(fq.get("ohlc", {}).get("close", futures_ltp) or futures_ltp)

        # B1 FIX: Retrieve EOD baseline non-blocking.
        # Check SQLite cache first; queue background fetch if missing (no blocking).
        futures_oi_prev = futures_oi  # safe default
        if fut_candidates and nearest_fut.get("instrument_token"):
            fut_ts = nearest_fut["tradingsymbol"]
            cached_fut_eod = get_cached_baseline(today_str, fut_ts)
            if cached_fut_eod is not None and cached_fut_eod > 0:
                futures_oi_prev = cached_fut_eod
            else:
                # Fall back to oi_day_low until background worker populates SQLite
                futures_oi_prev = int(fq.get("oi_day_low", 0) or 0)
                if futures_oi_prev == 0:
                    futures_oi_prev = futures_oi
                # Queue background fetch if not already in flight
                key = (today_str, fut_ts)
                with _baseline_lock:
                    if key not in _pending_baselines:
                        _pending_baselines.add(key)
                        if _worker_thread is None or not _worker_thread.is_alive():
                            _worker_thread = threading.Thread(target=_baseline_worker, daemon=True)
                            _worker_thread.start()
                        _baseline_queue.put((0, next(_baseline_seq), kite, today_str,
                                             nearest_fut["instrument_token"], fut_ts, futures_oi))
        else:
            futures_oi_prev = int(fq.get("oi_day_low", 0) or 0)
            if futures_oi_prev == 0:
                futures_oi_prev = futures_oi
    by_strike = defaultdict(dict)
    for instr in chain_instr:
        ts      = f"{exchange}:{instr['tradingsymbol']}"
        q       = quotes.get(ts, {})
        oi      = int(q.get("oi", 0) or 0)
        # Use oi_day_low as previous OI baseline — same source as FNO Trap Detector
        prev_oi = int(q.get("oi_day_low", 0) or 0)
        if prev_oi == 0:
            prev_oi = oi  # fallback: no change if no baseline

        # EOD OI baseline for ALL strikes (near-ATM and OTM alike).
        # Near-ATM (≤5% from futures LTP): queued at priority 0 — served first.
        # OTM (>5%): queued at priority 2 — background, yields to near-ATM fetches.
        # Falls back to oi_day_low only until background worker populates SQLite.
        is_near_atm = (futures_ltp <= 0) or (abs(instr["strike"] - futures_ltp) / futures_ltp <= 0.05)
        bl_priority  = 0 if is_near_atm else 2

        prev_day_eod_oi = prev_oi
        if instr.get("instrument_token"):
            cached_val = get_cached_baseline(today_str, instr["tradingsymbol"])
            if cached_val is not None:
                prev_day_eod_oi = cached_val
            else:
                # Trigger background fetch if not already in flight
                key = (today_str, instr["tradingsymbol"])
                with _baseline_lock:
                    if key not in _pending_baselines:
                        _pending_baselines.add(key)

                        # Initialize worker thread if not running
                        if _worker_thread is None or not _worker_thread.is_alive():
                            _worker_thread = threading.Thread(target=_baseline_worker, daemon=True)
                            _worker_thread.start()

                        # Queue baseline fetch (priority 0 = near-ATM, 2 = OTM)
                        _baseline_queue.put((bl_priority, next(_baseline_seq), kite, today_str, instr["instrument_token"], instr["tradingsymbol"], oi))
            
        # Calculate % Change vs EOD Snapshot
        oi_eod_chg_pct = 0.0
        if prev_day_eod_oi > 0:
            oi_eod_chg_pct = round(((oi - prev_day_eod_oi) / prev_day_eod_oi) * 100, 2)
            
        oi_chg  = oi - prev_day_eod_oi
        ltp     = float(q.get("last_price", 0) or 0)
        # Use ohlc.close as previous price — same source as FNO Trap Detector
        prev_ltp = float(q.get("ohlc", {}).get("close", q.get("close_price", ltp)) or ltp)

        by_strike[instr["strike"]][instr["instrument_type"]] = {
            "ltp":      ltp,
            "prev_ltp": prev_ltp,   # prev close for signal engine
            "oi":       oi,
            "prev_oi":  prev_day_eod_oi,
            "oi_chg":   oi_chg,
            "oi_eod_chg_pct": oi_eod_chg_pct,
            "vol":      int(q.get("volume", 0) or 0),
        }

    rows = []
    for strike in sorted(by_strike.keys()):
        ce = by_strike[strike].get("CE", {})
        pe = by_strike[strike].get("PE", {})
        rows.append({
            "strike":        strike,
            "ce_ltp":        ce.get("ltp",      0), "ce_oi":      ce.get("oi",      0),
            "ce_oi_chg":     ce.get("oi_chg",   0), "ce_vol":     ce.get("vol",     0),
            "ce_prev_ltp":   ce.get("prev_ltp", 0), "ce_prev_oi": ce.get("prev_oi", 0),
            "ce_oi_eod_chg_pct": ce.get("oi_eod_chg_pct", 0.0),
            "pe_ltp":        pe.get("ltp",      0), "pe_oi":      pe.get("oi",      0),
            "pe_oi_chg":     pe.get("oi_chg",   0), "pe_vol":     pe.get("vol",     0),
            "pe_prev_ltp":   pe.get("prev_ltp", 0), "pe_prev_oi": pe.get("prev_oi", 0),
            "pe_oi_eod_chg_pct": pe.get("oi_eod_chg_pct", 0.0),
        })
    return rows, str(nearest), futures_oi, futures_oi_prev, futures_ltp, futures_prev_close, None

# ── Analytics ──────────────────────────────────────────────────────────────────

def compute_max_pain(chain):
    if not chain:
        return None
    min_loss, mp = float("inf"), chain[0]["strike"]
    for test in [r["strike"] for r in chain]:
        loss = sum(
            max(0, test - r["strike"]) * r["ce_oi"] +
            max(0, r["strike"] - test) * r["pe_oi"]
            for r in chain
        )
        if loss < min_loss:
            min_loss, mp = loss, test
    return mp


def compute_pcr(chain):
    ce = sum(r["ce_oi"] for r in chain)
    pe = sum(r["pe_oi"] for r in chain)
    return round(pe / ce, 3) if ce else None


def top_strikes(chain, ltp, n=5):
    """Classify top CE/PE strikes with corrected buildup & trap logic.
    FIXES:
      - Buildup uses net_change direction (not option price > 0)
      - Trap labels renamed to industry-standard terms
      - ce_wall/pe_wall = highest OI strike nearest to LTP (not global max OI)
      - strike_pcr from full chain lookup
    """
    if not chain:
        return {"top_ce": [], "top_pe": [], "ce_wall": None, "pe_wall": None}

    strikes_sorted = sorted(set(r["strike"] for r in chain))
    gap = (strikes_sorted[1] - strikes_sorted[0]) if len(strikes_sorted) >= 2 else (ltp * 0.01)

    # Full chain PCR lookup
    chain_pcr = {}
    for r in chain:
        ce_oi = r.get("ce_oi", 0) or 0
        pe_oi = r.get("pe_oi", 0) or 0
        chain_pcr[r["strike"]] = round(pe_oi / ce_oi, 2) if ce_oi > 0 else None

    # FIX: ATM-proximal walls (first meaningful resistance/support near current price)
    # CE Wall = highest OI CE strike at or ABOVE LTP (nearest resistance)
    ce_candidates = [r for r in chain if r["strike"] >= ltp]
    pe_candidates = [r for r in chain if r["strike"] <= ltp]
    ce_wall = max(ce_candidates, key=lambda r: r["ce_oi"])["strike"] if ce_candidates else None
    pe_wall = max(pe_candidates, key=lambda r: r["pe_oi"])["strike"] if pe_candidates else None

    def classify(r, side):
        oi_chg    = r[f"{side}_oi_chg"]
        # FIX 1: net_chg was never stored in chain rows (always 0 → always "Short Buildup").
        # Use ltp vs prev_ltp (ohlc.close from Kite) which IS stored in chain rows.
        _ltp      = r.get(f"{side}_ltp",      0) or 0
        _prev_ltp = r.get(f"{side}_prev_ltp", _ltp) or _ltp
        price_up  = _ltp > (_prev_ltp * 1.005) if _prev_ltp > 0 else False
        strike    = r["strike"]
        oi        = r[f"{side}_oi"]

        # Buildup: only label when intraday OI change data is present
        # FIX: oi_chg=0 means market closed or session-start baseline → show "–"
        # rather than the misleading "Long Unwinding"
        if oi_chg > 0 and price_up:
            buildup = "Long Buildup"
        elif oi_chg > 0 and not price_up:
            buildup = "Short Buildup"
        elif oi_chg < 0 and price_up:
            buildup = "Short Covering"
        elif oi_chg < 0:
            buildup = "Long Unwinding"
        else:
            buildup = "–"   # no intraday change data (market closed / first fetch)

        # Trap detection — retail buyer perspective:
        # CALL TRAP: Heavy CE writing at/just above LTP creates a ceiling.
        #   Retail buys calls expecting breakout → price can't break writers' wall → trapped.
        # PUT TRAP:  Heavy PE writing at/just below LTP creates a floor.
        #   Retail buys puts expecting breakdown → price bounces off writers' support → trapped.
        # Window = 2x gap so that the nearest strike on each side is reliably captured.
        trap = None
        if ltp:
            if side == "ce":
                if ltp <= strike <= ltp + 2 * gap:
                    trap = "Call Trap"   # CE wall near spot — call buyers exposed
            else:  # pe
                if ltp - 2 * gap <= strike <= ltp:
                    trap = "Put Trap"    # PE wall near spot — put buyers exposed

        return {"strike": strike, "oi": oi, "oi_chg": oi_chg,
                "buildup": buildup, "trap": trap,
                "strike_pcr": chain_pcr.get(strike)}

    top_ce = sorted(chain, key=lambda r: r["ce_oi"], reverse=True)[:n]
    top_pe = sorted(chain, key=lambda r: r["pe_oi"], reverse=True)[:n]
    return {
        "top_ce":  [classify(r, "ce") for r in top_ce],
        "top_pe":  [classify(r, "pe") for r in top_pe],
        "ce_wall": ce_wall,
        "pe_wall": pe_wall,
    }


def compute_atm_5_analysis(chain_sorted, atm_idx, ltp):
    """Computes dynamic ATM +- 5 immediate support/resistance levels, level strength,
    multi-strike cluster buildup velocity, and dynamic risk flags.
    Handles irregular strike steps (e.g. TVSMOTOR 10/20/40 steps) safely.
    """
    if not chain_sorted or atm_idx is None or ltp <= 0:
        return None

    # Array-index window for 11 strikes around ATM (5 below, ATM, 5 above)
    start_idx = max(0, atm_idx - 5)
    end_idx = min(len(chain_sorted), atm_idx + 6)
    atm_5_slice = chain_sorted[start_idx:end_idx]

    if not atm_5_slice:
        return None

    # Candidate strikes: CE > LTP (first immediate resistance above spot), PE <= LTP (first immediate support at/below spot)
    ce_candidates = [r for r in atm_5_slice if r["strike"] >= ltp] or [chain_sorted[atm_idx]]
    pe_candidates = [r for r in atm_5_slice if r["strike"] <= ltp] or [chain_sorted[atm_idx]]

    ce_above_with_oi = [r for r in chain_sorted if r["strike"] > ltp and (r.get("ce_oi", 0) or 0) > 0]
    pe_below_with_oi = [r for r in chain_sorted if r["strike"] <= ltp and (r.get("pe_oi", 0) or 0) > 0]

    # Immediate Resistance = first strike directly above LTP with valid CE OI (> 0)
    if ce_above_with_oi:
        imm_res_row = min(ce_above_with_oi, key=lambda r: r["strike"])
    else:
        ce_above_all = [r for r in atm_5_slice if r["strike"] > ltp]
        imm_res_row = min(ce_above_all, key=lambda r: r["strike"]) if ce_above_all else (ce_candidates[0] if ce_candidates else chain_sorted[atm_idx])
    imm_res_strike = imm_res_row["strike"]
    imm_res_oi = imm_res_row.get("ce_oi", 0) or 0

    # Immediate Support = first strike directly at or below LTP with valid PE OI (> 0)
    if pe_below_with_oi:
        imm_sup_row = max(pe_below_with_oi, key=lambda r: r["strike"])
    else:
        pe_below_all = [r for r in atm_5_slice if r["strike"] <= ltp]
        imm_sup_row = max(pe_below_all, key=lambda r: r["strike"]) if pe_below_all else (pe_candidates[0] if pe_candidates else chain_sorted[atm_idx])
    imm_sup_strike = imm_sup_row["strike"]
    imm_sup_oi = imm_sup_row.get("pe_oi", 0) or 0

    # Strike PCR Calculations & PCR-Based Static Strength Scoring
    res_pe_oi = imm_res_row.get("pe_oi", 0) or 0
    res_pcr = round(res_pe_oi / imm_res_oi, 2) if imm_res_oi > 0 else 0.0

    sup_ce_oi = imm_sup_row.get("ce_oi", 0) or 0
    sup_pcr = round(imm_sup_oi / sup_ce_oi, 2) if sup_ce_oi > 0 else 99.0

    # 1. Resistance PCR Rules (Strike Above Spot):
    #    PCR < 0.4   → Strong Resistance (Heavy Call writing capping upside)
    #    PCR 0.4–0.8 → Moderate Resistance
    #    PCR > 0.8   → Weak / Unusual Resistance (Put activity dilutes wall)
    if res_pcr < 0.4:
        static_res_score = 90
    elif 0.4 <= res_pcr <= 0.8:
        static_res_score = 65
    else:
        static_res_score = 30

    # 2. Support PCR Rules (Strike Below Spot):
    #    PCR >= 1.5  → Strong Support (Heavy Put writing relative to Calls)
    #    PCR 1.0–1.5 → Moderate Support
    #    PCR < 1.0   → Weak / Unreliable Support (Lack of Put conviction)
    if sup_pcr >= 1.5:
        static_sup_score = 90
    elif 1.0 <= sup_pcr < 1.5:
        static_sup_score = 65
    else:
        static_sup_score = 30

    # Helper to calculate 4-way buildup vector
    def _bld_vec(oi_chg, cur_ltp, prv_ltp):
        if prv_ltp <= 0 or cur_ltp <= 0 or oi_chg == 0:
            return 0.0, "–"
        pct_chg = (cur_ltp - prv_ltp) / prv_ltp
        if abs(pct_chg) <= 0.0025:
            return 0.0, "Flat"
        price_up = pct_chg > 0.0025
        oi_up = oi_chg > 0
        if oi_up and price_up:
            return +0.5, "Long Buildup"
        if oi_up and not price_up:
            return +1.0, "Short Buildup"
        if not oi_up and price_up:
            return -1.0, "Short Covering"
        return -0.5, "Long Unwinding"

    # Multi-strike cluster buildup velocity calculation
    # For CE cluster (resistance side)
    ce_abs_oi_chg_sum = sum(abs(r.get("ce_oi_chg", 0) or 0) for r in ce_candidates)
    if ce_abs_oi_chg_sum > 0:
        v_ce_cluster = 0.0
        for r in ce_candidates:
            val, _ = _bld_vec(r.get("ce_oi_chg", 0), r.get("ce_ltp", 0), r.get("ce_prev_ltp", 0))
            weight = abs(r.get("ce_oi_chg", 0)) / ce_abs_oi_chg_sum
            v_ce_cluster += weight * val
    else:
        v_ce_cluster = 0.0

    # For PE cluster (support side)
    pe_abs_oi_chg_sum = sum(abs(r.get("pe_oi_chg", 0) or 0) for r in pe_candidates)
    if pe_abs_oi_chg_sum > 0:
        v_pe_cluster = 0.0
        for r in pe_candidates:
            val, _ = _bld_vec(r.get("pe_oi_chg", 0), r.get("pe_ltp", 0), r.get("pe_prev_ltp", 0))
            weight = abs(r.get("pe_oi_chg", 0)) / pe_abs_oi_chg_sum
            v_pe_cluster += weight * val
    else:
        v_pe_cluster = 0.0

    # Combine static PCR score (50%) with intraday cluster velocity (50%)
    if ce_abs_oi_chg_sum == 0:
        res_strength = static_res_score
    else:
        res_strength = round((static_res_score * 0.50) + (((v_ce_cluster + 1.0) / 2.0) * 50))

    if pe_abs_oi_chg_sum == 0:
        sup_strength = static_sup_score
    else:
        sup_strength = round((static_sup_score * 0.50) + (((v_pe_cluster + 1.0) / 2.0) * 50))

    res_strength = max(0, min(100, res_strength))
    sup_strength = max(0, min(100, sup_strength))

    # Helper ratings
    def _rating(score):
        if score >= 80:
            return "STRONG", "var(--green)"
        if score >= 50:
            return "MODERATE", "var(--yellow)"
        return "WEAK", "var(--red)"

    res_rating, res_color = _rating(res_strength)
    sup_rating, sup_color = _rating(sup_strength)

    imm_res_vec, imm_res_buildup = _bld_vec(imm_res_row.get("ce_oi_chg", 0), imm_res_row.get("ce_ltp", 0), imm_res_row.get("ce_prev_ltp", 0))
    imm_sup_vec, imm_sup_buildup = _bld_vec(imm_sup_row.get("pe_oi_chg", 0), imm_sup_row.get("pe_ltp", 0), imm_sup_row.get("pe_prev_ltp", 0))

    # ── Symmetrical Air Pocket & Resistance Vacuum Detection ──
    # Downside Air Pocket: Nearest 2-3 PE strikes below immediate support
    pe_below_imm = [r for r in atm_5_slice if r["strike"] < imm_sup_strike]
    pe_air_pocket_rows = sorted(pe_below_imm, key=lambda r: r["strike"], reverse=True)[:3]
    pe_air_pocket_oi_sum = sum(r.get("pe_oi", 0) or 0 for r in pe_air_pocket_rows)
    total_pe_oi_window = sum(r.get("pe_oi", 0) or 0 for r in atm_5_slice) or 1
    pe_density_ratio = pe_air_pocket_oi_sum / total_pe_oi_window

    # Upside Resistance Vacuum: Nearest 2-3 CE strikes above immediate resistance
    ce_above_imm = [r for r in atm_5_slice if r["strike"] > imm_res_strike]
    ce_vacuum_rows = sorted(ce_above_imm, key=lambda r: r["strike"])[:3]
    ce_vacuum_oi_sum = sum(r.get("ce_oi", 0) or 0 for r in ce_vacuum_rows)
    total_ce_oi_window = sum(r.get("ce_oi", 0) or 0 for r in atm_5_slice) or 1
    ce_density_ratio = ce_vacuum_oi_sum / total_ce_oi_window

    has_pe_air_pocket = len(pe_air_pocket_rows) > 0 and (pe_density_ratio < 0.18 or pe_air_pocket_oi_sum < (imm_sup_oi * 0.30))
    has_ce_vacuum = len(ce_vacuum_rows) > 0 and (ce_density_ratio < 0.18 or ce_vacuum_oi_sum < (imm_res_oi * 0.30))

    # Determine Combination Risk Flag
    if sup_strength < 50 and has_pe_air_pocket:
        flag_code = "AIR_POCKET_DOWNSIDE"
        alert_title = "💀 DOWNSIDE AIR POCKET RISK"
        short_desc = f"Weak support at {imm_sup_strike} ({sup_strength}/100) with thin PE backing for 2-3 strikes below. High flush risk."
        flag_cls = "tag-red"
    elif res_strength < 50 and has_ce_vacuum:
        flag_code = "RESISTANCE_VACUUM_UPSIDE"
        alert_title = "⚡ RESISTANCE VACUUM SQUEEZE"
        short_desc = f"Weak resistance at {imm_res_strike} ({res_strength}/100) with thin CE writing for 2-3 strikes above. Squeeze risk."
        flag_cls = "tag-green"
    elif v_ce_cluster < -0.20 and v_pe_cluster > +0.10:
        flag_code = "RISK_UPSIDE_SQUEEZE"
        alert_title = "⚡ UPSIDE SQUEEZE RISK"
        short_desc = f"Call Writers unwinding at {imm_res_strike} ({res_strength}/100) while Put Writers add support at {imm_sup_strike} ({sup_strength}/100)."
        flag_cls = "tag-green"
    elif v_pe_cluster < -0.20 and v_ce_cluster > +0.10:
        flag_code = "RISK_DOWNSIDE_FLUSH"
        alert_title = "💀 DOWNSIDE FLUSH RISK"
        short_desc = f"Put Floor crumbling at {imm_sup_strike} ({sup_strength}/100) while Call Writers press resistance at {imm_res_strike} ({res_strength}/100)."
        flag_cls = "tag-red"
    elif v_ce_cluster > +0.20 and v_pe_cluster > +0.20:
        flag_code = "RANGE_LOCK_STABLE"
        alert_title = "🛡️ INSTITUTIONAL RANGE LOCK"
        short_desc = f"Writers actively defending both sides between {imm_sup_strike} – {imm_res_strike}."
        flag_cls = "tag-blue"
    elif v_ce_cluster < -0.20 and v_pe_cluster < -0.20:
        flag_code = "DUAL_UNWIND_VOLATILITY"
        alert_title = "⚠️ DUAL UNWINDING VOLATILITY"
        short_desc = "Writers abandoning both sides. Rapid price expansion expected."
        flag_cls = "tag-yellow"
    else:
        flag_code = "NEUTRAL_BALANCED"
        alert_title = "⚖️ BALANCED ATM BOUNDS"
        short_desc = f"Immediate bounds: Support at {imm_sup_strike} ({sup_rating}) | Resistance at {imm_res_strike} ({res_rating})."
        flag_cls = "tag-blue"

    return {
        "atm_strike": chain_sorted[atm_idx]["strike"],
        "immediate_resistance": {
            "strike": imm_res_strike,
            "oi": imm_res_oi,
            "pcr": res_pcr,
            "buildup": imm_res_buildup,
            "strength_score": res_strength,
            "strength_rating": res_rating,
            "color": res_color,
        },
        "immediate_support": {
            "strike": imm_sup_strike,
            "oi": imm_sup_oi,
            "pcr": sup_pcr,
            "buildup": imm_sup_buildup,
            "strength_score": sup_strength,
            "strength_rating": sup_rating,
            "color": sup_color,
        },
        "cluster_velocity": {
            "v_ce": round(v_ce_cluster, 2),
            "v_pe": round(v_pe_cluster, 2),
        },
        "risk_analysis": {
            "flag_code": flag_code,
            "alert_title": alert_title,
            "short_desc": short_desc,
            "flag_cls": flag_cls,
        }
    }



def compute_pivots(high, low, close):
    p = (high + low + close) / 3
    return {
        "P":  round(p, 2),
        "R1": round(2*p - low, 2),   "R2": round(p + high - low, 2),
        "R3": round(high + 2*(p - low), 2),
        "S1": round(2*p - high, 2),  "S2": round(p - (high - low), 2),
        "S3": round(low - 2*(high - p), 2),
    }


# FIX: Extended exchange map (NIFTYIT + BANKEX were missing → silently returned 0 LTP)
EXCHANGE_MAP = {
    "NIFTY":      "NSE:NIFTY 50",
    "NIFTY50":    "NSE:NIFTY 50",
    "BANKNIFTY":  "NSE:NIFTY BANK",
    "FINNIFTY":   "NSE:NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NSE:NIFTY MID SELECT",
    "SENSEX":     "BSE:SENSEX",
    "BANKEX":     "BSE:BANKEX",
    "NIFTYIT":    "NSE:NIFTY IT",
    "NIFTYNXT50": "NSE:NIFTY NEXT 50",
}

# For frontend to apply correct PCR thresholds (stocks ≠ indices)
INDEX_SYMBOLS = set(EXCHANGE_MAP.keys())


def get_ltp_and_pivots(kite, symbol):
    """Get LTP, price_change_pct, and PREVIOUS DAY pivots.
    P1 FIX: Previous-day OHLC and derived pivots are cached per (symbol, date) —
    historical_data() is called only ONCE per symbol per trading day.
    LTP and price_change_pct are always fetched fresh from kite.quote()."""
    exch_sym  = EXCHANGE_MAP.get(symbol.upper(), f"NSE:{symbol}")
    today_str = datetime.date.today().isoformat()
    cache_key = (symbol.upper(), today_str)

    try:
        q_data = kite.quote([exch_sym])
        d      = q_data.get(exch_sym, {})
        ltp    = d.get("last_price", 0)
        token  = d.get("instrument_token")
        ohlc   = d.get("ohlc", {})
        open_px = ohlc.get("open", 0) or 0

        # ── Pivot cache hit: reuse historical data, only recompute LTP-derived fields ──
        with _pivot_cache_lock:
            cached = _pivot_cache.get(cache_key)
        if cached:
            prev_close = cached["prev_close"]
            price_change_pct = round((ltp - prev_close) / prev_close * 100, 2) if prev_close > 0 else 0.0
            return ltp, price_change_pct, cached["pivots"], cached["pivot_source"], prev_close, cached["open_gap_pct"], None

        # ── Cache miss: full fetch including historical_data() ──
        pivot_source = "prev_day"
        prev_close   = ohlc.get("close", 0) or 0
        if prev_close > 0:
            price_change_pct = round((ltp - prev_close) / prev_close * 100, 2)
            open_gap_pct     = round((open_px - prev_close) / prev_close * 100, 2) if open_px > 0 else 0.0
        else:
            price_change_pct = 0.0
            open_gap_pct     = 0.0

        # Previous session OHLC for correct pivot calculation
        high, low, close = 0, 0, 0
        if token:
            today   = datetime.date.today()
            from_dt = today - datetime.timedelta(days=7)
            to_dt   = today - datetime.timedelta(days=1)
            hist    = kite.historical_data(token, from_dt, to_dt, "day")
            if hist:
                last  = hist[-1]
                high  = last["high"]
                low   = last["low"]
                close = last["close"]
                # BUG 7 FIX: BSE quotes return ohlc.close=0 before auction settles (SENSEX/BANKEX).
                # Recompute price_change_pct and open_gap_pct using historical prev close.
                if prev_close == 0 and close > 0:
                    prev_close = close
                    price_change_pct = round((ltp - prev_close) / prev_close * 100, 2)
                    open_gap_pct = round((open_px - prev_close) / prev_close * 100, 2) if open_px > 0 else 0.0

        if not (high and low and close):
            high         = ohlc.get("high", 0)
            low          = ohlc.get("low", 0)
            close        = ohlc.get("close", 0)
            pivot_source = "today_ohlc"

        pivots = compute_pivots(high, low, close) if (high and low and close) else None

        # Store pivot data in cache (LTP not cached — always refreshed from quote)
        with _pivot_cache_lock:
            _pivot_cache[cache_key] = {
                "pivots":       pivots,
                "pivot_source": pivot_source,
                "prev_close":   prev_close,
                "open_gap_pct": open_gap_pct,
            }

        return ltp, price_change_pct, pivots, pivot_source, prev_close, open_gap_pct, None

    except Exception as e:

        return 0, 0.0, None, None, 0, 0.0, str(e)


# ── Routes ─────────────────────────────────────────────────────────────────────

@oi_spurt_bp.route("/spurt")
def api_spurt():
    """GET /api/oi/spurt?min_pct=5  →  NSE OI spurt list."""
    min_pct = float(request.args.get("min_pct", 5.0))
    market_open = is_market_hours()
    today_str = datetime.date.today().isoformat()
    cache_key = (today_str, min_pct)

    # Off-hours caching logic
    if not market_open:
        with _spurt_eod_lock:
            # Clean up old date entries
            stale_keys = [k for k in _spurt_eod_cache.keys() if k[0] != today_str]
            for sk in stale_keys:
                del _spurt_eod_cache[sk]

            if cache_key in _spurt_eod_cache:
                cached = _spurt_eod_cache[cache_key]
                return jsonify({
                    "data":         cached["data"],
                    "source":       cached["source"],
                    "count":        len(cached["data"]),
                    "market_open":  False,
                    "cache_source": "eod_cache",
                    "data_as_of":   cached["fetched_at"],
                    "timestamp":    cached["fetched_at"],
                })

    spurt_list, source, err = fetch_oi_spurt(min_pct)
    if err:
        return jsonify({"error": err}), 502

    spurt_list = sync_spurt_timestamps(spurt_list)
    now_iso = datetime.datetime.now().isoformat()
    if not market_open:
        with _spurt_eod_lock:
            _spurt_eod_cache[cache_key] = {
                "data": spurt_list,
                "source": source,
                "fetched_at": now_iso
            }

    return jsonify({
        "data":         spurt_list,
        "source":       source,
        "count":        len(spurt_list),
        "market_open":  market_open,
        "cache_source": "live" if market_open else "eod_cache",
        "data_as_of":   now_iso,
        "timestamp":    now_iso,
    })


@oi_spurt_bp.route("/symbol/<symbol>")
def api_symbol(symbol):
    """GET /api/oi/symbol/RELIANCE  →  full detail."""
    try:
        kite = _get_kite()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 401

    sym = symbol.upper()
    ltp, price_change_pct, pivots, pivot_source, prev_close, open_gap_pct, perr = get_ltp_and_pivots(kite, sym)
    chain, expiry, futures_oi, futures_oi_prev, futures_ltp, futures_prev_close, cerr = get_option_chain(kite, sym)

    # Calculate Futures stats and Buildup (Layer 1)
    futures_price_chg_pct = 0.0
    futures_oi_chg_pct = 0.0
    futures_buildup = "–"
    
    if futures_oi > 0:
        if futures_prev_close > 0:
            futures_price_chg_pct = round(((futures_ltp - futures_prev_close) / futures_prev_close * 100), 2)
        if futures_oi_prev > 0:
            futures_oi_chg_pct = round(((futures_oi - futures_oi_prev) / futures_oi_prev * 100), 2)
            
        futures_price_up = futures_ltp > futures_prev_close
        futures_oi_up = futures_oi > futures_oi_prev
        
        # Matrix classification
        if futures_oi_prev == 0 or futures_prev_close == 0:
            futures_buildup = "–"
        elif futures_oi == futures_oi_prev or futures_ltp == futures_prev_close:
            futures_buildup = "Flat"
        elif futures_oi_up and futures_price_up:
            futures_buildup = "Long Buildup"
        elif futures_oi_up and not futures_price_up:
            futures_buildup = "Short Buildup"
        elif not futures_oi_up and not futures_price_up:
            futures_buildup = "Long Unwinding"
        elif not futures_oi_up and futures_price_up:
            futures_buildup = "Short Covering"

    # ── History & Checklist updates ──
    is_flat_futures = False
    futures_oi_change = 0
    basis_trend = "Flat"
    history = []
    
    if chain and ltp:
        with _spurt_lock:
            if sym not in _spurt_history:
                # Issue 10 FIX: cap dict at 100 symbols to prevent unbounded growth
                # on long-running servers where many F&O symbols get clicked over a session.
                if len(_spurt_history) >= 100:
                    _spurt_history.pop(next(iter(_spurt_history)))
                _spurt_history[sym] = []
            
            # ATM strike CE/PE detail for checklist steps
            atm_ce_oi = 0
            atm_pe_oi = 0
            atm_ce_ltp = 0
            atm_pe_ltp = 0
            chain_sorted = sorted(chain, key=lambda r: r["strike"])
            atm_idx      = min(range(len(chain_sorted)), key=lambda i: abs(chain_sorted[i]["strike"] - ltp))
            atm_row      = chain_sorted[atm_idx]
            atm_ce_oi    = atm_row["ce_oi"]
            atm_pe_oi    = atm_row["pe_oi"]
            atm_ce_ltp   = atm_row["ce_ltp"]
            atm_pe_ltp   = atm_row["pe_ltp"]

            tick = {
                "timestamp": time.time(),
                "futures_oi": futures_oi,
                "futures_ltp": futures_ltp,
                "spot_ltp": ltp,
                "atm_ce_oi": atm_ce_oi,
                "atm_pe_oi": atm_pe_oi,
                "atm_ce_ltp": atm_ce_ltp,
                "atm_pe_ltp": atm_pe_ltp,
                "strikes": {
                    r["strike"]: {
                        "ce_oi": r["ce_oi"], "ce_ltp": r["ce_ltp"],
                        "pe_oi": r["pe_oi"], "pe_ltp": r["pe_ltp"]
                    } for r in chain
                }
            }
            
            _spurt_history[sym].append(tick)
            if len(_spurt_history[sym]) > 4:
                _spurt_history[sym] = _spurt_history[sym][-4:]
            history = list(_spurt_history[sym])

        # Step 1: Flat Futures OI Gate
        if len(history) >= 3:
            fut_oi_ticks = [t["futures_oi"] for t in history[-3:]]
            # BUG 6 FIX: Time-gate flat-futures suppression to avoid session-open false positives.
            # First 3 ticks (≈45s) are identical simply because Kite OI hasn't moved yet.
            # Only suppress signals after 10 minutes have elapsed since the first history tick.
            elapsed_since_first = time.time() - history[0]["timestamp"]
            if len(set(fut_oi_ticks)) == 1 and elapsed_since_first >= 600:
                is_flat_futures = True
            futures_oi_change = history[-1]["futures_oi"] - history[-2]["futures_oi"]
        else:
            futures_oi_change = futures_oi - (futures_oi_prev or futures_oi)
            if futures_oi_prev and futures_oi == futures_oi_prev:
                is_flat_futures = True

        # Step 2: Basis Expansion/Contraction
        basis = futures_ltp - ltp
        prev_basis = history[-2]["futures_ltp"] - history[-2]["spot_ltp"] if len(history) >= 2 else basis
        if basis > prev_basis:
            basis_trend = "Expanding"
        elif basis < prev_basis:
            basis_trend = "Contracting"

        # Step 3: ATM Delta Bias & Step 4: ATM LTP Divergence Checklist Gates
        is_atm_consolidation = False
        is_atm_ce_writers_dominating = False
        is_atm_pe_writers_dominating = False
        if len(history) >= 2:
            prev_tick = history[-2]
            curr_tick = history[-1]
            atm_ce_oi_change = curr_tick["atm_ce_oi"] - prev_tick["atm_ce_oi"]
            atm_pe_oi_change = curr_tick["atm_pe_oi"] - prev_tick["atm_pe_oi"]
            atm_ce_ltp_change = curr_tick["atm_ce_ltp"] - prev_tick["atm_ce_ltp"]
            atm_pe_ltp_change = curr_tick["atm_pe_ltp"] - prev_tick["atm_pe_ltp"]

            # Step 3: Consolidation check (Threshold: difference less than noise floor)
            noise_limit = 25000 if (sym in INDEX_SYMBOLS) else 2000
            if abs(atm_ce_oi_change - atm_pe_oi_change) < (noise_limit * 0.5):
                is_atm_consolidation = True

            # Step 4: ATM LTP Divergence (Writers dominating CE or PE)
            if atm_ce_oi_change > 0 and atm_ce_ltp_change < 0:
                is_atm_ce_writers_dominating = True
            if atm_pe_oi_change > 0 and atm_pe_ltp_change < 0:
                is_atm_pe_writers_dominating = True

    max_pain = compute_max_pain_cached(sym, expiry, chain) if chain else None
    strikes  = top_strikes(chain, ltp) if chain else {"top_ce": [], "top_pe": [], "ce_wall": None, "pe_wall": None}

    # Build chain_data: ±10 strikes around ATM for the heatmap
    chain_data, straddle, atm_strike = [], None, None
    raw_slice = []
    pcr = None
    if chain and ltp:
        chain_sorted = sorted(chain, key=lambda r: r["strike"])
        atm_idx      = min(range(len(chain_sorted)), key=lambda i: abs(chain_sorted[i]["strike"] - ltp))
        atm_strike   = chain_sorted[atm_idx]["strike"]
        start        = max(0, atm_idx - 9)
        end          = min(len(chain_sorted), atm_idx + 10)
        raw_slice    = chain_sorted[start:end]
        pcr          = compute_pcr(raw_slice) if raw_slice else None

        def _bld(oi_chg, cur_ltp, prv_ltp):
            """4-way buildup classifier for per-strike chain_data rows."""
            if prv_ltp <= 0 or cur_ltp <= 0: return "–"
            if oi_chg == 0: return "Flat"

            pct_chg = (cur_ltp - prv_ltp) / prv_ltp
            THRESHOLD = 0.0025  # 0.25%, applied symmetrically

            if abs(pct_chg) <= THRESHOLD:
                return "Flat"  # price move too small to classify direction

            price_up = pct_chg > THRESHOLD
            oi_up    = oi_chg > 0

            if oi_up and price_up:      return "Long Buildup"
            if oi_up and not price_up:  return "Short Buildup"
            if not oi_up and price_up:  return "Short Covering"
            return "Long Unwinding"

        chain_data = []
        for row in raw_slice:
            chain_data.append({
                **row,
                "ce_buildup": _bld(row.get("ce_oi_chg", 0), row.get("ce_ltp", 0), row.get("ce_prev_ltp", 0)),
                "pe_buildup": _bld(row.get("pe_oi_chg", 0), row.get("pe_ltp", 0), row.get("pe_prev_ltp", 0)),
            })

        atm_row  = chain_sorted[atm_idx]
        straddle = round(atm_row["ce_ltp"] + atm_row["pe_ltp"], 2)
        if strikes is not None:
            strikes["atm_window_5"] = compute_atm_5_analysis(chain_sorted, atm_idx, ltp)

    # ── Retail Action & HAction Logic ──
    final_retail_action = ""
    final_h_action      = ""
    highest_conviction  = 0
    signal_source       = None
    h_action_source     = None
    _strike_actions     = {}  # {strike: {"CE": action, "PE": action}} — per-strike retail actions for heatmap

    errors = [e for e in [perr, cerr] if e]

    # FIX 2: Gap guard — significant overnight gaps re-price ALL options at open.
    # Comparing today's LTP to yesterday's ohlc.close is meaningless on gap days:
    # every CE above new LTP looks "Rising" and every PE below looks "Falling"
    # purely from the gap move, not intraday institutional action.
    # Threshold: 0.5% for indices (noisier, tighter), 0.75% for stocks.
    is_index_sym  = sym in INDEX_SYMBOLS
    gap_threshold = 0.5 if is_index_sym else 0.75
    gap_detected  = abs(open_gap_pct) >= gap_threshold
    if gap_detected:
        gap_dir = "up" if open_gap_pct > 0 else "down"
        errors.append(
            f"⚠ Gap-{gap_dir} day ({open_gap_pct:+.2f}%): "
            f"price/IV trends suppressed — signals based on OI momentum only"
        )

    # Minimum OI to treat a strike as institutional-scale (filters retail noise)
    min_sig_oi = 25_000 if is_index_sym else 2_000

    if chain and ltp and prev_close and expiry:
        # Parse expiry date
        try:
            exp_date = datetime.datetime.strptime(expiry, "%Y-%m-%d").date()
            T = trading_time_to_expiry(exp_date)
            r_rate = RISK_FREE_RATE
            
            # Calculate dynamic strike step size from option chain strikes
            strike_step = 50.0
            if chain and len(chain) >= 2:
                chain_sorted_all = sorted(chain, key=lambda r: r["strike"])
                strike_step = chain_sorted_all[1]["strike"] - chain_sorted_all[0]["strike"]
            elif ltp:
                strike_step = ltp * 0.01

            sorted_chain = sorted(chain, key=lambda x: abs(x['strike'] - ltp))
            for i, r_data in enumerate(sorted_chain):
                loc_str = "Close" if i <= 8 else "Far"
                is_above = r_data['strike'] >= ltp
                is_below = r_data['strike'] < ltp
                
                for opt_type in ('CE', 'PE'):
                    prefix = opt_type.lower()
                    opt_ltp      = r_data.get(f"{prefix}_ltp",      0)
                    opt_oi_chg   = r_data.get(f"{prefix}_oi_chg",   0)
                    opt_prev_ltp = r_data.get(f"{prefix}_prev_ltp", opt_ltp)  # ohlc.close from Kite
                    opt_prev_oi  = r_data.get(f"{prefix}_prev_oi",  0)
                    opt_oi       = r_data.get(f"{prefix}_oi",        0)  # absolute OI for size filter

                    if not opt_ltp: continue
                    # FIX 3 (part): skip strikes with insufficient OI (retail noise)
                    if opt_oi < min_sig_oi: continue

                    # IV — compare current vs prev-close (same as FNO Trap)
                    iv_curr = implied_vol(opt_ltp,      ltp,        r_data['strike'], T, r_rate, opt_type) if opt_ltp      > 0 else 0
                    iv_prev = implied_vol(opt_prev_ltp, prev_close, r_data['strike'], T, r_rate, opt_type) if opt_prev_ltp > 0 else 0
                    
                    # OI trend: ±2% dead-zone
                    # FIX 4: prev_oi==0 means a new/fresh strike. A large intraday OI buildup
                    # on a 0-baseline IS "Increasing" — previously silenced as "Flat".
                    if opt_prev_oi == 0:
                        oi_trend = "Increasing" if opt_oi_chg > 0 else "Flat"
                    else:
                        oi_trend = "Increasing" if opt_oi_chg > (opt_prev_oi * 0.02) else "Reducing" if opt_oi_chg < -(opt_prev_oi * 0.02) else "Flat"
                    
                    # Price trend: ±1% dead-zone (same as FNO Trap)
                    px_trend = "Rising" if opt_ltp > (opt_prev_ltp * 1.01) else "Falling" if opt_ltp < (opt_prev_ltp * 0.99) else "Flat"
                    if opt_prev_ltp == 0: px_trend = "Flat"

                    # IV trend: ±1% dead-zone
                    iv_trend = "Rising" if iv_curr > (iv_prev * 1.01) else "Falling" if iv_curr < (iv_prev * 0.99) else "Flat"
                    if iv_prev == 0: iv_trend = "Flat"

                    # FIX 2 (apply): suppress px/iv trends on significant gap days
                    if gap_detected:
                        px_trend = "Flat"
                        iv_trend = "Flat"
                    
                    # Calculate OI Velocity from tick history
                    oi_velocity = "Flat"
                    strike = r_data["strike"]
                    if len(history) >= 3:
                        h_st = [h["strikes"].get(strike, {}).get(f"{prefix}_oi", opt_oi) for h in history[-3:]]
                        chg_t0 = h_st[2] - h_st[1]
                        chg_t1 = h_st[1] - h_st[0]
                        if chg_t0 > chg_t1 and chg_t0 > 0:
                            oi_velocity = "Accelerating"
                        elif chg_t0 < chg_t1 and chg_t0 > 0:
                            oi_velocity = "Decelerating"
                        elif chg_t1 > 0 and chg_t0 < 0:
                            oi_velocity = "Reversing"
                        elif chg_t0 < 0 and chg_t1 < 0:
                            oi_velocity = "Reducing"

                    # Calculate strike proximity dynamically based on dynamic strike step size
                    dist = abs(strike - ltp)
                    if dist <= 0.5 * strike_step:
                        loc_str = "At Wall Zone"
                    elif dist <= 2.5 * strike_step:
                        loc_str = "Testing"
                    else:
                        loc_str = "Approaching"

                    action = ""
                    h_action = ""

                    # ── Evaluate Checklist Gates & Scenario Matrix ──
                    if is_flat_futures:
                        action = "IGNORE"
                        h_action = "❌ IGNORE — No institutional flow"
                    elif loc_str == "At Wall Zone":
                        action = "IGNORE"
                        h_action = "❌ NO ENTRY — At Wall/Floor Zone"
                    else:
                        # High Conviction Institutional H-Action Logic
                        # 1. ★★★★★ CE BUY (Breakout)
                        if opt_type == 'CE' and oi_trend == "Reducing" and px_trend == "Rising" and futures_oi_change > 0 and ltp >= strike:
                            h_action = "★★★★★ CE BUY"
                        # 2. ★★★★★ PE BUY (Breakdown)
                        elif opt_type == 'PE' and oi_trend == "Reducing" and px_trend == "Rising" and futures_oi_change > 0 and ltp <= strike:
                            h_action = "★★★★★ PE BUY"
                        # 3. ★★★★★ CE BUY Preemptive
                        elif opt_type == 'CE' and oi_trend == "Reducing" and px_trend in ("Rising", "Flat") and loc_str == "Testing" and is_above and futures_oi_change > 0:
                            h_action = "★★★★★ CE BUY Preemptive"
                        # 4. ★★★★★ PE BUY Preemptive
                        elif opt_type == 'PE' and oi_trend == "Reducing" and px_trend in ("Rising", "Flat") and loc_str == "Testing" and is_below and futures_oi_change > 0:
                            h_action = "★★★★★ PE BUY Preemptive"
                        # 5. ★★★★ CE BUY
                        elif opt_type == 'CE' and oi_trend == "Increasing" and px_trend == "Rising" and futures_oi_change > 0 and loc_str == "Testing" and is_above:
                            h_action = "★★★★ CE BUY"
                        # 6. ★★★★ PE BUY
                        elif opt_type == 'PE' and oi_trend == "Increasing" and px_trend == "Rising" and futures_oi_change > 0 and loc_str == "Testing" and is_below:
                            h_action = "★★★★ PE BUY"
                        # 7. ⭐⭐⭐ PREPARE for CE BUY
                        elif opt_type == 'CE' and oi_trend == "Increasing" and px_trend == "Falling" and oi_velocity == "Decelerating" and loc_str == "Testing" and is_above:
                            h_action = "⭐⭐⭐ PREPARE for CE BUY"
                        # 8. ⭐⭐⭐ PREPARE for PE BUY
                        elif opt_type == 'PE' and oi_trend == "Increasing" and px_trend == "Falling" and oi_velocity == "Decelerating" and loc_str == "Testing" and is_below:
                            h_action = "⭐⭐⭐ PREPARE for PE BUY"
                        # 9. ⭐⭐⭐ WAIT — Floor/Wall forming
                        elif opt_type == 'CE' and oi_trend == "Increasing" and oi_velocity == "Accelerating" and px_trend == "Falling" and loc_str == "Approaching":
                            h_action = "⭐⭐⭐ WAIT — Floor/Wall forming"
                        elif opt_type == 'PE' and oi_trend == "Increasing" and oi_velocity == "Accelerating" and px_trend == "Falling" and loc_str == "Approaching":
                            h_action = "⭐⭐⭐ WAIT — Floor/Wall forming"

                        # ── Apply Advanced Checklist Step 3 & Step 4 Gates ──
                        if "BUY" in h_action:
                            # Step 3: ATM consolidation check
                            if is_atm_consolidation:
                                h_action = "⚠️ CONSOLIDATION — Strike OI Bias Neutral"
                            # Step 4: ATM LTP divergence check (Writers dominating)
                            elif opt_type == 'CE' and is_atm_ce_writers_dominating:
                                h_action = "⚠️ WAIT — Call Writers Dominating ATM"
                            elif opt_type == 'PE' and is_atm_pe_writers_dominating:
                                h_action = "⚠️ WAIT — Put Writers Dominating ATM"
                            else:
                                # Step 2 (Gap 3): Apply position sizing scaling based on basis expansion
                                size_tag = " (Full Size)"
                                if basis_trend == "Flat":
                                    size_tag = " (Half Size — Flat Basis)"
                                elif basis_trend == "Contracting":
                                    size_tag = " (Quarter Size — Contracting Basis)"
                                h_action += size_tag

                        # Retail Action Logic
                        if opt_type == 'CE':
                            if oi_trend == "Increasing" and px_trend == "Falling" and iv_trend == "Falling":
                                action = "WAIT (Building wall)" if is_above else "PE BUY"
                            elif oi_trend == "Increasing" and px_trend == "Rising" and iv_trend == "Rising":
                                action = "CE BUY"
                            elif oi_trend == "Reducing" and px_trend == "Rising" and iv_trend == "Rising":
                                action = "CE BUY" if is_above else "IGNORE"
                            elif oi_trend == "Flat" and px_trend == "Rising" and iv_trend == "Rising":
                                action = "WAIT"
                        elif opt_type == 'PE':
                            if oi_trend == "Increasing" and px_trend == "Falling" and iv_trend == "Falling":
                                action = "WAIT (Building floor)" if is_below else "CE BUY"
                            elif oi_trend == "Increasing" and px_trend == "Rising" and iv_trend == "Rising":
                                action = "PE BUY"
                            elif oi_trend == "Reducing" and px_trend == "Rising" and iv_trend == "Rising":
                                action = "PE BUY" if is_below else "IGNORE"
                            elif oi_trend == "Flat" and px_trend == "Rising" and iv_trend == "Rising":
                                action = "WAIT"

                    # Resolve Conviction Precedence
                    conviction_score = 0
                    if "★★★★★" in h_action: conviction_score = 5
                    elif "★★★★" in h_action: conviction_score = 4
                    elif "⭐⭐⭐" in h_action: conviction_score = 3
                    elif "WAIT — " in h_action or "CONSOLIDATION" in h_action: conviction_score = 2
                    elif "NO ENTRY" in h_action: conviction_score = 2
                    elif "IGNORE" in h_action: conviction_score = 1

                    if action and "IGNORE" not in action and not final_retail_action:
                        final_retail_action = action
                        signal_source = {"strike": strike, "oi": opt_oi, "side": opt_type}

                    # Store per-strike action for heatmap column
                    _strike_actions.setdefault(strike, {})[opt_type] = action if action and "IGNORE" not in action else ""

                    if h_action and conviction_score > highest_conviction:
                        highest_conviction = conviction_score
                        final_h_action = h_action
                        h_action_source = {"strike": strike, "oi": opt_oi, "side": opt_type}
        except Exception as e:
            errors.append(f"Action calc err: {e}")

    # BUG 2 FIX: Resolve wall buildup from the full chain, not just the top-5 list.
    def _wall_buildup(row, side):
        if not row:
            return "\u2013"
        oi_chg = row.get(f"{side}_oi_chg", 0)
        _ltp   = row.get(f"{side}_ltp",      0) or 0
        _prev  = row.get(f"{side}_prev_ltp", _ltp) or _ltp
        price_up = _ltp > (_prev * 1.005) if _prev > 0 else False
        if   oi_chg > 0 and price_up:  return "Long Buildup"
        elif oi_chg > 0:               return "Short Buildup"
        elif oi_chg < 0 and price_up:  return "Short Covering"
        elif oi_chg < 0:               return "Long Unwinding"
        return "\u2013"

    top_ce_list = strikes.get("top_ce", [])
    top_pe_list = strikes.get("top_pe", [])
    ce_wall_strike = strikes.get("ce_wall")   # highest OI CE at/above LTP (actual resistance wall)
    pe_wall_strike = strikes.get("pe_wall")   # highest OI PE at/below LTP (actual support wall)

    ce_wall_row     = next((r for r in chain if r["strike"] == ce_wall_strike), None)
    pe_wall_row     = next((r for r in chain if r["strike"] == pe_wall_strike), None)
    ce_wall_buildup = _wall_buildup(ce_wall_row, "ce")
    pe_wall_buildup = _wall_buildup(pe_wall_row, "pe")

    # Enrich chain_data with per-strike actions captured from retail action loop
    for row in chain_data:
        s = row["strike"]
        row["ce_action"] = _strike_actions.get(s, {}).get("CE", "")
        row["pe_action"] = _strike_actions.get(s, {}).get("PE", "")

    # ── F&O Synergy Profile (7-Profile Master Combination Matrix) ──
    synergy_profile = "Mixed Flow (No Setup)"
    synergy_action  = "WAIT"

    f_b = futures_buildup
    ce_b = ce_wall_buildup
    pe_b = pe_wall_buildup
    
    if f_b == "Long Buildup" and ce_b == "Short Covering" and pe_b == "Short Buildup":
        synergy_profile = "🟢 Bull-Lock (Full Bullish Harmony)"
        synergy_action = "CALL BUY (At Next Strike)"
    elif f_b == "Short Buildup" and ce_b == "Short Buildup" and pe_b == "Short Covering":
        synergy_profile = "🔴 Bear-Lock (Full Bearish Harmony)"
        synergy_action = "PUT BUY (At Next Strike)"
    elif f_b == "Short Covering" and ce_b == "Short Covering" and pe_b == "Short Buildup":
        synergy_profile = "⚡ V-Squeeze (Fast Intraday Rally)"
        synergy_action = "FAST CALL BUY (Scalp Only)"
    elif f_b == "Long Unwinding" and ce_b == "Short Buildup" and pe_b == "Short Covering":
        synergy_profile = "💀 Floor Collapse (Panic Flush)"
        synergy_action = "PUT BUY / EXIT LONGS IMMEDIATELY"
    elif f_b == "Long Buildup" and ce_b == "Short Buildup" and pe_b == "Long Unwinding":
        synergy_profile = "🟡 Institutional Trap (Ceiling Exhaustion)"
        synergy_action = "BOOK PROFIT ON CALLS / HOLD CASH"
    elif f_b == "Short Buildup" and ce_b == "Long Unwinding" and pe_b == "Short Buildup":
        synergy_profile = "🪤 Bear Trap (Floor Holding)"
        # Issue 11 FIX: Bear Trap = floor holding → bullish reversal expected.
        # "SELL PUTS" was wrong (contradicts the trap definition — floor is holding).
        synergy_action = "CE BUY / Avoid Selling Puts"
    elif (f_b in ("Flat", "–") or is_flat_futures) and ce_b == "Short Buildup" and pe_b == "Short Buildup":
        synergy_profile = "🔁 Range Lock (Institutional Pinning)"
        synergy_action = "SELL STRADDLE or STRANGLE"

    if pivot_source == "today_ohlc":
        errors.append("⚠ Pivot: using today's OHLC (prev-day data unavailable)")

    # Pattern engine removed — pattern_type/pattern_signal no longer served
    pattern_data = {}

    # Lazy-trigger expired contract housekeeping
    global _housekeeping_done
    if not _housekeeping_done:
        with _housekeeping_lock:
            if not _housekeeping_done:
                try:
                    from oi_transition_engine import run_housekeeping
                    threading.Thread(target=run_housekeeping, daemon=True).start()
                except Exception as ex:
                    errors.append(f"Housekeeping init error: {ex}")
                _housekeeping_done = True

    # Calculate State Transition Conviction
    transition_data = None
    if chain and expiry and ltp:
        try:
            from oi_transition_engine import process_symbol_transitions
            # Suppress/dampen during major volatility events if needed
            event_dates = [] 
            transition_data = process_symbol_transitions(sym, chain, expiry, ltp, iv_event_windows=event_dates)
        except Exception as ex:
            errors.append(f"Transition Conviction engine error: {ex}")

    # Calculate Straddle / Dual-Side Addition Analysis
    dual_side_analysis = None
    if chain and ltp and atm_strike:
        # Find ATM strike row
        atm_row = next((r for r in chain if r["strike"] == atm_strike), None)
        if atm_row:
            ce_oi_chg_pct = atm_row.get("ce_oi_eod_chg_pct", 0.0)
            pe_oi_chg_pct = atm_row.get("pe_oi_eod_chg_pct", 0.0)
            
            ce_ltp = atm_row.get("ce_ltp", 0.0)
            ce_prev = atm_row.get("ce_prev_ltp", ce_ltp)
            ce_prem_chg = ((ce_ltp - ce_prev) / ce_prev * 100) if ce_prev > 0 else 0.0
            
            pe_ltp = atm_row.get("pe_ltp", 0.0)
            pe_prev = atm_row.get("pe_prev_ltp", pe_ltp)
            pe_prem_chg = ((pe_ltp - pe_prev) / pe_prev * 100) if pe_prev > 0 else 0.0
            
            # Determine thresholds based on asset class
            # Issue 12 FIX: Widen index spot_threshold on expiry day.
            # Expiry opening gaps/auction volatility routinely exceed 0.20% and would
            # force TRENDING state all morning — suppressing the RANGE_PINNING /
            # VOL_COILING signals that are most actionable precisely on expiry day.
            # Uses the actual nearest-expiry date (already in scope) so each symbol
            # auto-detects its own correct expiry day (Tue/Wed/Thu/Mon as applicable).
            sym_upper = sym.upper()
            try:
                _today_str = str(datetime.date.today())
                is_expiry_day = bool(expiry and str(expiry)[:10] == _today_str)
            except Exception:
                is_expiry_day = False

            if sym_upper in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX", "NIFTYIT", "NIFTYNXT50"):
                spot_threshold = 0.35 if is_expiry_day else 0.20   # wider on expiry day to avoid false TRENDING
                oi_add_threshold = 3.0  # 3.0% daily OI increase
            elif sym_upper in ("RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "BHARTIARTL", "ITC", "SBIN", "LT", "AXISBANK", "MARUTI", "HINDUNILVR", "KOTAKBANK", "TATASTEEL", "M&M", "TRENT"):
                spot_threshold = 0.30   # 0.30% daily spot change
                oi_add_threshold = 5.0  # 5.0% daily OI increase
            else:
                spot_threshold = 0.40   # 0.40% daily spot change
                oi_add_threshold = 5.0  # 5.0% daily OI increase

            dual_side_analysis = classify_dual_side_addition(
                ce_oi_chg=ce_oi_chg_pct,
                pe_oi_chg=pe_oi_chg_pct,
                ce_prem_chg=ce_prem_chg,
                pe_prem_chg=pe_prem_chg,
                spot_pct_chg=price_change_pct,
                spot_threshold=spot_threshold,
                oi_add_threshold=oi_add_threshold
            )
            
    # Default to "Not live" if it couldn't be derived (e.g. out of market hours or chain is empty)
    if not dual_side_analysis:
        dual_side_analysis = {
            "state": "Not live",
            "signal": "Not live",
            "bias": "Not live"
        }

    now_iso = datetime.datetime.now().isoformat()
    mkt_open = is_market_hours()

    return jsonify({
        "symbol":             sym,
        "ltp":                ltp,
        "price_change_pct":   price_change_pct,  # accurate % from Kite net_change/prev_close
        "expiry":             expiry,
        "pivots":             pivots,
        "pivot_source":       pivot_source,
        "max_pain":           max_pain,
        "pcr":                pcr,
        "strikes":            strikes,
        "chain_data":         chain_data,
        "straddle":           straddle,
        "atm":                atm_strike,
        "dual_side_analysis": dual_side_analysis,
        "retail_action":    final_retail_action or "WAIT (No clear setup)",
        "h_action":         final_h_action,
        "signal_source":    signal_source,    # {strike, oi, side} — which strike fired retail action
        "h_action_source":  h_action_source,  # {strike, oi, side} — which strike fired h_action
        "synergy_profile":  synergy_profile,
        "synergy_action":   synergy_action,
        "gap_detected":     gap_detected,
        "is_index":         sym in INDEX_SYMBOLS,
        "errors":           errors,
        "market_open":      mkt_open,
        "cache_source":     "live" if mkt_open else "eod_cache",
        "data_as_of":       now_iso,
        "timestamp":        now_iso,
        "transition_conviction": transition_data,
        
        # LAYER 1 INTEGRATION: Live Futures Metadata
        "futures_data": {
            "ltp": futures_ltp,
            "prev_close": futures_prev_close,
            "price_change_pct": futures_price_chg_pct,
            "oi": futures_oi,
            "oi_prev": futures_oi_prev,
            "oi_change_pct": futures_oi_chg_pct,
            "buildup": futures_buildup
        }
    })


# ── F&O Synergy Scan REST Endpoint ────────────────────────────────────────────
@oi_spurt_bp.route("/synergy-scan")
def synergy_scan():
    """
    Returns current in-memory synergy scan state from the WebSocket-driven scanner.
    Used for initial page load and as a fallback when Socket.IO is unavailable.

    Query params:
      ?alerts_only=true  — return only BUY-class profiles (default: false)
    """
    try:
        from server import lazy_start_synergy_scanner
        lazy_start_synergy_scanner()
    except Exception as e:
        logging.error(f"Failed to lazy start Synergy Scanner: {e}")

    try:
        from synergy_scanner import get_synergy_results, get_buy_alerts, get_cpr_cross_alerts
        alerts_only = request.args.get("alerts_only", "false").lower() == "true"
        data = get_buy_alerts() if alerts_only else get_synergy_results()

        # Sort: BUY signals first, then by symbol alphabetically
        sorted_rows = sorted(
            data.values(),
            key=lambda x: (0 if x.get("is_buy_signal") else 1, x.get("symbol", ""))
        )

        return jsonify({
            "ok":          True,
            "count":       len(sorted_rows),
            "alerts":      len([r for r in sorted_rows if r.get("is_buy_signal")]),
            "results":     sorted_rows,
            "cpr_alerts":  get_cpr_cross_alerts(),
            "timestamp":   datetime.datetime.now().isoformat(),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "results": []}), 500


# ── Gemini AI OI Heatmap Analysis ──────────────────────────────────────────────
import os
import logging

@oi_spurt_bp.route("/symbol/<symbol>/ai-analyze", methods=["POST"])
def ai_analyze_heatmap(symbol):
    """
    Analyzes the OI Chain Heatmap for a given symbol using Gemini AI.

    Request body (JSON):
        chain_data  : list of strike rows (same shape as api_symbol response)
        pcr         : float  — overall put-call ratio
        spot        : float  — current spot price (LTP)
        max_pain    : float  — computed max pain strike
        straddle    : float  — ATM straddle premium
        expiry      : str    — nearest expiry date string
        atm         : float  — ATM strike

    Returns:
        { ok: true, analysis: "<markdown string>" }
    """
    import json as _json

    # ── Read API key from environment ──
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key or api_key == "your_gemini_api_key_here":
        return jsonify({
            "ok": False,
            "error": "GEMINI_API_KEY not set in .env — please add your key from https://aistudio.google.com/apikey"
        }), 400

    # ── Parse request ──
    body       = request.get_json(force=True) or {}
    chain_data = body.get("chain_data", [])
    pcr        = body.get("pcr")
    spot       = body.get("spot")
    max_pain   = body.get("max_pain")
    straddle   = body.get("straddle")
    expiry     = body.get("expiry", "–")
    atm        = body.get("atm")

    if not chain_data:
        return jsonify({"ok": False, "error": "chain_data is empty. Load the heatmap first."}), 400

    # ── Build prompt table ──
    # Sort descending by strike (same as heatmap display)
    sorted_chain = sorted(chain_data, key=lambda r: r.get("strike", 0), reverse=True)

    header = "| Strike | CE OI | CE ΔOI | CE Buildup | CE LTP | PE LTP | PE ΔOI | PE Buildup | PE OI | PCR |"
    divider = "|--------|-------|--------|------------|--------|--------|--------|------------|-------|-----|"
    rows = []
    for r in sorted_chain:
        strike   = r.get("strike", 0)
        atm_flag = " ◄ATM" if strike == atm else ""
        mp_flag  = " ◄MAXPAIN" if strike == max_pain else ""
        label    = f"{int(strike)}{atm_flag}{mp_flag}"
        ce_oi    = f"{r.get('ce_oi', 0):,}"
        ce_doi   = f"{r.get('ce_oi_chg', 0):+,}"
        ce_build = r.get("ce_buildup", "–")
        ce_ltp   = f"₹{r.get('ce_ltp', 0):.1f}"
        pe_ltp   = f"₹{r.get('pe_ltp', 0):.1f}"
        pe_doi   = f"{r.get('pe_oi_chg', 0):+,}"
        pe_build = r.get("pe_buildup", "–")
        pe_oi    = f"{r.get('pe_oi', 0):,}"
        strike_pcr = r.get("strike_pcr")
        pcr_str  = f"{strike_pcr:.2f}" if strike_pcr is not None else "–"
        rows.append(f"| {label} | {ce_oi} | {ce_doi} | {ce_build} | {ce_ltp} | {pe_ltp} | {pe_doi} | {pe_build} | {pe_oi} | {pcr_str} |")

    table = "\n".join([header, divider] + rows)

    system_prompt = (
        "You are an expert Indian derivatives market analyst specializing in NSE F&O options chain analysis. "
        "Provide concise, actionable insights. Use markdown formatting with clear section headers. "
        "Be specific about strike levels. Keep total response under 400 words."
    )

    user_prompt = f"""Analyze the following NSE F&O Options Chain Heatmap for **{symbol.upper()}** and provide a structured analysis:

**Key Metrics:**
- Spot Price: ₹{spot if spot else '–'}
- Overall PCR: {f"{pcr:.3f}" if pcr else '–'}
- Max Pain: ₹{int(max_pain) if max_pain else '–'}
- ATM Strike: ₹{int(atm) if atm else '–'}
- ATM Straddle Premium: ₹{f"{straddle:.1f}" if straddle else '–'}
- Expiry: {expiry}

**Options Chain Heatmap:**
{table}

**Provide analysis in exactly this structure:**

## 🎯 Overall Bias
[Bullish / Bearish / Neutral] — one sentence reasoning based on PCR and OI distribution.

## 📊 Key OI Observations
- [2–3 bullet points: where major CE/PE writing is concentrated, max OI strikes]

## 🛡️ Support & Resistance
- **Resistance:** [Strike] — [reason]
- **Support:** [Strike] — [reason]

## ⚠️ Max Pain vs Spot
[Implication: Is spot above/below max pain? What does it mean for expiry?]

## 🚨 Squeeze Risk Zones
[Any strikes with heavy short OI at risk of squeeze — or "None identified"]

## ✅ Session Bias
[1 actionable line: what a trader should lean towards this session]"""

    # ── Call Gemini ──
    try:
        from google import genai as gai
        from google.genai import types as gai_types

        client = gai.Client(api_key=api_key)
        
        # Try gemini-3.5-flash first (success on free quota), fallback to 3.1-flash-lite
        models_to_try = ["gemini-3.5-flash", "gemini-3.1-flash-lite"]
        resp = None
        last_err = None

        for model in models_to_try:
            try:
                logging.info(f"[AI-HEATMAP] Attempting Gemini call with model: {model}")
                resp = client.models.generate_content(
                    model=model,
                    contents=user_prompt,
                    config=gai_types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        temperature=0.2,
                        max_output_tokens=1024,
                    ),
                )
                if resp and resp.text:
                    logging.info(f"[AI-HEATMAP] Success using {model}. Response length: {len(resp.text)} chars")
                    return jsonify({"ok": True, "symbol": symbol.upper(), "analysis": resp.text.strip()})
            except Exception as e:
                logging.warning(f"[AI-HEATMAP] Model {model} failed: {str(e)}")
                last_err = e
                continue

        # If all models fail
        raise last_err if last_err else Exception("All models failed to respond")

    except ImportError:
        return jsonify({"ok": False, "error": "google-genai not installed. Run: pip install google-genai"}), 500
    except Exception as e:
        logging.error(f"[AI-HEATMAP] Gemini error: {str(e)}")
        return jsonify({"ok": False, "error": f"Gemini API error: {str(e)}"}), 500


