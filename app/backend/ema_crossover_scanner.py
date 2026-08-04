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
from cpr_utils import get_cpr_pivots

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

# ── EMA Collision Alert globals ───────────────────────────────────────────────
# Separate from squeeze breakout alerts — different trigger mechanism.
EMA_COLLISION_LOOKBACK      = 8        # candles to look back for collision (8 x 5m = 40 min)
EMA_COLLISION_DEDUP_SEC     = 3600     # 60 min dedup per symbol+direction
_COLLISION_THRESHOLD_PCT    = 0.15    # |EMA9-EMA21|/close must be < this % to be "in zone"
_collision_alerts      = []            # [{type, symbol, direction, ltp, time, trigger_epoch}]
_collision_alerts_lock = threading.Lock()
_collision_dedup       = {}            # {"SYMBOL_bullish": epoch}
_collision_watchlist   = {}            # {symbol: {ltp, ema_gap_pct, time}} — in-zone but not yet confirmed
_collision_wl_lock     = threading.Lock()

# ── Symmetric Alert Safeguard Constants ───────────────────────────────────────
MIN_ALERT_VOLUME_RATIO  = 2.0   # Mandatory volume ratio (vs baseline or 20-MA) >= 2.0x
MIN_ALERT_MOVE_PCT_1M   = 0.15  # Mandatory 1-minute candle body move % >= 0.15%
MIN_ALERT_MOVE_PCT_5M   = 0.20  # Mandatory 5-minute candle body move % >= 0.20%

_fh_rescan_needed   = False    # Set by warm thread to trigger a re-scan with fresh baselines

# ── EMA Crossover Alert globals & dedup ──────────────────────────────────────
_ema_cross_dedup = {}  # {"SYMBOL_direction": epoch}
_ema_cross_dedup_lock = threading.Lock()
EMA_CROSS_DEDUP_SEC = 14400  # 4 hours (14400 seconds)

def _send_telegram_ema_cross(symbol, direction, ltp, vol_ratio, slot_str, move_pct=0.0):
    """Format and send a Telegram alert for a 5m EMA Crossover."""
    try:
        from session_utils import is_market_hours
        if not is_market_hours():
            return

        if slot_str and slot_str > "15:15":
            return

        from server import _send_telegram_message, _telegram_configured
        if not _telegram_configured():
            return

        emoji = "🟢 BULLISH CROSS" if direction == "bullish" else "🔴 BEARISH CROSS"
        vol_str = f"{vol_ratio}x" if vol_ratio is not None else "N/A"
        move_str = f"{move_pct:.2f}%" if move_pct is not None else "0.00%"

        msg = (
            f"⚡ [5M] {emoji} — #{symbol}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 Stock LTP: ₹{ltp}\n"
            f"📊 Volume  : {vol_str} (vs 20D slot avg)\n"
            f"📏 2C Move : {move_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 Time     : {slot_str} IST"
        )

        _send_telegram_message(msg)
        logging.info("[EMA Cross Alert] Telegram alert dispatched for %s (%s)", symbol, direction)
    except Exception as e:
        logging.warning("[EMA Cross Alert] Failed to dispatch Telegram alert: %s", e)


def _send_telegram_pre_cross_5m(symbol, direction, ltp, vol_ratio, move_pct, slot_str):
    """Format and send a Telegram alert for a Pre-Cross 5M Momentum Surge."""
    try:
        from session_utils import is_market_hours
        if not is_market_hours():
            return

        if slot_str and slot_str > "15:15":
            return

        from server import _send_telegram_message, _telegram_configured
        if not _telegram_configured():
            return

        emoji = "🟢 BULLISH PRE-CROSS" if direction == "bullish" else "🔴 BEARISH PRE-CROSS"
        vol_str = f"{vol_ratio:.2f}x" if vol_ratio is not None else "N/A"
        move_str = f"{move_pct:.2f}%" if move_pct is not None else "0.00%"

        msg = (
            f"⚡ [5M PRE-CROSS] {emoji} — #{symbol}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 Stock LTP: ₹{ltp}\n"
            f"📊 Volume  : {vol_str} (vs 20D slot avg)\n"
            f"📏 5M Move : {move_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 Time     : {slot_str} IST"
        )

        _send_telegram_message(msg)
        logging.info("[Pre-Cross 5M Alert] Telegram alert dispatched for %s (%s)", symbol, direction)
    except Exception as e:
        logging.warning("[Pre-Cross 5M Alert] Failed to dispatch Telegram alert: %s", e)


def _compute_fh_spurt(candles, symbol):
    """Compute first-hour volume spurt metrics by comparing today's 5-min candle
    volumes against the per-slot historical baseline.

    Returns (spurt_ratio, cumulative_ratio, spurt_tag) or (None, None, None) if
    baselines are not yet available or no first-hour candles exist today."""
    from volume_baseline import get_symbol_baselines
    baselines = get_symbol_baselines(symbol, "5m")
    if not baselines:
        return None, None, None

    active_date_str = _get_active_trading_date(now_ist())

    # First-hour slots: 09:15 to 10:10
    fh_slots = frozenset([
        "09:15", "09:20", "09:25", "09:30", "09:35", "09:40",
        "09:45", "09:50", "09:55", "10:00", "10:05", "10:10"
    ])

    # Extract today's first-hour candles
    today_slots = {}  # {slot_str: volume}
    for c in candles:
        dt_val = c.get('date', '')
        if isinstance(dt_val, str):
            if not dt_val.startswith(active_date_str):
                continue
            c_time = dt_val[11:16]
        elif hasattr(dt_val, 'date'):
            if dt_val.date().isoformat() != active_date_str:
                continue
            c_time = dt_val.strftime("%H:%M")
        else:
            continue

        if c_time in fh_slots:
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

    # After 09:00 IST, clear any data whose last_update is stale —
    # BUT only do this on actual trading days (weekdays, non-holidays).
    # On weekends/holidays we always keep the last EOD data visible.
    if state.get('crossovers') and now.hour >= 9:
        from session_utils import NSE_HOLIDAYS
        is_trading_day = now.weekday() < 5 and now.date() not in NSE_HOLIDAYS
        if is_trading_day:
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
    """Returns True between 09:15 and 15:40 IST."""
    if now.hour == 9 and now.minute >= 15: return True
    if 10 <= now.hour <= 14: return True
    if now.hour == 15 and now.minute <= 40: return True
    return False

def _detect_ema_crossover(candles, period_short=9, period_long=21, require_two_candle_close=False):
    """
    Computes EMA 9 and EMA 21 and validates crossover quality:
      - 5M (require_two_candle_close=True): Tracks EMA9 hold duration post-crossover. Requires minimum 10 min hold (2 candles, Y10/N10).
      - 15M, 1H, Daily (require_two_candle_close=False): Standard single-candle EMA crossover detection.
    """
    if not candles or len(candles) < period_long + 2:
        return 'neutral', 'none', None, 0, 0.0, None
        
    closes = [c['close'] for c in candles]
    ema_short = compute_ema(closes, period_short)
    ema_long = compute_ema(closes, period_long)
    
    if len(ema_short) < 2 or len(ema_long) < 2:
        return 'neutral', 'none', None, 0, 0.0, None
        
    prev_s, curr_s = ema_short[-2], ema_short[-1]
    prev_l, curr_l = ema_long[-2], ema_long[-1]
    
    if prev_s is None or curr_s is None or prev_l is None or curr_l is None:
        return 'neutral', 'none', None, 0, 0.0, None
        
    trend_state = 'bullish' if curr_s > curr_l else 'bearish'
    
    crossover_time_elapsed = None
    crossover_epoch = 0
    crossover_candle_size_pct = 0.0
    crossover_type = 'none'
    confirm_idx = -1
    cross_hold_tag = None

    for i in range(len(candles) - 1, 0, -1):
        p_s, c_s = ema_short[i - 1], ema_short[i]
        p_l, c_l = ema_long[i - 1], ema_long[i]
        
        if p_s is None or c_s is None or p_l is None or c_l is None:
            continue
            
        is_bull = (p_s <= p_l and c_s > c_l)
        is_bear = (p_s >= p_l and c_s < c_l)
        
        if not (is_bull or is_bear):
            continue
            
        o = candles[i].get('open', 0.0) or 0.0
        c = candles[i].get('close', 0.0) or 0.0
        if c <= 0:
            continue

        # 1. Close position check for Candle #1 (crossover candle must close above/below EMAs)
        if is_bull and c <= max(c_s, c_l):
            continue
        if is_bear and c >= min(c_s, c_l):
            continue

        if require_two_candle_close:
            # 2. Rule of 5M EMA Crossover Confirmation:
            # Candle #2 (index i+1) must confirm Candle #1 (index i):
            if i >= len(candles) - 1:
                # Candle #2 has not closed yet — wait for Candle #2 to close before alerting
                continue

            next_o = candles[i + 1].get('open', 0.0) or 0.0
            next_c = candles[i + 1].get('close', 0.0) or 0.0

            if next_c <= 0:
                continue

            if is_bull:
                # Bullish confirmation:
                # 1. 2nd candle must close positive (green: close > open)
                # 2. 2nd candle must close above 1st candle close (next_c > c)
                if not (next_c > next_o and next_c > c):
                    continue
            elif is_bear:
                # Bearish confirmation:
                # 1. 2nd candle must close negative (red: close < open)
                # 2. 2nd candle must close below 1st candle close (next_c < c)
                if not (next_c < next_o and next_c < c):
                    continue

            # Walk forward from crossover candle i (index i+1 to len-1) to count 5m bars holding trend.
            # Reversal Rule:
            # Bullish trend reverses ONLY when 2 consecutive bearish candles close below EMA 21 (with 2nd candle closing below 1st candle close).
            # Bearish trend reverses ONLY when 2 consecutive bullish candles close above EMA 21 (with 2nd candle closing above 1st candle close).
            consecutive_hold_bars = 0
            break_streak = 0
            prev_break_close = None
            hold_still_active = False

            for j in range(i + 1, len(candles)):
                oj = candles[j].get('open', 0.0) or 0.0
                cj = candles[j].get('close', 0.0) or 0.0
                e21j = ema_long[j] or 0.0

                if is_bull:
                    # Check if candle j is a bearish breakdown candle below EMA 21
                    is_bear_break = (cj < oj and cj < e21j)
                    if is_bear_break:
                        if break_streak == 1 and prev_break_close is not None and cj < prev_break_close:
                            # Confirmed bullish trend reversal! (2 consecutive bearish closes below EMA21, 2nd < 1st)
                            break_streak = 2
                            break
                        else:
                            break_streak = 1
                            prev_break_close = cj
                            consecutive_hold_bars += 1
                            if j == len(candles) - 1:
                                hold_still_active = True
                    else:
                        break_streak = 0
                        prev_break_close = None
                        consecutive_hold_bars += 1
                        if j == len(candles) - 1:
                            hold_still_active = True
                elif is_bear:
                    # Check if candle j is a bullish breakout candle above EMA 21
                    is_bull_break = (cj > oj and cj > e21j)
                    if is_bull_break:
                        if break_streak == 1 and prev_break_close is not None and cj > prev_break_close:
                            # Confirmed bearish trend reversal! (2 consecutive bullish closes above EMA21, 2nd > 1st)
                            break_streak = 2
                            break
                        else:
                            break_streak = 1
                            prev_break_close = cj
                            consecutive_hold_bars += 1
                            if j == len(candles) - 1:
                                hold_still_active = True
                    else:
                        break_streak = 0
                        prev_break_close = None
                        consecutive_hold_bars += 1
                        if j == len(candles) - 1:
                            hold_still_active = True

            # If a confirmed reversal occurred prior to current bar, the crossover hold is no longer active
            if not hold_still_active or consecutive_hold_bars < 1:
                continue

            confirm_idx = i + 1
            hold_mins = (consecutive_hold_bars + 1) * 5
            cross_hold_tag = f"Y{hold_mins}" if is_bull else f"N{hold_mins}"
        else:
            # For 15M, 1H, Daily: standard single crossover candle confirmation
            confirm_idx = i

        # Valid crossover confirmed!
        if require_two_candle_close and confirm_idx > i:
            # Total % move across Candle #1 (crossover bar) and Candle #2 (confirmation bar)
            next_c = candles[confirm_idx].get('close', 0.0) or 0.0
            move_pct = (abs(next_c - o) / o) * 100.0 if o > 0 else 0.0
        else:
            move_pct = (abs(c - o) / c) * 100.0 if c > 0 else 0.0

        crossover_candle_size_pct = round(move_pct, 2)
        crossover_type = 'bullish' if is_bull else 'bearish'

        cross_time_str = candles[confirm_idx]['date']
        if isinstance(cross_time_str, str):
            if '+' in cross_time_str:
                cross_time_str = cross_time_str.split('+')[0]
            try:
                cross_dt = datetime.strptime(cross_time_str, "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                try:
                    cross_dt = datetime.strptime(cross_time_str, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    cross_dt = None
        elif hasattr(cross_time_str, 'strftime'):
            cross_dt = cross_time_str
        else:
            cross_dt = None

        if cross_dt:
            try:
                if cross_dt.hour == 0 and cross_dt.minute == 0:
                    crossover_time_elapsed = cross_dt.strftime("%d %b")
                else:
                    crossover_time_elapsed = cross_dt.strftime("%d %b, %H:%M")
                crossover_epoch = int(cross_dt.timestamp())
            except Exception:
                crossover_time_elapsed = str(cross_time_str)
                crossover_epoch = 0
        break

    crossover_signal = 'none'
    if confirm_idx == len(candles) - 1:
        crossover_signal = crossover_type

    return trend_state, crossover_signal, crossover_time_elapsed, crossover_epoch, crossover_candle_size_pct, cross_hold_tag

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


# ── EMA Collision Detection ────────────────────────────────────────────────────

def _check_ema_collision_state(candles):
    """
    Two-phase EMA9/EMA21 collision detector for 5-min candles.
    Returns: (in_zone: bool, direction: str|None, gap_pct: float)
      in_zone   → True if EMAs were within _COLLISION_THRESHOLD_PCT in last
                  EMA_COLLISION_LOOKBACK candles
      direction → 'bullish' | 'bearish' | None  (None = in zone but not confirmed)
      gap_pct   → current EMA gap as % of close (for watchlist display)
    """
    if not candles or len(candles) < 25:
        return False, None, 0.0

    closes = [c['close'] for c in candles]

    def _ema(prices, period):
        k = 2.0 / (period + 1)
        result = [prices[0]]
        for p in prices[1:]:
            result.append(p * k + result[-1] * (1 - k))
        return result

    ema9  = _ema(closes, 9)
    ema21 = _ema(closes, 21)

    c_now   = closes[-1]
    c_prev  = closes[-2]
    e9_now  = ema9[-1]
    e21_now = ema21[-1]
    e9_prev = ema9[-2]

    # Current EMA gap as % of close
    gap_pct = abs(e9_now - e21_now) / c_now * 100.0 if c_now else 0.0

    # Collision check: any of last EMA_COLLISION_LOOKBACK candles had gap < threshold
    lookback = min(EMA_COLLISION_LOOKBACK, len(closes))
    in_zone = any(
        closes[-(i+1)] > 0 and
        abs(ema9[-(i+1)] - ema21[-(i+1)]) / closes[-(i+1)] * 100.0 < _COLLISION_THRESHOLD_PCT
        for i in range(lookback)
    )

    if not in_zone:
        return False, None, gap_pct

    # Confirm direction: 2 consecutive closes on same side + EMA9 crossed EMA21
    bullish = (e9_now > e21_now and c_prev > e9_prev and c_now > e9_now)
    bearish = (e9_now < e21_now and c_prev < e9_prev and c_now < e9_now)

    if bullish:
        return True, 'bullish', gap_pct
    if bearish:
        return True, 'bearish', gap_pct

    return True, None, gap_pct  # in zone, not yet confirmed


def _send_telegram_collision(alert):
    """Send Telegram notification for a confirmed EMA collision alert."""
    try:
        from session_utils import is_market_hours
        if not is_market_hours():
            return
        from server import _send_telegram_message, _telegram_configured
        if not _telegram_configured():
            return

        direction = alert.get('direction', '')
        symbol    = alert.get('symbol', '')
        ltp       = alert.get('ltp', 0.0)
        time_str  = alert.get('time', '')
        gap_pct   = alert.get('gap_pct', 0.0)
        vol_rat   = alert.get('vol_multiplier', 0.0)
        move_pct  = alert.get('move_pct', 0.0)

        emoji = "\U0001f535 EMA COLLISION \u2014 BULLISH" if direction == 'bullish' else "\U0001f7e0 EMA COLLISION \u2014 BEARISH"
        vol_info = f"Vol Ratio: {vol_rat:.1f}x | Move: {move_pct:.2f}%" if vol_rat > 0 else f"Move: {move_pct:.2f}%"
        msg = (
            f"\u26a1 {emoji}\n"
            f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"\U0001f4cc Symbol  : #{symbol}\n"
            f"\U0001f4c8 LTP     : \u20b9{ltp}\n"
            f"\U0001f4ca Metrics : {vol_info}\n"
            f"\U0001f4ca EMA Gap : {gap_pct:.3f}% of LTP\n"
            f"\U0001f4ca Signal  : EMA9/EMA21 Coil \u2192 Confirmed Break\n"
            f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"\U0001f550 Time    : {time_str} IST"
        )
        _send_telegram_message(msg)
        logging.info("[EMA Collision] Telegram alert dispatched for %s", symbol)
    except Exception as e:
        logging.warning("[EMA Collision] Telegram failed: %s", e)


def _emit_collision_alert(symbol, direction, ltp, vol_ratio=0.0, move_pct=0.0):
    """
    Emit a confirmed EMA collision alert via SocketIO + Telegram.
    60-min dedup per symbol+direction pair.
    """
    import time as _time
    key = f"{symbol}_{direction}"
    now_epoch = _time.time()

    # Dedup guard
    last = _collision_dedup.get(key, 0)
    if now_epoch - last < EMA_COLLISION_DEDUP_SEC:
        return

    _collision_dedup[key] = now_epoch

    # Get gap_pct for display (best-effort from watchlist)
    with _collision_wl_lock:
        gap_pct = _collision_watchlist.get(symbol, {}).get('ema_gap_pct', 0.0)

    alert = {
        "type":           "ema_collision",
        "symbol":         symbol,
        "direction":      direction,
        "ltp":            round(ltp, 2),
        "gap_pct":        round(gap_pct, 3),
        "vol_multiplier": round(vol_ratio, 2),
        "move_pct":       round(move_pct, 2),
        "time":           now_ist().strftime("%H:%M:%S"),
        "trigger_epoch":  now_epoch,
    }

    with _collision_alerts_lock:
        _collision_alerts.append(alert)

    # SocketIO emit
    try:
        from server import socketio
        socketio.emit('ema_collision_alert', alert)
    except Exception as e:
        logging.warning("[EMA Collision] SocketIO emit failed: %s", e)

    # Telegram
    _send_telegram_collision(alert)
    logging.info("[EMA Collision] Alert fired: %s %s @ \u20b9%s", symbol, direction, ltp)


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
    vol_surge   = (avg_1m_volume > 0 and candle_volume >= MIN_ALERT_VOLUME_RATIO * avg_1m_volume)
    strong_body = (body_pct >= MIN_ALERT_MOVE_PCT_1M)

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
                "watch_type":         "bb_squeeze",  # distinguish from EMA coil
            })

    # Merge EMA collision zone stocks into watchlist
    with _collision_wl_lock:
        for sym, info in _collision_watchlist.items():
            watchlist_symbols.append({
                "symbol":             sym,
                "squeeze_duration_mins": 0,
                "consolidation_high": 0,
                "consolidation_low":  0,
                "last_ltp":           info["ltp"],
                "watch_type":         "ema_coil",   # blue badge in UI
                "ema_gap_pct":        info["ema_gap_pct"],
                "coil_time":          info["time"],
            })

    # EMA collision alerts snapshot
    with _collision_alerts_lock:
        collision_snapshot = list(_collision_alerts)

    bb_squeezes = [w for w in watchlist_symbols if w.get("watch_type") == "bb_squeeze"]
    ema_coils   = [w for w in watchlist_symbols if w.get("watch_type") == "ema_coil"]
    return {
        "squeeze_watchlist": watchlist_symbols,   # kept for backward compat
        "triggered_alerts":  alerts_snapshot,
        "collision_alerts":  collision_snapshot,
        "bb_squeezes":       bb_squeezes,
        "ema_coils":         ema_coils,
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
    Computes EMA9 Hold state from 5-minute candles using 2-candle hysteresis.

    Rules:
      - Price closing ABOVE EMA9 -> Y (holding above)
      - Price closing BELOW EMA9 -> N (holding below)
      - Flip requires minimum 2 consecutive 5m candle closes on opposite side (hysteresis filter to avoid noise)

    Returns:
      (state, hold_minutes)
        state: 'Y' (above EMA9) or 'N' (below EMA9) or None
        hold_minutes: how many minutes the current confirmed state has persisted
    """
    if not candles or len(candles) < 12:
        return None, 0

    closes = [c.get('close', 0) or 0 for c in candles]
    ema9 = compute_ema(closes, 9)

    if not ema9 or len(ema9) != len(closes):
        return None, 0

    # Walk candles to determine confirmed state with 2-candle hysteresis
    confirmed_state = None   # 'Y' or 'N'
    consecutive_opposite = 0
    state_start_idx = 0

    for i in range(len(closes)):
        if ema9[i] is None or ema9[i] <= 0 or closes[i] <= 0:
            continue

        above = closes[i] > ema9[i]

        if confirmed_state is None:
            # First valid candle sets the initial state
            confirmed_state = 'Y' if above else 'N'
            state_start_idx = i
            consecutive_opposite = 0
            continue

        current_is_same = (confirmed_state == 'Y' and above) or \
                          (confirmed_state == 'N' and not above)

        if current_is_same:
            consecutive_opposite = 0
        else:
            consecutive_opposite += 1
            if consecutive_opposite >= 2:  # 2 consecutive closes confirm flip
                confirmed_state = 'Y' if above else 'N'
                state_start_idx = i - 1  # Flip started 1 candle ago
                consecutive_opposite = 0

    # Calculate hold duration from state_start_idx to last candle
    hold_candles = max(0, len(closes) - 1 - state_start_idx)
    hold_minutes = hold_candles * 5   # Each candle = 5 minutes

    return confirmed_state, hold_minutes


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
        is_5m = (label == '5m')
        trend, cross, elapsed, epoch, size_pct, cross_hold = _detect_ema_crossover(candles, require_two_candle_close=is_5m)
        res[f"state_{label}"] = trend
        res[f"cross_{label}"] = cross
        res[f"cross_time_{label}"] = elapsed
        res[f"cross_epoch_{label}"] = epoch
        res[f"cross_candle_size_{label}"] = size_pct
        if is_5m:
            res["cross_hold_5m"] = cross_hold

        # New 5m crossover & pre-cross momentum volume baseline integration & Telegram alerting
        if label == '5m' and candles:
            last_c = candles[-1]
            ltp_now = last_c.get('close', 0) or 0
            open_now = last_c.get('open', 0) or 0
            dt_val = last_c.get('date')
            slot_str = None
            if isinstance(dt_val, str):
                slot_str = dt_val[11:16]
            elif hasattr(dt_val, 'strftime'):
                slot_str = dt_val.strftime("%H:%M")

            if slot_str and ltp_now > 0:
                import volume_baseline
                vol_ratio, base_val = volume_baseline.get_vol_ratio(symbol, '5m', slot_str, last_c.get('volume', 0) or 0)
                res["cross_5m_vol_ratio"] = vol_ratio
                res["cross_5m_vol_baseline"] = base_val

                now_epoch = int(time.time())
                current_hm = now_ist().strftime("%H:%M")

                # Case 1: Confirmed 5M EMA Crossover Alert
                if cross != 'none':
                    key = f"{symbol}_{cross}_{slot_str}"
                    with _ema_cross_dedup_lock:
                        already_alerted_for_slot = key in _ema_cross_dedup
                        if not already_alerted_for_slot:
                            if len(_ema_cross_dedup) > 1000:
                                cutoff = now_epoch - 86400
                                to_del = [k for k, t in _ema_cross_dedup.items() if t < cutoff]
                                for k in to_del:
                                    _ema_cross_dedup.pop(k, None)

                            if slot_str <= "15:15" and current_hm <= "15:15":
                                _ema_cross_dedup[key] = now_epoch
                                _send_telegram_ema_cross(symbol, cross, round(ltp_now, 2), vol_ratio or 1.0, slot_str, move_pct=size_pct)

                                alert = {
                                    "symbol":           symbol,
                                    "direction":        cross,
                                    "time":             now_ist().strftime("%H:%M:%S"),
                                    "ltp":              round(ltp_now, 2),
                                    "grade":            "5M Cross",
                                    "vol_multiplier":   vol_ratio,
                                    "move_pct":          size_pct,
                                    "trigger_epoch":    now_epoch,
                                    "candles_5m_elapsed": 1,
                                }
                                with _alerts_lock:
                                    _clear_stale_alerts_under_lock()
                                    if not any(a["symbol"] == symbol and a["grade"] == "5M Cross" for a in _triggered_alerts):
                                        _triggered_alerts.append(alert)

                                try:
                                    from server import socketio
                                    socketio.emit('live_breakout_alert', alert)
                                except Exception:
                                    pass

                # Case 2: Pre-Cross 5M Alert — 2-candle + CPR boundary validation
                # Logic mirrors scan_pre_ema_cross.py (validated; vol logged only, no vol gate)
                # ISOLATED: only fires to _triggered_alerts / live_breakout_alert
                # NO impact to res[state_5m], res[alignment], res[cross_5m] — Bulls/Bears count safe
                elif cross == 'none' and len(candles) >= 2 and open_now > 0:
                    prev_c   = candles[-2]
                    o1       = float(prev_c.get('open',  0.0) or 0.0)
                    c1_close = float(prev_c.get('close', 0.0) or 0.0)
                    o2       = float(open_now)
                    c2_close = float(ltp_now)

                    move_pct_c1 = (abs(c1_close - o1) / o1 * 100.0) if o1 > 0 else 0.0
                    move_pct_c2 = (abs(c2_close - o2) / o2 * 100.0) if o2 > 0 else 0.0

                    # Both candles must have meaningful bodies (C1 ≥ 0.20%, C2 ≥ 0.30%)
                    if move_pct_c1 >= 0.20 and move_pct_c2 >= 0.30:
                        closes_5m = [c.get('close', 0) for c in candles]
                        ema9_5m  = compute_ema(closes_5m, 9)
                        ema21_5m = compute_ema(closes_5m, 21)
                        if ema9_5m and ema21_5m and len(ema9_5m) == len(closes_5m) and len(ema21_5m) == len(closes_5m):
                            e9_last  = ema9_5m[-1]
                            e21_last = ema21_5m[-1]
                            if e9_last is not None and e21_last is not None and c2_close > 0:
                                gap_pct = (abs(e9_last - e21_last) / c2_close) * 100.0
                                if gap_pct <= 0.30:
                                    # --- CPR boundary (graceful: fire without CPR if cache cold) ---
                                    cpr_bottom = None
                                    try:
                                        cpr = get_cpr_pivots(symbol)
                                        if cpr:
                                            cpr_bottom = min(cpr['bc'], cpr['tc'])
                                    except Exception:
                                        cpr_bottom = None

                                    # 2-candle color + extension + EMA-side checks
                                    is_bull = (
                                        (c1_close > o1) and          # C1 green
                                        (c2_close > o2) and          # C2 green
                                        (c2_close > c1_close) and    # C2 extends above C1
                                        (e9_last < e21_last)         # EMA9 below EMA21 (pre-cross)
                                    )
                                    is_bear = (
                                        (c1_close < o1) and          # C1 red
                                        (c2_close < o2) and          # C2 red
                                        (c2_close < c1_close) and    # C2 extends below C1
                                        (e9_last > e21_last)         # EMA9 above EMA21 (pre-cross)
                                    )

                                    # Apply CPR filter only when data is available
                                    if cpr_bottom is not None:
                                        if is_bull:
                                            is_bull = is_bull and (c2_close > cpr_bottom)
                                        if is_bear:
                                            is_bear = is_bear and (c2_close < cpr_bottom)

                                    pre_dir = "bullish" if is_bull else ("bearish" if is_bear else None)

                                    if pre_dir:
                                        pre_key = f"{symbol}_precross_{slot_str}"
                                        with _ema_cross_dedup_lock:
                                            already_pre_alerted = pre_key in _ema_cross_dedup
                                            if not already_pre_alerted:
                                                if slot_str <= "15:15" and current_hm <= "15:15":
                                                    _ema_cross_dedup[pre_key] = now_epoch
                                                    _send_telegram_pre_cross_5m(
                                                        symbol, pre_dir, round(c2_close, 2),
                                                        vol_ratio, round(move_pct_c2, 2), slot_str
                                                    )

                                                    alert = {
                                                        "symbol":             symbol,
                                                        "direction":          pre_dir,
                                                        "time":               now_ist().strftime("%H:%M:%S"),
                                                        "ltp":                round(c2_close, 2),
                                                        "grade":              "Pre-Cross 5M",
                                                        "vol_multiplier":     round(vol_ratio, 2) if vol_ratio else 0.0,
                                                        "move_pct":           round(move_pct_c2, 2),
                                                        "trigger_epoch":      now_epoch,
                                                        "candles_5m_elapsed": 1,
                                                        "cpr_bottom":         round(cpr_bottom, 2) if cpr_bottom else None,
                                                    }
                                                    with _alerts_lock:
                                                        _clear_stale_alerts_under_lock()
                                                        if not any(a["symbol"] == symbol and a["grade"] == "Pre-Cross 5M" for a in _triggered_alerts):
                                                            _triggered_alerts.append(alert)

                                                    try:
                                                        from server import socketio
                                                        socketio.emit('live_breakout_alert', alert)
                                                    except Exception:
                                                        pass
        
        # Compute intraday session linearity on 15m candles
        if label == '15m':
            active_date_str = _get_active_trading_date(now_ist())
            lin_score, net_move = _calculate_intraday_linearity(candles, active_date_str)
            res["linearity_score"] = lin_score
            res["net_movement"] = net_move
            
        # Piggyback squeeze calculations on 5m candles to avoid extra API calls
        if label == '5m':
            # EMA Collision — two-phase: watchlist (zone) + alert (confirmed)
            try:
                in_zone, collision_dir, gap_pct = _check_ema_collision_state(candles)
                res["ema_collision"] = collision_dir
                ltp_now = candles[-1]['close'] if candles else 0

                # Phase 1: update collision watchlist
                with _collision_wl_lock:
                    if in_zone and not collision_dir:  # in zone but not yet confirmed
                        _collision_watchlist[symbol] = {
                            "ltp":         round(ltp_now, 2),
                            "ema_gap_pct": gap_pct,
                            "time":        now_ist().strftime("%H:%M:%S"),
                        }
                    else:
                        _collision_watchlist.pop(symbol, None)  # confirmed or exited zone

                # Phase 2: emit alert on confirmation (requires vol_ratio >= 2.0x and move_pct >= 0.20%)
                if collision_dir:
                    last_c = candles[-1] if candles else {}
                    c_open = last_c.get('open', 0.0) or 0.0
                    c_close = last_c.get('close', 0.0) or 0.0
                    c_vol = last_c.get('volume', 0.0) or 0.0
                    move_pct_5m = (abs(c_close - c_open) / c_close * 100.0) if c_close > 0 else 0.0

                    v_ratio = None
                    if slot_str:
                        try:
                            import volume_baseline
                            v_ratio, _ = volume_baseline.get_vol_ratio(symbol, '5m', slot_str, c_vol)
                        except Exception:
                            v_ratio = None
                    if v_ratio is None and len(candles) >= 20:
                        v_20_avg = sum([c.get('volume', 0.0) or 0.0 for c in candles[-21:-1]]) / 20.0
                        if v_20_avg > 0:
                            v_ratio = c_vol / v_20_avg

                    effective_vol_ratio = v_ratio if v_ratio is not None else 0.0

                    current_hm = now_ist().strftime("%H:%M")
                    if slot_str and slot_str <= "15:15" and current_hm <= "15:15" and effective_vol_ratio >= MIN_ALERT_VOLUME_RATIO and move_pct_5m >= MIN_ALERT_MOVE_PCT_5M:
                        _emit_collision_alert(symbol, collision_dir, ltp_now, effective_vol_ratio, move_pct_5m)
                    else:
                        logging.info("[EMA Collision Filtered] %s %s confirmed break ignored (vol_ratio=%.2fx, move_pct=%.2f%%)",
                                     symbol, collision_dir, effective_vol_ratio, move_pct_5m)
            except Exception:
                res["ema_collision"] = None

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

            # Store raw candle details for Nifty constituent contribution reuse
            if candles:
                last_c = candles[-1]
                res["last_candle_close"] = last_c.get('close', 0.0)
                res["last_candle_open"] = last_c.get('open', 0.0)
                res["last_candle_vol"] = last_c.get('volume', 0)
                res["prev_candle_close"] = candles[-2].get('close', last_c.get('close', 0.0)) if len(candles) >= 2 else last_c.get('close', 0.0)

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
                                    import volume_baseline
                                    volume_baseline.ensure_baselines_warm(kite, token_map)
                        except Exception as e:
                            logging.warning(f"[Volume Baseline] Out-of-hours Case 1 baseline warm trigger failed: {e}")

                        time.sleep(300)
                        continue

                    if _eod_saved_date == today_str:
                        # Case 2: already did EOD scan/save today but crossovers are empty → sleep
                        try:
                            kite = _get_kite()
                            if kite:
                                token_map = _get_token_map()
                                if token_map:
                                    import volume_baseline
                                    volume_baseline.ensure_baselines_warm(kite, token_map)
                        except Exception as e:
                            logging.warning(f"[Volume Baseline] Out-of-hours Case 2 baseline warm trigger failed: {e}")
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

                # Ensure volume baselines are warm before scanning
                try:
                    token_map_bl = _get_token_map()
                    if token_map_bl:
                        import volume_baseline
                        volume_baseline.ensure_baselines_warm(kite, token_map_bl)
                except Exception as e:
                    logging.warning(f"[Volume Baseline] Pre-scan baseline warm trigger failed: {e}")
                
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

                # Trigger Nifty index candle analysis — DISABLED (board integration removed)
                # To re-enable: uncomment the 3 lines below
                try:
                    pass  # import nifty_candle_analyzer
                    # nifty_time_str = now.strftime("%Y-%m-%d %H:%M:%S")
                    # nifty_candle_analyzer.analyze_and_store_candle(kite, temp_crossovers, nifty_time_str)
                except Exception as ex:
                    logging.warning(f"[Nifty Analyzer] Execution failed during scan: {ex}")

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
                                import volume_baseline
                                volume_baseline.ensure_baselines_warm(kite, token_map)
                    except Exception as e:
                        logging.warning(f"[Volume Baseline] Post-scan warm trigger failed: {e}")
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

        # Valid if the snapshot is from the most recent trading session
        # _get_expected_trading_date() correctly handles weekends:
        #   e.g. on Sunday it returns Friday, so Friday's snapshot is accepted.
        last_trading_date = _get_expected_trading_date(now)
        if snap_date == last_trading_date and last_update_hour >= 9:
            is_valid = True
        else:
            is_valid = False
            logging.info(
                f"[EMA Crossover] EOD snapshot rejected — "
                f"snap_date={snap_date}, last_trading_date={last_trading_date}, "
                f"captured_hour={last_update_hour} — skipped."
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
    
    # Trigger baseline warm on startup if cache is not yet warmed today
    try:
        kite = _get_kite()
        if kite:
            token_map = _get_token_map()
            if token_map:
                import volume_baseline
                volume_baseline.ensure_baselines_warm(kite, token_map)
    except Exception as e:
        logging.warning(f"[Volume Baseline] Startup warm trigger failed: {e}")

    if _ema_thread is None or not _ema_thread.is_alive():
        logging.info("[EMA Crossover Scanner] Spawning background thread...")
        print("[EMA Crossover Scanner] Spawning background thread...")
        _ema_thread = threading.Thread(target=_ema_crossover_loop, daemon=True)
        _ema_thread.start()


# ── Daily EMA 9 vs EMA 21 Crossover Count (DXCNT) Cache ──────────────────────
_dxcnt_cache = {}
_dxcnt_date = None
_dxcnt_lock = threading.Lock()

def get_daily_dxcnt_map():
    """
    Returns cached dict of {symbol: dxcnt_int} for all F&O stocks.
    dxcnt_int > 0: Bullish state (EMA9 > EMA21) count of daily candles (+N).
    dxcnt_int < 0: Bearish state (EMA9 < EMA21) count of daily candles (-N).
    Persisted in SQLite database for instant (<5ms) startup loading.
    """
    global _dxcnt_cache, _dxcnt_date
    import datetime as _dt
    import sqlite3
    today = _dt.date.today()
    today_str = today.strftime('%Y-%m-%d')

    with _dxcnt_lock:
        if _dxcnt_cache and _dxcnt_date == today:
            return dict(_dxcnt_cache)

    db_path = os.path.join(os.path.dirname(__file__), "tradesignal_cache.db")

    # 1. Try reading from SQLite cache first
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS dxcnt_daily_cache (
                date TEXT PRIMARY KEY,
                data_json TEXT,
                updated_at TEXT
            )
        """)
        cursor.execute("SELECT data_json FROM dxcnt_daily_cache WHERE date = ?", (today_str,))
        row = cursor.fetchone()
        conn.close()
        if row and row[0]:
            loaded_map = json.loads(row[0])
            with _dxcnt_lock:
                _dxcnt_cache = loaded_map
                _dxcnt_date = today
            return dict(_dxcnt_cache)
    except Exception as e:
        logging.warning(f"[DXCNT Cache] SQLite read failed: {e}")

    # 2. Build via Kite API if not yet cached in SQLite today
    try:
        from db_instruments import get_fno_symbols
        from indicators import compute_ema
        from server import get_kite, get_historical_candles
        kite = get_kite()
        if not kite:
            return dict(_dxcnt_cache) if _dxcnt_cache else {}

        symbols = get_fno_symbols()
        indices = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
        stock_symbols = [s for s in symbols if s not in indices]

        new_map = {}
        for sym in stock_symbols:
            try:
                candles = get_historical_candles(kite, sym, "day", days_back=300)
                if not candles or len(candles) < 21:
                    continue
                closes = [c['close'] for c in candles if c.get('close') is not None]
                if len(closes) < 21:
                    continue

                ema9 = compute_ema(closes, 9)
                ema21 = compute_ema(closes, 21)
                if not ema9 or not ema21 or len(ema9) != len(closes) or len(ema21) != len(closes):
                    continue

                e9_last = round(float(ema9[-1]), 2)
                e21_last = round(float(ema21[-1]), 2)

                is_bullish = e9_last > e21_last
                streak = 0
                for i in range(len(closes) - 1, -1, -1):
                    if ema9[i] is None or ema21[i] is None:
                        break
                    e9 = float(ema9[i])
                    e21 = float(ema21[i])
                    if is_bullish and (e9 > e21):
                        streak += 1
                    elif (not is_bullish) and (e9 < e21):
                        streak += 1
                    else:
                        break

                new_map[sym] = streak if is_bullish else -streak
            except Exception:
                continue

        if new_map:
            # Save to SQLite for sub-millisecond future reads
            try:
                conn = sqlite3.connect(db_path, timeout=5.0)
                cursor = conn.cursor()
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS dxcnt_daily_cache (
                        date TEXT PRIMARY KEY,
                        data_json TEXT,
                        updated_at TEXT
                    )
                """)
                cursor.execute(
                    "INSERT OR REPLACE INTO dxcnt_daily_cache (date, data_json, updated_at) VALUES (?, ?, ?)",
                    (today_str, json.dumps(new_map), now_ist().strftime("%Y-%m-%d %H:%M:%S"))
                )
                conn.commit()
                conn.close()
            except Exception as ex:
                logging.warning(f"[DXCNT Cache] SQLite save failed: {ex}")

        with _dxcnt_lock:
            _dxcnt_cache = new_map
            _dxcnt_date = today
        return dict(_dxcnt_cache)
    except Exception as e:
        logging.warning(f"[DXCNT Cache] Failed to build DXCNT map: {e}")
        return dict(_dxcnt_cache) if _dxcnt_cache else {}

