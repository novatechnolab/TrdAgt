import sqlite3
import time
import datetime
import os
import logging

# Database path in same directory as backend files
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tradesignal_cache.db')

def init_db():
    """Initializes transition-specific cache tables inside tradesignal_cache.db."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS oi_transition_states (
                symbol TEXT NOT NULL,
                strike REAL NOT NULL,
                expiry TEXT NOT NULL,
                leg TEXT NOT NULL,
                premium REAL NOT NULL,
                oi INTEGER NOT NULL,
                state TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (symbol, strike, expiry, leg)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS oi_transition_v2_meta (
                symbol TEXT PRIMARY KEY,
                last_snapshot_time REAL NOT NULL,
                last_calculated_bias TEXT DEFAULT 'Neutral',
                last_confirmed_bias TEXT DEFAULT 'Neutral'
            )
        """)
        conn.commit()
    except Exception as e:
        logging.getLogger(__name__).error(f"[Transition V2] Failed to initialize DB tables: {e}")
    finally:
        conn.close()

def classify_buildup(oi_chg, cur_ltp, prv_ltp):
    """Classifies leg buildup based on cumulative daily changes."""
    if prv_ltp <= 0 or cur_ltp <= 0:
        return "Flat"
    if oi_chg == 0:
        return "Flat"
    
    price_up = cur_ltp > prv_ltp * 1.0025
    oi_up = oi_chg > 0
    
    if oi_up and price_up:
        return "LB"
    elif oi_up and not price_up:
        return "SB"
    elif not oi_up and price_up:
        return "SC"
    else:
        return "LU"

def process_symbol_transitions(symbol, chain_rows, expiry, ltp, iv_event_windows=[]):
    """
    Computes real-time state transition conviction scores for a symbol's option chain.
    Derived directly from daily cumulative buildup states (the heatmap) across ATM±5.
    Persistence confirms bias updates only when held for 2 consecutive 5-min intervals.
    """
    if not chain_rows or not ltp:
        return None

    init_db()
    current_time = time.time()
    today_date = datetime.date.today().isoformat()

    # 1. Sort the chain and locate the ATM strike
    sorted_chain = sorted(chain_rows, key=lambda x: x["strike"])
    atm_idx = min(range(len(sorted_chain)), key=lambda i: abs(sorted_chain[i]["strike"] - ltp))
    atm_strike = sorted_chain[atm_idx]["strike"]

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 2. Retrieve metadata for the symbol
    cursor.execute("""
        SELECT last_snapshot_time, last_calculated_bias, last_confirmed_bias 
        FROM oi_transition_v2_meta WHERE symbol = ?
    """, (symbol,))
    meta = cursor.fetchone()

    # Check if we need to reset (first run, or date mismatch indicating a new day)
    should_reset = False
    if meta:
        last_snap_ts = meta[0]
        last_snap_date = datetime.date.fromtimestamp(last_snap_ts).isoformat()
        if last_snap_date != today_date:
            should_reset = True

    if not meta or should_reset:
        # Seed initial state
        if should_reset:
            cursor.execute("DELETE FROM oi_transition_states WHERE symbol = ?", (symbol,))
            cursor.execute("DELETE FROM oi_transition_v2_meta WHERE symbol = ?", (symbol,))
            conn.commit()

        cursor.execute("""
            INSERT OR REPLACE INTO oi_transition_v2_meta 
            (symbol, last_snapshot_time, last_calculated_bias, last_confirmed_bias)
            VALUES (?, ?, 'Neutral', 'Neutral')
        """, (symbol, current_time))

        for i, row in enumerate(sorted_chain):
            if abs(i - atm_idx) <= 5:
                strike = row["strike"]
                for leg in ("CE", "PE"):
                    prefix = leg.lower()
                    oi_chg = row.get(f"{prefix}_oi_chg", 0)
                    curr_ltp = row.get(f"{prefix}_ltp", 0.0)
                    prev_ltp = row.get(f"{prefix}_prev_ltp", curr_ltp)
                    curr_state = classify_buildup(oi_chg, curr_ltp, prev_ltp)
                    cursor.execute("""
                        INSERT OR REPLACE INTO oi_transition_states 
                        (symbol, strike, expiry, leg, premium, oi, state, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """, (symbol, strike, expiry, leg, curr_ltp, row.get(f"{prefix}_oi", 0), curr_state))
        conn.commit()

        last_snap_ts = current_time
        last_calculated_bias = "Neutral"
        last_confirmed_bias = "Neutral"
    else:
        last_snap_ts, last_calculated_bias, last_confirmed_bias = meta

    elapsed = current_time - last_snap_ts

    # 3. Handle 5-Minute Interval Boundary Completion
    if elapsed >= 300:  # 5 minutes
        # Compute interval score to determine the new calculated bias (B_t)
        interval_scores = []
        for i, row in enumerate(sorted_chain):
            if abs(i - atm_idx) <= 5:
                strike = row["strike"]
                # CE Leg
                ce_oi_chg = row.get("ce_oi_chg", 0)
                ce_ltp = row.get("ce_ltp", 0.0)
                ce_prev_ltp = row.get("ce_prev_ltp", ce_ltp)
                ce_state = classify_buildup(ce_oi_chg, ce_ltp, ce_prev_ltp)
                ce_dir = 1 if ce_state in ("SC", "LB") else -1 if ce_state in ("SB", "LU") else 0
                ce_weight = 2 if strike < ltp else 1
                interval_scores.append(ce_dir * ce_weight)

                # PE Leg
                pe_oi_chg = row.get("pe_oi_chg", 0)
                pe_ltp = row.get("pe_ltp", 0.0)
                pe_prev_ltp = row.get("pe_prev_ltp", pe_ltp)
                pe_state = classify_buildup(pe_oi_chg, pe_ltp, pe_prev_ltp)
                pe_dir = 1 if pe_state in ("SB", "LU") else -1 if pe_state in ("SC", "LB") else 0
                pe_weight = 2 if strike > ltp else 1
                interval_scores.append(pe_dir * pe_weight)

        interval_sum = sum(interval_scores)

        # Convert to interval bias (B_t)
        if interval_sum > 2:
            B_t = "Bullish"
        elif interval_sum < -2:
            B_t = "Bearish"
        else:
            B_t = "Neutral"

        # Apply persistence filter (C_t)
        if B_t == last_calculated_bias:
            C_t = B_t
        else:
            C_t = last_confirmed_bias

        # Save updates and take a new 5-minute snapshot baseline states
        cursor.execute("""
            UPDATE oi_transition_v2_meta
            SET last_snapshot_time = ?, last_calculated_bias = ?, last_confirmed_bias = ?
            WHERE symbol = ?
        """, (current_time, B_t, C_t, symbol))

        for i, row in enumerate(sorted_chain):
            if abs(i - atm_idx) <= 5:
                strike = row["strike"]
                for leg in ("CE", "PE"):
                    prefix = leg.lower()
                    oi_chg = row.get(f"{prefix}_oi_chg", 0)
                    curr_ltp = row.get(f"{prefix}_ltp", 0.0)
                    prev_ltp = row.get(f"{prefix}_prev_ltp", curr_ltp)
                    curr_state = classify_buildup(oi_chg, curr_ltp, prev_ltp)
                    cursor.execute("""
                        INSERT OR REPLACE INTO oi_transition_states 
                        (symbol, strike, expiry, leg, premium, oi, state, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """, (symbol, strike, expiry, leg, curr_ltp, row.get(f"{prefix}_oi", 0), curr_state))
        conn.commit()

        # Update runtime variables for current API return payload
        last_snap_ts = current_time
        last_calculated_bias = B_t
        last_confirmed_bias = C_t

    # 4. Fetch Active Snapshotted States from DB for transition display
    cursor.execute("SELECT strike, leg, state FROM oi_transition_states WHERE symbol = ?", (symbol,))
    db_states = {(r[0], r[1]): r[2] for r in cursor.fetchall()}

    strike_details = []
    realtime_scores = []

    for i, row in enumerate(sorted_chain):
        dist = abs(i - atm_idx)
        if dist <= 5:
            strike = row["strike"]
            # Map zones for frontend visual splitting (ATM±3 in table, ATM±4-5 in registry)
            zone = "ATM±3" if dist <= 3 else "ATM±4-7"

            # CE
            ce_oi_chg = row.get("ce_oi_chg", 0)
            ce_ltp = row.get("ce_ltp", 0.0)
            ce_prev_ltp = row.get("ce_prev_ltp", ce_ltp)
            ce_state = classify_buildup(ce_oi_chg, ce_ltp, ce_prev_ltp)
            ce_dir = 1 if ce_state in ("SC", "LB") else -1 if ce_state in ("SB", "LU") else 0
            ce_weight = 2 if strike < ltp else 1
            ce_score = ce_dir * ce_weight
            realtime_scores.append(ce_score)
            
            from_ce = db_states.get((strike, "CE"), ce_state)

            # PE
            pe_oi_chg = row.get("pe_oi_chg", 0)
            pe_ltp = row.get("pe_ltp", 0.0)
            pe_prev_ltp = row.get("pe_prev_ltp", pe_ltp)
            pe_state = classify_buildup(pe_oi_chg, pe_ltp, pe_prev_ltp)
            pe_dir = 1 if pe_state in ("SB", "LU") else -1 if pe_state in ("SC", "LB") else 0
            pe_weight = 2 if strike > ltp else 1
            pe_score = pe_dir * pe_weight
            realtime_scores.append(pe_score)

            from_pe = db_states.get((strike, "PE"), pe_state)

            comp_score = ce_score + pe_score

            strike_details.append({
                "strike": strike,
                "zone": zone,
                "ce": {
                    "from_state": from_ce,
                    "to_state": ce_state,
                    "score": ce_score
                },
                "pe": {
                    "from_state": from_pe,
                    "to_state": pe_state,
                    "score": pe_score
                },
                "composite": comp_score,
                "bias": "Bullish" if comp_score > 0 else "Bearish" if comp_score < 0 else "Neutral",
                "strength": "High" if abs(comp_score) >= 3 else "Medium" if abs(comp_score) >= 2 else "Low"
            })

    realtime_composite = sum(realtime_scores)
    conn.close()

    # Calculate net drift based on above/below ATM strike scores
    above_sum = sum(d["composite"] for d in strike_details if d["strike"] > ltp)
    below_sum = sum(d["composite"] for d in strike_details if d["strike"] < ltp)

    if above_sum > 0 and below_sum > 0:
        net_drift = "Bullish (Lifting)"
    elif above_sum < 0 and below_sum < 0:
        net_drift = "Bearish (Sinking)"
    elif above_sum < 0 and below_sum > 0:
        net_drift = "Range-Bound"
    else:
        # Expansion / Breakout: resolve direction based on relative strength
        if above_sum > abs(below_sum):
            net_drift = "Bullish Breakout"
        elif above_sum < abs(below_sum):
            net_drift = "Bearish Breakout"
        else:
            net_drift = "Bullish Breakout" if realtime_composite >= 0 else "Bearish Breakout"

    # 5. Return JSON payload matching UI specifications
    return {
        "symbol": symbol,
        "atm_strike": atm_strike,
        "composite_score": realtime_composite,
        "bias": last_confirmed_bias,       # Active confirmed label (C_t)
        "net_drift": net_drift,
        "strike_details": strike_details,
        "alerts": [],
        "is_event_window": False
    }

def run_housekeeping():
    """Housekeeping keeps DB clean."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM oi_transition_states")
        cursor.execute("DELETE FROM oi_transition_v2_meta")
        conn.commit()
        conn.close()
    except Exception as e:
        logging.getLogger(__name__).warning(f"[Transition V2] Housekeeping failed: {e}")
