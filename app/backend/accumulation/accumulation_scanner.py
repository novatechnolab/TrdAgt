# accumulation_scanner.py — Core Scanner Engine
# Three-layer scoring, state machine, regime detection, ranking
import time
import logging
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional

from config import *
from bollinger_squeeze import compute_bb_squeeze, compute_range_stats
from oi_engine import analyze_oi_for_stock, check_liquidity
from historical_profile import get_track_record
from event_calendar import (has_event_tomorrow, should_suppress_options_play,
                             is_expiry_day, is_expiry_tomorrow, is_last_hour_wednesday)
from sector_map import get_sector, get_weight, compute_sector_signals

log = logging.getLogger("accumulation_scanner")

_session_state = {}       # {symbol: {state, failed_attempts, first_coil_time, prev_state}}
_last_scan_results = []
_last_scan_time = None
_scan_progress = {"current": 0, "total": 0, "status": "Idle"}
_scan_errors = []
_nifty_history = []       # Recent Nifty 15-min bars for regime detection


# ── Regime Detection ──────────────────────────────────────────────────────────

def detect_market_regime(nifty_bars: list, india_vix: float, vix_30m_ago: float) -> str:
    """
    Returns: TRENDING / SIDEWAYS / VOLATILE / EXPIRY
    """
    # Expiry takes priority
    if is_expiry_day() or is_last_hour_wednesday():
        return "EXPIRY"

    if not nifty_bars or len(nifty_bars) < 4:
        return "SIDEWAYS"

    closes = [b["close"] for b in nifty_bars[-5:]]

    # Volatile: VIX spike or large single candle
    if vix_30m_ago and (india_vix - vix_30m_ago) > REGIME_VOLATILE_VIX_RISE:
        return "VOLATILE"
    if len(nifty_bars) >= 2:
        last_bar = nifty_bars[-1]
        last_move = abs(last_bar["close"] - last_bar["open"]) / last_bar["open"] if last_bar["open"] > 0 else 0
        if last_move > REGIME_VOLATILE_MOVE:
            return "VOLATILE"

    # Trending: 3+ consecutive HH/HL or LL/LH
    if len(closes) >= 4:
        ups = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i-1])
        downs = sum(1 for i in range(1, len(closes)) if closes[i] < closes[i-1])
        if ups >= REGIME_TREND_CANDLES or downs >= REGIME_TREND_CANDLES:
            if india_vix is not None and abs(india_vix - (vix_30m_ago or india_vix)) < 1.0:
                return "TRENDING"

    # Sideways: oscillating within 0.3%
    if len(closes) >= REGIME_SIDEWAYS_CANDLES:
        rng = max(closes) - min(closes)
        mid = (max(closes) + min(closes)) / 2
        if mid > 0 and rng / mid <= REGIME_SIDEWAYS_RANGE:
            return "SIDEWAYS"

    return "SIDEWAYS"


# ── OHLCV Fetch ───────────────────────────────────────────────────────────────

# Minimum candles required by compute_range_stats: lookback(6) + prior(20) = 26
_MIN_CANDLES_REQUIRED = 26

def fetch_ohlcv(kite, symbol: str, days: int = 5, instrument_token: int = None) -> Optional[pd.DataFrame]:
    """Fetch 15-min OHLCV via server's shared TTL cache (zero extra Kite calls when EMA scanner is warm).

    Uses same params as EMA scanner (days_back=10, limit=100) for guaranteed cache hit.
    compute_range_stats requires lookback(6) + prior(20) = 26 rows minimum.
    """
    try:
        from server import get_historical_candles
        # Match EMA scanner params exactly → guaranteed cache hit, 0 fresh Kite calls
        hist = get_historical_candles(kite, symbol, "15minute", days_back=10, limit=100)
        if not hist:
            return None

        df = pd.DataFrame(hist)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()

        # Use today-only candles only when there are enough for all indicators.
        # compute_range_stats needs lookback(6) + prior(20) = 26 rows minimum.
        today = datetime.now().date()
        df_today = df[df.index.date == today]
        if len(df_today) >= _MIN_CANDLES_REQUIRED:
            return df_today

        # Not enough today — use multi-day tail so indicators have enough history
        return df.tail(40)

    except Exception as e:
        log.warning(f"OHLCV fetch failed for {symbol}: {e}")
        return None


def fetch_daily_ohlcv(kite, symbol: str, instrument_token: int = None) -> Optional[pd.DataFrame]:
    """Fetch daily OHLCV via server's shared TTL cache (zero extra Kite calls when EMA scanner is warm)."""
    try:
        from server import get_historical_candles
        # Match EMA scanner params (days_back=120, limit=100) for cache hit
        hist = get_historical_candles(kite, symbol, "day", days_back=120, limit=100)
        if not hist:
            return None
        df = pd.DataFrame(hist)
        df["date"] = pd.to_datetime(df["date"])
        return df.set_index("date").sort_index()
    except Exception:
        return None


def fetch_hourly_ohlcv(kite, symbol: str, instrument_token: int = None) -> Optional[pd.DataFrame]:
    """Fetch 60-min OHLCV via server's shared TTL cache (zero extra Kite calls when EMA scanner is warm)."""
    try:
        from server import get_historical_candles
        # Match EMA scanner params (days_back=15, limit=100) for cache hit
        hist = get_historical_candles(kite, symbol, "60minute", days_back=15, limit=100)
        if not hist:
            return None
        df = pd.DataFrame(hist)
        df["date"] = pd.to_datetime(df["date"])
        return df.set_index("date").sort_index()
    except Exception:
        return None


# ── EMA Crossover Cache Adapter ───────────────────────────────────────────────
EMA_CACHE_MAX_AGE_SEC = 300  # Reject cache older than 5 minutes

def _get_trends_from_ema_scanner(symbol: str) -> Optional[dict]:
    """Read pre-computed Daily/Hourly/15min trends from the running EMA crossover
    scanner cache. Returns None if cache is absent, stale, or scanner not started.
    No Kite calls made here — pure read from in-memory state."""
    try:
        from ema_crossover_scanner import get_ema_crossover_state
        state = get_ema_crossover_state()

        # Reject if data is stale
        last_update_str = state.get("last_update")
        if not last_update_str:
            return None
        from datetime import datetime as _dt
        last_dt = _dt.strptime(last_update_str, "%Y-%m-%d %H:%M:%S")
        age_sec = (datetime.now() - last_dt).total_seconds()
        if age_sec > EMA_CACHE_MAX_AGE_SEC:
            return None

        sym_data = state.get("crossovers", {}).get(symbol)
        if not sym_data:
            return None

        return {
            "daily":  sym_data.get("state_day", "neutral").capitalize(),
            "hourly": sym_data.get("state_1h",  "neutral").capitalize(),
            "15min":  sym_data.get("state_15m", "neutral").capitalize(),
        }
    except Exception:
        return None


def compute_multi_timeframe_trends(df_15m: pd.DataFrame,
                                    daily_df: Optional[pd.DataFrame],
                                    hourly_df: Optional[pd.DataFrame],
                                    symbol: str = None) -> dict:
    """Compute Bullish/Bearish/Neutral trend for Daily, Hourly, and 15-min timeframes.

    Tries the EMA crossover scanner cache first (zero Kite calls).
    Falls back to fresh OHLCV computation when cache is absent or stale.

    Uses EMA-9 vs EMA-21 crossover plus close position for each timeframe.
    Returns: {"daily": "Bullish"|"Bearish"|"Neutral",
              "hourly": ..., "15min": ...}
    """
    # ── Fast path: reuse EMA crossover scanner cache ─────────────────────
    if symbol:
        cached = _get_trends_from_ema_scanner(symbol)
        if cached:
            return cached

    # ── Fallback: compute from OHLCV dataframes ─────────────────────────
    def _trend_from_df(df, min_rows=21):
        if df is None or len(df) < min_rows:
            return "Neutral"
        close = df["close"]
        ema9 = close.ewm(span=9, adjust=False).mean()
        ema21 = close.ewm(span=21, adjust=False).mean()
        last_close = float(close.iloc[-1])
        last_ema9 = float(ema9.iloc[-1])
        last_ema21 = float(ema21.iloc[-1])
        # Bullish: EMA9 > EMA21 and close above EMA21
        if last_ema9 > last_ema21 and last_close > last_ema21:
            return "Bullish"
        # Bearish: EMA9 < EMA21 and close below EMA21
        if last_ema9 < last_ema21 and last_close < last_ema21:
            return "Bearish"
        return "Neutral"

    return {
        "daily": _trend_from_df(daily_df),
        "hourly": _trend_from_df(hourly_df),
        "15min": _trend_from_df(df_15m),
    }


# ── Scoring ───────────────────────────────────────────────────────────────────

def score_layer1(squeeze: dict, stats: dict, time_in_coil: int) -> dict:
    """Accumulation quality scoring — Layer 1."""
    s = {}
    score = 0

    # 1. BB Squeeze active (+3)
    s["squeeze_active"] = squeeze.get("squeeze_active", False)
    if s["squeeze_active"]:
        score += 3

    # 2. BB width at 20-candle minimum (+2)
    s["at_width_minimum"] = squeeze.get("at_width_minimum", False)
    if s["at_width_minimum"]:
        score += 2

    # 3. Range tightness (+2)
    s["range_tight"] = stats.get("range_tightness_ratio", 1.0) < RANGE_TIGHTNESS_PCT
    if s["range_tight"]:
        score += 2

    # 4. Volume dry-up (+2)
    s["volume_dryup"] = stats.get("volume_ratio", 1.0) < VOLUME_DRYUP_PCT
    if s["volume_dryup"]:
        score += 2

    # 5. Candle body compression (+1)
    s["body_compressed"] = stats.get("body_ratio", 1.0) < BODY_COMPRESSION_PCT
    if s["body_compressed"]:
        score += 1

    # 6. Time in accumulation (+1 to +3)
    if time_in_coil >= 17:
        time_score = 3
        s["time_flag"] = "EXTENDED_COIL"
    elif time_in_coil >= 9:
        time_score = 3
        s["time_flag"] = "HIGH_ENERGY"
    elif time_in_coil >= 5:
        time_score = 2
        s["time_flag"] = "ESTABLISHED"
    elif time_in_coil >= 3:
        time_score = 1
        s["time_flag"] = "EARLY"
    else:
        time_score = 0
        s["time_flag"] = "TOO_SHORT"
    score += time_score
    s["time_score"] = time_score

    # Post-move penalty
    post_move_penalty = 0
    s["post_move"] = stats.get("pre_move_pct", 0) > POST_MOVE_THRESHOLD_PCT
    if s["post_move"]:
        post_move_penalty = 2
        score = max(0, score - post_move_penalty)

    return {"score": score, "factors": s, "post_move_penalty": post_move_penalty}


def score_layer2(df: pd.DataFrame, stats: dict, oi_data: dict,
                 vwap: float, regime: str, sector_score: int,
                 is_expiry: bool, daily_df=None) -> dict:
    """Directional bias scoring — Layer 2."""
    s = {}
    score = 0
    close = df["close"]
    ltp = float(close.iloc[-1])

    # 7. SMA stack 15-min
    ema9 = close.ewm(span=9).mean().iloc[-1]
    sma20 = close.rolling(20).mean().iloc[-1] if len(close) >= 20 else ltp
    sma50 = close.rolling(50).mean().iloc[-1] if len(close) >= 50 else ltp
    sma_bull = ema9 > sma20 > sma50
    sma_bear = ema9 < sma20 < sma50
    s["sma_stack"] = "BULLISH" if sma_bull else ("BEARISH" if sma_bear else "MIXED")
    if sma_bull:
        score += 2
    elif sma_bear:
        score -= 2

    # 8. VWAP position
    s["above_vwap"] = ltp > vwap
    score += 2 if ltp > vwap else -2

    # 9. BB momentum histogram
    from bollinger_squeeze import compute_bb_squeeze
    sq = compute_bb_squeeze(df)
    mom = sq.get("momentum_now", 0)
    mom_rising = sq.get("momentum_rising", False)
    s["momentum_bullish"] = mom > 0 and mom_rising
    s["momentum_bearish"] = mom < 0 and not mom_rising
    if s["momentum_bullish"]:
        score += 2
    elif s["momentum_bearish"]:
        score -= 2

    # 10. Close position in range
    range_high = stats["range_high"]
    range_low = stats["range_low"]
    rng = range_high - range_low
    if rng > 0:
        pos_in_range = (ltp - range_low) / rng if rng > 0 else 0
        s["upper_third"] = pos_in_range >= 0.7
        s["lower_third"] = pos_in_range <= 0.3
        if s["upper_third"]:
            score += 2
        elif s["lower_third"]:
            score -= 2
    else:
        s["upper_third"] = s["lower_third"] = False

    # 11. Volume bias within range
    vol_bias = stats.get("volume_bias", "NEUTRAL")
    s["volume_bias"] = vol_bias
    score += 1 if vol_bias == "BULLISH" else (-1 if vol_bias == "BEARISH" else 0)

    # 12. Daily SMA trend
    if daily_df is not None and len(daily_df) >= 50:
        d_close = daily_df["close"]
        d20 = d_close.rolling(20).mean().iloc[-1]
        d50 = d_close.rolling(50).mean().iloc[-1]
        d_ltp = float(d_close.iloc[-1])
        s["above_d20_d50"] = d_ltp > d20 and d_ltp > d50
        s["below_d20_d50"] = d_ltp < d20 and d_ltp < d50
        if s["above_d20_d50"]:
            score += 1
        elif s["below_d20_d50"]:
            score -= 1
    else:
        s["above_d20_d50"] = s["below_d20_d50"] = False

    # 13–18. OI-based factors
    if oi_data and "error" not in oi_data:
        pe_wall = oi_data.get("pe_wall_strike")
        ce_wall = oi_data.get("ce_wall_strike")
        pcr_data = oi_data.get("pcr", {})
        fresh = oi_data.get("fresh_writing", {})
        max_pain = oi_data.get("max_pain", {})

        # 13. PE wall below range
        pe_below = pe_wall and pe_wall < range_low * (1 + OI_WALL_PROXIMITY_PCT / 100)
        s["pe_wall_below"] = bool(pe_below)
        if s["pe_wall_below"]:
            score += 1

        # 14. PCR direction
        pcr_signal = pcr_data.get("signal", "NEUTRAL")
        if pcr_signal in ("EXTREME_BEARISH", "BULLISH", "EXTREME_BULLISH"):
            pcr_bull = pcr_signal in ("BULLISH", "EXTREME_BULLISH")
            score += 1 if pcr_bull else -1
            s["pcr_direction"] = "BULLISH" if pcr_bull else "BEARISH"
        else:
            s["pcr_direction"] = "NEUTRAL"

        # 15. Max pain position (time-gated weight)
        mp = max_pain.get("direction", "NEUTRAL")
        mp_weight = max_pain.get("weight", MAX_PAIN_EARLY_WEIGHT)
        s["max_pain_dir"] = mp
        if mp == "UP":
            score += mp_weight
        elif mp == "DOWN":
            score -= mp_weight

        # 16. Fresh put writing at support
        fresh_pe = fresh.get("fresh_pe", [])
        has_fresh_pe_support = any(
            r["strike"] >= range_low * 0.99 and r["strike"] <= range_low * 1.01
            for r in fresh_pe
        )
        s["fresh_pe_support"] = has_fresh_pe_support
        if has_fresh_pe_support:
            score += 1

        # 17. Sector alignment
        s["sector_score"] = sector_score
        if sector_score > 0:
            score += min(sector_score, 2)
        elif sector_score < 0:
            score -= min(abs(sector_score), 2)

        # 18. Fresh call writing at resistance
        fresh_ce = fresh.get("fresh_ce", [])
        fresh_ce_resist = any(
            r["strike"] >= range_high * 0.99 and r["strike"] <= range_high * 1.01
            for r in fresh_ce
        )
        s["fresh_ce_resist"] = fresh_ce_resist
        if fresh_ce_resist:
            score -= 2
        elif not fresh_ce_resist:
            score += 1

    return {"score": score, "factors": s}


def score_layer3(ltp: float, stats: dict, squeeze: dict, df: pd.DataFrame) -> dict:
    """Breakout proximity — Layer 3 (the action layer)."""
    s = {}
    score = 0
    range_high = stats["range_high"]
    range_low = stats["range_low"]

    # Price at range boundary
    proximity_bull = (range_high - ltp) / range_high * 100 <= BREAKOUT_PROXIMITY_PCT if range_high > 0 else False
    proximity_bear = (ltp - range_low) / range_low * 100 <= BREAKOUT_PROXIMITY_PCT if range_low > 0 else False
    s["at_boundary"] = proximity_bull or proximity_bear
    s["direction_proximity"] = "UP" if proximity_bull else ("DOWN" if proximity_bear else "NONE")
    if s["at_boundary"]:
        score += 3

    # BB expanding after squeeze
    s["bb_expanding"] = squeeze.get("bb_expanding", False)
    if s["bb_expanding"] and squeeze.get("squeeze_candles", 0) >= 3:
        score += 3

    # Volume surge on last candle
    vol_surge = stats.get("volume_surge", 1.0)
    s["volume_surge"] = vol_surge >= 2.0
    if s["volume_surge"]:
        score += 2

    # Momentum cross
    mom_cross = squeeze.get("momentum_cross", "NONE")
    s["momentum_cross"] = mom_cross
    if mom_cross in ("CROSS_UP", "CROSS_DOWN"):
        score += 2

    return {"score": score, "factors": s}


def assign_grade(l1: int, l2_abs: int, l3: int,
                 failed_attempts: int, wall_tags: list,
                 regime: str) -> str:
    """Assign grade A/B/C/D with wall override and regime adjustment."""

    # Failed breakout penalty — cap grade
    if failed_attempts >= 2:
        return "C"

    # Regime adjustment for Layer 3 threshold
    l3_a_threshold = GRADE_A_L3
    l3_b_threshold = GRADE_B_L3
    if regime == "SIDEWAYS":
        l3_a_threshold += 2
        l3_b_threshold += 1

    if l1 >= GRADE_A_L1 and l2_abs >= GRADE_A_L2 and l3 >= l3_a_threshold:
        grade = "A"
    elif l1 >= GRADE_B_L1 and l2_abs >= GRADE_B_L2 and l3 >= l3_b_threshold:
        grade = "B"
    elif l1 >= GRADE_C_L1:
        grade = "C"
    else:
        grade = "D"

    # OI wall override — cap at C if wall directly at breakout
    if grade in ("A", "B") and "CE_WALL_OVERHEAD" in wall_tags:
        grade = "C"
    if grade in ("A", "B") and "TRAPPED_IN_WALLS" in wall_tags:
        grade = "C"

    return grade


def get_confidence(grade: str, regime: str, track: str, wall_tags: list,
                   oi_aligned: bool) -> str:
    """Compute confidence meter: VERY STRONG / STRONG / MODERATE."""
    confirming = 0
    if grade == "A":
        confirming += 2
    elif grade == "B":
        confirming += 1

    if regime == "TRENDING":
        confirming += 1
    if track == "STRONG":
        confirming += 1
    if not any(t in wall_tags for t in ("CE_WALL_OVERHEAD", "TRAPPED_IN_WALLS")):
        confirming += 1
    if oi_aligned:
        confirming += 1

    if grade == "A" and confirming >= 5:
        return "VERY_STRONG"
    elif confirming >= 3:
        return "STRONG"
    else:
        return "MODERATE"


def detect_distribution(stats: dict, oi_data: dict) -> bool:
    """
    Distribution: volume bias bearish + fresh put writing absent at support
    + fresh call writing at top.
    """
    vol_bias = stats.get("volume_bias", "NEUTRAL")
    bias_margin = stats.get("bias_margin", 0)
    if vol_bias != "BEARISH" or bias_margin < DISTRIBUTION_VOL_BIAS:
        return False

    if not oi_data or "error" in oi_data:
        return False

    fresh = oi_data.get("fresh_writing", {})
    fresh_pe = fresh.get("fresh_pe", [])
    fresh_ce = fresh.get("fresh_ce", [])

    no_fresh_pe = len(fresh_pe) == 0
    has_fresh_ce = len(fresh_ce) > 0

    return no_fresh_pe and has_fresh_ce


def compute_iv_rank_warning(symbol: str) -> dict:
    """Placeholder — IV rank computation requires options IV history."""
    # In production: compute IV percentile from 60-day IV history
    return {"rank": "NORMAL", "warning": None}


def get_options_play(direction: str, range_high: float, range_low: float,
                     ltp: float, oi_data: dict, symbol: str,
                     has_event: bool, iv_rank: str, regime: str) -> dict:
    """EOD options play suggestion with liquidity check."""
    if has_event:
        return {"play": "SKIP", "reason": "EVENT_RISK"}
    if iv_rank == "EXTREME":
        return {"play": "SKIP", "reason": "IV_EXTREME"}

    # OTM strike: 1× range height above/below breakout level
    rng_height = range_high - range_low
    if direction == "UP":
        otm_strike = round((range_high + rng_height * 0.5) / 50) * 50
        play_type = "CE"
    elif direction == "DOWN":
        otm_strike = round((range_low - rng_height * 0.5) / 50) * 50
        play_type = "PE"
    else:
        return {"play": "SKIP", "reason": "NO_DIRECTION"}

    # Volatile regime: suggest ATM instead of OTM
    if regime == "VOLATILE":
        atm_strike = round(ltp / 50) * 50
        return {"play": f"ATM_{play_type}", "strike": atm_strike, "reason": "VOLATILE_REGIME"}

    # Liquidity check
    chain = oi_data.get("_chain", [])
    if chain:
        liq = check_liquidity(chain, otm_strike, play_type)
        if liq["status"] == "AVOID":
            # Fall back to ATM
            atm_strike = round(ltp / 50) * 50
            return {"play": f"ATM_{play_type}_FALLBACK", "strike": atm_strike,
                    "reason": f"OTM_ILLIQUID: {liq['note']}",
                    "liquidity": "GOOD"}
        return {"play": f"OTM_{play_type}", "strike": otm_strike,
                "reason": "", "liquidity": liq["status"]}

    return {"play": f"OTM_{play_type}", "strike": otm_strike, "reason": ""}


# ── Single Stock Scanner ──────────────────────────────────────────────────────

def scan_stock(kite, symbol: str, vwap: float, ltp: float,
               india_vix: float, sector_score: int,
               regime: str, oi_data: dict = None,
               is_eod: bool = False, instrument_token: int = None) -> Optional[dict]:
    """
    Full scan for one stock. Returns result dict or None if D-grade / filtered.
    """
    sess = _session_state.setdefault(symbol, {
        "state": "NONE",
        "failed_attempts": 0,
        "first_coil_time": None,
        "prior_states": []
    })

    # Fetch OHLCV — needs at least _MIN_CANDLES_REQUIRED rows for compute_range_stats
    df = fetch_ohlcv(kite, symbol, days=5, instrument_token=instrument_token)
    if df is None or len(df) < _MIN_CANDLES_REQUIRED:
        return None

    # Price discovery check: flag but don't score
    now = datetime.now()
    price_discovery = (now.hour == 9 and now.minute < 45)

    # Compute indicators
    try:
        squeeze = compute_bb_squeeze(df)
        stats = compute_range_stats(df, ACCUM_CANDLE_LOOKBACK)
    except Exception as e:
        log.warning(f"Indicator computation failed for {symbol}: {e}")
        return None

    if not stats:
        return None

    current_ltp = float(df["close"].iloc[-1])
    range_high = stats["range_high"]
    range_low = stats["range_low"]

    # Time in coil
    if sess["first_coil_time"] is None and squeeze.get("squeeze_active"):
        sess["first_coil_time"] = now
    coil_minutes = int((now - sess["first_coil_time"]).total_seconds() / 60) if sess["first_coil_time"] else 0
    coil_candles = max(squeeze.get("squeeze_candles", 0), coil_minutes // 15)

    # OI analysis (passed in from batch fetch)
    if oi_data is None:
        oi_data = {}

    wall_rel = oi_data.get("wall_relationship", {})
    wall_tags = wall_rel.get("wall_tags", [])
    wall_score_adj = wall_rel.get("score_adj", 0)

    # Distribution detection
    is_distribution = detect_distribution(stats, oi_data)

    # Fetch daily OHLCV for SMA trend
    daily_df = fetch_daily_ohlcv(kite, symbol, instrument_token=instrument_token)

    # Fetch hourly OHLCV for multi-timeframe trends
    hourly_df = fetch_hourly_ohlcv(kite, symbol, instrument_token=instrument_token)

    # Multi-timeframe trend computation
    trends = compute_multi_timeframe_trends(df, daily_df, hourly_df, symbol=symbol)

    last_breakout_days_ago = None
    if daily_df is not None and not daily_df.empty and len(daily_df) > 1:
        recent_daily = daily_df.tail(15).copy()
        recent_daily["pct_change"] = recent_daily["close"].pct_change() * 100
        recent_daily = recent_daily.dropna(subset=["pct_change"])
        for i in range(len(recent_daily) - 1, -1, -1):
            if recent_daily["pct_change"].iloc[i] > 2.0:
                last_breakout_days_ago = len(recent_daily) - 1 - i
                break

    # Score layer 1
    l1 = score_layer1(squeeze, stats, coil_candles)

    # Score layer 2
    l2 = score_layer2(df, stats, oi_data, vwap, regime, sector_score,
                      is_expiry_day(), daily_df)

    # Score layer 3
    l3 = score_layer3(current_ltp, stats, squeeze, df)

    # Apply wall score adjustment to layer 3
    l3["score"] = max(0, l3["score"] + wall_score_adj)

    l1_score = l1["score"]
    l2_score = l2["score"]
    l3_score = l3["score"]
    l2_abs = abs(l2_score)

    failed_attempts = sess.get("failed_attempts", 0)
    grade = assign_grade(l1_score, l2_abs, l3_score, failed_attempts, wall_tags, regime)

    if grade == "D":
        return None

    # Direction
    direction = "UP" if l2_score > 0 else ("DOWN" if l2_score < 0 else "NEUTRAL")
    if is_distribution:
        direction = "DOWN"

    # State machine
    prev_state = sess.get("state", "NONE")
    state = "DISTRIBUTION" if is_distribution else "ACCUMULATING"

    # Check for BREAKING: BB expanding after squeeze AND price crossing boundary
    breaking = (
        squeeze.get("bb_expanding") and
        squeeze.get("squeeze_candles", 0) >= 3 and
        (current_ltp > range_high * (1 - BREAKOUT_PROXIMITY_PCT / 100) or
         current_ltp < range_low * (1 + BREAKOUT_PROXIMITY_PCT / 100))
    )
    if breaking:
        state = "BREAKING"

    # Check for BROKEN: 2 consecutive closes outside range
    broken = (
        df["close"].tail(2).gt(range_high).all() or
        df["close"].tail(2).lt(range_low).all()
    )
    if broken:
        state = "BROKEN"
        if prev_state == "BREAKING":
            # Log in session history
            sess.setdefault("breakout_log", []).append({
                "time": now.strftime("%H:%M"),
                "direction": direction,
                "success": True,
            })
        return None  # Remove from accumulation list

    # Failed breakout: price crossed boundary but closed back inside
    if prev_state in ("BREAKING", "ACCUMULATING"):
        if df["close"].tail(1).iloc[0] < range_high and df["high"].tail(2).max() > range_high:
            sess["failed_attempts"] = failed_attempts + 1
            sess.setdefault("breakout_log", []).append({
                "time": now.strftime("%H:%M"),
                "direction": direction,
                "success": False,
            })

    sess["state"] = state

    # Track record
    track_data = get_track_record(symbol)
    track_badge = track_data.get("badge", "UNKNOWN")

    # IV rank
    iv_rank_data = compute_iv_rank_warning(symbol)
    iv_rank = iv_rank_data.get("rank", "NORMAL")

    # Confidence
    oi_aligned = direction == oi_data.get("max_pain", {}).get("direction", "NEUTRAL")
    confidence = get_confidence(grade, regime, track_badge, wall_tags, oi_aligned)

    # Breakout targets
    rng_height = stats["range_height"]
    breakout_level = range_high if direction == "UP" else range_low
    stop_level = range_low if direction == "UP" else range_high
    target1 = round(range_high + rng_height, 2) if direction == "UP" else round(range_low - rng_height, 2)
    target2 = round(range_high + rng_height * 1.5, 2) if direction == "UP" else round(range_low - rng_height * 1.5, 2)

    # Room to run
    ce_wall = oi_data.get("ce_wall_strike")
    pe_wall = oi_data.get("pe_wall_strike")
    if direction == "UP" and ce_wall:
        room_to_run_pct = round((ce_wall - range_high) / range_high * 100, 2) if range_high > 0 else 0
    elif direction == "DOWN" and pe_wall:
        room_to_run_pct = round((range_low - pe_wall) / range_low * 100, 2) if range_low > 0 else 0
    else:
        room_to_run_pct = None

    # EOD options play
    options_play = {}
    if is_eod:
        has_ev = has_event_tomorrow(symbol)
        options_play = get_options_play(direction, range_high, range_low,
                                        current_ltp, oi_data, symbol,
                                        has_ev, iv_rank, regime)

    # Coil time string
    if coil_candles >= 17:
        coil_str = f"Day {max(1, coil_candles // 26)}+"
    elif coil_minutes >= 60:
        h = coil_minutes // 60
        m = coil_minutes % 60
        coil_str = f"{h}h {m}m"
    else:
        coil_str = f"{coil_minutes}m"

    # Failed breakout tags
    tags = []
    if failed_attempts > 0:
        tags.append(f"⚠ PRIOR_FAIL×{failed_attempts}")
    if stats.get("pre_move_pct", 0) > POST_MOVE_THRESHOLD_PCT:
        tags.append("POST-MOVE")
    if coil_candles >= 17:
        tags.append("EXTENDED_COIL")
    if has_event_tomorrow(symbol):
        tags.append("📅 EVENT")
    if price_discovery:
        tags.append("PRICE_DISCOVERY")
    if is_distribution:
        tags.append("DIST")
    for wt in wall_tags:
        tags.append(wt)

    return {
        "symbol": symbol,
        "state": state,
        "grade": grade,
        "confidence": confidence,
        "direction": direction,
        "ltp": round(current_ltp, 2),
        "range_high": round(range_high, 2),
        "range_low": round(range_low, 2),
        "breakout_level": round(breakout_level, 2),
        "stop_level": round(stop_level, 2),
        "target1": target1,
        "target2": target2,
        "room_to_run_pct": room_to_run_pct,
        "time_in_coil": coil_str,
        "coil_candles": coil_candles,
        "sector": get_sector(symbol),
        "tags": tags,
        "price_discovery": price_discovery,
        # OI summary
        "ce_wall": ce_wall,
        "pe_wall": pe_wall,
        "max_pain": oi_data.get("max_pain", {}),
        "pcr": oi_data.get("pcr", {}),
        "fresh_writing": oi_data.get("fresh_writing", {}),
        "oi_timestamp": oi_data.get("oi_timestamp", "N/A"),
        "wall_tags": wall_tags,
        # Scores (Advanced View)
        "l1_score": l1_score,
        "l2_score": l2_score,
        "l3_score": l3_score,
        "l1_factors": l1["factors"],
        "l2_factors": l2["factors"],
        "l3_factors": l3["factors"],
        # Track record
        "track_badge": track_badge,
        "track_data": track_data,
        # IV
        "iv_rank": iv_rank,
        # Squeeze
        "squeeze_active": squeeze.get("squeeze_active", False),
        "squeeze_candles": squeeze.get("squeeze_candles", 0),
        "bb_expanding": squeeze.get("bb_expanding", False),
        # EOD
        "options_play": options_play,
        # Failed attempts
        "failed_attempts": failed_attempts,
        "breakout_log": sess.get("breakout_log", []),
        "last_breakout_days_ago": last_breakout_days_ago,
        # Multi-timeframe trends
        "trends": trends,
    }


# ── Full Scan Cycle ───────────────────────────────────────────────────────────

def run_scan(kite, symbols: list, get_vwap, get_ltp_fn,
             india_vix_fn, gift_nifty_fn=None) -> dict:
    """
    Run one full scan cycle across all FnO symbols.
    Returns full payload for the /api/accumulation endpoint.
    """
    global _last_scan_results, _last_scan_time

    scan_start = datetime.now()
    now = scan_start
    log.info(f"Scan cycle starting at {now.strftime('%H:%M:%S')}")

    # Market state
    def t(s): return datetime.strptime(s, "%H:%M").time()
    now_t = now.time()
    if now.weekday() >= 5:
        market_state = "MARKET_CLOSED"
    elif now_t < t(MARKET_OPEN):
        market_state = "PRE_MARKET"
    elif now_t < t(PRICE_DISCOVERY_END):
        market_state = "PRICE_DISCOVERY"
    elif now_t < t(EOD_MODE_START):
        market_state = "LIVE"
    elif now_t <= t(MARKET_CLOSE):
        market_state = "EOD_MODE"
    else:
        market_state = "SESSION_CLOSED"

    is_eod = market_state == "EOD_MODE"

    # Market-wide data
    try:
        india_vix = india_vix_fn()
    except Exception:
        india_vix = None

    try:
        gift_nifty = gift_nifty_fn() if gift_nifty_fn else None
    except Exception:
        gift_nifty = None

    # Regime detection (use cached Nifty bars)
    vix_30m_ago = None  # Would need history to compute
    regime = detect_market_regime(_nifty_history, india_vix or 15, vix_30m_ago)

    # Fetch bulk quotes for LTP, VWAP, and Instrument Tokens
    global _scan_progress
    _scan_progress["total"] = len(symbols)
    _scan_progress["current"] = 0
    _scan_progress["status"] = "Fetching bulk quotes..."
    bulk_quotes = {}
    try:
        ex_symbols = [f"NSE:{s}" for s in symbols]
        for i in range(0, len(ex_symbols), 400):
            batch = ex_symbols[i:i+400]
            bulk_quotes.update(kite.quote(batch))
    except Exception as e:
        log.error(f"Bulk quote fetch failed: {e}")

    # Fetch OI for all symbols (batch)
    _scan_progress["status"] = "Fetching Option Chains..."
    _scan_progress["current"] = 0
    oi_results = {}
    oi_failures = 0
    import concurrent.futures

    def _fetch_oi(sym):
        try:
            q = bulk_quotes.get(f"NSE:{sym}")
            ltp_val = q.get("last_price") if q else get_ltp_fn(sym)
            if ltp_val:
                return sym, analyze_oi_for_stock(kite, sym, ltp_val, 0, 0, is_expiry_day())
        except Exception as e:
            return sym, e
        return sym, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = {executor.submit(_fetch_oi, sym): sym for sym in symbols}
        for future in concurrent.futures.as_completed(futures):
            sym, res = future.result()
            _scan_progress["current"] += 1
            if isinstance(res, Exception):
                oi_failures += 1
            elif res is not None:
                oi_results[sym] = res

    oi_degraded = oi_failures / max(len(symbols), 1) > OI_FAILURE_DEGRADE_PCT

    # Stock scanning
    _scan_progress["status"] = "Running Accumulation Scans..."
    _scan_progress["current"] = 0
    results = []
    ohlcv_failures = 0
    raw_results = []

    def _scan_stock(sym):
        try:
            q = bulk_quotes.get(f"NSE:{sym}", {})
            ltp = q.get("last_price") or get_ltp_fn(sym)
            vwap = q.get("average_price") or get_vwap(sym)
            token = q.get("instrument_token")
            # Outside market hours, average_price (VWAP) = 0 — use ohlc.close as proxy; 0 if unavailable
            if not vwap:
                vwap = (q.get("ohlc") or {}).get("close") or 0
            if not ltp:  # VWAP is optional — scan proceeds with vwap=0 if unavailable
                return sym, None
            oi_data = oi_results.get(sym, {})
            result = scan_stock(
                kite, sym, vwap, ltp, india_vix or 15,
                0, regime, oi_data if not oi_degraded else {},
                is_eod, instrument_token=token
            )
            return sym, result
        except Exception as e:
            return sym, e

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(_scan_stock, sym): sym for sym in symbols}
        for future in concurrent.futures.as_completed(futures):
            sym, res = future.result()
            _scan_progress["current"] += 1
            if isinstance(res, Exception):
                ohlcv_failures += 1
                log.warning(f"Stock scan failed for {sym}: {res}")
            elif res is not None:
                raw_results.append(res)

    _scan_progress["status"] = "Finalizing scores..."
    
    # Skip abort check on weekends/holidays — quote failures are expected outside market hours
    if market_state != "MARKET_CLOSED" and ohlcv_failures / max(len(symbols), 1) > OHLCV_FAILURE_ABORT_PCT:
        log.error("Too many OHLCV failures — returning previous results")
        return _build_payload(
            _last_scan_results, market_state, regime, india_vix,
            gift_nifty, oi_degraded, scan_error="SCAN_FAILED"
        )

    # Sector signal computation (second pass)
    sector_sigs = compute_sector_signals(raw_results)

    # Re-score sector contribution per stock
    for r in raw_results:
        sec = r["sector"]
        sec_data = sector_sigs.get(sec, {})
        sec_score = sec_data.get("score", 0)
        if sec_data.get("direction", "NEUTRAL") != r.get("direction", "NEUTRAL"):
            sec_score = -sec_score
        r["l2_score"] += sec_score
        r["sector_score"] = sec_score
        r["sector_data"] = sec_data
        # Recompute confidence with updated scores
        l2_abs = abs(r["l2_score"])
        r["grade"] = assign_grade(
            r["l1_score"], l2_abs, r["l3_score"],
            r["failed_attempts"], r["wall_tags"], regime
        )
        if r["grade"] == "D":
            continue
        results.append(r)

    # Rank: by state (BREAKING > PRE_BREAKOUT > ACCUMULATING), then coil length, then confidence
    conf_order = {"VERY_STRONG": 3, "STRONG": 2, "MODERATE": 1}
    state_prio = {"BREAKING": 4, "PRE_BREAKOUT": 3, "ACCUMULATING": 2, "DISTRIBUTION": 1}
    results.sort(key=lambda x: (
        -state_prio.get(x["state"], 0),
        -x["coil_candles"],
        -conf_order.get(x["confidence"], 0)
    ))

    _last_scan_results = results
    _last_scan_time = now

    scan_duration = (datetime.now() - scan_start).total_seconds()
    if scan_duration > MAX_SCAN_DURATION_SEC:
        log.warning(f"Scan took {scan_duration:.0f}s — exceeded limit")

    return _build_payload(
        results, market_state, regime, india_vix, gift_nifty,
        oi_degraded, scan_duration=scan_duration,
        sector_signals=sector_sigs
    )


def _build_payload(results, market_state, regime, india_vix, gift_nifty,
                   oi_degraded, scan_error=None, scan_duration=None,
                   sector_signals=None):
    """Build the JSON payload for the API endpoint."""
    from event_calendar import (is_expiry_day, is_expiry_tomorrow,
                                 get_calendar_status)

    breaking = [r for r in results if r["state"] == "BREAKING"]
    grade_counts = {
        "A": sum(1 for r in results if r["grade"] == "A"),
        "B": sum(1 for r in results if r["grade"] == "B"),
        "C": sum(1 for r in results if r["grade"] == "C"),
    }

    # Market bias from regime + VIX + dominant direction
    directions = [r["direction"] for r in results if r["confidence"] in ("STRONG", "VERY_STRONG")]
    bull_count = directions.count("UP")
    bear_count = directions.count("DOWN")
    if bull_count > bear_count * 1.5:
        market_bias = "BULLISH"
    elif bear_count > bull_count * 1.5:
        market_bias = "CAUTIOUS"
    else:
        market_bias = "NEUTRAL"

    cal = get_calendar_status()

    return {
        "timestamp": datetime.now().isoformat(),
        "market_state": market_state,
        "regime": regime,
        "india_vix": india_vix,
        "gift_nifty": gift_nifty,
        "market_bias": market_bias,
        "stocks_in_accumulation": grade_counts,
        "breaking_now": breaking,
        "results": results,
        "sector_signals": sector_signals or {},
        "oi_degraded": oi_degraded,
        "scan_error": scan_error,
        "scan_duration_sec": round(scan_duration, 1) if scan_duration else None,
        "expiry_day": is_expiry_day(),
        "expiry_tomorrow": is_expiry_tomorrow(),
        "calendar_status": cal,
        "last_scan": _last_scan_time.isoformat() if _last_scan_time else None,
        "data_date": datetime.now().date().isoformat(),
    }
