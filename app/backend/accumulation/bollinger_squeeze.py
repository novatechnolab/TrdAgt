# bollinger_squeeze.py — LazyBear BB Squeeze + Keltner Channels + Momentum Histogram
import numpy as np
import pandas as pd
from config import BB_PERIOD, BB_STD, BB_KC_MULT, KC_ATR_PERIOD


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"].shift(1)
    tr = pd.concat([high - low, (high - close).abs(), (low - close).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def compute_bb_squeeze(df: pd.DataFrame) -> dict:
    """
    Compute Bollinger Band squeeze (BB inside Keltner Channels).
    
    Returns:
        squeeze_active   : bool — BB inside KC right now
        bb_width         : float — current BB width
        bb_width_min20   : float — 20-candle min of BB width
        at_width_minimum : bool — BB width at or near 20-candle min
        kc_upper/lower   : Keltner channel bounds
        bb_upper/lower   : Bollinger band bounds
        momentum         : Series — histogram value (positive=bullish)
        momentum_now     : float — current bar momentum value
        momentum_rising  : bool — momentum trending up
        momentum_cross   : str — CROSS_UP / CROSS_DOWN / NONE (zero-line cross on last candle)
        squeeze_candles  : int — consecutive candles squeeze has been active
        bb_expanding     : bool — BB width increasing vs prior candle (energy releasing)
    """
    close = df["close"]
    high = df["high"]
    low = df["low"]

    # Bollinger Bands
    bb_mid = close.rolling(BB_PERIOD).mean()
    bb_std = close.rolling(BB_PERIOD).std()
    bb_upper = bb_mid + BB_STD * bb_std
    bb_lower = bb_mid - BB_STD * bb_std
    bb_width = bb_upper - bb_lower

    # Keltner Channels (ATR-based)
    atr = compute_atr(df, KC_ATR_PERIOD)
    kc_mid = close.rolling(KC_ATR_PERIOD).mean()
    kc_upper = kc_mid + BB_KC_MULT * atr
    kc_lower = kc_mid - BB_KC_MULT * atr

    # Squeeze: BB inside KC
    squeeze = (bb_upper < kc_upper) & (bb_lower > kc_lower)

    # Momentum: delta of (close - midpoint(high,low,bb_mid,kc_mid)) — LazyBear formula
    # Simplified robust version: close - midpoint of (20-period high+low average)
    hl_avg = (df["high"].rolling(BB_PERIOD).max() + df["low"].rolling(BB_PERIOD).min()) / 2
    midline = (hl_avg + bb_mid) / 2
    delta = close - midline
    momentum = delta.rolling(BB_PERIOD).mean()

    # Momentum direction
    mom_now = momentum.iloc[-1] if not momentum.empty else 0.0
    mom_prev = momentum.iloc[-2] if len(momentum) > 1 else mom_now
    mom_prev2 = momentum.iloc[-3] if len(momentum) > 2 else mom_prev
    momentum_rising = (mom_now > mom_prev)

    cross_up = (mom_now >= 0) and (mom_prev < 0)
    cross_down = (mom_now < 0) and (mom_prev >= 0)
    momentum_cross = "CROSS_UP" if cross_up else ("CROSS_DOWN" if cross_down else "NONE")

    # Squeeze duration
    squeeze_candles = 0
    for sq in reversed(squeeze.values):
        if sq:
            squeeze_candles += 1
        else:
            break

    # BB expanding (energy releasing)
    bw_now = bb_width.iloc[-1] if not bb_width.empty else 0
    bw_prev = bb_width.iloc[-2] if len(bb_width) > 1 else bw_now
    bb_expanding = bw_now > bw_prev

    # At width minimum (near 20-candle min)
    bw_min20 = bb_width.tail(20).min()
    at_width_minimum = bw_now <= bw_min20 * 1.05  # within 5% of min

    return {
        "squeeze_active": bool(squeeze.iloc[-1]) if not squeeze.empty else False,
        "squeeze_candles": squeeze_candles,
        "bb_width": round(float(bw_now), 4),
        "bb_width_min20": round(float(bw_min20), 4),
        "at_width_minimum": bool(at_width_minimum),
        "bb_expanding": bool(bb_expanding),
        "bb_upper": round(float(bb_upper.iloc[-1]), 2),
        "bb_lower": round(float(bb_lower.iloc[-1]), 2),
        "kc_upper": round(float(kc_upper.iloc[-1]), 2),
        "kc_lower": round(float(kc_lower.iloc[-1]), 2),
        "momentum_now": round(float(mom_now), 4),
        "momentum_rising": bool(momentum_rising),
        "momentum_cross": momentum_cross,
    }


def compute_range_stats(df: pd.DataFrame, lookback: int = 6) -> dict:
    """
    Compute range tightness, volume behaviour, candle body compression.
    Uses last `lookback` candles as the accumulation window.
    """
    if len(df) < lookback + 20:
        return {}

    recent = df.tail(lookback)
    prior = df.iloc[-(lookback + 20):-(lookback)]

    # ATR proxy: avg(high-low) for prior 20 candles
    prior_atr = (prior["high"] - prior["low"]).mean()
    recent_range = (recent["high"].max() - recent["low"].min())

    range_tightness_ratio = (recent_range / prior_atr) if prior_atr > 0 else 1.0

    # Volume dry-up
    prior_vol_avg = prior["volume"].mean()
    recent_vol_avg = recent["volume"].mean()
    volume_ratio = (recent_vol_avg / prior_vol_avg) if prior_vol_avg > 0 else 1.0

    # Candle body compression
    prior_body_avg = (prior["close"] - prior["open"]).abs().mean()
    recent_body_avg = (recent["close"] - recent["open"]).abs().mean()
    body_ratio = (recent_body_avg / prior_body_avg) if prior_body_avg > 0 else 1.0

    # Volume bias: up vs down candle volume
    up_vol = recent[recent["close"] >= recent["open"]]["volume"].sum()
    dn_vol = recent[recent["close"] < recent["open"]]["volume"].sum()
    total_vol = up_vol + dn_vol
    up_vol_pct = (up_vol / total_vol) if total_vol > 0 else 0.5
    dn_vol_pct = (dn_vol / total_vol) if total_vol > 0 else 0.5
    volume_bias = "BULLISH" if up_vol > dn_vol else "BEARISH"
    bias_margin = abs(up_vol_pct - dn_vol_pct)  # how strong the bias is

    # Last candle volume surge
    last_vol = df["volume"].iloc[-1]
    accum_avg_vol = recent["volume"].mean()
    volume_surge = (last_vol / accum_avg_vol) if accum_avg_vol > 0 else 1.0

    # Range high/low
    range_high = float(recent["high"].max())
    range_low = float(recent["low"].min())

    # Post-move check: prior 3 candles move
    if len(df) >= lookback + 3:
        pre_range = df.iloc[-(lookback + 3):-(lookback)]
        pre_move_pct = abs(pre_range["close"].iloc[-1] - pre_range["close"].iloc[0]) / pre_range["close"].iloc[0] * 100
    else:
        pre_move_pct = 0.0

    return {
        "range_high": round(range_high, 2),
        "range_low": round(range_low, 2),
        "range_height": round(range_high - range_low, 2),
        "range_tightness_ratio": round(float(range_tightness_ratio), 3),
        "volume_ratio": round(float(volume_ratio), 3),
        "body_ratio": round(float(body_ratio), 3),
        "volume_bias": volume_bias,
        "bias_margin": round(float(bias_margin), 3),
        "volume_surge": round(float(volume_surge), 2),
        "up_vol_pct": round(float(up_vol_pct), 3),
        "dn_vol_pct": round(float(dn_vol_pct), 3),
        "pre_move_pct": round(float(pre_move_pct), 2),
        "prior_atr": round(float(prior_atr), 2),
    }
