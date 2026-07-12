# historical_profile.py — 60-day rolling track record per stock
# Runs nightly after market close. Also supports calibration replay mode.
import os
import json
import logging
import time
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from config import API_CALL_DELAY_SEC

log = logging.getLogger("historical_profile")
PROFILES_FILE = "historical_profiles.json"

# ── Profile Storage ──────────────────────────────────────────────────────────

def load_profiles() -> dict:
    if os.path.exists(PROFILES_FILE):
        with open(PROFILES_FILE) as f:
            return json.load(f)
    return {}

def save_profiles(profiles: dict):
    with open(PROFILES_FILE, "w") as f:
        json.dump(profiles, f, indent=2)

# ── Track Record Computation ─────────────────────────────────────────────────

def compute_track_record(kite, symbol: str) -> dict:
    """
    Fetch 60-day 15-min OHLCV for `symbol` and compute breakout track record.
    Returns: success_rate, false_breakout_rate, avg_move_pct, avg_time_in_coil_candles
    """
    try:
        from_date = (datetime.now() - timedelta(days=62)).date()
        to_date = datetime.now().date()
        time.sleep(API_CALL_DELAY_SEC)
        hist = kite.historical_data(
            kite.ltp(f"NSE:{symbol}")[f"NSE:{symbol}"]["instrument_token"],
            from_date, to_date, "15minute"
        )
        if not hist or len(hist) < 40:
            return {"status": "INSUFFICIENT", "setups": 0}

        df = pd.DataFrame(hist)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()

        successes = 0
        false_breakouts = 0
        total_setups = 0
        move_magnitudes = []
        time_in_coils = []

        # Sliding window to find historical squeeze setups
        window = 26  # lookback for squeeze detection
        for i in range(window, len(df) - 5):
            sub = df.iloc[i-window:i]
            # Simple squeeze proxy: rolling std at low
            rolling_std = sub["close"].rolling(6).std().iloc[-1]
            avg_std = sub["close"].rolling(20).std().mean()
            if rolling_std > avg_std * 0.6:
                continue  # not compressed

            # Range high/low of last 6 candles
            rng = sub.tail(6)
            range_high = rng["high"].max()
            range_low = rng["low"].min()
            range_height = range_high - range_low

            # ATR-based extension threshold
            atr = (sub["high"] - sub["low"]).tail(10).mean()
            ext_threshold = atr  # 1× ATR

            total_setups += 1

            # Evaluate next 3 candles for success
            future = df.iloc[i:i+3]
            breakout_up = False
            breakout_down = False
            false_break = False

            for j in range(len(future)):
                row = future.iloc[j]
                if row["close"] > range_high + ext_threshold:
                    breakout_up = True
                    move_magnitudes.append((row["close"] - range_high) / range_high * 100)
                    break
                elif row["close"] < range_low - ext_threshold:
                    breakout_down = True
                    move_magnitudes.append((range_low - row["close"]) / range_low * 100)
                    break
                elif row["high"] > range_high or row["low"] < range_low:
                    false_break = True

            if breakout_up or breakout_down:
                successes += 1
                time_in_coils.append(6)  # proxy
            elif false_break:
                false_breakouts += 1

        if total_setups < 5:
            return {"status": "INSUFFICIENT", "setups": total_setups}

        success_rate = round(successes / total_setups * 100, 1)
        false_rate = round(false_breakouts / total_setups * 100, 1)
        avg_move = round(float(np.mean(move_magnitudes)), 2) if move_magnitudes else 0.0
        avg_time = round(float(np.mean(time_in_coils)), 1) if time_in_coils else 0.0

        # Badge
        if success_rate > 65 and false_rate < 20:
            badge = "STRONG"
        elif success_rate >= 40:
            badge = "MIXED"
        else:
            badge = "WEAK"

        return {
            "status": "OK",
            "badge": badge,
            "success_rate": success_rate,
            "false_breakout_rate": false_rate,
            "avg_move_pct": avg_move,
            "avg_time_in_coil": avg_time,
            "setups": total_setups,
            "computed_at": datetime.now().isoformat(),
        }

    except Exception as e:
        log.error(f"Track record computation failed for {symbol}: {e}")
        return {"status": "ERROR", "error": str(e)}


def get_track_record(symbol: str) -> dict:
    """Get cached track record from profiles JSON."""
    profiles = load_profiles()
    return profiles.get(symbol, {"status": "MISSING", "badge": "UNKNOWN"})


def run_nightly_update(kite, symbols: list):
    """
    Nightly job: recompute track records for all FnO symbols.
    Call this from a scheduler after 15:30.
    """
    log.info(f"Starting nightly profile update for {len(symbols)} symbols")
    profiles = load_profiles()
    success_count = 0

    for sym in symbols:
        try:
            profile = compute_track_record(kite, sym)
            profiles[sym] = profile
            if profile.get("status") == "OK":
                success_count += 1
            time.sleep(API_CALL_DELAY_SEC * 2)
        except Exception as e:
            log.error(f"Nightly update failed for {sym}: {e}")

    # Purge profiles for symbols removed >30 days ago (not in current list)
    # (symbol removal tracking would need integration with sector_map.refresh)

    save_profiles(profiles)
    log.info(f"Nightly update complete: {success_count}/{len(symbols)} profiles updated")
    return success_count
