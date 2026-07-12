import threading
import time
import datetime
import logging
from collections import deque

# Global memory for 15-minute patterns
global_15m_patterns = {}
_pattern_lock = threading.Lock()
_pattern_thread = None

# ── Active-client heartbeat ───────────────────────────────────────────────────
# Incremented by any route that serves pattern data (or /api/heartbeat).
# The loop skips heavy Kite API calls when no page has polled for >90 s.
_pattern_last_client_time = 0.0
_pattern_client_lock = threading.Lock()

def notify_pattern_client():
    """Call from any API route that indicates a browser page is open."""
    global _pattern_last_client_time
    with _pattern_client_lock:
        _pattern_last_client_time = time.time()

def _has_pattern_clients(timeout_sec=90):
    """Returns True if a page has polled within the last timeout_sec seconds."""
    with _pattern_client_lock:
        return (time.time() - _pattern_last_client_time) < timeout_sec

def _get_kite():
    from server import get_kite
    return get_kite()

def analyze_15m_structure(symbol, hist_data):
    """
    Analyzes 15m OHLC data to identify structural Continuation/Reversal patterns.
    Uses basic geometric swing detection.
    """
    if not hist_data or len(hist_data) < 15:
        return {"pattern_type": "Insufficient Data", "pattern_signal": "—"}
        
    closes = [d['close'] for d in hist_data]
    highs = [d['high'] for d in hist_data]
    lows = [d['low'] for d in hist_data]
    
    c = closes[-1]
    
    # 1. Very basic Double Bottom (Reversal) Check
    recent_lows = sorted(lows[-15:])
    if len(recent_lows) >= 2 and recent_lows[1] > 0:
        low_diff = abs(recent_lows[0] - recent_lows[1]) / recent_lows[1]
        if low_diff < 0.003: # Lows are within 0.3%
            if c > recent_lows[1] * 1.01: # Bouncing off the double bottom
                return {"pattern_type": "Reversal", "pattern_signal": "Bullish Double Bottom"}

    # 2. Very basic Double Top (Reversal) Check
    recent_highs = sorted(highs[-15:], reverse=True)
    if len(recent_highs) >= 2 and recent_highs[1] > 0:
        high_diff = abs(recent_highs[0] - recent_highs[1]) / recent_highs[1]
        if high_diff < 0.003: # Highs are within 0.3%
            if c < recent_highs[1] * 0.99: # Rejecting from the double top
                return {"pattern_type": "Reversal", "pattern_signal": "Bearish Double Top"}
                
    # 3. Basic Bull Flag (Continuation) Check
    # A strong thrust followed by slight downward consolidation
    thrust = closes[-10] < closes[-5] # Was rising
    pullback = closes[-5] > closes[-1] # Now pulling back slightly
    if thrust and pullback and c > lows[-5]:
        return {"pattern_type": "Continuation", "pattern_signal": "Bull Flag (Forming)"}

    # Default
    return {"pattern_type": "Consolidation", "pattern_signal": "No structural setup"}


def _pattern_loop():
    logging.info("[Pattern Engine] Background thread started. Running every 15m.")
    while True:
        try:
            # ── Pause when no webpage is open ─────────────────────────────────
            if not _has_pattern_clients():
                time.sleep(10)
                continue
            # ──────────────────────────────────────────────────────────────────
            now = datetime.datetime.now()
            # Run exactly at 00, 15, 30, 45 minutes of the hour
            if now.minute % 15 == 0:
                kite = _get_kite()
                if kite:
                    logging.info(f"[Pattern Engine] Waking up for 15m structural scan at {now.strftime('%H:%M')}")
                    
                    # 1. Dynamically get all FNO underlyings
                    from db_instruments import get_fno_symbols
                    fno_symbols = set(get_fno_symbols())
                    
                    from oi_spurt_routes import EXCHANGE_MAP
                    query_symbols = []
                    sym_map = {} # Maps kite exchange_symbol back to our base symbol
                    
                    for sym in fno_symbols:
                        exch_sym = EXCHANGE_MAP.get(sym, f"NSE:{sym}")
                        query_symbols.append(exch_sym)
                        sym_map[exch_sym] = sym
                        
                    # 2. Batch fetch quotes to get instrument tokens (1 API call instead of 200)
                    tokens_to_fetch = {}
                    try:
                        # kite.quote supports up to 500 symbols per batch
                        quotes = kite.quote(query_symbols)
                        for exch_sym, data in quotes.items():
                            token = data.get("instrument_token")
                            if token:
                                tokens_to_fetch[sym_map[exch_sym]] = token
                    except Exception as e:
                        logging.error(f"[Pattern Engine] Failed to fetch quotes: {e}")
                    
                    temp_patterns = {}
                    from_dt = now - datetime.timedelta(days=4)
                    
                    # 3. Fetch historical data sequentially (safe rate limit)
                    for sym, token in tokens_to_fetch.items():
                        try:
                            hist = kite.historical_data(token, from_dt, now, "15minute")
                            if hist:
                                pattern_result = analyze_15m_structure(sym, hist)
                                temp_patterns[sym] = pattern_result
                        except Exception as e:
                            logging.error(f"[Pattern Engine] Failed history for {sym}: {e}")
                        
                        # Sleep to respect Kite's 3 requests/sec rate limit
                        time.sleep(0.4)
                        
                    # Safely update global dictionary
                    with _pattern_lock:
                        global_15m_patterns.update(temp_patterns)
                        
                    logging.info(f"[Pattern Engine] Scan complete. Cached {len(temp_patterns)} patterns.")
                
                # Sleep 60s so we don't re-trigger in the same minute
                time.sleep(60)
            
            # Check time every 10 seconds
            time.sleep(10)
            
        except Exception as e:
            logging.error(f"[Pattern Engine] Fatal thread error: {e}")
            time.sleep(10)

def start_pattern_engine():
    global _pattern_thread
    if _pattern_thread is None:
        _pattern_thread = threading.Thread(target=_pattern_loop, daemon=True)
        _pattern_thread.start()
