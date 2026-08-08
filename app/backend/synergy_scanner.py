"""
================================================================================
⚡ F&O Synergy Scanner — WebSocket-driven Real-time Institutional Alerts
================================================================================
Uses ONE server-global KiteTicker connection (~1,155 tokens: futures + options).
On each tick batch: updates in-memory state, recomputes 7-profile synergy matrix,
and pushes Socket.IO 'synergy_alert' events when BUY-class profiles fire.

Zero REST API rate-limit usage during scanning (pure WebSocket ticks).

Token Budget (±5 strike window):
  ~220 stocks × 1 near-month future =   220
  ~220 stocks × 10 options (ATM±5)  = 2,200
  Total: ~2,420  (Kite limit: 3,000 per connection) ✅

Startup: deferred 2 minutes for system startup and stability.
================================================================================
"""

import os
import threading
import time
import logging
from datetime import datetime, date, timedelta
from collections import Counter, deque

log = logging.getLogger("SynergyScanner")

# ── Telegram Helper ───────────────────────────────────────────────────
def _send_telegram(msg):
    """Send a Telegram message via the shared server utility (same as option_gainers_scanner)."""
    pass


# 30-minute cooldown per symbol — prevents duplicate Telegram alerts on repeated ticks
_TELEGRAM_COOLDOWN_SEC = 1800  # 30 minutes
_telegram_last_sent    = {}    # {symbol: timestamp_float}

# ── Shared State ───────────────────────────────────────────────────────────────
_state_lock      = threading.Lock()
_token_info      = {}   # {token_int: {symbol, role: futures/ce/pe, strike}}
_sym_state       = {}   # {symbol: {futures_*, ce_strikes:{strike:{ltp,prev_close,oi,oi_day_change}}, pe_strikes:{...}}}
_synergy_results = {}   # {symbol: full result dict}
_prev_profiles   = {}   # {symbol: str} — detect profile changes
_ltp_history     = {}   # {symbol: deque(maxlen=20)} — rolling LTP for Efficiency Ratio
_socketio_ref    = None
_kws             = None
_is_running      = False
_scanner_thread  = None

_cpr_cache       = {}
_cpr_cache_lock  = threading.Lock()
CPR_CACHE_FILE   = os.path.join(os.path.dirname(__file__), "cpr_cache.json")

def _load_cpr_cache_from_disk():
    global _cpr_cache
    if os.path.exists(CPR_CACHE_FILE):
        try:
            import json
            with open(CPR_CACHE_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    with _cpr_cache_lock:
                        _cpr_cache.update(data)
                    log.info(f"[Synergy] Loaded {len(data)} CPR pivots from disk cache.")
        except Exception as e:
            log.warning(f"[Synergy] Failed to load CPR cache from disk: {e}")

def _save_cpr_cache_to_disk():
    try:
        import json
        with _cpr_cache_lock:
            data = dict(_cpr_cache)
        with open(CPR_CACHE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        log.warning(f"[Synergy] Failed to save CPR cache to disk: {e}")

_load_cpr_cache_from_disk()

_last_computed_ltp = {}  # {symbol: spot_ltp}
_last_cross_time   = {}  # {symbol: timestamp_float}
_cpr_cross_alerts  = deque(maxlen=50) # Keep last 50 alerts

# Persistent state tracking for the Refined CPR State Machine
_prev_zones = {}          # {symbol: "Discount" | "Equilibrium" | "Premium"}
_active_cpr_signal = {}   # {symbol: "CE buy" | "PE buy" | "No trade"}
_cpr_signal_fire_ts = {}  # {symbol: float (timestamp)}

_opening_5m_cache = {}  # {symbol: "positive" | "negative" | "flat" | None}
_opening_5m_lock  = threading.Lock()
_opening_5m_date  = None  # date object to track daily reset



# Canonical index symbols — kept in this fixed display order on the dashboard
INDICES       = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}
INDEX_ORDER   = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"]


# ── Buildup Classifier ────────────────────────────────────────────────────────
def _classify(oi_day_change, ltp, prev_close):
    """4-way buildup matrix with graceful handling of zero OI change.
    Returns 'Flat' (not '–') when oi_day_change == 0 so the matrix can still
    fire near-match profiles even early in the session or on illiquid legs.
    Price threshold relaxed to 0.25% (was 0.5%) to catch smaller option moves."""
    if prev_close <= 0 or ltp <= 0:
        return "–"   # genuinely no data — still unclassifiable
    if oi_day_change == 0:
        return "Flat"  # neutral — no intraday OI change; keeps match alive
    price_up = ltp > prev_close * 1.0025   # relaxed from 1.005 → 1.0025
    oi_up    = oi_day_change > 0
    if oi_up and price_up:     return "Long Buildup"
    if oi_up and not price_up: return "Short Buildup"
    if not oi_up and price_up: return "Short Covering"
    return "Long Unwinding"


def _apply_matrix(f_b, ce_b, pe_b):
    """Map 3-way buildup to 7-Profile Master Combination Matrix.

    Matching strategy:
      Full match  — all 3 legs exactly correct  → highest conviction label
      Near match  — 2 key legs match + 3rd is 'Flat' or '–' (early-session
                    data gap or illiquid leg) → 'Emerging' label, still BUY
    'Flat' and '–' are treated as neutral — they no longer kill a signal.
    """
    def _neutral(v):
        return v in ("–", "Flat")

    # ── Full BUY-class matches (highest conviction) ───────────────────────────
    if f_b == "Long Buildup"   and ce_b == "Short Covering" and pe_b == "Short Buildup":
        return "🟢 Bull-Lock (Full Bullish Harmony)",        "CALL BUY (At Next Strike)",         True
    if f_b == "Short Buildup"  and ce_b == "Short Buildup"  and pe_b == "Short Covering":
        return "🔴 Bear-Lock (Full Bearish Harmony)",        "PUT BUY (At Next Strike)",          True
    if f_b == "Short Covering" and ce_b == "Short Covering" and pe_b == "Short Buildup":
        return "⚡ V-Squeeze (Fast Intraday Rally)",         "FAST CALL BUY (Scalp Only)",        True
    if f_b == "Long Unwinding" and ce_b == "Short Buildup"  and pe_b == "Short Covering":
        return "💀 Floor Collapse (Panic Flush)",            "PUT BUY / EXIT LONGS IMMEDIATELY",  True

    # ── Near-match BUY variants (2 key legs + 1 neutral leg) ─────────────────
    # Bull-Lock: Futures up + PE wall building; CE wall not yet confirmed
    if f_b == "Long Buildup" and _neutral(ce_b) and pe_b == "Short Buildup":
        return "🟢 Bull-Lock (Emerging — CE Pending)",  "CALL BUY (Watch CE Wall Confirm)",      True
    # Bull-Lock: Futures up + CE covering; PE wall not yet confirmed
    if f_b == "Long Buildup" and ce_b == "Short Covering" and _neutral(pe_b):
        return "🟢 Bull-Lock (Emerging — PE Pending)",  "CALL BUY (Watch PE Wall Confirm)",      True

    # Bear-Lock: Futures down + CE wall building; PE not yet confirmed
    if f_b == "Short Buildup" and ce_b == "Short Buildup" and _neutral(pe_b):
        return "🔴 Bear-Lock (Emerging — PE Pending)",  "PUT BUY (Watch PE Wall Confirm)",       True
    # Bear-Lock: Futures down + PE covering; CE not yet confirmed
    if f_b == "Short Buildup" and _neutral(ce_b) and pe_b == "Short Covering":
        return "🔴 Bear-Lock (Emerging — CE Pending)",  "PUT BUY (Watch CE Wall Confirm)",       True

    # V-Squeeze: Futures covering + PE building; CE not yet confirmed
    if f_b == "Short Covering" and _neutral(ce_b) and pe_b == "Short Buildup":
        return "⚡ V-Squeeze (Emerging)",               "PREPARE CALL BUY (Confirm CE Wall)",   True

    # Floor Collapse: Futures unwinding + CE building; PE not yet confirmed
    if f_b == "Long Unwinding" and ce_b == "Short Buildup" and _neutral(pe_b):
        return "💀 Floor Collapse (Emerging)",          "PREPARE PUT BUY (Confirm PE Wall)",    True

    # ── Non-BUY profiles ─────────────────────────────────────────────────────
    if f_b == "Long Buildup"  and ce_b == "Short Buildup" and pe_b == "Long Unwinding":
        return "🟡 Institutional Trap (Ceiling Exhaustion)", "BOOK PROFIT / HOLD CASH",          False
    if f_b == "Short Buildup" and ce_b == "Long Unwinding" and pe_b == "Short Buildup":
        return "🪤 Bear Trap (Floor Holding)",               "SELL PUTS / WAIT",                 False
    # Range Lock: both walls building regardless of futures direction
    if ce_b == "Short Buildup" and pe_b == "Short Buildup":
        return "🔁 Range Lock (Institutional Pinning)",      "SELL STRADDLE or STRANGLE",        False

    return "Mixed Flow (No Setup)", "WAIT", False


# ── Per-symbol Synergy Computation ────────────────────────────────────────────
def _efficiency_ratio(prices):
    """Perry Kaufman Efficiency Ratio (0–1): 1=linear, 0=choppy.
    Returns None if fewer than 5 samples or stock is flat."""
    if len(prices) < 5:
        return None
    lst = list(prices)
    net_move   = abs(lst[-1] - lst[0])
    total_path = sum(abs(lst[i] - lst[i-1]) for i in range(1, len(lst)))
    if total_path == 0:
        return None
    return round(net_move / total_path, 3)


def _compute_synergy(symbol):
    # GAP FIX 1: copy the mutable ss dict under lock to prevent race conditions
    # with _on_ticks() modifying it concurrently on tick callbacks.
    with _state_lock:
        raw = _sym_state.get(symbol)
        if not raw:
            return None
        # Shallow-copy top-level fields + deep-copy the strike dicts to avoid races
        ss = {
            "futures_ltp":           raw.get("futures_ltp", 0),
            "futures_prev_close":    raw.get("futures_prev_close", 0),
            "futures_open":          raw.get("futures_open", 0),
            "futures_high":          raw.get("futures_high", 0),
            "futures_low":           raw.get("futures_low", 0),
            "spot_ltp":              raw.get("spot_ltp", 0),
            "spot_prev_close":       raw.get("spot_prev_close", 0),
            "spot_open":             raw.get("spot_open", 0),
            "spot_high":             raw.get("spot_high", 0),
            "spot_low":              raw.get("spot_low", 0),
            "spot_volume":           raw.get("spot_volume", 0),
            "futures_oi_day_change": raw.get("futures_oi_day_change", 0),
            "ce_strikes":            dict(raw.get("ce_strikes", {})),
            "pe_strikes":            dict(raw.get("pe_strikes", {})),
        }
        ltp_hist = list(_ltp_history.get(symbol, []))
        prev_ltp = _last_computed_ltp.get(symbol, 0)

    futures_ltp     = ss["futures_ltp"]
    futures_prev_cl = ss["futures_prev_close"]
    futures_oi_chg  = ss["futures_oi_day_change"]

    # GAP FIX 2: guard against no-tick state (futures not yet received)
    if futures_ltp <= 0:
        return None

    futures_buildup = _classify(futures_oi_chg, futures_ltp, futures_prev_cl)

    # Use active spot price (fall back to futures LTP as proxy if not ticked yet)
    spot_ltp     = ss["spot_ltp"] if ss["spot_ltp"] > 0 else futures_ltp
    spot_prev_cl = ss["spot_prev_close"] if ss["spot_prev_close"] > 0 else futures_prev_cl
    spot_open    = ss["spot_open"] if ss["spot_open"] > 0 else ss["futures_open"]
    spot_high    = ss["spot_high"] if ss["spot_high"] > 0 else ss["futures_high"]
    spot_low     = ss["spot_low"] if ss["spot_low"] > 0 else ss["futures_low"]

    spot_proxy = spot_ltp

    opening_5m_dir = None
    now_ist_dt = datetime.now()
    from datetime import time as dt_time
    if now_ist_dt.time() < dt_time(9, 20):
        if spot_ltp > 0 and spot_open > 0:
            opening_5m_dir = "positive" if spot_ltp > spot_open else "negative" if spot_ltp < spot_open else "flat"
    else:
        opening_5m_dir = _opening_5m_cache.get(symbol)
        if not opening_5m_dir and spot_ltp > 0 and spot_open > 0:
            opening_5m_dir = "positive" if spot_ltp > spot_open else "negative" if spot_ltp < spot_open else "flat"


    dealing_range_pct = 50.0
    if spot_high > spot_low:
        dealing_range_pct = round(((spot_ltp - spot_low) / (spot_high - spot_low)) * 100, 2)

    # ── Advanced Intraday Confluence: CPR & PDH/PDL Dealing Range ──
    dr_pct          = None
    cpr_pos         = "Inside CPR"
    intraday_zone   = "Equilibrium"
    intraday_action = "No trade"
    pdh, pdl, tc, bc = None, None, None, None

    cpr_data = _cpr_cache.get(symbol)
    if cpr_data:
        pdh = cpr_data["pdh"]
        pdl = cpr_data["pdl"]
        tc  = cpr_data["tc"]
        bc  = cpr_data["bc"]
        if pdh > pdl:
            dr_pct = round(((spot_ltp - pdl) / (pdh - pdl)) * 100, 2)
        
        if spot_ltp < bc:
            cpr_pos = "Below BC"
        elif spot_ltp > tc:
            cpr_pos = "Above TC"
        else:
            cpr_pos = "Inside CPR"

        # ── CPR Crossing Alert Detection ──
        # Strict: CPR alerts must ONLY be triggered by the actual spot price.
        actual_spot_ltp = ss["spot_ltp"]
        if prev_ltp > 0 and actual_spot_ltp > 0 and tc and bc:
            crossing_type = None
            message = ""
            if prev_ltp <= tc and actual_spot_ltp > tc:
                crossing_type = "tc_cross_above"
                message = "Crossed Above TC"
            elif prev_ltp >= tc and actual_spot_ltp < tc:
                crossing_type = "tc_cross_below"
                message = "Crossed Below TC"
            elif prev_ltp < bc and actual_spot_ltp >= bc:
                crossing_type = "bc_cross_above"
                message = "Crossed Above BC"
            elif prev_ltp >= bc and actual_spot_ltp < bc:
                crossing_type = "bc_cross_below"
                message = "Crossed Below BC"

            if crossing_type:
                alert_data = {
                    "symbol": symbol,
                    "timestamp": datetime.now().strftime("%H:%M:%S"),
                    "ltp": round(actual_spot_ltp, 2),
                    "prev_ltp": round(prev_ltp, 2),
                    "tc": round(tc, 2),
                    "bc": round(bc, 2),
                    "type": crossing_type,
                    "message": message
                }
                with _state_lock:
                    _cpr_cross_alerts.append(alert_data)
                if _socketio_ref:
                    try:
                        _socketio_ref.emit("cpr_cross_alert", alert_data)
                        log.info(f"[Synergy] ⚡ CPR Cross: {symbol} {message} @ ₹{round(actual_spot_ltp, 2)}")
                    except Exception as e:
                        log.warning(f"[Synergy] CPR Cross emit error: {e}")

        if tc and bc:
            lower_cpr = min(bc, tc)
            upper_cpr = max(bc, tc)
            
            # 1. Determine Current Zone
            if spot_ltp < lower_cpr:
                intraday_zone = "Discount Zone"
            elif spot_ltp > upper_cpr:
                intraday_zone = "Premium Zone"
            else:
                intraday_zone = "Equilibrium Zone"
                
            # 2. Check 5-Minute Cool-down Period (300 seconds)
            now_ts = time.time()
            last_fire_ts = _cpr_signal_fire_ts.get(symbol, 0)
            current_signal = _active_cpr_signal.get(symbol, "No trade")
            
            if now_ts - last_fire_ts < 300.0:
                # Maintain the active signal to prevent noise
                intraday_action = current_signal
            else:
                # Cooldown period has elapsed: run the state machine transitions
                prev_zone = _prev_zones.get(symbol)
                new_signal = "No trade"
                
                if prev_zone is not None and prev_zone != intraday_zone:
                    # Zone transition occurred
                    if prev_zone == "Discount Zone":
                        if intraday_zone in ("Equilibrium Zone", "Premium Zone"):
                            # Rule 3b: BC Reclaim (Spot breaks above BC and holds)
                            new_signal = "CE buy"
                            
                    elif prev_zone == "Equilibrium Zone":
                        if intraday_zone == "Discount Zone":
                            # Rule 2c: Spot breaks below BC and holds
                            new_signal = "PE buy"
                        elif intraday_zone == "Premium Zone":
                            # Rule 2a: Spot breaks above TC and holds
                            new_signal = "CE buy"
                            
                    elif prev_zone == "Premium Zone":
                        if intraday_zone in ("Equilibrium Zone", "Discount Zone"):
                            # Rule 3c: TC breaks (spot falls below TC and holds below)
                            new_signal = "PE buy"
                else:
                    # Price remained inside the same zone. Check for rejections or failed breakout attempts:
                    if intraday_zone == "Discount Zone":
                        # 1. BC Reclaim Fails: broke above BC but fell back below
                        if prev_ltp >= lower_cpr:
                            new_signal = "PE buy"
                        # 2. BC Rejection: hit BC from below and rejected
                        elif prev_ltp > 0 and (lower_cpr - prev_ltp) / lower_cpr < 0.0015 and spot_ltp < prev_ltp:
                            new_signal = "PE buy"
                            
                    elif intraday_zone == "Premium Zone":
                        # 1. TC Holds as Support: dipped to TC and bounced
                        if prev_ltp <= upper_cpr:
                            new_signal = "CE buy"
                        # 2. TC Rejection: hit TC from above and fell below
                        elif prev_ltp > 0 and (prev_ltp - upper_cpr) / upper_cpr < 0.0015 and spot_ltp < prev_ltp:
                            new_signal = "PE buy"
                
                # Commit state and fire signal if updated
                _prev_zones[symbol] = intraday_zone
                if new_signal != current_signal:
                    _active_cpr_signal[symbol] = new_signal
                    _cpr_signal_fire_ts[symbol] = now_ts
                    intraday_action = new_signal
                else:
                    intraday_action = current_signal

    # ── Multi-Layer Weighted Confluence Logic ──
    pwh, pwl, weekly_dr = None, None, None
    multi_action = "No trade"
    is_confluence_active = False

    if cpr_data:
        pwh = cpr_data.get("pwh")
        pwl = cpr_data.get("pwl")
        
        if pwh is not None and pwl is not None and pwh > pwl:
            weekly_dr = round(((spot_ltp - pwl) / (pwh - pwl)) * 100, 2)
            
        if dr_pct is not None:
            if dr_pct <= 30.0:
                # CE bias confirmed
                if weekly_dr is not None:
                    if weekly_dr <= 30.0:
                        # Weekly also discount = full size
                        if spot_ltp <= bc:
                            multi_action = "CE BUY* (Full Size)"
                            is_confluence_active = True
                        else:
                            multi_action = "CE Bias (Wait for BC)"
                    elif weekly_dr >= 70.0:
                        # Weekly premium = skip
                        multi_action = "SKIP (Low Conviction CE)"
                    else:
                        # Weekly neutral = half size
                        if spot_ltp <= bc:
                            multi_action = "CE BUY* (Half Size)"
                            is_confluence_active = True
                        else:
                            multi_action = "CE Bias (Wait for BC)"
            elif dr_pct >= 70.0:
                # PE bias confirmed
                if weekly_dr is not None:
                    if weekly_dr >= 70.0:
                        # Weekly also premium = full size
                        if spot_ltp >= tc:
                            multi_action = "PE BUY* (Full Size)"
                            is_confluence_active = True
                        else:
                            multi_action = "PE Bias (Wait for TC)"
                    elif weekly_dr <= 30.0:
                        # Weekly discount = skip
                        multi_action = "SKIP (Low Conviction PE)"
                    else:
                        # Weekly neutral = half size
                        if spot_ltp >= tc:
                            multi_action = "PE BUY* (Half Size)"
                            is_confluence_active = True
                        else:
                            multi_action = "PE Bias (Wait for TC)"
            else:
                # Daily DR% 30-70% (Neutral)
                if spot_ltp > tc:
                    multi_action = "CE BREAKOUT"
                elif spot_ltp < bc:
                    multi_action = "PE BREAKDOWN"
                else:
                    multi_action = "No trade"

    ce_candidates = {k: v for k, v in ss["ce_strikes"].items() if k >= spot_proxy and v.get("oi", 0) > 0}
    pe_candidates = {k: v for k, v in ss["pe_strikes"].items() if k <= spot_proxy and v.get("oi", 0) > 0}

    ce_wall = max(ce_candidates.values(), key=lambda x: x["oi"], default={})
    pe_wall = max(pe_candidates.values(), key=lambda x: x["oi"], default={})

    ce_buildup = _classify(ce_wall.get("oi_day_change", 0), ce_wall.get("ltp", 0), ce_wall.get("prev_close", 0))
    pe_buildup = _classify(pe_wall.get("oi_day_change", 0), pe_wall.get("ltp", 0), pe_wall.get("prev_close", 0))

    profile, action, is_buy = _apply_matrix(futures_buildup, ce_buildup, pe_buildup)

    er       = _efficiency_ratio(ltp_hist)
    er_tier  = ("strong"   if er and er >= 0.65 else
                "moderate" if er and er >= 0.50 else
                "choppy"   if er is not None else None)

    open_gap_pct = round(((spot_open - spot_prev_cl) / spot_prev_cl) * 100, 2) if spot_prev_cl > 0 and spot_open > 0 else None

    # Store only actual spot price for future crossover comparison to prevent futures crossover false positives
    with _state_lock:
        if ss["spot_ltp"] > 0:
            _last_computed_ltp[symbol] = ss["spot_ltp"]

    rvol_ratio = None
    avg_volume_m = None
    try:
        from session_utils import now_ist
        _now_ist = now_ist()
        _elapsed = (_now_ist.hour - 9) * 60 + _now_ist.minute - 15  # minutes since 09:15

        from option_gainers_scanner import get_avg_volume
        avg_vol = get_avg_volume(symbol)
        curr_vol = ss.get("spot_volume", 0)

        if avg_vol and avg_vol > 0:
            avg_volume_m = round(avg_vol / 1_000_000, 2)  # e.g. 5.08
            if curr_vol > 0:
                if _elapsed > 375 or _now_ist.hour >= 15:
                    rvol_ratio = round(curr_vol / avg_vol, 1)
                elif _elapsed > 0:
                    expected = avg_vol * (_elapsed / 375.0)
                    if expected > 0:
                        rvol_ratio = round(curr_vol / expected, 1)
    except Exception as ex:
        log.warning(f"[Synergy] RVOL calc error for {symbol}: {ex}")

    return {
        "symbol":           symbol,
        "ltp":              round(spot_ltp, 2),
        "spot_change_pct":  round(((spot_ltp - spot_prev_cl) / spot_prev_cl) * 100, 2) if spot_prev_cl > 0 else None,
        "spot_high":        round(spot_high, 2) if spot_high > 0 else None,
        "spot_low":         round(spot_low, 2) if spot_low > 0 else None,
        "dealing_range_pct": dealing_range_pct if (spot_high > spot_low) else None,
        "dr_pct":           dr_pct,
        "cpr_pos":          cpr_pos,
        "intraday_zone":    intraday_zone,
        "intraday_action":  intraday_action,
        "pdh":              pdh,
        "pdl":              pdl,
        "tc":               tc,
        "bc":               bc,
        "pwh":              pwh,
        "pwl":              pwl,
        "weekly_dr":        weekly_dr,
        "multi_action":     multi_action,
        "is_confluence_active": is_confluence_active,
        "open_gap_pct":     open_gap_pct,
        "efficiency_ratio": er,
        "er_tier":          er_tier,
        "futures_buildup":  futures_buildup,
        "ce_buildup":       ce_buildup,
        "pe_buildup":       pe_buildup,
        "synergy_profile":  profile,
        "synergy_action":   action,
        "is_buy_signal":    is_buy,
        "is_index":         symbol in INDICES,
        "rvol_ratio":       rvol_ratio,
        "avg_volume_m":     avg_volume_m,
        "opening_5m_dir":   opening_5m_dir,
        "last_updated":     datetime.now().strftime("%H:%M:%S"),
    }


# ── Tick Handler ──────────────────────────────────────────────────────────────
def _on_ticks(ws, ticks):
    changed = set()

    with _state_lock:
        for tick in ticks:
            token = tick.get("instrument_token")
            info  = _token_info.get(token)
            if not info:
                continue

            sym    = info["symbol"]
            role   = info["role"]
            ltp    = float(tick.get("last_price", 0) or 0)
            oi     = int(tick.get("oi", 0) or 0)
            oi_chg = int(tick.get("oi_day_change", 0) or 0)
            volume = int(tick.get("volume", 0) or 0)
            ohlc   = tick.get("ohlc") or {}
            prev_cl = float(ohlc.get("close", 0) or 0)
            open_px = float(ohlc.get("open", 0) or 0)
            high_px = float(ohlc.get("high", 0) or 0)
            low_px  = float(ohlc.get("low", 0) or 0)

            if sym not in _sym_state:
                _sym_state[sym] = {"ce_strikes": {}, "pe_strikes": {}}
            ss = _sym_state[sym]

            if role == "futures":
                ss["futures_ltp"]           = ltp
                ss["futures_prev_close"]    = prev_cl
                ss["futures_open"]          = open_px
                ss["futures_high"]          = high_px
                ss["futures_low"]           = low_px
                ss["futures_oi"]            = oi
                ss["futures_oi_day_change"] = oi_chg
                if ltp > 0:
                    if sym not in _ltp_history:
                        _ltp_history[sym] = deque(maxlen=20)
                    _ltp_history[sym].append(ltp)
            elif role == "spot":
                ss["spot_ltp"]              = ltp
                ss["spot_prev_close"]       = prev_cl
                ss["spot_open"]             = open_px
                ss["spot_high"]             = high_px
                ss["spot_low"]              = low_px
                ss["spot_volume"]           = volume
            elif role in ("ce", "pe"):
                strike = info["strike"]
                ss[f"{role}_strikes"][strike] = {
                    "ltp": ltp, "prev_close": prev_cl,
                    "oi": oi, "oi_day_change": oi_chg,
                }
            changed.add(sym)

    # Compute synergy outside lock for performance
    for sym in changed:
        result = _compute_synergy(sym)
        if not result:
            continue

        prev = _prev_profiles.get(sym, "")
        new  = result["synergy_profile"]

        with _state_lock:
            _synergy_results[sym] = result
            _prev_profiles[sym]   = new

        # Push individual alert only when profile transitions TO a BUY-class signal
        # This is the primary real-time path — zero REST API calls to Kite
        if new != prev and result["is_buy_signal"] and _socketio_ref:
            try:
                _socketio_ref.emit("synergy_alert", result)
                log.info(f"[Synergy] 🔔 {sym} → {new}")
            except Exception as e:
                log.warning(f"[Synergy] Emit error: {e}")

            # ── Telegram alert (30-min cooldown per symbol) ─────────────────────
            now_ts = time.time()
            last_sent = _telegram_last_sent.get(sym, 0)
            if now_ts - last_sent >= _TELEGRAM_COOLDOWN_SEC:
                _telegram_last_sent[sym] = now_ts
                _send_synergy_telegram(sym, result)


def _send_synergy_telegram(symbol, result):
    """Format and send a Telegram alert for a BUY-class F&O Synergy signal."""
    profile = result.get("synergy_profile", "")
    action  = result.get("synergy_action", "")
    ltp     = result.get("ltp", "–")
    f_b     = result.get("futures_buildup", "–")
    ce_b    = result.get("ce_buildup", "–")
    pe_b    = result.get("pe_buildup", "–")
    ts      = result.get("last_updated", "")

    # Emoji prefix based on profile
    if "🟢" in profile:   urgency = "🟢 BULLISH LOCK"
    elif "🔴" in profile: urgency = "🔴 BEARISH LOCK"
    elif "⚡" in profile: urgency = "⚡ V-SQUEEZE"
    elif "💀" in profile: urgency = "💀 FLOOR COLLAPSE"
    else:                  urgency = "🔔 SYNERGY ALERT"

    msg = (
        f"{urgency} — #{symbol}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Profile : {profile}\n"
        f"📈 LTP     : ₹{ltp}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔷 Futures : {f_b}\n"
        f"🟥 CE Wall : {ce_b}\n"
        f"🟩 PE Wall : {pe_b}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 Action  : {action}\n"
        f"🕐 Time    : {ts} IST"
    )
    _send_telegram(msg)
    log.info(f"[Synergy] 📱 Telegram sent for {symbol}: {profile}")


def _on_ws_connect(ws, response, all_tokens):
    try:
        ws.subscribe(all_tokens)
        ws.set_mode(ws.MODE_FULL, all_tokens)
        log.info(f"[Synergy] Subscribed to {len(all_tokens)} tokens in FULL mode.")

        # ── Initialization alert — confirms WebSocket + Telegram are both live ──
        _send_telegram(
            f"⚡ F&O Synergy Scanner — LIVE\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ KiteTicker connected\n"
            f"📡 Streaming {len(all_tokens)} tokens\n"
            f"🕐 Started : {datetime.now().strftime('%H:%M:%S')} IST\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"BUY alerts will fire when Bull-Lock / Bear-Lock /\n"
            f"V-Squeeze / Floor Collapse profiles are detected.\n"
            f"Cooldown: 30 min per symbol."
        )
    except Exception as e:
        log.error(f"[Synergy] Subscribe error: {e}")


def _on_error(ws, code, reason):
    log.error(f"[Synergy] KiteTicker error {code}: {reason}")


def _on_close(ws, code, reason):
    log.warning(f"[Synergy] KiteTicker closed {code}: {reason}")


# ── Token Resolution ──────────────────────────────────────────────────────────
def _resolve_tokens(kite):
    """
    Dynamically discovers ALL F&O underlyings from the live NFO instrument list.
    Does NOT rely on the hardcoded FNO_SYMBOLS — auto-covers new NSE F&O additions.

    Returns: (all_tokens: list[int], token_info: dict[int -> {symbol, role, strike}])
    """
    from oi_scanner_routes import SPOT_MAP, get_all_instruments

    today = date.today()
    # get_all_instruments returns NFO + BFO — includes SENSEX / BANKEX
    instruments = get_all_instruments(kite)

    # ── Step 1: Discover ALL unique F&O underlyings with FUT + options ────────
    # Group by underlying name; only include names that have both FUT and CE/PE
    has_fut = set()
    has_opt = set()
    by_sym  = {}

    for inst in instruments:
        name  = inst.get("name", "")
        itype = inst.get("instrument_type", "")
        expiry = inst.get("expiry")
        if not name or itype not in ("FUT", "CE", "PE"):
            continue
        if not expiry or expiry < today:
            continue
        if itype == "FUT":
            has_fut.add(name)
        else:
            has_opt.add(name)
        by_sym.setdefault(name, []).append(inst)

    # Only underlyings with BOTH futures AND options are valid F&O instruments
    valid_syms = has_fut & has_opt
    log.info(f"[Synergy] Discovered {len(valid_syms)} F&O underlyings from live NFO instruments.")

    # ── Step 2: Fetch spot prices for all discovered underlyings ──────────────
    # Build spot query keys: indices use SPOT_MAP, stocks default to NSE:<SYM>
    spot_keys = [SPOT_MAP.get(s, f"NSE:{s}") for s in valid_syms]
    spot_prices = {}
    spot_tokens_map = {}  # {token_id -> symbol}
    spot_keys_list = list(spot_keys)
    for i in range(0, len(spot_keys_list), 400):
        try:
            batch = kite.ltp(spot_keys_list[i:i+400])
            for kite_key, val in batch.items():
                ltp_val = val.get("last_price", 0)
                tok_val = int(val.get("instrument_token", 0))
                # Reverse-map kite_key → F&O symbol name
                matched = False
                for sym in valid_syms:
                    if SPOT_MAP.get(sym, f"NSE:{sym}") == kite_key:
                        spot_prices[sym] = ltp_val
                        if tok_val > 0:
                            spot_tokens_map[tok_val] = sym
                        matched = True
                        break
                if not matched:
                    # Fallback: strip exchange prefix
                    raw = kite_key.split(":", 1)[1]
                    spot_prices[raw] = ltp_val
                    for sym in valid_syms:
                        if sym == raw:
                            if tok_val > 0:
                                spot_tokens_map[tok_val] = sym
                            break
        except Exception as e:
            log.warning(f"[Synergy] Spot LTP batch {i}: {e}")

    log.info(f"[Synergy] Got spot prices for {len(spot_prices)} / {len(valid_syms)} underlyings.")

    # ── Step 3: Resolve futures + ATM±5 option tokens per symbol ─────────────
    token_info = {}
    all_tokens = []

    for sym in valid_syms:
        insts = by_sym.get(sym, [])
        spot  = spot_prices.get(sym, 0)
        if spot <= 0:
            continue

        # ── Expiry anchor: use the near-month FUTURES expiry ──────────────────
        # Rationale: indices (NIFTY, SENSEX) have weekly options but monthly
        # futures. Picking the nearest overall expiry gives a weekly options date
        # with zero matching futures → symbol silently dropped.
        # Fix: anchor on the nearest futures expiry, then find the closest options
        # expiry that is ≤ that futures expiry to build the strike chain.
        fut_expiries = sorted(set(
            i["expiry"] for i in insts if i["instrument_type"] == "FUT"
        ))
        if not fut_expiries:
            continue
        near_fut_exp = fut_expiries[0]  # near-month futures expiry

        # Nearest options expiry on or before the futures expiry
        opt_expiries = sorted(set(
            i["expiry"] for i in insts
            if i["instrument_type"] in ("CE", "PE") and i["expiry"] <= near_fut_exp
        ))
        if not opt_expiries:
            # Fallback: first available options expiry regardless
            opt_expiries = sorted(set(
                i["expiry"] for i in insts if i["instrument_type"] in ("CE", "PE")
            ))
        near_opt_exp = opt_expiries[-1] if opt_expiries else near_fut_exp

        # Strike step from the chosen options expiry
        opt_strikes = sorted(set(
            float(i["strike"]) for i in insts
            if i["expiry"] == near_opt_exp and i["instrument_type"] in ("CE", "PE")
        ))
        # Use Counter mode for step size (robust against irregular deep-OTM strikes)
        if len(opt_strikes) < 2:
            continue
        diffs = [opt_strikes[j+1] - opt_strikes[j] for j in range(len(opt_strikes)-1)]
        step  = Counter(diffs).most_common(1)[0][0]
        atm   = round(spot / step) * step
        target_strikes = {atm + j * step for j in range(-5, 6)}  # ±5 strikes (11 total)

        for inst in insts:
            itype = inst["instrument_type"]
            tok   = int(inst["instrument_token"])

            if itype == "FUT" and inst["expiry"] == near_fut_exp:
                token_info[tok] = {"symbol": sym, "role": "futures", "strike": None}
                all_tokens.append(tok)
            elif itype in ("CE", "PE") and inst["expiry"] == near_opt_exp and float(inst["strike"]) in target_strikes:
                token_info[tok] = {
                    "symbol": sym,
                    "role":   itype.lower(),
                    "strike": float(inst["strike"]),
                }
                all_tokens.append(tok)

    # Register all resolved live spot cash tokens for real-time spot updates
    for tok, sym in spot_tokens_map.items():
        token_info[tok] = {"symbol": sym, "role": "spot", "strike": None}
        all_tokens.append(tok)

    log.info(f"[Synergy] Resolved {len(all_tokens)} tokens (including spots) across {len(valid_syms)} F&O symbols.")
    return all_tokens, token_info


def _bootstrap_state_from_rest(kite, token_map):
    """
    Pre-populate _sym_state for ALL F&O symbols (stocks + indices) using a single
    kite.quote() REST call immediately after token resolution.

    WHY: The WebSocket scanner only populates _sym_state when ticks arrive.
    Before the first tick, every symbol's state is empty → _compute_synergy
    returns None → symbols are invisible in the dashboard.

    This bootstrap seeds _sym_state with real exchange data (same data source
    as Zerodha's own platform) so ALL symbols appear from the moment the scanner
    starts, not after their first live tick.

    SIGNAL INTEGRITY: _compute_synergy() is completely unchanged. It receives
    the same data fields it would from a tick. Live ticks override bootstrapped
    values in real-time, field by field, as they arrive.
    """
    bootstrap_tokens = [tok for tok, info in token_map.items() if info["role"] in ("futures", "spot")]
    if not bootstrap_tokens:
        return

    log.info(f"[Synergy] Bootstrapping state for {len(bootstrap_tokens)} tokens (futures + spots) via kite.quote()...")
    bootstrapped = 0

    for i in range(0, len(bootstrap_tokens), 500):
        batch = bootstrap_tokens[i:i+500]
        try:
            quotes = kite.quote(batch)
            with _state_lock:
                for _key, q in quotes.items():
                    tok = int(q.get("instrument_token", 0))
                    info = token_map.get(tok)
                    if not info:
                        continue
                    sym  = info["symbol"]
                    role = info["role"]
                    ohlc = q.get("ohlc", {}) or {}

                    if sym not in _sym_state:
                        _sym_state[sym] = {"ce_strikes": {}, "pe_strikes": {}}
                    ss = _sym_state[sym]

                    is_ws_active = (_kws is not None and _kws.is_connected())

                    if role == "futures":
                        if not is_ws_active or ss.get("futures_ltp", 0) == 0:
                            ss["futures_ltp"]           = float(q.get("last_price", 0) or 0)
                            ss["futures_prev_close"]    = float(ohlc.get("close", 0) or 0)
                            ss["futures_open"]          = float(ohlc.get("open", 0) or 0)
                            ss["futures_high"]          = float(ohlc.get("high", 0) or 0)
                            ss["futures_low"]           = float(ohlc.get("low", 0) or 0)
                            ss["futures_oi"]            = int(q.get("oi", 0) or 0)
                            ss["futures_oi_day_change"] = int(q.get("oi_day_change", 0) or 0)
                            bootstrapped += 1
                    elif role == "spot":
                        if not is_ws_active or ss.get("spot_ltp", 0) == 0:
                            ss["spot_ltp"]              = float(q.get("last_price", 0) or 0)
                            ss["spot_prev_close"]       = float(ohlc.get("close", 0) or 0)
                            ss["spot_open"]             = float(ohlc.get("open", 0) or 0)
                            ss["spot_high"]             = float(ohlc.get("high", 0) or 0)
                            ss["spot_low"]              = float(ohlc.get("low", 0) or 0)
                            ss["spot_volume"]           = int(q.get("volume", 0) or 0)
                            bootstrapped += 1
        except Exception as e:
            log.warning(f"[Synergy] Bootstrap REST batch {i}: {e}")

    log.info(f"[Synergy] Bootstrap complete — {bootstrapped} symbols pre-seeded. Live ticks will override.")

    # Immediately compute synergy for all bootstrapped symbols so they appear
    # in the dashboard before the first live tick.  _compute_synergy() reads
    # _sym_state (which we just populated) and returns a result dict.
    computed = 0
    with _state_lock:
        syms_to_compute = list(_sym_state.keys())
    for sym in syms_to_compute:
        result = _compute_synergy(sym)
        if result:
            with _state_lock:
                _synergy_results[sym] = result
                _prev_profiles[sym]   = result["synergy_profile"]
            computed += 1
    log.info(f"[Synergy] Bootstrap synergy computed for {computed} / {len(syms_to_compute)} symbols.")


def _fetch_opening_5m_candles_bg(kite, token_map):
    """
    Fetches the first 5-minute candle of today for all symbols in a background thread.
    Caches the result: 'positive' if close > open, 'negative' if close < open, else 'flat'.
    """
    global _opening_5m_date
    now = datetime.now()
    from datetime import time as dt_time
    if now.time() < dt_time(9, 20):
        return

    spot_tokens = {tok: info["symbol"] for tok, info in token_map.items() if info["role"] == "spot"}
    log.info(f"[Synergy] Starting background fetch of first 5m candle for {len(spot_tokens)} symbols...")
    
    from_dt = now.replace(hour=9, minute=15, second=0, microsecond=0)
    to_dt = now.replace(hour=9, minute=25, second=0, microsecond=0)

    for tok, sym in spot_tokens.items():
        if sym in _opening_5m_cache:
            continue
        try:
            hist = kite.historical_data(tok, from_dt, to_dt, "5minute")
            if hist:
                first_candle = hist[0]
                o = float(first_candle.get("open", 0) or 0)
                c = float(first_candle.get("close", 0) or 0)
                direction = "positive" if c > o else "negative" if c < o else "flat"
                with _opening_5m_lock:
                    _opening_5m_cache[sym] = direction
            time.sleep(0.35)  # Pace queries to respect Zerodha's 3 requests/sec limit
        except Exception as e:
            log.warning(f"[Synergy] Failed to fetch 5m candle for {sym}: {e}")
            time.sleep(0.3)

    log.info(f"[Synergy] Background fetch of 5m candles complete. Cached {len(_opening_5m_cache)} symbols.")


def _fetch_all_cpr_pivots(kite, spot_tokens_map):
    """
    Fetch previous day's OHLC for all resolved spot symbols,
    calculate CPR pivots (PDH, PDL, Pivot, BC, TC), and cache them.
    """
    global _cpr_cache

    # Only fetch symbols that are not already cached
    with _cpr_cache_lock:
        tokens_to_fetch = {tok: sym for tok, sym in spot_tokens_map.items() if sym not in _cpr_cache}
    if not tokens_to_fetch:
        log.info("[Synergy] All CPR pivots are already cached. Skipping historical fetch.")
        return

    today = date.today()
    from_dt = today - timedelta(days=7)
    to_dt   = today - timedelta(days=1)
    
    log.info(f"[Synergy] Fetching CPR pivots for {len(tokens_to_fetch)} underlyings from Kite history...")
    
    bootstrapped_pivots = {}
    
    for tok, sym in tokens_to_fetch.items():
        try:
            hist = kite.historical_data(tok, from_dt, to_dt, "day")
            if hist:
                last  = hist[-1]
                pdh   = float(last["high"] or 0)
                pdl   = float(last["low"] or 0)
                pdc   = float(last["close"] or 0)
                
                # Calculate Weekly High (PWH) and Weekly Low (PWL) from the historical list
                pwh = float(max(candle["high"] for candle in hist) or 0)
                pwl = float(min(candle["low"] for candle in hist) or 0)
                
                if pdh > 0 and pdl > 0 and pdc > 0:
                    pivot = (pdh + pdl + pdc) / 3.0
                    bc    = (pdh + pdl) / 2.0
                    tc    = 2.0 * pivot - bc
                    
                    if tc < bc:
                        tc, bc = bc, tc
                        
                    bootstrapped_pivots[sym] = {
                        "pdh":   round(pdh, 2),
                        "pdl":   round(pdl, 2),
                        "pdc":   round(pdc, 2),
                        "pwh":   round(pwh, 2),
                        "pwl":   round(pwl, 2),
                        "pivot": round(pivot, 2),
                        "bc":    round(bc, 2),
                        "tc":    round(tc, 2)
                    }
                    with _cpr_cache_lock:
                        _cpr_cache[sym] = bootstrapped_pivots[sym]
            # Safety sleep — 0.4s = 2.5 req/s, giving safe headroom below Zerodha's
            # 3 req/s cap even when REST fallbacks and bootstrap calls run in parallel
            time.sleep(0.40)
        except Exception as e:
            log.warning(f"[Synergy] Failed to fetch CPR history for {sym}: {e}")
            time.sleep(1.0)
        
    log.info(f"[Synergy] CPR pivots successfully cached for {len(_cpr_cache)} symbols.")
    _save_cpr_cache_to_disk()


# ── Scanner Lifecycle ─────────────────────────────────────────────────────────
def _scanner_loop(socketio_instance, get_kite_fn):
    global _kws, _socketio_ref
    _socketio_ref = socketio_instance

    log.info("[Synergy] Thread started. Deferring 2 min for system startup...")
    time.sleep(120)  # 2-minute delay clears startup and allows sockets to connect

    while _is_running:
        try:
            kite = get_kite_fn()
            if not kite:
                log.warning("[Synergy] Kite not connected. Retry in 60s.")
                time.sleep(60)
                continue

            from server import _load_kite_session
            api_key, access_token = _load_kite_session()
            if not api_key or not access_token:
                log.warning("[Synergy] No Kite session. Retry in 60s.")
                time.sleep(60)
                continue

            all_tokens, token_map = _resolve_tokens(kite)
            if not all_tokens:
                log.warning("[Synergy] No tokens resolved. Retry in 120s.")
                time.sleep(120)
                continue

            with _state_lock:
                _token_info.clear()
                _token_info.update(token_map)

            # Fetch and cache previous day's CPR pivots
            spot_tokens_map = {tok: info["symbol"] for tok, info in token_map.items() if info["role"] == "spot"}
            _fetch_all_cpr_pivots(kite, spot_tokens_map)

            # Pre-seed _sym_state for ALL symbols (stocks + indices) from REST.
            # Live ticks will override these values field-by-field as they arrive.
            _bootstrap_state_from_rest(kite, token_map)

            # Trigger background 5m opening candle fetch if after 09:20 AM and cache is empty
            now_dt = datetime.now()
            from datetime import time as dt_time
            global _opening_5m_date
            if _opening_5m_date != now_dt.date():
                _opening_5m_cache.clear()
                _opening_5m_date = now_dt.date()

            if now_dt.time() >= dt_time(9, 20) and len(_opening_5m_cache) < len(spot_tokens_map):
                threading.Thread(
                    target=_fetch_opening_5m_candles_bg,
                    args=(kite, token_map),
                    daemon=True
                ).start()


            from global_ticker import get_ticker_for_feature, get_ticker_mode
            kws = get_ticker_for_feature("synergy", all_tokens, _on_ticks, mode="FULL")
            kws.on_ticks   = _on_ticks
            kws.on_connect = lambda ws, r: _on_ws_connect(ws, r, all_tokens)
            kws.on_error   = _on_error
            kws.on_close   = _on_close
            _kws = kws
            kws.connect(threaded=True)
            log.info(f"[Synergy] Ticker initialized via TickerFactory. Streaming {len(all_tokens)} tokens.")

            # Keep alive — broadcast full state every 60s via Socket.IO
            # This ensures new browser tabs receive complete synergy state without any REST call to Kite
            _last_broadcast = 0
            is_centralized = (get_ticker_mode("synergy") == "centralized")
            _last_rvol_warm = 0
            while _is_running:
                time.sleep(10)
                try:
                    # In centralized mode, the connection is managed by the GlobalTickerManager.
                    # We should not break the loop and re-bootstrap on connection drops.
                    if not is_centralized and not kws.is_connected():
                        log.warning("[Synergy] Connection lost. Reconnecting...")
                        break
                except Exception:
                    break

                # Trigger background 5m opening candle fetch when time crosses 9:20 AM
                now_dt = datetime.now()
                from datetime import time as dt_time
                if _opening_5m_date != now_dt.date():
                    _opening_5m_cache.clear()
                    _opening_5m_date = now_dt.date()
                if now_dt.time() >= dt_time(9, 20) and len(_opening_5m_cache) < len(spot_tokens_map):
                    threading.Thread(
                        target=_fetch_opening_5m_candles_bg,
                        args=(kite, token_map),
                        daemon=True
                    ).start()


                # Warm up average volume cache for all spot symbols (checked every 60s)
                now = time.time()
                if now - _last_rvol_warm >= 60:
                    try:
                        from option_gainers_scanner import ensure_avg_volume_warm
                        spot_sym_to_tok = {info["symbol"]: tok for tok, info in token_map.items() if info["role"] == "spot"}
                        if spot_sym_to_tok:
                            ensure_avg_volume_warm(kite, spot_sym_to_tok)
                        _last_rvol_warm = now
                    except Exception as ex:
                        log.warning(f"[Synergy] Failed to trigger RVOL warming: {ex}")

                # Broadcast full state to all connected frontend clients every 60s
                now = time.time()
                if _socketio_ref and (now - _last_broadcast) >= 60:
                    try:
                        with _state_lock:
                            snapshot = list(_synergy_results.values())
                        _socketio_ref.emit("synergy_update", {
                            "results":   snapshot,
                            "timestamp": datetime.now().isoformat(),
                        })
                        _last_broadcast = now
                        log.debug(f"[Synergy] Broadcast {len(snapshot)} results to all clients.")
                    except Exception as e:
                        log.warning(f"[Synergy] Broadcast error: {e}")

            log.warning("[Synergy] Reconnecting and polling in 15s...")
            time.sleep(15)

        except Exception as e:
            log.error(f"[Synergy] Loop error: {e}")
            time.sleep(60)


def start_synergy_scanner(socketio_instance, get_kite_fn):
    """Start the server-global KiteTicker synergy scanner (daemon thread)."""
    global _is_running, _scanner_thread
    if _scanner_thread and _scanner_thread.is_alive():
        log.info("[Synergy] Already running.")
        return
    _is_running = True
    _scanner_thread = threading.Thread(
        target=_scanner_loop,
        args=(socketio_instance, get_kite_fn),
        daemon=True,
        name="SynergyScannerThread",
    )
    _scanner_thread.start()
    log.info("[Synergy] Synergy scanner thread spawned.")


def get_synergy_results():
    """Return full in-memory synergy state (REST endpoint use)."""
    with _state_lock:
        return dict(_synergy_results)


def get_buy_alerts():
    """Return only BUY-class profiles."""
    return {k: v for k, v in get_synergy_results().items() if v.get("is_buy_signal")}


def get_cpr_cross_alerts():
    """Return recent CPR crossing alerts list (REST endpoint fallback)."""
    with _state_lock:
        return list(_cpr_cross_alerts)
