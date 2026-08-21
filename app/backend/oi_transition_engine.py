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
        
        # Safe migration check: drop table if it uses the older single-state schema
        cursor.execute("PRAGMA table_info(oi_transition_states)")
        cols = [col[1] for col in cursor.fetchall()]
        if cols and "state" in cols:
            cursor.execute("DROP TABLE IF EXISTS oi_transition_states")
            
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS oi_transition_states (
                symbol TEXT NOT NULL,
                strike REAL NOT NULL,
                expiry TEXT NOT NULL,
                leg TEXT NOT NULL,
                premium REAL NOT NULL,
                oi INTEGER NOT NULL,
                baseline_status TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
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


def initialize_daily_baselines(kite=None):
    """
    Runs daily (normally at 09:30 hrs) to fetch baseline status for all F&O stocks.
    Unconditionally deletes all data in the table by default before loading any data.
    Will exit immediately on weekends or holidays (non-market days).
    Also checks if baseline data for today already exists in the database and bypasses if so (restores on restart).
    """
    import pytz
    tz = pytz.timezone("Asia/Kolkata")
    now = datetime.datetime.now(tz)
    today_date = now.date().isoformat()
    
    # 1. Exit immediately on weekends (Saturday=5, Sunday=6)
    if now.weekday() > 4:
        logging.getLogger(__name__).info("[Daily Baseline] Skipping initialization (Weekend).")
        return False
        
    logging.getLogger(__name__).info("[Daily Baseline] Starting daily baseline setup...")
    
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        # Pre-flight Check: Exit if baseline already exists for today (restart protection)
        cursor.execute("SELECT last_snapshot_time FROM oi_transition_v2_meta LIMIT 1")
        meta_row = cursor.fetchone()
        if meta_row:
            last_snap_ts = meta_row[0]
            last_snap_date = datetime.date.fromtimestamp(last_snap_ts).isoformat()
            if last_snap_date == today_date:
                cursor.execute("SELECT COUNT(*) FROM oi_transition_states")
                record_count = cursor.fetchone()[0]
                if record_count > 0:
                    logging.getLogger(__name__).info("[Daily Baseline] Baseline records for today already exist in DB. Skipping setup.")
                    return True

        # 2. Unconditionally delete all data in the tables by default before load
        cursor.execute("DELETE FROM oi_transition_states")
        cursor.execute("DELETE FROM oi_transition_v2_meta")
        conn.commit()
        logging.getLogger(__name__).info("[Daily Baseline] Unconditional database purge completed.")
        
        # 3. If kite is not provided, fetch it lazily
        if not kite:
            try:
                from server import get_kite
                kite = get_kite()
            except ImportError:
                pass
                
        if not kite:
            logging.getLogger(__name__).error("[Daily Baseline] Kite Connect session not available. Baseline load aborted.")
            return False

        # 4. Fetch all active F&O underlyings from DB
        cursor.execute("SELECT DISTINCT name FROM instruments WHERE segment = 'NFO-FUT' AND name IS NOT NULL AND name != ''")
        underlyings = sorted(list({r[0] for r in cursor.fetchall()}))

        if not underlyings:
            underlyings = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK"]

        logging.getLogger(__name__).info(f"[Daily Baseline] Resolving initial states for {len(underlyings)} symbols...")

        # 5. Fetch initial states and write baseline records
        from oi_spurt_routes import get_option_chain
        current_time = time.time()
        
        for sym in underlyings:
            try:
                chain, expiry_val, _, _, ltp_val, _, _ = get_option_chain(kite, sym)
                if not chain or not ltp_val:
                    continue
                    
                sorted_chain = sorted(chain, key=lambda x: x["strike"])
                atm_idx = min(range(len(sorted_chain)), key=lambda i: abs(sorted_chain[i]["strike"] - ltp_val))
                
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
                                (symbol, strike, expiry, leg, premium, oi, baseline_status, from_state, to_state, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'NA', CURRENT_TIMESTAMP)
                            """, (sym, strike, expiry_val or "–", leg, curr_ltp, row.get(f"{prefix}_oi", 0), curr_state, curr_state))
                            
                # Insert meta row
                cursor.execute("""
                    INSERT OR REPLACE INTO oi_transition_v2_meta
                    (symbol, last_snapshot_time, last_calculated_bias, last_confirmed_bias)
                    VALUES (?, ?, 'Neutral', 'Neutral')
                """, (sym, current_time))
                
                conn.commit()
            except Exception as ex:
                logging.getLogger(__name__).warning(f"[Daily Baseline] Error for {sym}: {ex}")
                
        logging.getLogger(__name__).info("[Daily Baseline] Daily baseline setup completed successfully.")
        return True
    except Exception as e:
        logging.getLogger(__name__).error(f"[Daily Baseline] Baseline setup failed: {e}")
        return False
    finally:
        conn.close()

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

        # Fetch existing DB states
        cursor.execute("SELECT strike, leg, baseline_status, from_state, to_state FROM oi_transition_states WHERE symbol = ?", (symbol,))
        db_states = {(r[0], r[1]): (r[2], r[3], r[4]) for r in cursor.fetchall()}

        for i, row in enumerate(sorted_chain):
            if abs(i - atm_idx) <= 5:
                strike = row["strike"]
                for leg in ("CE", "PE"):
                    prefix = leg.lower()
                    oi_chg = row.get(f"{prefix}_oi_chg", 0)
                    curr_ltp = row.get(f"{prefix}_ltp", 0.0)
                    prev_ltp = row.get(f"{prefix}_prev_ltp", curr_ltp)
                    curr_state = classify_buildup(oi_chg, curr_ltp, prev_ltp)
                    
                    existing = db_states.get((strike, leg))
                    if not existing:
                        baseline_status = curr_state
                        from_state = curr_state
                        to_state = "NA"
                    else:
                        baseline_status, prev_from, prev_to = existing
                        if prev_to == "NA":
                            if curr_state != baseline_status:
                                from_state = baseline_status
                                to_state = curr_state
                            else:
                                from_state = baseline_status
                                to_state = "NA"
                        else:
                            if curr_state != prev_to:
                                from_state = prev_to
                                to_state = curr_state
                            else:
                                from_state = prev_from
                                to_state = prev_to
                    
                    cursor.execute("""
                        INSERT OR REPLACE INTO oi_transition_states 
                        (symbol, strike, expiry, leg, premium, oi, baseline_status, from_state, to_state, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """, (symbol, strike, expiry or "–", leg, curr_ltp, row.get(f"{prefix}_oi", 0), baseline_status, from_state, to_state))
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
                # Fetch DB status to calculate scoring
                cursor.execute("""
                    SELECT baseline_status, to_state FROM oi_transition_states 
                    WHERE symbol = ? AND strike = ? AND leg = 'CE'
                """, (symbol, strike))
                ce_row = cursor.fetchone()
                if ce_row:
                    ce_base, ce_to = ce_row
                    ce_state = ce_to if ce_to != "NA" else ce_base
                else:
                    ce_state = "Flat"
                
                ce_dir = 1 if ce_state in ("SC", "LB") else -1 if ce_state in ("SB", "LU") else 0
                ce_weight = 2 if strike < ltp else 1
                interval_scores.append(ce_dir * ce_weight)

                cursor.execute("""
                    SELECT baseline_status, to_state FROM oi_transition_states 
                    WHERE symbol = ? AND strike = ? AND leg = 'PE'
                """, (symbol, strike))
                pe_row = cursor.fetchone()
                if pe_row:
                    pe_base, pe_to = pe_row
                    pe_state = pe_to if pe_to != "NA" else pe_base
                else:
                    pe_state = "Flat"
                
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

        # Save updates only to meta table
        cursor.execute("""
            UPDATE oi_transition_v2_meta
            SET last_snapshot_time = ?, last_calculated_bias = ?, last_confirmed_bias = ?
            WHERE symbol = ?
        """, (current_time, B_t, C_t, symbol))
        conn.commit()

        # Update runtime variables for current API return payload
        last_snap_ts = current_time
        last_calculated_bias = B_t
        last_confirmed_bias = C_t

    # 4. Fetch Active Snapshotted States from DB for transition display
    # Run dynamic transition updates on current live status
    cursor.execute("SELECT strike, leg, baseline_status, from_state, to_state FROM oi_transition_states WHERE symbol = ?", (symbol,))
    db_states = {(r[0], r[1]): (r[2], r[3], r[4]) for r in cursor.fetchall()}

    for i, row in enumerate(sorted_chain):
        if abs(i - atm_idx) <= 5:
            strike = row["strike"]
            for leg in ("CE", "PE"):
                prefix = leg.lower()
                oi_chg = row.get(f"{prefix}_oi_chg", 0)
                curr_ltp = row.get(f"{prefix}_ltp", 0.0)
                prev_ltp = row.get(f"{prefix}_prev_ltp", curr_ltp)
                curr_state = classify_buildup(oi_chg, curr_ltp, prev_ltp)

                existing = db_states.get((strike, leg))
                if not existing:
                    baseline_status = curr_state
                    from_state = curr_state
                    to_state = "NA"
                else:
                    baseline_status, prev_from, prev_to = existing
                    if prev_to == "NA":
                        if curr_state != baseline_status:
                            from_state = baseline_status
                            to_state = curr_state
                        else:
                            from_state = baseline_status
                            to_state = "NA"
                    else:
                        if curr_state != prev_to:
                            from_state = prev_to
                            to_state = curr_state
                        else:
                            from_state = prev_from
                            to_state = prev_to

                cursor.execute("""
                    INSERT OR REPLACE INTO oi_transition_states 
                    (symbol, strike, expiry, leg, premium, oi, baseline_status, from_state, to_state, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """, (symbol, strike, expiry or "–", leg, curr_ltp, row.get(f"{prefix}_oi", 0), baseline_status, from_state, to_state))
                # Update in-memory dict for rendering
                db_states[(strike, leg)] = (baseline_status, from_state, to_state)
    conn.commit()

    strike_details = []
    realtime_scores = []

    for i, row in enumerate(sorted_chain):
        dist = abs(i - atm_idx)
        if dist <= 5:
            strike = row["strike"]
            zone = "ATM±3" if dist <= 3 else "ATM±4-7"

            # CE
            existing_ce = db_states.get((strike, "CE"))
            if existing_ce:
                base_ce, from_ce, to_ce = existing_ce
            else:
                base_ce, from_ce, to_ce = "Flat", "Flat", "NA"
            active_ce = to_ce if to_ce != "NA" else base_ce
            ce_dir = 1 if active_ce in ("SC", "LB") else -1 if active_ce in ("SB", "LU") else 0
            ce_weight = 2 if strike < ltp else 1
            ce_score = ce_dir * ce_weight
            realtime_scores.append(ce_score)

            # PE
            existing_pe = db_states.get((strike, "PE"))
            if existing_pe:
                base_pe, from_pe, to_pe = existing_pe
            else:
                base_pe, from_pe, to_pe = "Flat", "Flat", "NA"
            active_pe = to_pe if to_pe != "NA" else base_pe
            pe_dir = 1 if active_pe in ("SB", "LU") else -1 if active_pe in ("SC", "LB") else 0
            pe_weight = 2 if strike > ltp else 1
            pe_score = pe_dir * pe_weight
            realtime_scores.append(pe_score)

            comp_score = ce_score + pe_score

            strike_details.append({
                "strike": strike,
                "zone": zone,
                "ce": {
                    "from_state": from_ce,
                    "to_state": to_ce if to_ce != "NA" else from_ce,
                    "score": ce_score
                },
                "pe": {
                    "from_state": from_pe,
                    "to_state": to_pe if to_pe != "NA" else from_pe,
                    "score": pe_score
                },
                "composite": comp_score,
                "bias": "Bullish" if comp_score > 0 else "Bearish" if comp_score < 0 else "Neutral",
                "strength": "High" if abs(comp_score) >= 3 else "Medium" if abs(comp_score) >= 2 else "Low"
            })

    realtime_composite = sum(realtime_scores)
    conn.close()

    # 5. Return JSON payload matching UI specifications
    return {
        "symbol": symbol,
        "atm_strike": atm_strike,
        "composite_score": realtime_composite,
        "bias": last_confirmed_bias,       # Active confirmed label (C_t)
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
