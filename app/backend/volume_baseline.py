"""
================================================================================
📊 Shared Multi-Timeframe Volume Baseline Module
================================================================================
Provides slot-by-slot normal volume baselines across multiple timeframes (5m, 15m, 30m, 60m).
Computes baselines from the last 20 clean trading days using outlier filtering.
"""

import os
import sys
import json
import time
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, date as dt_date
from statistics import mean

# Add parent path to allow imports if needed
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from session_utils import now_ist, NSE_HOLIDAYS

log = logging.getLogger(__name__)

# DB path
_DB_PATH = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tradesignal_cache.db"))

# Slot generators
def _gen_slots(start_h, start_m, end_h, end_m, step):
    slots = []
    curr = datetime(2000, 1, 1, start_h, start_m)
    end = datetime(2000, 1, 1, end_h, end_m)
    while curr <= end:
        slots.append(curr.strftime("%H:%M"))
        curr += timedelta(minutes=step)
    return slots

_SLOTS = {
    "5m":  _gen_slots(9, 15, 15, 25, 5),   # 75 slots
    "15m": _gen_slots(9, 15, 15, 15, 15),  # 25 slots
    "30m": _gen_slots(9, 15, 14, 45, 30),  # 12 slots (15:15 is partial -> excluded)
    "60m": _gen_slots(9, 15, 14, 15, 60),  # 6 slots  (15:15 is partial -> excluded)
}
_SLOTS_SET = {tf: frozenset(lst) for tf, lst in _SLOTS.items()}

# Cache State
_allday_baseline_cache = {}  # {symbol: {timeframe: {slot: baseline_val}}}
_allday_baseline_date = None  # active_date_str
_allday_baseline_lock = threading.RLock()
_allday_baseline_warming = False

def _init_db():
    """Create the allday_baselines table if it doesn't exist."""
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS allday_baselines (
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                baselines TEXT NOT NULL,
                active_date TEXT NOT NULL,
                PRIMARY KEY (symbol, timeframe)
            )
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"[Volume Baseline] DB init error: {e}")

def _get_active_trading_date(now):
    target = now.date()
    # Before 09:15 AM today, the active session is the previous trading day
    if now.hour < 9 or (now.hour == 9 and now.minute < 15):
        target = target - timedelta(days=1)
    while target.weekday() >= 5 or target in NSE_HOLIDAYS:
        target = target - timedelta(days=1)
    return target.strftime('%Y-%m-%d')

def _load_from_db():
    """Load cached baselines from SQLite. Only use if active_date matches today."""
    global _allday_baseline_cache, _allday_baseline_date
    try:
        today_str = _get_active_trading_date(now_ist())
        conn = sqlite3.connect(_DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT active_date FROM allday_baselines LIMIT 1")
        row = cursor.fetchone()
        if row and row[0] == today_str:
            cursor.execute("SELECT symbol, timeframe, baselines FROM allday_baselines WHERE active_date = ?", (today_str,))
            rows = cursor.fetchall()
            cache = {}
            for sym, tf, bl_json in rows:
                if sym not in cache:
                    cache[sym] = {}
                try:
                    cache[sym][tf] = json.loads(bl_json)
                except Exception:
                    pass
            with _allday_baseline_lock:
                _allday_baseline_cache = cache
                _allday_baseline_date = today_str
            log.info(f"[Volume Baseline] Loaded {len(cache)} baselines from SQLite (date={today_str}).")
        else:
            log.info(f"[Volume Baseline] SQLite baselines stale or empty (db_date={row[0] if row else 'none'}, need={today_str}).")
        conn.close()
    except Exception as e:
        log.warning(f"[Volume Baseline] DB load error: {e}")

def _save_single_to_db(symbol, timeframe, slots_dict, active_date_str):
    """Save a single computed baseline row to SQLite."""
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.execute(
            "INSERT OR REPLACE INTO allday_baselines (symbol, timeframe, baselines, active_date) VALUES (?, ?, ?, ?)",
            (symbol, timeframe, json.dumps(slots_dict), active_date_str)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"[Volume Baseline] DB save error for {symbol} ({timeframe}): {e}")

def _is_other_warm_running():
    """Check if any other heavy volume warms are running in other modules to avoid rate limit conflicts."""
    try:
        import sys
        if 'option_gainers_scanner' in sys.modules:
            from option_gainers_scanner import _avg_volume_warming
            if _avg_volume_warming:
                return True
    except Exception:
        pass
    return False

def _aggregate_to_tf(by_date_5m, target_interval_min, full_slots_set):
    """
    Aggregate per-date 5-min slot volumes into higher timeframes.
    by_date_5m: {date: {"09:15": vol, "09:20": vol, ...}}
    Returns: {date: {tf_slot_str: aggregated_vol}}
    """
    result = {}
    for d_val, slots in by_date_5m.items():
        tf_slots = {}
        for tf_slot in full_slots_set:
            try:
                tf_start = datetime.strptime(tf_slot, "%H:%M")
                vol_sum = 0
                for n in range(0, target_interval_min, 5):
                    sub_slot = (tf_start + timedelta(minutes=n)).strftime("%H:%M")
                    vol_sum += slots.get(sub_slot, 0)
                tf_slots[tf_slot] = vol_sum
            except Exception:
                pass
        result[d_val] = tf_slots
    return result

def _compute_filtered_baseline(volumes_list, lookback_days=20):
    """
    Compute mean of volumes list excluding outlier spikes (> 2 * raw average).
    Falls back to raw list if filtering leaves too few days.
    """
    if not volumes_list:
        return 0.0
    raw_avg = mean(volumes_list)
    filtered = [v for v in volumes_list if v <= 2 * raw_avg]
    if len(filtered) < len(volumes_list) * 0.6:
        filtered = volumes_list  # skip filtering if not enough data remains
    return round(mean(filtered), 0)

def _warm_baselines_bg(kite, token_map):
    """Background thread worker to compile baselines across all 4 timeframes."""
    global _allday_baseline_warming
    import random
    
    try:
        today = dt_date.today()
        active_date_str = _get_active_trading_date(now_ist())
        from_dt = today - timedelta(days=35)  # ~25 trading days
        to_dt = today - timedelta(days=1)
        
        # 1. Prune old records from database
        try:
            conn = sqlite3.connect(_DB_PATH)
            conn.execute("DELETE FROM allday_baselines WHERE active_date != ?", (active_date_str,))
            conn.commit()
            conn.close()
        except Exception as e:
            log.warning(f"[Volume Baseline] DB prune error: {e}")
            
        # 2. Get Expiry Dates
        expiry_dates = set()
        try:
            from db_instruments import get_cached_instruments
            nfo_instruments = get_cached_instruments("NFO")
            for inst in nfo_instruments:
                exp = inst.get("expiry")
                if exp and isinstance(exp, dt_date):
                    if from_dt <= exp <= to_dt:
                        expiry_dates.add(exp)
                elif exp and isinstance(exp, str):
                    try:
                        exp_d = datetime.strptime(exp[:10], "%Y-%m-%d").date()
                        if from_dt <= exp_d <= to_dt:
                            expiry_dates.add(exp_d)
                    except Exception:
                        pass
        except Exception as e:
            log.warning(f"[Volume Baseline] Expiry detection failed: {e}")
            
        # 3. Get India VIX Shocks
        vix_shock_dates = set()
        try:
            vix_token = None
            from db_instruments import get_cached_instruments
            nse_instruments = get_cached_instruments("NSE")
            for inst in nse_instruments:
                if inst.get("tradingsymbol") == "INDIA VIX":
                    vix_token = inst.get("instrument_token")
                    break
            if vix_token:
                time.sleep(0.6)
                vix_hist = kite.historical_data(int(vix_token), from_dt, to_dt, "day")
                for i in range(1, len(vix_hist)):
                    prev_close = vix_hist[i-1].get("close", 0)
                    curr_close = vix_hist[i].get("close", 0)
                    if prev_close > 0:
                        vix_change_pct = abs((curr_close - prev_close) / prev_close * 100)
                        if vix_change_pct >= 10.0:
                            candle_date = vix_hist[i].get("date")
                            if hasattr(candle_date, 'date'):
                                candle_date = candle_date.date()
                            elif isinstance(candle_date, str):
                                candle_date = datetime.strptime(candle_date[:10], "%Y-%m-%d").date()
                            vix_shock_dates.add(candle_date)
        except Exception as e:
            log.warning(f"[Volume Baseline] VIX fetch failed: {e}")
            
        # 4. Fetch loop per symbol
        pending_symbols = list(token_map.keys())
        processed = 0
        total = len(token_map)
        
        while pending_symbols:
            sym = pending_symbols.pop(0)
            token = token_map[sym]
            try:
                time.sleep(0.6)  # Rate limit safety
                hist_5m = kite.historical_data(int(token), from_dt, to_dt, "5minute")
                if not hist_5m:
                    continue
                    
                time.sleep(0.6)
                hist_daily = kite.historical_data(int(token), from_dt, to_dt, "day")
                
                # Gap open exclusion
                gap_dates = set()
                if hist_daily and len(hist_daily) >= 2:
                    for i in range(1, len(hist_daily)):
                        prev_close = hist_daily[i-1].get("close", 0)
                        day_open = hist_daily[i].get("open", 0)
                        if prev_close > 0 and day_open > 0:
                            gap_pct = abs((day_open - prev_close) / prev_close * 100)
                            if gap_pct >= 1.5:
                                candle_date = hist_daily[i].get("date")
                                if hasattr(candle_date, 'date'):
                                    candle_date = candle_date.date()
                                elif isinstance(candle_date, str):
                                    candle_date = datetime.strptime(candle_date[:10], "%Y-%m-%d").date()
                                gap_dates.add(candle_date)
                                
                # Parse 5-min candles
                by_date_5m = {}
                for c in hist_5m:
                    dt_val = c.get("date")
                    if hasattr(dt_val, 'date'):
                        c_date = dt_val.date()
                        c_time = dt_val.strftime("%H:%M")
                    elif isinstance(dt_val, str):
                        c_date = datetime.strptime(dt_val[:10], "%Y-%m-%d").date()
                        c_time = dt_val[11:16]
                    else:
                        continue
                        
                    if c_time not in _SLOTS_SET["5m"]:
                        continue
                    if c_date not in by_date_5m:
                        by_date_5m[c_date] = {}
                    by_date_5m[c_date][c_time] = c.get("volume", 0) or 0
                    
                all_excluded = expiry_dates | vix_shock_dates | gap_dates | set(NSE_HOLIDAYS)
                clean_dates = sorted(
                    [d for d in by_date_5m.keys()
                     if d not in all_excluded
                     and d.weekday() < 5
                     and d != today],
                    reverse=True
                )
                
                # Keep top 20 clean trading days
                clean_dates = clean_dates[:20]
                if len(clean_dates) < 3:
                    continue  # not enough historical clean data
                    
                # Aggregate to higher timeframes in Python
                tf_data = {
                    "5m":  by_date_5m,
                    "15m": _aggregate_to_tf(by_date_5m, 15, _SLOTS_SET["15m"]),
                    "30m": _aggregate_to_tf(by_date_5m, 30, _SLOTS_SET["30m"]),
                    "60m": _aggregate_to_tf(by_date_5m, 60, _SLOTS_SET["60m"]),
                }
                
                # Compute baselines for each timeframe
                symbol_baselines = {}
                for tf in ["5m", "15m", "30m", "60m"]:
                    tf_slots = _SLOTS[tf]
                    tf_dates_dict = tf_data[tf]
                    
                    slot_baselines = {}
                    for slot in tf_slots:
                        vols = [tf_dates_dict[d].get(slot, 0) for d in clean_dates if slot in tf_dates_dict.get(d, {})]
                        if vols:
                            slot_baselines[slot] = _compute_filtered_baseline(vols, len(clean_dates))
                        else:
                            slot_baselines[slot] = 0.0
                            
                    symbol_baselines[tf] = slot_baselines
                    _save_single_to_db(sym, tf, slot_baselines, active_date_str)
                    
                # Update memory cache
                with _allday_baseline_lock:
                    if sym not in _allday_baseline_cache:
                        _allday_baseline_cache[sym] = {}
                    for tf in symbol_baselines:
                        _allday_baseline_cache[sym][tf] = symbol_baselines[tf]
                    global _allday_baseline_date
                    _allday_baseline_date = active_date_str
                    
                processed += 1
                
            except Exception as e:
                is_rate_limit = "429" in str(e) or "too many" in str(e).lower()
                if is_rate_limit:
                    backoff = 5.0 + random.uniform(0.5, 2.0)
                    log.warning(f"[Volume Baseline] Rate limited at {sym}, backoff {backoff:.1f}s...")
                    pending_symbols.append(sym)
                    time.sleep(backoff)
                else:
                    log.warning(f"[Volume Baseline] Error for {sym}: {e}")
                    
        log.info(f"[Volume Baseline] Baseline warm completed for {processed}/{total} symbols.")
        
        # Trigger scanner rescan if possible
        try:
            import ema_crossover_scanner
            ema_crossover_scanner._fh_rescan_needed = True
        except ImportError:
            pass
            
    except Exception as e:
        log.error(f"[Volume Baseline] Failed baseline warm: {e}")
    finally:
        _allday_baseline_warming = False

# Public API

def ensure_baselines_warm(kite, token_map):
    """Triggers baseline warming if stale or not populated."""
    global _allday_baseline_warming
    if not token_map:
        return
        
    active_date = _get_active_trading_date(now_ist())
    
    # Load from SQLite if cache is empty
    with _allday_baseline_lock:
        if not _allday_baseline_cache or _allday_baseline_date != active_date:
            _init_db()
            _load_from_db()
            
    # Check if anything is missing
    with _allday_baseline_lock:
        if _allday_baseline_date == active_date:
            missing_syms = {sym: tok for sym, tok in token_map.items() if sym not in _allday_baseline_cache}
        else:
            missing_syms = dict(token_map)
            
        if not missing_syms:
            return  # Already completely warm
            
        if _allday_baseline_warming:
            return  # Already warming in background
            
    # Check if other heavy scanner warmups are running to avoid rate limit exhaustion
    if _is_other_warm_running():
        log.debug("[Volume Baseline] Deferred warming — other scanner warmups active.")
        return
        
    with _allday_baseline_lock:
        if _allday_baseline_warming:
            return
        _allday_baseline_warming = True
        
    log.info(f"[Volume Baseline] Warming {len(missing_syms)} symbols in background thread...")
    threading.Thread(target=_warm_baselines_bg, args=(kite, missing_syms), daemon=True).start()

def is_warm():
    """Returns True if baseline cache is successfully warmed for today's session."""
    active_date = _get_active_trading_date(now_ist())
    with _allday_baseline_lock:
        return bool(_allday_baseline_cache) and _allday_baseline_date == active_date

def get_symbol_baselines(symbol, timeframe):
    """Returns the slots dict baseline for a specific symbol & timeframe, or None."""
    with _allday_baseline_lock:
        return _allday_baseline_cache.get(symbol, {}).get(timeframe)

def get_vol_ratio(symbol, timeframe, time_slot, current_vol):
    """
    Computes vol_ratio for current_vol against baseline for timeframe and time_slot (HH:MM).
    Returns (vol_ratio: float|None, baseline_val: float|0.0)
    """
    if not time_slot:
        return None, 0.0
        
    # Trim slot string to HH:MM format
    if len(time_slot) > 5:
        time_slot = time_slot[:5]
        
    baselines = get_symbol_baselines(symbol, timeframe)
    if not baselines:
        return None, 0.0
        
    base_val = baselines.get(time_slot, 0.0)
    if base_val <= 0:
        return None, 0.0
        
    ratio = round(current_vol / base_val, 2)
    return ratio, base_val
