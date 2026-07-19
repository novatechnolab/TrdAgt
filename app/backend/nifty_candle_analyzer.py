"""
================================================================================
📊 Nifty Index Candle-by-Candle Move Analyzer
================================================================================
Analyzes price contributions, sector rotations, and volume conviction (RVOL)
for Nifty constituent price movements on a candle-by-candle basis.
"""

import os
import json
import logging
import sqlite3
from datetime import datetime, date
from statistics import mean
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

# DB Path
_DB_PATH = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tradesignal_cache.db"))

# Static reference weights for Nifty 50 constituents (normalized dynamically)
_CONSTITUENTS_RAW = {
    "HDFCBANK": (0.115, "Financial Services"),
    "RELIANCE": (0.098, "Oil Gas & Fuels"),
    "ICICIBANK": (0.078, "Financial Services"),
    "INFY": (0.056, "Information Technology"),
    "LT": (0.043, "Construction"),
    "TCS": (0.041, "Information Technology"),
    "ITC": (0.041, "Fast Moving Consumer Goods"),
    "BHARTIARTL": (0.038, "Telecommunication"),
    "AXISBANK": (0.032, "Financial Services"),
    "KOTAKBANK": (0.030, "Financial Services"),
    "SBIN": (0.028, "Financial Services"),
    "M&M": (0.024, "Automobile"),
    "HINDUNILVR": (0.024, "Fast Moving Consumer Goods"),
    "TATAMOTORS": (0.022, "Automobile"),
    "BAJFINANCE": (0.020, "Financial Services"),
    "MARUTI": (0.017, "Automobile"),
    "SUNPHARMA": (0.017, "Healthcare"),
    "HCLTECH": (0.017, "Information Technology"),
    "NTPC": (0.016, "Power"),
    "TATASTEEL": (0.015, "Metals & Mining"),
    "ASIANPAINT": (0.014, "Consumer Durables"),
    "TITAN": (0.014, "Consumer Durables"),
    "ADANIENT": (0.013, "Metals & Mining"),
    "JIOFIN": (0.012, "Financial Services"),
    "ULTRACEMCO": (0.012, "Construction Materials"),
    "COALINDIA": (0.012, "Metals & Mining"),
    "POWERGRID": (0.011, "Power"),
    "JSWSTEEL": (0.010, "Metals & Mining"),
    "ADANIPORTS": (0.010, "Services"),
    "GRASIM": (0.009, "Construction Materials"),
    "HINDALCO": (0.009, "Metals & Mining"),
    "BAJAJFINSV": (0.009, "Financial Services"),
    "NESTLEIND": (0.009, "Fast Moving Consumer Goods"),
    "TECHM": (0.008, "Information Technology"),
    "BRITANNIA": (0.008, "Fast Moving Consumer Goods"),
    "CIPLA": (0.007, "Healthcare"),
    "EICHERMOT": (0.007, "Automobile"),
    "SHRIRAMFIN": (0.007, "Financial Services"),
    "TATACONSUM": (0.007, "Fast Moving Consumer Goods"),
    "BPCL": (0.006, "Oil Gas & Fuels"),
    "INDUSINDBK": (0.006, "Financial Services"),
    "DRREDDY": (0.006, "Healthcare"),
    "DIVISLAB": (0.006, "Healthcare"),
    "HEROMOTOCO": (0.006, "Automobile"),
    "SBILIFE": (0.006, "Financial Services"),
    "APOLLOHOSP": (0.006, "Healthcare"),
    "WIPRO": (0.006, "Information Technology"),
    "LTIM": (0.005, "Information Technology"),
    "ONGC": (0.005, "Oil Gas & Fuels"),
    "HDFCLIFE": (0.005, "Financial Services"),
}

# Normalize weights so they sum to exactly 1.0
_TOTAL_RAW_WEIGHT = sum(w for w, _ in _CONSTITUENTS_RAW.values())
CONSTITUENTS = {
    sym: (round(raw_w / _TOTAL_RAW_WEIGHT, 6), sector)
    for sym, (raw_w, sector) in _CONSTITUENTS_RAW.items()
}

SECTORS = sorted(list({sec for _, sec in CONSTITUENTS.values()}))

# Thresholds
CONCENTRATION_THRESHOLD = 0.50
SECTOR_DOMINANCE_THRESHOLD = 0.50

def init_db():
    """Create the nifty weights and candle analysis tables if they do not exist."""
    try:
        conn = sqlite3.connect(_DB_PATH)
        # Create weights table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS nifty_weights (
                symbol TEXT PRIMARY KEY,
                weight REAL NOT NULL,
                sector TEXT NOT NULL
            )
        """)
        # Create analysis table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS nifty_candle_analysis (
                timestamp TEXT PRIMARY KEY,
                direction TEXT NOT NULL,
                net_contribution REAL NOT NULL,
                weighted_breadth REAL NOT NULL,
                driver TEXT NOT NULL,
                flagged_symbols TEXT,
                flagged_sector TEXT,
                rvol_results TEXT,
                conviction TEXT,
                analysis_details TEXT NOT NULL
            )
        """)
        conn.commit()

        # Gracefully handle upgrade from previous execution
        try:
            conn.execute("ALTER TABLE nifty_candle_analysis ADD COLUMN direction TEXT DEFAULT 'choppy'")
            conn.commit()
        except Exception:
            pass
        
        # Populate nifty_weights
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM nifty_weights")
        if cursor.fetchone()[0] == 0:
            items = [(sym, w, sec) for sym, (w, sec) in CONSTITUENTS.items()]
            conn.executemany("INSERT INTO nifty_weights (symbol, weight, sector) VALUES (?, ?, ?)", items)
            conn.commit()
            log.info(f"[Nifty Analyzer] Seeded {len(items)} constituent weights into nifty_weights table.")
            
        conn.close()
    except Exception as e:
        log.warning(f"[Nifty Analyzer] Database initialization failed: {e}")

def get_nifty_fut_token():
    """Dynamically resolves the active front-month Nifty futures contract token."""
    try:
        from db_instruments import get_cached_instruments
        nfo = get_cached_instruments("NFO")
        nifty_futs = [i for i in nfo if i.get('name') == 'NIFTY' and i.get('segment') == 'NFO-FUT']
        if not nifty_futs:
            return None, None
            
        # Parse expiry date
        def _parse_exp(exp):
            if isinstance(exp, date):
                return exp
            try:
                return datetime.strptime(exp[:10], "%Y-%m-%d").date()
            except Exception:
                return date.max
                
        nifty_futs.sort(key=lambda i: _parse_exp(i.get('expiry')))
        fut = nifty_futs[0]
        return fut.get('instrument_token'), fut.get('tradingsymbol')
    except Exception as e:
        log.warning(f"[Nifty Analyzer] Failed to resolve Nifty Futures token: {e}")
        return None, None

def analyze_and_store_candle(kite, scan_results: Dict[str, Dict], timestamp_str: str):
    """
    Computes candle-by-candle contribution, breadth, drivers, and conviction.
    Stores analysis to nifty_candle_analysis database table.
    
    scan_results: { symbol: { last_candle_close, last_candle_open, last_candle_vol, prev_candle_close } }
    """
    init_db()
    
    # Check if NIFTY spot itself exists in the scan results to confirm target timestamp
    nifty_spot_data = scan_results.get("NIFTY")
    if not nifty_spot_data:
        log.debug("[Nifty Analyzer] Deferred analysis: NIFTY spot candle details missing in this cycle.")
        return
        
    contributions = []
    up_weight = 0.0
    total_scanned_weight = 0.0
    
    sector_up_weights = {sec: 0.0 for sec in SECTORS}
    sector_total_weights = {sec: 0.0 for sec in SECTORS}
    sector_contributions = {sec: 0.0 for sec in SECTORS}
    
    for symbol, (weight, sector) in CONSTITUENTS.items():
        data = scan_results.get(symbol)
        if not data:
            continue
            
        close = data.get("last_candle_close", 0)
        prev = data.get("prev_candle_close", 0)
        if close <= 0 or prev <= 0:
            continue
            
        pct_change = (close - prev) / prev
        contrib = weight * pct_change
        
        contributions.append({
            "symbol": symbol,
            "sector": sector,
            "weight": weight,
            "pct_change": round(pct_change * 100, 4),
            "contribution": round(contrib * 100, 4)
        })
        
        # Breadth sums
        total_scanned_weight += weight
        sector_total_weights[sector] += weight
        sector_contributions[sector] += contrib
        
        if pct_change > 0:
            up_weight += weight
            sector_up_weights[sector] += weight
            
    if not contributions:
        log.warning("[Nifty Analyzer] No valid constituent candle close prices resolved.")
        return
        
    # Sort contributions by absolute value desc
    contributions.sort(key=lambda c: abs(c["contribution"]), reverse=True)
    net_contrib = sum(c["contribution"] for c in contributions)
    
    # Calculate breadths
    weighted_breadth = round((up_weight / total_scanned_weight) * 100, 2) if total_scanned_weight > 0 else 50.0
    
    sector_breadth = {}
    for sec in SECTORS:
        sec_total = sector_total_weights[sec]
        sector_breadth[sec] = round((sector_up_weights[sec] / sec_total) * 100, 2) if sec_total > 0 else 50.0
        sector_contributions[sec] = round(sector_contributions[sec] * 100, 4)
        
    # Identify Driver
    driver = "broad_based"
    flagged_symbols = []
    flagged_sector = None
    
    if abs(net_contrib) > 1e-6:
        # Concentrated check (top 3 explain > 50% of move)
        top3 = contributions[:3]
        top3_sum = sum(c["contribution"] for c in top3)
        if abs(top3_sum) >= CONCENTRATION_THRESHOLD * abs(net_contrib):
            driver = "concentrated"
            flagged_symbols = [c["symbol"] for c in top3]
        else:
            # Sector rotation check
            same_dir_sectors = {
                sec: val for sec, val in sector_contributions.items()
                if (val > 0) == (net_contrib > 0)
            }
            if same_dir_sectors:
                dom_sector, dom_value = max(same_dir_sectors.items(), key=lambda kv: abs(kv[1]))
                if abs(dom_value) >= SECTOR_DOMINANCE_THRESHOLD * abs(net_contrib):
                    driver = "sector_rotation"
                    flagged_sector = dom_sector
                    # Top 4 stocks in that sector
                    flagged_symbols = [c["symbol"] for c in contributions if c["sector"] == dom_sector][:4]
                    
    # Conviction Check (RVOL) on flagged symbols + Nifty Futures
    rvol_results = {}
    conviction = "not_checked"
    
    if driver != "broad_based" and flagged_symbols:
        # Resolve dynamic future token
        fut_token, fut_symbol = get_nifty_fut_token()
        
        symbols_to_check = list(flagged_symbols)
        if fut_symbol:
            symbols_to_check.append(fut_symbol)
            
        import volume_baseline
        # Determine current slot time (HH:MM) from timestamp_str
        try:
            # Assumes format "YYYY-MM-DD HH:MM:SS" or "YYYY-MM-DD HH:MM"
            slot_str = timestamp_str[11:16]
        except Exception:
            slot_str = None
            
        if slot_str:
            for sym in symbols_to_check:
                # Query recent candle volume from scan_results dictionary
                sym_data = scan_results.get(sym)
                vol = sym_data.get("last_candle_vol", 0) if sym_data else 0
                vol_ratio, _ = volume_baseline.get_vol_ratio(sym, '5m', slot_str, vol)
                if vol_ratio is not None:
                    rvol_results[sym] = vol_ratio
                    
            if rvol_results:
                avg_rvol = mean(rvol_results.values())
                conviction = "confirmed" if avg_rvol >= 1.5 else "thin"
                
    # Direction Classification:
    # - bullish: net_contrib > 0.005 and weighted_breadth >= 55.0
    # - bearish: net_contrib < -0.005 and weighted_breadth <= 45.0
    # - choppy: everything else (breadth split between 45% and 55%, or flat)
    if net_contrib > 0.005 and weighted_breadth >= 55.0:
        direction = "bullish"
    elif net_contrib < -0.005 and weighted_breadth <= 45.0:
        direction = "bearish"
    else:
        direction = "choppy"

    # Prepare details payload
    details = {
        "contributions": contributions,
        "sector_breadth": sector_breadth,
        "sector_contribution": sector_contributions
    }
    
    # Store to Database
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.execute("""
            INSERT OR REPLACE INTO nifty_candle_analysis 
            (timestamp, direction, net_contribution, weighted_breadth, driver, flagged_symbols, flagged_sector, rvol_results, conviction, analysis_details)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            timestamp_str,
            direction,
            round(net_contrib, 4),
            weighted_breadth,
            driver,
            json.dumps(flagged_symbols),
            flagged_sector,
            json.dumps(rvol_results),
            conviction,
            json.dumps(details)
        ))
        conn.commit()
        conn.close()
        log.info(f"[Nifty Analyzer] Completed and saved candle analysis for timestamp={timestamp_str} (Driver={driver}, Conviction={conviction}).")
    except Exception as e:
        log.error(f"[Nifty Analyzer] Failed saving analysis for timestamp={timestamp_str}: {e}")

def get_historical_analysis(limit: int = 100) -> List[Dict]:
    """Exposes saved chronological analysis rows to return via Flask endpoint."""
    init_db()
    results = []
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("""
            SELECT * FROM nifty_candle_analysis 
            ORDER BY datetime(timestamp) DESC 
            LIMIT ?
        """, (limit,))
        rows = cursor.fetchall()
        for r in rows:
            d = dict(r)
            # Parse json columns
            for col in ["flagged_symbols", "rvol_results", "analysis_details"]:
                if d.get(col):
                    try:
                        d[col] = json.loads(d[col])
                    except Exception:
                        pass
            results.append(d)
        conn.close()
    except Exception as e:
        log.error(f"[Nifty Analyzer] Database read failed: {e}")
    return results
