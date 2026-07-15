"""
================================================================================
📈 Real-Time Multi-Timeframe EMA 9 / EMA 21 Crossover & Alignment Scanner
================================================================================
Objective:
  Tracks EMA 9 and EMA 21 crossover events (bullish crossover above, bearish
  crossover below) and trend alignment across 5 Minutes, 15 Minutes, 1 Hour,
  and Daily timeframes for all active F&O symbols in real-time.
"""

import threading
import time
import logging
import concurrent.futures
import json
import os
import sqlite3
from datetime import datetime
from session_utils import now_ist
from indicators import compute_ema, check_ema9_respect

try:
    from kiteconnect import KiteTicker
except ImportError:
    KiteTicker = None  # Guard: KiteConnect may not be installed in test environments

# EOD snapshot: persists last completed scan so after-hours server restarts
# still show today's final crossover state without re-scanning.
# EOD snapshots now use _DDMMYYYYHHMM.json suffix instead of a static filename

_eod_saved_date = None   # Date string (YYYY-MM-DD) when today's EOD snapshot was saved
_ema_thread = None

# Global thread-safe state container
_ema_crossover_state = {
    "last_update": None,
    "status": "idle",
    "symbols_count": 0,
    "scan_current": 0,
    "scan_total": 0,
    "crossovers": {}, # { symbol: { state_15m, cross_15m, state_1h, cross_1h, state_day, cross_day, alignment } }
}
_state_lock = threading.Lock()

# ── 1-Minute Live Breakout Globals ───────────────────────────────────────────
_live_ticker = None
_live_ticker_thread = None
_live_subscribed_tokens = set()
_tick_buffers = {}
_tick_buffers_lock = threading.Lock()
_triggered_alerts = []  # List of triggered alerts today (Fix 2: protected by _alerts_lock)
_alerts_lock = threading.Lock()  # Fix 2: separate lock so _tick_buffers_lock is never held during alert emit

# ── First-Hour Volume Spurt: per-slot baseline cache ─────────────────────────
# Warmed ONCE per day (after market close or at startup if stale).
# Stores avg volume for each of 12 five-minute slots (09:15–10:15) computed
# from 10 clean historical trading days (excludes expiry, gap, VIX-shock days).
_fh_baseline_cache  = {}      # {symbol: {"09:15:00": avg_vol, "09:20:00": avg_vol, ...}}
_fh_baseline_date   = None    # date object when cache was last built
_fh_baseline_lock   = threading.Lock()
_fh_baseline_warming = False
_fh_rescan_needed   = False    # Set by warm thread to trigger a re-scan with fresh baselines

_FH_SLOTS = [
    "09:15:00", "09:20:00", "09:25:00", "09:30:00", "09:35:00", "09:40:00",
    "09:45:00", "09:50:00", "09:55:00", "10:00:00", "10:05:00", "10:10:00",
]
_FH_SLOTS_SET = frozenset(_FH_SLOTS)  # O(1) membership lookups
_FH_DB_PATH = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tradesignal_cache.db"))


def _fh_init_db():
    """Create the first_hour_baselines table if it doesn't exist."""
    try:
        conn = sqlite3.connect(_FH_DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS first_hour_baselines (
                symbol TEXT PRIMARY KEY,
                baselines TEXT NOT NULL,
                active_date TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        logging.warning(f"[FH Volume] DB init error: {e}")


def _fh_load_from_db():
    """Load cached baselines from SQLite. Only use if active_date matches today."""
    global _fh_baseline_cache, _fh_baseline_date
    try:
        today_str = _get_active_trading_date(now_ist())
        conn = sqlite3.connect(_FH_DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT symbol, baselines, active_date FROM first_hour_baselines LIMIT 1")
        row = cursor.fetchone()
        if row and row[2] == today_str:
            # Date matches — load all rows
            cursor.execute("SELECT symbol, baselines FROM first_hour_baselines WHERE active_date = ?", (today_str,))
            rows = cursor.fetchall()
            cache = {}
            for sym, bl_json in rows:
                try:
                    cache[sym] = json.loads(bl_json)
                except Exception:
                    pass
            with _fh_baseline_lock:
                _fh_baseline_cache = cache
                _fh_baseline_date = today_str
            logging.info(f"[FH Volume] Loaded {len(cache)} baselines from SQLite (date={today_str}).")
        else:
            logging.info(f"[FH Volume] SQLite baselines stale or empty (db_date={row[2] if row else 'none'}, need={today_str}).")
        conn.close()
    except Exception as e:
        logging.warning(f"[FH Volume] DB load error: {e}")


def _fh_save_single_to_db(symbol, slots, active_date_str):
    """Save a single computed baseline to SQLite."""
    try:
        conn = sqlite3.connect(_FH_DB_PATH)
        conn.execute(
            "INSERT OR REPLACE INTO first_hour_baselines (symbol, baselines, active_date) VALUES (?, ?, ?)",
            (symbol, json.dumps(slots), active_date_str)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logging.warning(f"[FH Volume] DB save error for {symbol}: {e}")


def _fh_save_to_db(baselines_dict, active_date_str):
    """Save computed baselines to SQLite."""
    try:
        conn = sqlite3.connect(_FH_DB_PATH)
        conn.execute("DELETE FROM first_hour_baselines WHERE active_date != ?", (active_date_str,))
        items = [(sym, json.dumps(slots), active_date_str) for sym, slots in baselines_dict.items()]
        conn.executemany(
            "INSERT OR REPLACE INTO first_hour_baselines (symbol, baselines, active_date) VALUES (?, ?, ?)",
            items
        )
        conn.commit()
        conn.close()
        logging.info(f"[FH Volume] Saved {len(items)} baselines to SQLite (date={active_date_str}).")
    except Exception as e:
        logging.warning(f"[FH Volume] DB save error: {e}")


def get_fh_baseline(symbol):
    """Thread-safe read of a symbol's per-slot baseline. Returns dict or None."""
    with _fh_baseline_lock:
        return _fh_baseline_cache.get(symbol)


def _fh_warm_baselines_bg(kite, token_map):
    """Background: compute first-hour per-slot volume baselines for all symbols.
    Fetches 25 calendar days of 5-min candles, applies exclusion filters
    (expiry days, gap opens >1.5%, VIX >10% daily move), keeps 10 clean days,
    and averages each slot's volume across those days.

    token_map = {symbol: instrument_token} — reuses existing resolved tokens."""
    global _fh_baseline_warming
    from datetime import timedelta, date as dt_date
    import datetime as dt_mod
    from session_utils import NSE_HOLIDAYS
    import random

    try:
        today = dt_date.today()
        active_date_str = _get_active_trading_date(now_ist())
        from_dt = today - timedelta(days=30)  # ~25 calendar days → 15+ trading days
        to_dt   = today - timedelta(days=1)

        # Prune old day baselines from database once before warming
        try:
            conn = sqlite3.connect(_FH_DB_PATH)
            conn.execute("DELETE FROM first_hour_baselines WHERE active_date != ?", (active_date_str,))
            conn.commit()
            conn.close()
        except Exception as e:
            logging.warning(f"[FH Volume] DB prune error: {e}")

        # ── Step 1: Identify expiry dates from NFO instruments cache ──────
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
            logging.info(f"[FH Volume] Found {len(expiry_dates)} expiry dates in range.")
        except Exception as e:
            logging.warning(f"[FH Volume] Expiry detection failed: {e}")

        # ── Step 2: Fetch India VIX daily candles for VIX filter ──────────
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
                logging.info(f"[FH Volume] VIX shock dates: {len(vix_shock_dates)}")
        except Exception as e:
            logging.warning(f"[FH Volume] VIX fetch failed (non-critical): {e}")

        # ── Step 3: Per-symbol baseline computation ───────────────────────
        pending_symbols = list(token_map.keys())
        results = {}
        processed = 0
        total = len(token_map)

        while pending_symbols:
            sym = pending_symbols.pop(0)  # FIFO queue: pop from front
            token = token_map[sym]
            try:
                time.sleep(0.6)  # Rate limit: 1.67 req/s
                
                # Fetch 5-min candles for the lookback window
                hist_5m = kite.historical_data(int(token), from_dt, to_dt, "5minute")
                if not hist_5m:
                    continue

                # Also need daily candles for gap-open filter
                time.sleep(0.6)
                hist_daily = kite.historical_data(int(token), from_dt, to_dt, "day")

                # Build gap-open exclusion set
                gap_dates = set()
                if hist_daily and len(hist_daily) >= 2:
                    for i in range(1, len(hist_daily)):
                        prev_close = hist_daily[i-1].get("close", 0)
                        day_open   = hist_daily[i].get("open", 0)
                        if prev_close > 0 and day_open > 0:
                            gap_pct = abs((day_open - prev_close) / prev_close * 100)
                            if gap_pct >= 1.5:
                                candle_date = hist_daily[i].get("date")
                                if hasattr(candle_date, 'date'):
                                    candle_date = candle_date.date()
                                elif isinstance(candle_date, str):
                                    candle_date = datetime.strptime(candle_date[:10], "%Y-%m-%d").date()
                                gap_dates.add(candle_date)

                # Group 5-min candles by date, filter first-hour slots only
                by_date = {}  # {date_obj: {slot_str: volume}}
                for c in hist_5m:
                    dt_val = c.get("date")
                    if hasattr(dt_val, 'date'):
                        c_date = dt_val.date()
                        c_time = dt_val.strftime("%H:%M:%S")
                    elif isinstance(dt_val, str):
                        c_date = datetime.strptime(dt_val[:10], "%Y-%m-%d").date()
                        c_time = dt_val[11:19]
                    else:
                        continue

                    if c_time not in _FH_SLOTS_SET:
                        continue
                    if c_date not in by_date:
                        by_date[c_date] = {}
                    by_date[c_date][c_time] = c.get("volume", 0) or 0

                # Apply exclusion filters
                all_excluded = expiry_dates | vix_shock_dates | gap_dates | set(NSE_HOLIDAYS)
                clean_dates = sorted(
                    [d for d in by_date.keys()
                     if d not in all_excluded
                     and d.weekday() < 5
                     and d != today],  # exclude today
                    reverse=True  # newest first
                )

                # Keep at most 10 clean days (drop oldest first)
                clean_dates = clean_dates[:10]

                if len(clean_dates) < 3:
                    # Not enough clean data — skip this symbol
                    continue

                # Compute per-slot average across clean days
                slot_baselines = {}
                for slot in _FH_SLOTS:
                    slot_vols = [by_date[d].get(slot, 0) for d in clean_dates if slot in by_date.get(d, {})]
                    if slot_vols:
                        slot_baselines[slot] = round(sum(slot_vols) / len(slot_vols), 0)
                    else:
                        slot_baselines[slot] = 0

                results[sym] = slot_baselines
                processed += 1

                # Save immediately to memory cache & database
                with _fh_baseline_lock:
                    _fh_baseline_cache[sym] = slot_baselines
                    global _fh_baseline_date
                    _fh_baseline_date = active_date_str

                _fh_save_single_to_db(sym, slot_baselines, active_date_str)

            except Exception as e:
                is_rate_limit = "429" in str(e) or "too many" in str(e).lower()
                if is_rate_limit:
                    backoff = 5.0 + random.uniform(0.5, 2.0)
                    logging.warning(f"[FH Volume] Rate limited at {sym}, appending to back of queue. Sleeping {backoff:.1f}s...")
                    pending_symbols.append(sym)  # Send to back to retry later
                    time.sleep(backoff)
                else:
                    logging.warning(f"[FH Volume] Error for {sym}: {e}")

        logging.info(f"[FH Volume] Baseline warm complete: {processed}/{total} symbols successfully warmed.")

        # Signal the scanner loop to re-scan so FH values populate in the board
        global _fh_rescan_needed
        _fh_rescan_needed = True

    except Exception as e:
        logging.error(f"[FH Volume] Baseline warm failed: {e}")
    finally:
        _fh_baseline_warming = False


def ensure_fh_baselines_warm(kite, token_map):
    """Non-blocking: triggers baseline warm if cache is missing/stale."""
    global _fh_baseline_warming
    if not token_map:
        return

    active_date = _get_active_trading_date(now_ist())

    # Try loading from SQLite first if memory is empty
    with _fh_baseline_lock:
        if not _fh_baseline_cache or _fh_baseline_date != active_date:
            _fh_init_db()
            _fh_load_from_db()

    # Identify which symbols are missing from the cache for today
    with _fh_baseline_lock:
        if _fh_baseline_date == active_date:
            missing_tokens = {sym: tok for sym, tok in token_map.items() if sym not in _fh_baseline_cache}
        else:
            missing_tokens = dict(token_map)

        if not missing_tokens:
            return  # Everything is warmed for today!

        if _fh_baseline_warming:
            return  # Already warming

    # Check RVOL warm isn't running (avoid rate-limit collision)
    try:
        from option_gainers_scanner import _avg_volume_warming
        if _avg_volume_warming:
            logging.debug("[FH Volume] Deferred — RVOL warm in progress.")
            return
    except ImportError:
        pass

    with _fh_baseline_lock:
        if _fh_baseline_warming:
            return
        _fh_baseline_warming = True

    logging.info(f"[FH Volume] Warmed={len(token_map) - len(missing_tokens)}/{len(token_map)}. Warming {len(missing_tokens)} remaining symbols...")
    threading.Thread(target=_fh_warm_baselines_bg, args=(kite, missing_tokens), daemon=True).start()


def _compute_fh_spurt(candles, symbol):
    """Compute first-hour volume spurt metrics by comparing today's 5-min candle
    volumes against the per-slot historical baseline.

    Returns (spurt_ratio, cumulative_ratio, spurt_tag) or (None, None, None) if
    baselines are not yet available or no first-hour candles exist today."""
    baselines = get_fh_baseline(symbol)
    if not baselines:
        return None, None, None

    active_date_str = _get_active_trading_date(now_ist())

    # Extract today's first-hour candles
    today_slots = {}  # {slot_str: volume}
    for c in candles:
        dt_val = c.get('date', '')
        if isinstance(dt_val, str):
            if not dt_val.startswith(active_date_str):
                continue
            c_time = dt_val[11:19]
        elif hasattr(dt_val, 'date'):
            if dt_val.date().isoformat() != active_date_str:
                continue
            c_time = dt_val.strftime("%H:%M:%S")
        else:
            continue

        if c_time in _FH_SLOTS_SET:
            today_slots[c_time] = c.get('volume', 0) or 0

    if not today_slots:
        return None, None, None

    # Latest completed slot's spurt ratio
    latest_slot = max(today_slots.keys())
    latest_vol  = today_slots[latest_slot]
    baseline_vol = baselines.get(latest_slot, 0)
    spurt_ratio = round(latest_vol / baseline_vol, 1) if baseline_vol > 0 else None

    # Cumulative ratio: avg of completed slots vs avg of their baselines
    completed_slots = sorted(today_slots.keys())
    running_avg = sum(today_slots[s] for s in completed_slots) / len(completed_slots)
    running_baseline = sum(baselines.get(s, 0) for s in completed_slots) / len(completed_slots)
    cumulative_ratio = round(running_avg / running_baseline, 1) if running_baseline > 0 else None

    # Tag based on spurt ratio
    if spurt_ratio is not None:
        if spurt_ratio >= 3.0:
            spurt_tag = "Abnormal"
        elif spurt_ratio >= 2.0:
            spurt_tag = "Elevated"
        elif spurt_ratio < 1.0:
            spurt_tag = "Weak"
        else:
            spurt_tag = "Normal"
    else:
        spurt_tag = None

    return spurt_ratio, cumulative_ratio, spurt_tag


# ── Active-client heartbeat ───────────────────────────────────────────────────
# The loop skips the heavy scan when no page has polled for >90 s.
_ema_last_client_time = 0.0
_ema_client_lock = threading.Lock()

def notify_ema_client():
    """Call from /api/ema-crossovers each time the frontend polls."""
    global _ema_last_client_time
    with _ema_client_lock:
        _ema_last_client_time = time.time()

def _has_ema_clients(timeout_sec=90):
    """Returns True if a page has polled within the last timeout_sec seconds."""
    with _ema_client_lock:
        return (time.time() - _ema_last_client_time) < timeout_sec

def get_ema_crossover_state():
    """Returns a copy of the current crossover scan state.
    Suppresses any data captured before 09:00 IST (pre-market scans or previous
    day EOD) once the clock reaches 09:00 IST, so only current session data shows."""
    now = now_ist()
    with _state_lock:
        state = dict(_ema_crossover_state)

    # After 09:00 IST, clear any data whose last_update is either:
    #   (a) from a different calendar day, OR
    #   (b) from today but before 09:00 (pre-market overnight scan)
    if state.get('crossovers') and now.hour >= 9:
        last_update = state.get('last_update') or ''
        try:
            update_dt   = datetime.strptime(last_update, '%Y-%m-%d %H:%M:%S')
            update_date = update_dt.strftime('%Y-%m-%d')
            today_str   = now.strftime('%Y-%m-%d')
            # Stale if different date OR same date but captured before market open
            if update_date != today_str or update_dt.hour < 9:
                state = {**state, 'crossovers': {}, 'status': 'idle', 'last_update': None}
        except Exception:
            pass

    return state

def _get_kite():
    from server import get_kite
    return get_kite()

def _is_active_window(now):
    """Returns True between 09:15 and 15:30 IST."""
    if now.hour == 9 and now.minute >= 15: return True
    if 10 <= now.hour <= 14: return True
    if now.hour == 15 and now.minute <= 30: return True
    return False

def _detect_ema_crossover(candles, period_short=9, period_long=21):
    """
    Computes EMA 9 and EMA 21.
    Returns (trend_state, crossover_signal, crossover_time_elapsed, crossover_epoch)
      trend_state: 'bullish' (EMA9 > EMA21), 'bearish' (EMA9 < EMA21), 'neutral'
      crossover_signal: 'bullish' (crossed above), 'bearish' (crossed below), 'none'
      crossover_time_elapsed: formatted string (e.g. '15m ago') or None
      crossover_epoch: int Unix epoch timestamp or 0
    """
    if not candles or len(candles) < period_long + 2:
        return 'neutral', 'none', None, 0, 0.0
        
    closes = [c['close'] for c in candles]
    ema_short = compute_ema(closes, period_short)
    ema_long = compute_ema(closes, period_long)
    
    if len(ema_short) < 2 or len(ema_long) < 2:
        return 'neutral', 'none', None, 0, 0.0
        
    prev_s, curr_s = ema_short[-2], ema_short[-1]
    prev_l, curr_l = ema_long[-2], ema_long[-1]
    
    if prev_s is None or curr_s is None or prev_l is None or curr_l is None:
        return 'neutral', 'none', None, 0, 0.0
        
    trend_state = 'bullish' if curr_s > curr_l else 'bearish'
    
    # 1. Determine if a fresh crossover happened on the current candle
    crossover = 'none'
    if prev_s <= prev_l and curr_s > curr_l:
        crossover = 'bullish'
    elif prev_s >= prev_l and curr_s < curr_l:
        crossover = 'bearish'
        
    # 2. Find how long ago the *most recent* crossover of any kind occurred in history
    crossover_time_elapsed = None
    crossover_epoch = 0
    crossover_candle_size_pct = 0.0
    for i in range(len(candles) - 1, 0, -1):
        p_s, c_s = ema_short[i - 1], ema_short[i]
        p_l, c_l = ema_long[i - 1], ema_long[i]
        
        if p_s is None or c_s is None or p_l is None or c_l is None:
            continue
            
        is_cross = False
        if p_s <= p_l and c_s > c_l:
            is_cross = True
        elif p_s >= p_l and c_s < c_l:
            is_cross = True
            
        if is_cross:
            o = candles[i].get('open', 0) or 0
            c = candles[i].get('close', 0) or 0
            crossover_candle_size_pct = round((abs(c - o) / c) * 100, 2) if c > 0 else 0.0
            
            cross_time_str = candles[i]['date']
            if isinstance(cross_time_str, str):
                if '+' in cross_time_str:
                    cross_time_str = cross_time_str.split('+')[0]
                try:
                    cross_dt = datetime.strptime(cross_time_str, "%Y-%m-%dT%H:%M:%S")
                except ValueError:
                    try:
                        cross_dt = datetime.strptime(cross_time_str, "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        crossover_time_elapsed = "just now"
                        try:
                            crossover_epoch = int(time.time())
                        except Exception:
                            crossover_epoch = 0
                        break
            elif hasattr(cross_time_str, 'strftime'):
                cross_dt = cross_time_str
            else:
                crossover_time_elapsed = "just now"
                try:
                    crossover_epoch = int(time.time())
                except Exception:
                    crossover_epoch = 0
                break
                
            try:
                if cross_dt.hour == 0 and cross_dt.minute == 0:
                    crossover_time_elapsed = cross_dt.strftime("%d %b")
                else:
                    crossover_time_elapsed = cross_dt.strftime("%d %b, %H:%M")
            except Exception:
                crossover_time_elapsed = str(cross_time_str)
                
            try:
                crossover_epoch = int(cross_dt.timestamp())
            except Exception:
                crossover_epoch = 0
            break
            
    return trend_state, crossover, crossover_time_elapsed, crossover_epoch, crossover_candle_size_pct

# ── 1-Minute Live Breakout Helper Methods ────────────────────────────────────

def calculate_squeeze_metrics(candles):
    """
    Computes Bollinger Band width and squeeze conditions on 5m candles.
    Returns (in_squeeze, bb_width, lowest_bb_20, ema_gap, consolidation_high, consolidation_low)
    """
    if not candles or len(candles) < 20:
        return False, 0.0, 0.0, 0.0, 0.0, 0.0
        
    closes = [c.get('close', 0.0) or 0.0 for c in candles]
    highs = [c.get('high', 0.0) or 0.0 for c in candles]
    lows = [c.get('low', 0.0) or 0.0 for c in candles]
    
    # 1. EMAs for gap check
    ema9 = compute_ema(closes, 9)
    ema21 = compute_ema(closes, 21)
    if len(ema9) < 1 or len(ema21) < 1 or not ema9[-1] or not ema21[-1]:
        return False, 0.0, 0.0, 0.0, 0.0, 0.0
    curr_e9 = ema9[-1]
    curr_e21 = ema21[-1]
    ltp = closes[-1]
    if ltp <= 0:
        return False, 0.0, 0.0, 0.0, 0.0, 0.0
    ema_gap = (abs(curr_e9 - curr_e21) / ltp) * 100
    
    # 2. Bollinger Bands (20 period, 2 StdDev)
    bb_widths = []
    for i in range(len(closes) - 20 + 1):
        window = closes[i : i + 20]
        mean = sum(window) / 20.0
        variance = sum((x - mean) ** 2 for x in window) / 20.0
        stddev = variance ** 0.5
        upper = mean + 2 * stddev
        lower = mean - 2 * stddev
        if mean > 0:
            width = ((upper - lower) / mean) * 100
        else:
            width = 0.0
        bb_widths.append(width)
        
    if not bb_widths:
        return False, 0.0, 0.0, 0.0, 0.0, 0.0
        
    curr_bb_width = bb_widths[-1]
    lowest_bb_20 = min(bb_widths)
    
    # 3. Consolidation range (last 6 candles)
    last_6_highs = highs[-6:]
    last_6_lows = lows[-6:]
    consolidation_high = max(last_6_highs)
    consolidation_low = min(last_6_lows)
    
    # Price range last 3 candles as a % of LTP
    last_3_closes = closes[-3:]
    price_range_pct = ((max(last_3_closes) - min(last_3_closes)) / ltp) * 100
    
    # Squeeze criteria (relaxed from original 0.15/1.20/0.25 which was too restrictive)
    in_squeeze = (
        ema_gap < 0.50 and
        curr_bb_width <= lowest_bb_20 * 1.50 and
        price_range_pct < 0.80
    )
    
    return in_squeeze, curr_bb_width, lowest_bb_20, ema_gap, consolidation_high, consolidation_low

def _get_token_map():
    """Returns a dict mapping symbol (e.g. 'BIOCON') -> instrument_token (int)"""
    import server
    token_map = {}
    if server._instruments_cache:
        INDEX_MAP = {
            'NIFTY':      'NIFTY 50',
            'BANKNIFTY':  'NIFTY BANK',
            'FINNIFTY':   'NIFTY FIN SERVICE',
            'MIDCPNIFTY': 'NIFTY MID SELECT',
            'NIFTYNXT50': 'NIFTY NEXT 50',
        }
        for inst in server._instruments_cache:
            sym = inst.get('tradingsymbol', '').upper()
            tok = inst.get('instrument_token')
            if tok:
                token_map[sym] = int(tok)
        
        # Add reverse lookup index mappings if present
        for short_name, long_name in INDEX_MAP.items():
            if long_name.upper() in token_map:
                token_map[short_name] = token_map[long_name.upper()]
    return token_map

def update_squeeze_ticker_subscriptions(squeeze_watchlist_data):
    """
    Dynamically diffs and updates active WebSocket subscriptions for squeezed stocks.
    """
    global _live_ticker, _live_ticker_thread, _live_subscribed_tokens
    
    token_map = _get_token_map()
    new_tokens_to_data = {}
    new_tokens = set()
    
    for sym, details in squeeze_watchlist_data.items():
        token = token_map.get(sym.upper())
        if token:
            new_tokens.add(token)
            new_tokens_to_data[token] = {
                "symbol": sym,
                "avg_5m_volume": details["avg_5m_volume"],
                "consolidation_high": details["consolidation_high"],
                "consolidation_low": details["consolidation_low"],
                "squeeze_start_time": details["squeeze_start_time"],
                "ltp": details.get("ltp", 0.0)
            }
            
    # Compute subscription diffs
    to_subscribe = new_tokens - _live_subscribed_tokens
    to_unsubscribe = _live_subscribed_tokens - new_tokens
    
    # 1. Update tick buffers (thread-safe)
    with _tick_buffers_lock:
        for token in to_unsubscribe:
            if token in _tick_buffers:
                del _tick_buffers[token]
                
        for token in to_subscribe:
            data = new_tokens_to_data[token]
            _tick_buffers[token] = {
                "symbol": data["symbol"],
                "ticks": [],
                "current_minute": None,
                "avg_5m_volume": data["avg_5m_volume"],
                "consolidation_high": data["consolidation_high"],
                "consolidation_low": data["consolidation_low"],
                "squeeze_start_time": data["squeeze_start_time"],
                "last_ltp": data["ltp"]
            }
            
    # 2. Spawn / control background KiteTicker dynamically
    if new_tokens and (_live_ticker is None or not _live_ticker.is_connected()):
        _start_live_ticker(list(new_tokens))
    elif _live_ticker and _live_ticker.is_connected():
        if to_unsubscribe:
            try:
                _live_ticker.unsubscribe(list(to_unsubscribe))
                logging.info(f"[Live Breakout Scanner] Unsubscribed from {len(to_unsubscribe)} tokens.")
            except Exception as e:
                logging.error(f"[Live Breakout Scanner] Unsubscribe failed: {e}")
                
        if to_subscribe:
            try:
                _live_ticker.subscribe(list(to_subscribe))
                _live_ticker.set_mode(_live_ticker.MODE_FULL, list(to_subscribe))
                logging.info(f"[Live Breakout Scanner] Subscribed to {len(to_subscribe)} tokens.")
            except Exception as e:
                logging.error(f"[Live Breakout Scanner] Subscribe failed: {e}")
                
    _live_subscribed_tokens = new_tokens

def _start_live_ticker(initial_tokens):
    global _live_ticker, _live_ticker_thread
    if KiteTicker is None:
        logging.error("[Live Breakout Scanner] KiteTicker not available — kiteconnect not installed.")
        return
    kite = _get_kite()
    if not kite:
        return
        
    try:
        logging.info("[Live Breakout Scanner] Initializing background KiteTicker...")
        from global_ticker import get_ticker_for_feature
        tokens_snapshot = list(initial_tokens) if initial_tokens else []
        _live_ticker = get_ticker_for_feature("ema_crossover", tokens_snapshot, _process_ticks, mode="FULL")
        
        def on_ticks(ws, ticks):
            _process_ticks(ticks)
            
        def on_connect(ws, response):
            logging.info("[Live Breakout Scanner] WebSocket connected. Subscribing to live feeds...")
            # Take a local snapshot of the set — avoids race with scanner thread
            # that replaces _live_subscribed_tokens every 2 minutes
            tokens_snapshot_conn = set(_live_subscribed_tokens)
            if tokens_snapshot_conn:
                ws.subscribe(list(tokens_snapshot_conn))
                ws.set_mode(ws.MODE_FULL, list(tokens_snapshot_conn))
                
        def on_error(ws, code, reason):
            logging.error(f"[Live Breakout Scanner] WebSocket error: {code} - {reason}")
            
        def on_close(ws, code, reason):
            logging.warning(f"[Live Breakout Scanner] WebSocket connection closed: {code} - {reason}")
            
        _live_ticker.on_ticks = on_ticks
        _live_ticker.on_connect = on_connect
        _live_ticker.on_error = on_error
        _live_ticker.on_close = on_close
        
        def run_kws():
            try:
                import time
                time.sleep(5)
                _live_ticker.connect(threaded=True)
            except Exception as e:
                logging.error(f"[Live Breakout Scanner] Connection exception: {e}")
                
        _live_ticker_thread = threading.Thread(target=run_kws, daemon=True)
        _live_ticker_thread.start()
        
    except Exception as e:
        logging.error(f"[Live Breakout Scanner] Failed starting ticker: {e}")

def _process_ticks(ticks):
    now = now_ist()
    minute_key = now.minute

    # Fix 1: collect completed candle snapshots inside the lock, evaluate OUTSIDE
    completed_snapshots = []

    with _tick_buffers_lock:
        for tick in ticks:
            tok = tick.get('instrument_token')
            ltp = tick.get('last_price', 0.0) or 0.0
            volume = tick.get('volume_traded', 0) or tick.get('volume', 0) or 0

            if not tok or tok not in _tick_buffers or ltp <= 0:
                continue

            buffer = _tick_buffers[tok]
            buffer["last_ltp"] = ltp

            if buffer["current_minute"] is None:
                buffer["current_minute"] = minute_key
                buffer["ticks"] = []

            if buffer["current_minute"] != minute_key:
                # 1M candle completed — snapshot it so we can evaluate outside the lock
                if buffer["ticks"]:
                    completed_snapshots.append({
                        "symbol":            buffer["symbol"],
                        "ticks":             list(buffer["ticks"]),  # shallow copy is sufficient
                        "avg_5m_volume":     buffer["avg_5m_volume"],
                        "consolidation_high": buffer["consolidation_high"],
                        "consolidation_low":  buffer["consolidation_low"],
                        "squeeze_start_time": buffer["squeeze_start_time"],
                    })
                # Reset builder for the new minute
                buffer["current_minute"] = minute_key
                buffer["ticks"] = []

            buffer["ticks"].append({
                "ltp":       ltp,
                "volume":    volume,
                "timestamp": now
            })

    # Fix 1: evaluate AFTER releasing _tick_buffers_lock to avoid reentrancy
    for snapshot in completed_snapshots:
        _evaluate_1m_candle_close(snapshot)

def _clear_stale_alerts_under_lock():
    """Filters out any alerts not from the current day. Assumes caller holds _alerts_lock."""
    global _triggered_alerts
    if not _triggered_alerts:
        return
    
    from datetime import timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    today_str = now_ist().strftime('%Y-%m-%d')
    
    # Filter list in place
    valid_alerts = []
    for a in _triggered_alerts:
        epoch = a.get('trigger_epoch', 0)
        if epoch > 0:
            a_date = datetime.fromtimestamp(epoch, tz=IST).strftime('%Y-%m-%d')
            if a_date == today_str:
                valid_alerts.append(a)
    
    if len(valid_alerts) != len(_triggered_alerts):
        _triggered_alerts.clear()
        _triggered_alerts.extend(valid_alerts)

def _send_telegram_breakout(alert):
    """Format and send a Telegram/Discord alert for a Live Stock Breakout."""
    try:
        # Strict Guard: Only send alerts during active market hours
        from session_utils import is_market_hours
        if not is_market_hours():
            return

        from server import _send_telegram_message, _telegram_configured
        if not _telegram_configured():
            return

        emoji = "🟢 BULLISH BREAKOUT" if alert.get("direction") == "bullish" else "🔴 BEARISH BREAKOUT"
        symbol = alert.get("symbol", "")
        grade = alert.get("grade", "")
        ltp = alert.get("ltp", 0.0)
        vol_multiplier = alert.get("vol_multiplier", 0.0)
        time_str = alert.get("time", "")

        msg = (
            f"⚡ {emoji} — #{symbol}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 Grade   : {grade}\n"
            f"📈 Stock LTP: ₹{ltp}\n"
            f"📊 Volume  : {vol_multiplier}x (vs 5m avg)\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 Time     : {time_str} IST"
        )

        _send_telegram_message(msg)
        logging.info("[Live Breakouts] Telegram/Discord alert dispatched for %s", symbol)
    except Exception as e:
        logging.warning("[Live Breakouts] Failed to dispatch Telegram/Discord alert: %s", e)


def _evaluate_1m_candle_close(snapshot):
    """
    Evaluate a completed 1M candle snapshot for breakout conditions.
    snapshot is a plain dict (copy of buffer fields) — NO lock is held when this runs.
    """
    ticks = snapshot["ticks"]
    if not ticks:
        return

    symbol            = snapshot["symbol"]
    avg_5m_volume     = snapshot["avg_5m_volume"]
    consolidation_high = snapshot["consolidation_high"]
    consolidation_low  = snapshot["consolidation_low"]

    # Compute OHLC from tick stream
    prices       = [t["ltp"] for t in ticks]
    candle_open  = prices[0]
    candle_close = prices[-1]

    ltp = candle_close
    if ltp <= 0:
        return

    body_pct = (abs(candle_close - candle_open) / ltp) * 100

    # Cumulative volume: difference in running daily volume between first and last tick
    vol_start     = ticks[0]["volume"]
    vol_end       = ticks[-1]["volume"]
    candle_volume = max(0, vol_end - vol_start)

    is_bullish_break = (candle_close > consolidation_high)
    is_bearish_break = (candle_close < consolidation_low)

    # Fix 4: skip entirely when no volume baseline — avoids false positives on illiquid symbols
    if avg_5m_volume <= 0:
        return

    # Compare 1M volume surge against average 1M volume (avg_5m_volume / 5.0)
    avg_1m_volume = avg_5m_volume / 5.0
    vol_surge   = (candle_volume > 1.5 * avg_1m_volume)
    strong_body = (body_pct > 0.15)

    if (is_bullish_break or is_bearish_break) and vol_surge and strong_body:
        direction = "bullish" if is_bullish_break else "bearish"

        squeeze_duration_mins = 0
        if snapshot["squeeze_start_time"]:
            squeeze_duration_mins = int(
                (now_ist() - snapshot["squeeze_start_time"]).total_seconds() / 60.0
            )

        grade = "Grade A" if (squeeze_duration_mins > 20 and body_pct > 0.25) else "Grade B"

        alert = {
            "symbol":           symbol,
            "direction":        direction,
            "time":             now_ist().strftime("%H:%M:%S"),
            "ltp":              round(ltp, 2),
            "grade":            grade,
            "vol_multiplier":   round(candle_volume / avg_1m_volume, 2),
            "trigger_epoch":    int(time.time()),
            "candles_5m_elapsed": 1,
        }

        # Fix 2: use _alerts_lock to protect both the duplicate-check and the append
        do_emit = False
        with _alerts_lock:
            _clear_stale_alerts_under_lock()
            already_triggered = any(a["symbol"] == symbol for a in _triggered_alerts)
            if not already_triggered:
                _triggered_alerts.append(alert)
                do_emit = True

        # Emit OUTSIDE both locks — socketio.emit can block/re-enter
        if do_emit:
            logging.info(f"[Live Breakout Triggered] {symbol} {direction.upper()} breakout! {grade} at {ltp}")
            print(f"[Live Breakout Triggered] {symbol} {direction.upper()} breakout! {grade} at {ltp}")
            try:
                from server import socketio
                socketio.emit('live_breakout_alert', alert)
            except Exception:
                pass

            # Send Telegram/Discord alert
            _send_telegram_breakout(alert)

def get_live_breakout_state():
    """Returns the current squeeze watchlist and triggered alerts in a clean JSON format.
    Fix 2: returns a snapshot copy of alerts (safe for JSON serialisation from any thread).
    Fix 2: resets the alert list at start of each new trading day."""
    from datetime import timezone, timedelta
    now = now_ist()
    today_str = now.strftime('%Y-%m-%d')
    IST = timezone(timedelta(hours=5, minutes=30))

    # Daily reset + snapshot — all under _alerts_lock
    with _alerts_lock:
        _clear_stale_alerts_under_lock()
        alerts_snapshot = list(_triggered_alerts)  # return a copy, not the live list

    watchlist_symbols = []
    with _tick_buffers_lock:
        for token, buf in _tick_buffers.items():
            squeeze_duration_mins = 0
            if buf["squeeze_start_time"]:
                squeeze_duration_mins = int((now - buf["squeeze_start_time"]).total_seconds() / 60.0)
            watchlist_symbols.append({
                "symbol":             buf["symbol"],
                "squeeze_duration_mins": squeeze_duration_mins,
                "consolidation_high": round(buf["consolidation_high"], 2),
                "consolidation_low":  round(buf["consolidation_low"],  2),
                "last_ltp":           round(buf["last_ltp"], 2),
            })

    return {
        "squeeze_watchlist": watchlist_symbols,
        "triggered_alerts":  alerts_snapshot,
    }

def _get_active_trading_date(now):
    """
    Returns the date string (YYYY-MM-DD) of the active or latest trading session.
    If market hasn't opened yet today (< 09:15), returns the previous trading day.
    """
    from datetime import timedelta
    from session_utils import NSE_HOLIDAYS
    target = now.date()
    
    # Before 09:15 AM today, the active session is the previous trading day
    if now.hour < 9 or (now.hour == 9 and now.minute < 15):
        target = target - timedelta(days=1)
        
    # Walk backward to skip weekends and holidays
    while target.weekday() >= 5 or target in NSE_HOLIDAYS:
        target = target - timedelta(days=1)
        
    return target.strftime('%Y-%m-%d')


def _compute_ema9_hold(candles, max_body_penetration_pct=20):
    """
    Computes EMA9 Respect and EMA21 Trend Alignment from 5-minute candles.

    Rules:
      1. Trend filter: EMA9 must remain on the correct side of EMA21 (EMA9 > EMA21 for Y, EMA9 < EMA21 for N).
      2. Penetration filter: Candle body penetration across EMA9 must not exceed 20%.
      3. Walk backward from the last candle to count consecutive valid bars.
    """
    if not candles or len(candles) < 21:
        return None, 0

    closes = [c.get('close', 0) or 0 for c in candles]
    ema9 = compute_ema(closes, 9)
    ema21 = compute_ema(closes, 21)

    if not ema9 or not ema21 or len(ema9) != len(closes) or len(ema21) != len(closes):
        return None, 0

    # Determine trend side on latest bar
    direction = "bullish" if ema9[-1] > ema21[-1] else "bearish"
    confirmed_state = "Y" if direction == "bullish" else "N"

    # Walk backward to find the number of consecutive aligned candles
    consecutive_bars = 0
    for i in range(len(closes) - 1, -1, -1):
        if ema9[i] is None or ema21[i] is None:
            break

        # 1. Trend Filter Check
        if direction == "bullish" and ema9[i] <= ema21[i]:
            break
        elif direction == "bearish" and ema9[i] >= ema21[i]:
            break

        # 2. Body Penetration Check using clean library helper
        respect_res = check_ema9_respect(
            [candles[i]], 
            [ema9[i]], 
            direction=direction, 
            max_body_penetration_pct=max_body_penetration_pct, 
            min_consecutive=1
        )
        if respect_res["state"] != "CONFIRMED":
            break

        consecutive_bars += 1

    if consecutive_bars == 0:
        return None, 0

    return confirmed_state, consecutive_bars * 5


def _calculate_intraday_linearity(candles, today_str):
    """
    Computes linearity and net points for the current session (today) using 15-minute candles.
    Returns (linearity_score, net_movement) where linearity_score is 0-100.
    """
    intraday = []
    for c in candles:
        dt_str = c.get('date', '')
        if isinstance(dt_str, str) and dt_str.startswith(today_str):
            intraday.append(c)
            
    if len(intraday) < 2:
        return 0.0, 0.0
        
    net_change = intraday[-1]['close'] - intraday[0]['open']
    
    # Sum of absolute close-to-close movements today
    total_path = sum(abs(intraday[i]['close'] - intraday[i-1]['close']) for i in range(1, len(intraday)))
    # Include first candle body
    total_path += abs(intraday[0]['close'] - intraday[0]['open'])
    
    if total_path > 0:
        linearity = abs(net_change) / total_path
    else:
        linearity = 1.0
        
    return round(linearity * 100, 0), round(net_change, 2)


def _scan_single_symbol(kite, symbol):
    """Fetches candles and computes crossover metrics across Daily, 1H, and 15M."""
    from server import get_historical_candles
    
    # Map index names to NSE Spot tradingsymbols
    INDEX_MAP = {
        'NIFTY':       'NIFTY 50',
        'BANKNIFTY':   'NIFTY BANK',
        'FINNIFTY':    'NIFTY FIN SERVICE',
        'MIDCPNIFTY':  'NIFTY MID SELECT',
        'NIFTYNXT50':  'NIFTY NEXT 50',
    }
    fetch_symbol = INDEX_MAP.get(symbol, symbol)
    
    intervals = [
        ('5m',  '5minute',  7,  100),   # 5-min: intraday scalp signal (covers weekend/holidays)
        ('15m', '15minute', 10, 100),   # 15-min (covers weekend/holidays)
        ('1h',  '60minute', 15, 100),   # 1-hour
        ('day', 'day',      120, 100),  # Daily
    ]
    
    res = {}
    for label, interval, days_back, limit in intervals:
        candles = get_historical_candles(kite, fetch_symbol, interval, days_back=days_back, limit=limit)
        trend, cross, elapsed, epoch, size_pct = _detect_ema_crossover(candles)
        res[f"state_{label}"] = trend
        res[f"cross_{label}"] = cross
        res[f"cross_time_{label}"] = elapsed
        res[f"cross_epoch_{label}"] = epoch
        res[f"cross_candle_size_{label}"] = size_pct
        
        # Compute intraday session linearity on 15m candles
        if label == '15m':
            active_date_str = _get_active_trading_date(now_ist())
            lin_score, net_move = _calculate_intraday_linearity(candles, active_date_str)
            res["linearity_score"] = lin_score
            res["net_movement"] = net_move
            
        # Piggyback squeeze calculations on 5m candles to avoid extra API calls
        if label == '5m':
            in_sq, bb_w, low_bb, gap, c_high, c_low = calculate_squeeze_metrics(candles)
            volumes = [c.get('volume', 0.0) or 0.0 for c in candles] if candles else []
            avg_vol = sum(volumes[-20:]) / 20.0 if len(volumes) >= 20 else 0.0
            
            res["squeeze"] = {
                "in_squeeze": in_sq,
                "bb_width": round(bb_w, 3),
                "lowest_bb_20": round(low_bb, 3),
                "ema_gap": round(gap, 4),
                "consolidation_high": c_high,
                "consolidation_low": c_low,
                "avg_5m_volume": avg_vol
            }

            # EMA9 Hold indicator — piggybacks on existing 5m candles
            try:
                e9h_state, e9h_mins = _compute_ema9_hold(candles)
                res["ema9_hold"] = e9h_state
                res["ema9_hold_minutes"] = e9h_mins
            except Exception:
                res["ema9_hold"] = None
                res["ema9_hold_minutes"] = 0

            # First-Hour Volume Spurt — piggybacks on existing 5m candles
            try:
                fh_spurt, fh_cumul, fh_tag = _compute_fh_spurt(candles, symbol)
                res["fh_spurt_ratio"]      = fh_spurt
                res["fh_cumulative_ratio"] = fh_cumul
                res["fh_spurt_tag"]        = fh_tag
            except Exception:
                res["fh_spurt_ratio"]      = None
                res["fh_cumulative_ratio"] = None
                res["fh_spurt_tag"]        = None
        
    # Calculate Multi-Timeframe Alignment
    states = [res.get("state_15m"), res.get("state_1h"), res.get("state_day")]
    if all(s == 'bullish' for s in states):
        res["alignment"] = "bullish"
    elif all(s == 'bearish' for s in states):
        res["alignment"] = "bearish"
    else:
        res["alignment"] = "mixed"
        
    return symbol, res

def _ema_crossover_loop():
    global _eod_saved_date, _fh_rescan_needed   # assigned inside loop — must declare global
    logging.info("[EMA Crossover Scanner] Background loop active.")
    print("[EMA Crossover Scanner] Background loop active.")
    
    last_scan_time = 0
    SCAN_INTERVAL = 120  # Scan F&O symbols every 2 minutes (120 seconds)
    
    while True:
        try:
            # ── Pause when no webpage is open ─────────────────────────────────
            # Scanner runs only while the F&O board is open.
            # When board is opened after hours, client gate passes and the
            # not-in-window block below handles the ONE post-close EOD scan.
            if not _has_ema_clients():
                time.sleep(10)
                continue
            # ──────────────────────────────────────────────────────────────────
            now       = now_ist()
            in_window = _is_active_window(now)
            today_str = now.strftime('%Y-%m-%d')

            if not in_window:
                # ── Check if baselines just warmed and we need a re-scan ──────
                if _fh_rescan_needed:
                    _fh_rescan_needed = False
                    logging.info("[FH Volume] Baselines warmed — forcing re-scan to populate FH values.")
                    last_scan_time = 0  # Force immediate scan
                    # Fall through to scan block below (skip Cases 1/2/3)
                else:
                    # ── Out-of-hours snapshot logic ───────────────────────────────
                    with _state_lock:
                        has_data = bool(_ema_crossover_state.get('crossovers'))

                    if has_data:
                        # Case 1: live data in memory → save once then long-sleep
                        if _eod_saved_date != today_str:
                            _save_eod_snapshot()
                            _eod_saved_date = today_str
                            logging.info("[EMA Crossover] EOD snapshot saved from last live scan.")

                        # Periodically try to warm baselines (safe: returns immediately if already warm)
                        try:
                            kite = _get_kite()
                            if kite:
                                token_map = _get_token_map()
                                if token_map:
                                    ensure_fh_baselines_warm(kite, token_map)
                        except Exception as e:
                            logging.warning(f"[FH Volume] Out-of-hours Case 1 baseline warm trigger failed: {e}")

                        time.sleep(300)
                        continue

                    if _eod_saved_date == today_str:
                        # Case 2: already did EOD scan/save today but crossovers are empty → sleep
                        try:
                            kite = _get_kite()
                            if kite:
                                token_map = _get_token_map()
                                if token_map:
                                    ensure_fh_baselines_warm(kite, token_map)
                        except Exception as e:
                            logging.warning(f"[FH Volume] Out-of-hours Case 2 baseline warm trigger failed: {e}")
                        time.sleep(300)
                        continue

                    # Case 3: no data + not yet scanned today → fall through for ONE EOD scan
                    logging.info("[EMA Crossover] No live data found. Running ONE post-close scan.")
                    last_scan_time = 0  # force immediate scan trigger below

            current_time = time.time()
            if current_time - last_scan_time >= SCAN_INTERVAL:
                kite = _get_kite()
                if not kite:
                    logging.warning("[EMA Crossover Scanner] Kite unavailable. Skipping cycle.")
                    print("[EMA Crossover Scanner] Kite unavailable. Skipping cycle.")
                    time.sleep(10)
                    continue
                logging.info("[EMA Crossover Scanner] Loading F&O instrument list...")
                print("[EMA Crossover Scanner] Loading F&O instrument list...")
                from db_instruments import get_cached_instruments
                nfo_cache = get_cached_instruments("NFO")
                underlying_names = sorted(list({
                    i["name"].upper() for i in nfo_cache
                    if i.get("instrument_type") in ["CE", "PE"] and i.get("name")
                }))
                
                if not underlying_names:
                    logging.warning("[EMA Crossover Scanner] No NFO symbols resolved.")
                    print("[EMA Crossover Scanner] No NFO symbols resolved.")
                    time.sleep(10)
                    continue
                
                import server
                if not server._instruments_cache:
                    logging.info("[EMA Crossover Scanner] Pre-warming server instruments cache...")
                    print("[EMA Crossover Scanner] Pre-warming server instruments cache...")
                    try:
                        nse_all = get_cached_instruments("NSE")
                        nse_map = {i['tradingsymbol'].upper(): i for i in nse_all}
                        INDEX_NSE_MAP = {
                            'NIFTY':      'NIFTY 50',
                            'BANKNIFTY':  'NIFTY BANK',
                            'FINNIFTY':   'NIFTY FIN SERVICE',
                            'MIDCPNIFTY': 'NIFTY MID SELECT',
                            'NIFTYNXT50': 'NIFTY NEXT 50',
                        }
                        minimal_cache = []
                        missing = []
                        for name in underlying_names:
                            lookup = INDEX_NSE_MAP.get(name, name)
                            if lookup in nse_map:
                                minimal_cache.append(nse_map[lookup])
                            else:
                                missing.append(name)
                        if missing:
                            bse_all = get_cached_instruments("BSE")
                            bse_map = {i['tradingsymbol'].upper(): i for i in bse_all}
                            for name in missing:
                                if name in bse_map:
                                    minimal_cache.append(bse_map[name])
                                else:
                                    logging.warning(f"[EMA Crossover Scanner] {name} not found in NSE or BSE")
                                    print(f"[EMA Crossover Scanner] WARNING: {name} not found in NSE or BSE")
                        server._instruments_cache = minimal_cache
                        logging.info(f"[EMA Crossover Scanner] Pre-warmed {len(minimal_cache)} FNO instruments.")
                        print(f"[EMA Crossover Scanner] Pre-warmed {len(minimal_cache)} FNO instruments.")
                    except Exception as ex:
                        logging.error(f"[EMA Crossover Scanner] Failed pre-warming: {ex}")

                # Ensure FH volume baselines are warm before scanning
                try:
                    token_map_fh = _get_token_map()
                    if token_map_fh:
                        ensure_fh_baselines_warm(kite, token_map_fh)
                except Exception as e:
                    logging.warning(f"[FH Volume] Pre-scan baseline warm trigger failed: {e}")

                # Reload baselines from DB in case a previous warm cycle saved them
                _fh_load_from_db()
                
                logging.info(f"[EMA Crossover Scanner] Starting crossover scan for {len(underlying_names)} symbols...")
                print(f"[EMA Crossover Scanner] Starting crossover scan for {len(underlying_names)} symbols...")
                
                with _state_lock:
                    _ema_crossover_state["status"]       = "scanning"
                    _ema_crossover_state["scan_total"]   = len(underlying_names)
                    _ema_crossover_state["scan_current"] = 0
                    # Keep existing _ema_crossover_state["crossovers"] intact so UI doesn't blank out
                
                temp_crossovers = {}
                
                # Scan in parallel with 2 threads to stay within Zerodha's 3 req/s historical API limit.
                # 8 threads was saturating the limit, starving OI Spurt baseline fetches with 429 errors.
                # With 2 threads: ~2 req/s consumed, leaving 1 req/s headroom for other callers.
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                    futures = {executor.submit(_scan_single_symbol, kite, sym): sym for sym in underlying_names}
                    for future in concurrent.futures.as_completed(futures):
                        sym = futures[future]
                        try:
                            symbol, data = future.result()
                            temp_crossovers[symbol] = data
                        except Exception as e:
                            logging.warning(f"[EMA Crossover Scanner] Failed scanning {sym}: {e}")
                            print(f"[EMA Crossover Scanner] Failed scanning {sym}: {e}")
                        with _state_lock:
                            _ema_crossover_state["scan_current"] += 1
                            
                # ── Enrich with spot % change (prev-day close → current LTP) ──
                try:
                    from oi_spurt_routes import EXCHANGE_MAP
                    spot_queries = [EXCHANGE_MAP.get(s, f"NSE:{s}") for s in underlying_names]
                    spot_change_map = {}
                    for b in range(0, len(spot_queries), 500):
                        sq = kite.quote(spot_queries[b:b+500])
                        for exch_sym, d in sq.items():
                            sym = exch_sym.split(":")[-1]
                            ltp = d.get("last_price", 0) or 0
                            prev = (d.get("ohlc") or {}).get("close") or 0
                            if prev > 0:
                                spot_change_map[sym] = round(((ltp - prev) / prev) * 100, 2)
                    for sym in temp_crossovers:
                        temp_crossovers[sym]["spot_change_pct"] = spot_change_map.get(sym)
                except Exception as e:
                    logging.warning(f"[EMA Crossover Scanner] Spot change fetch failed: {e}")

                with _state_lock:
                    _ema_crossover_state["last_update"] = now.strftime("%Y-%m-%d %H:%M:%S")
                    _ema_crossover_state["status"] = "completed"
                    _ema_crossover_state["symbols_count"] = len(temp_crossovers)
                    _ema_crossover_state["crossovers"] = temp_crossovers
                    
                logging.info(f"[EMA Crossover Scanner] Completed crossover scan. Cached {len(temp_crossovers)} symbols.")
                print(f"[EMA Crossover Scanner] Completed crossover scan. Cached {len(temp_crossovers)} symbols.")

                # Stage 1: Build the squeeze watchlist
                # Fix 6: get token map BEFORE taking any lock (avoids Python import-lock + our lock)
                token_map = _get_token_map()

                # Fix 3/6: read existing start-times in a brief, focused lock, then release immediately
                with _tick_buffers_lock:
                    existing_starts = {
                        tok: _tick_buffers[tok]["squeeze_start_time"]
                        for tok in _tick_buffers
                    }

                # Fix 3: build watchlist dict OUTSIDE any lock
                new_squeeze_watchlist = {}
                for sym, data in temp_crossovers.items():
                    sq = data.get("squeeze", {})
                    if not sq or not sq.get("in_squeeze"):
                        continue

                    tok = token_map.get(sym.upper())
                    # Preserve squeeze_start_time if the symbol is already being watched
                    existing_start = existing_starts.get(tok, now) if tok else now

                    squeeze_duration_mins = int((now - existing_start).total_seconds() / 60.0)

                    # Time decay: prune squeezes that exceed 180 minutes without a breakout (allow 3 hours)
                    if squeeze_duration_mins > 180:
                        logging.info(f"[Live Breakout Scanner] Pruning {sym} due to 180 min time decay.")
                        continue

                    new_squeeze_watchlist[sym] = {
                        "symbol":             sym,
                        "avg_5m_volume":      sq.get("avg_5m_volume", 0.0),
                        "consolidation_high": sq.get("consolidation_high", 0.0),
                        "consolidation_low":  sq.get("consolidation_low",  0.0),
                        "squeeze_start_time": existing_start,
                        "ltp": 0.0,  # Fix 5: don't store spot_change_pct here; live ticks will populate last_ltp
                    }

                # Fix 3: call update OUTSIDE all locks — it takes _tick_buffers_lock internally
                try:
                    update_squeeze_ticker_subscriptions(new_squeeze_watchlist)
                except Exception as ex:
                    logging.error(f"[EMA Crossover Scanner] Failed updating squeeze ticker subscriptions: {ex}")
                # Save EOD snapshot only when outside market hours (one post-close save)
                if not _is_active_window(now_ist()):
                    _save_eod_snapshot()
                    _eod_saved_date = now_ist().strftime('%Y-%m-%d')
                    
                    # Also trigger baseline warm after the post-close scan completes
                    try:
                        kite = _get_kite()
                        if kite:
                            token_map = _get_token_map()
                            if token_map:
                                ensure_fh_baselines_warm(kite, token_map)
                    except Exception as e:
                        logging.warning(f"[FH Volume] Post-scan warm trigger failed: {e}")
                last_scan_time = current_time
                
            time.sleep(10)
        except Exception as e:
            logging.error(f"[EMA Crossover Scanner] Exception in scanner background loop: {e}")
            print(f"[EMA Crossover Scanner] Exception in scanner background loop: {e}")
            time.sleep(15)

def _get_expected_trading_date(now):
    """
    Returns the expected date (datetime.date) of the trading session that
    the current/latest EOD snapshot represents.
    """
    from datetime import timedelta
    from session_utils import NSE_HOLIDAYS
    target = now.date()
    if now.hour < 9:
        target = target - timedelta(days=1)
    while target.weekday() >= 5 or target in NSE_HOLIDAYS:
        target = target - timedelta(days=1)
    return target


def _save_eod_snapshot():
    """Persist the last completed scan to disk. Called after every completed scan.
    On server restart outside market hours, _load_eod_snapshot() reads this back."""
    import glob
    try:
        now = now_ist()
        expected_date = _get_expected_trading_date(now)
        suffix = expected_date.strftime('%d%m%Y')
        snapshot_filename = f"ema_eod_snapshot_{suffix}.json"
        snapshot_path = os.path.join(os.path.dirname(__file__), snapshot_filename)

        with _state_lock:
            data = {
                'date':          expected_date.strftime('%Y-%m-%d'),
                'last_update':   _ema_crossover_state.get('last_update'),
                'symbols_count': _ema_crossover_state.get('symbols_count', 0),
                'crossovers':    _ema_crossover_state.get('crossovers', {}),
            }
        with open(snapshot_path, 'w') as f:
            json.dump(data, f)
        logging.info(f"[EMA Crossover] EOD snapshot saved: {snapshot_filename}")

        # Clean up any other old ema_eod_snapshot_*.json files
        pattern = os.path.join(os.path.dirname(__file__), "ema_eod_snapshot_*.json")
        for fpath in glob.glob(pattern):
            if os.path.basename(fpath) != snapshot_filename:
                try:
                    os.remove(fpath)
                except Exception:
                    pass
    except Exception as e:
        logging.warning(f"[EMA Crossover] EOD snapshot save failed: {e}")


def _load_eod_snapshot():
    """Load EOD snapshot on server startup — only if it contains actual market
    session data (last_update >= 09:00 IST on snap_date).
    Pre-market scans (captured before 09:00) are always rejected once it's past
    09:00 IST, so yesterday's overnight data never pollutes today's board."""
    from datetime import timedelta
    import glob
    try:
        pattern = os.path.join(os.path.dirname(__file__), "ema_eod_snapshot_*.json")
        files = glob.glob(pattern)
        if not files:
            return

        # Parse suffixes to find the latest
        valid_files = []
        for fpath in files:
            basename = os.path.basename(fpath)
            if basename.startswith("ema_eod_snapshot_") and basename.endswith(".json"):
                suffix = basename[len("ema_eod_snapshot_"):-len(".json")]
                if len(suffix) == 8 and suffix.isdigit():
                    try:
                        dt = datetime.strptime(suffix, "%d%m%Y")
                        valid_files.append((dt, fpath))
                    except ValueError:
                        pass
                elif len(suffix) == 12 and suffix.isdigit():
                    try:
                        dt = datetime.strptime(suffix, "%d%m%Y%H%M")
                        valid_files.append((dt, fpath))
                    except ValueError:
                        pass

        if not valid_files:
            return

        valid_files.sort(key=lambda x: x[0], reverse=True)
        latest_dt, latest_fpath = valid_files[0]

        with open(latest_fpath, 'r') as f:
            data = json.load(f)
        now       = now_ist()
        today     = now.date()
        snap_date = datetime.strptime(data['date'], '%Y-%m-%d').date()

        # Parse the actual capture time from last_update
        last_update_str = data.get('last_update', '')
        try:
            last_update_dt   = datetime.strptime(last_update_str, '%Y-%m-%d %H:%M:%S')
            last_update_hour = last_update_dt.hour
        except Exception:
            last_update_hour = 0   # unknown → treat as pre-market (safe default)

        # Valid only when the snapshot holds real market-session data:
        #   1. Same calendar day AND captured after 09:00 (live session data)
        #   2. Yesterday AND current time is still before 09:00 (pre-open window,
        #      yesterday's EOD is still the best available view)
        if snap_date == today and last_update_hour >= 9:
            is_valid = True
        elif snap_date == today - timedelta(days=1) and now.hour < 9:
            is_valid = True
        else:
            is_valid = False
            logging.info(
                f"[EMA Crossover] EOD snapshot rejected — pre-market data "
                f"(captured {last_update_str}, now {now.strftime('%Y-%m-%d %H:%M')}) — skipped."
            )

        if not is_valid:
            return

        with _state_lock:
            _ema_crossover_state['last_update']   = data.get('last_update')
            _ema_crossover_state['crossovers']    = data.get('crossovers', {})
            _ema_crossover_state['symbols_count'] = data.get('symbols_count', 0)
            _ema_crossover_state['status']        = 'completed'
        logging.info(f"[EMA Crossover] EOD snapshot loaded: {data.get('symbols_count')} symbols from {data.get('last_update')}.")
    except Exception as e:
        logging.warning(f"[EMA Crossover] EOD snapshot load failed: {e}")


def start_ema_crossover_scanner():
    global _ema_thread
    _load_eod_snapshot()   # Restore last scan's data immediately on startup
    _fh_init_db()          # Ensure first-hour baselines table exists
    _fh_load_from_db()     # Load cached baselines if fresh for today
    
    # Trigger first-hour baseline warm on startup if cache is not yet warmed today
    try:
        kite = _get_kite()
        if kite:
            token_map = _get_token_map()
            if token_map:
                ensure_fh_baselines_warm(kite, token_map)
    except Exception as e:
        logging.warning(f"[FH Volume] Startup warm trigger failed: {e}")

    if _ema_thread is None or not _ema_thread.is_alive():
        logging.info("[EMA Crossover Scanner] Spawning background thread...")
        print("[EMA Crossover Scanner] Spawning background thread...")
        _ema_thread = threading.Thread(target=_ema_crossover_loop, daemon=True)
        _ema_thread.start()
