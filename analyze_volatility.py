#!/usr/bin/env python3
"""
analyze_volatility.py
1. Fetches historical 5-minute option premium data for representative ATM and OTM
   contracts (NIFTY, RELIANCE, VOLTAS, RBLBANK) for the last 30 trading days.
2. Analyzes 5-minute volatility distributions of both underlying futures and options.
3. Outputs statistical recommendations in CSV format to volatility_threshold_recommendations.csv.
"""

import os
import sys
import json
import time
import csv
from datetime import datetime, date, timedelta
import logging
import numpy as np

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

# File Paths
SESSION_FILE_PATH = os.path.join("app", "backend", ".kite_session.json")
OPTIONS_CSV_PATH = "options_5m_volatility_data.csv"
FUTURES_L_CSV = "futures_5m_volatility_data.csv"
FUTURES_MS_CSV = "mid_small_5m_volatility_data.csv"
REPORT_CSV_PATH = "volatility_threshold_recommendations.csv"

# Representative Symbols
REPRESENTATIVE_SYMBOLS = [
    {"symbol": "NIFTY",      "spot_key": "NSE:NIFTY 50",   "class": "INDEX"},
    {"symbol": "RELIANCE",   "spot_key": "NSE:RELIANCE",   "class": "LARGE_CAP"},
    {"symbol": "VOLTAS",     "spot_key": "NSE:VOLTAS",     "class": "MID_CAP"},
    {"symbol": "RBLBANK",    "spot_key": "NSE:RBLBANK",    "class": "SMALL_CAP"}
]

# Analysis range: June 1 to July 10, 2026 (approx. 30 trading days)
START_DATE_STR = "2026-06-01"
END_DATE_STR = "2026-07-10"

def load_kite():
    """Initialize KiteConnect from persistent session."""
    if not os.path.isfile(SESSION_FILE_PATH):
        log.error(f"Kite session file not found: {SESSION_FILE_PATH}")
        sys.exit(1)
    try:
        with open(SESSION_FILE_PATH, "r") as f:
            session = json.load(f)
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=session["api_key"])
        kite.set_access_token(session["access_token"])
        return kite
    except Exception as e:
        log.error(f"Failed to load KiteConnect: {e}")
        sys.exit(1)

def fetch_option_data(kite):
    """Dynamically resolves ATM/OTM options for NIFTY, RELIANCE, VOLTAS, RBLBANK and fetches their 5m history."""
    log.info("Fetching spot prices and active option chains...")
    
    # 1. Fetch Spot LTP
    spot_keys = [item["spot_key"] for item in REPRESENTATIVE_SYMBOLS]
    try:
        ltp_data = kite.ltp(spot_keys)
    except Exception as e:
        log.error(f"Kite LTP fetch failed: {e}")
        return False

    # 2. Fetch NFO instruments
    try:
        nfo_insts = kite.instruments("NFO")
    except Exception as e:
        log.error(f"Kite instruments fetch failed: {e}")
        return False

    today = date.today()
    options_to_fetch = []

    for item in REPRESENTATIVE_SYMBOLS:
        symbol = item["symbol"]
        spot_price = ltp_data.get(item["spot_key"], {}).get("last_price")
        if not spot_price:
            log.warning(f"Spot price not found for {symbol}")
            continue

        # Filter option contracts
        opt_contracts = [
            inst for inst in nfo_insts 
            if inst["segment"] == "NFO-OPT" and inst["name"] == symbol and inst["expiry"] and inst["expiry"] >= today
        ]
        
        if not opt_contracts:
            log.warning(f"No option contracts found for {symbol}")
            continue

        # Sort and pick the nearest expiry date
        opt_contracts.sort(key=lambda x: x["expiry"])
        nearest_expiry = opt_contracts[0]["expiry"]
        expiry_contracts = [inst for inst in opt_contracts if inst["expiry"] == nearest_expiry]

        # Determine strike step dynamically
        strikes = sorted(list(set(float(inst["strike"]) for inst in expiry_contracts)))
        if len(strikes) >= 2:
            strike_step = strikes[1] - strikes[0]
        else:
            strike_step = 50.0 if symbol == "NIFTY" else 20.0

        # Find ATM Strike
        atm_strike = min(strikes, key=lambda s: abs(s - spot_price))
        otm_call_strike = atm_strike + (2 * strike_step)
        otm_put_strike = atm_strike - (2 * strike_step)

        # Select ATM CE/PE and OTM CE/PE
        targets = [
            {"type": "ATM_CE", "strike": atm_strike,     "type_flag": "CE"},
            {"type": "ATM_PE", "strike": atm_strike,     "type_flag": "PE"},
            {"type": "OTM_CE", "strike": otm_call_strike, "type_flag": "CE"},
            {"type": "OTM_PE", "strike": otm_put_strike,  "type_flag": "PE"},
        ]

        for target in targets:
            match = next((
                inst for inst in expiry_contracts 
                if float(inst["strike"]) == target["strike"] and inst["instrument_type"] == target["type_flag"]
            ), None)
            
            if match:
                options_to_fetch.append({
                    "underlying": symbol,
                    "class": item["class"],
                    "opt_type": target["type"],
                    "strike": target["strike"],
                    "tradingsymbol": match["tradingsymbol"],
                    "token": match["instrument_token"]
                })

    # 3. Fetch historical 5m data for selected options
    log.info(f"Targeting {len(options_to_fetch)} option contracts for historical download...")
    from_dt = datetime.strptime(START_DATE_STR, "%Y-%m-%d").date()
    to_dt = datetime.strptime(END_DATE_STR, "%Y-%m-%d").date()

    option_rows = []
    for opt in options_to_fetch:
        log.info(f"Fetching 5m data for option {opt['tradingsymbol']} (Token: {opt['token']})...")
        time.sleep(0.4) # Throttling
        try:
            candles = kite.historical_data(
                instrument_token=opt["token"],
                from_date=from_dt,
                to_date=to_dt,
                interval="5minute",
                oi=True
            )
            
            for candle in candles:
                c_dt = candle["date"]
                c_time = c_dt.time()
                # Filter market hours (09:15 - 15:30)
                if not (c_time >= datetime.strptime("09:15:00", "%H:%M:%S").time() and 
                        c_time <= datetime.strptime("15:30:00", "%H:%M:%S").time()):
                    continue
                    
                option_rows.append({
                    "symbol": opt["tradingsymbol"],
                    "underlying": opt["underlying"],
                    "class": opt["class"],
                    "opt_type": opt["opt_type"],
                    "strike": opt["strike"],
                    "timestamp": c_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "open": candle.get("open", ""),
                    "high": candle.get("high", ""),
                    "low": candle.get("low", ""),
                    "close": candle.get("close", ""),
                    "volume": candle.get("volume", ""),
                    "oi": candle.get("oi", "")
                })
        except Exception as e:
            log.warning(f"Failed to download history for option {opt['tradingsymbol']}: {e}")

    # Save Option Premium history to CSV
    if option_rows:
        try:
            with open(OPTIONS_CSV_PATH, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=[
                    "symbol", "underlying", "class", "opt_type", "strike", "timestamp",
                    "open", "high", "low", "close", "volume", "oi"
                ])
                writer.writeheader()
                writer.writerows(option_rows)
            log.info(f"Saved {len(option_rows)} option candles to {OPTIONS_CSV_PATH}")
            return True
        except Exception as e:
            log.error(f"Failed to write options CSV: {e}")
            return False
    else:
        log.error("No option historical data was downloaded.")
        return False

def calculate_returns(file_path, is_option=False):
    """Loads a CSV, groups by symbol and day, and computes intraday 5m close-to-close returns."""
    data = {}
    if not os.path.exists(file_path):
        log.warning(f"File not found for calculations: {file_path}")
        return data

    with open(file_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sym = row["symbol"] if not is_option else f"{row['underlying']}_{row['opt_type']}"
            if sym not in data:
                data[sym] = []
            
            close = float(row["close"]) if row["close"] else None
            ts = row["timestamp"]
            date_str = ts.split(" ")[0]
            
            if close is not None:
                data[sym].append((date_str, close))

    # Calculate returns day-by-day (to prevent overnight gap distortions)
    returns_by_sym = {}
    for sym, records in data.items():
        records.sort(key=lambda x: x[0])  # Sort by date/timestamp
        
        # Group by date
        days_data = {}
        for date_str, close in records:
            if date_str not in days_data:
                days_data[date_str] = []
            days_data[date_str].append(close)

        sym_returns = []
        for date_str, closes in days_data.items():
            if len(closes) < 2:
                continue
            # Return pct_change
            for idx in range(1, len(closes)):
                prev = closes[idx - 1]
                curr = closes[idx]
                if prev > 0:
                    pct = (curr - prev) / prev
                    sym_returns.append(pct)
        
        if sym_returns:
            returns_by_sym[sym] = np.array(sym_returns)

    return returns_by_sym

def main():
    # 1. Fetch Option Data
    kite = load_kite()
    fetch_success = fetch_option_data(kite)
    
    if not fetch_success and not os.path.exists(OPTIONS_CSV_PATH):
        log.error("Could not fetch options data from Kite and no cached CSV exists. Aborting.")
        sys.exit(1)

    # 2. Load and process Futures data
    log.info("Calculating returns for futures data...")
    futures_returns = {}
    
    # Process Large Cap futures
    futures_returns.update(calculate_returns(FUTURES_L_CSV, is_option=False))
    # Process Mid/Small Cap futures
    futures_returns.update(calculate_returns(FUTURES_MS_CSV, is_option=False))

    # 3. Load and process Option Premium data
    log.info("Calculating returns for options premium data...")
    options_returns = calculate_returns(OPTIONS_CSV_PATH, is_option=True)

    # 4. Perform Statistical Analysis
    log.info("Performing volatility distribution analysis...")
    
    report_rows = []
    
    # Print Console Header
    print("\n" + "="*95)
    print(f"{'Symbol / Asset Class':<25} | {'Std Dev':<10} | {'50% (Med)':<10} | {'75% Pct':<10} | {'95% Pct':<10} | {'Flat Zone% (<=0.25%)':<20}")
    print("="*95)

    # Helper function to print and store statistics
    def analyze_group(name, returns, asset_class, data_type):
        abs_ret = np.abs(returns)
        std_dev = np.std(returns)
        p50 = np.percentile(abs_ret, 50)
        p75 = np.percentile(abs_ret, 75)
        p90 = np.percentile(abs_ret, 90)
        p95 = np.percentile(abs_ret, 95)
        
        # Calculate % of candles that fall within the current 0.25% threshold
        flat_pct = np.mean(abs_ret <= 0.0025) * 100

        # Calibration of recommended thresholds:
        # We recommend the Spot threshold to filter out 75% of noise (75th percentile of spot/futures).
        # We recommend the Option Premium threshold to filter out 75% of premium noise.
        rec_spot = round(p75, 4)
        # Scale recommendation for Premium
        rec_prem = round(p75, 4) if data_type == "FUTURE" else round(p75, 4)

        print(f"{name:<25} | {std_dev*100:8.3f}% | {p50*100:8.3f}% | {p75*100:8.3f}% | {p95*100:8.3f}% | {flat_pct:18.2f}%")
        
        report_rows.append({
            "asset_class": asset_class,
            "symbol": name,
            "type": data_type,
            "std_dev_5m_pct": round(std_dev * 100, 4),
            "p50_abs_pct": round(p50 * 100, 4),
            "p75_abs_pct": round(p75 * 100, 4),
            "p90_abs_pct": round(p90 * 100, 4),
            "p95_abs_pct": round(p95 * 100, 4),
            "pct_flat_at_0_25_pct_threshold": round(flat_pct, 2),
            "recommended_spot_threshold_pct": round(rec_spot * 100, 3) if data_type == "FUTURE" else "",
            "recommended_premium_threshold_pct": round(rec_prem * 100, 3) if data_type == "OPTION" else ""
        })

    # Analyze Futures
    print(" UNDERLYING FUTURES VOLATILITY (Last 30 Trading Days)")
    print("-" * 95)
    for sym, ret in sorted(futures_returns.items()):
        # Map to class
        a_class = "LARGE_CAP"
        if sym in ("NIFTY", "BANKNIFTY"): a_class = "INDEX"
        elif sym in ("VOLTAS", "ASHOKLEY", "ABCAPITAL", "KPITTECH"): a_class = "MID_CAP"
        elif sym in ("MANAPPURAM", "RBLBANK", "IEX"): a_class = "SMALL_CAP"
        
        analyze_group(sym, ret, a_class, "FUTURE")

    print("\n OPTION PREMIUM VOLATILITY (Representative Active July Contracts)")
    print("-" * 95)
    for sym, ret in sorted(options_returns.items()):
        und = sym.split("_")[0]
        a_class = "LARGE_CAP"
        if und == "NIFTY": a_class = "INDEX"
        elif und == "VOLTAS": a_class = "MID_CAP"
        elif und == "RBLBANK": a_class = "SMALL_CAP"
        
        analyze_group(sym, ret, a_class, "OPTION")
        
    print("="*95)

    # 5. Export Report to CSV
    try:
        with open(REPORT_CSV_PATH, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "asset_class", "symbol", "type", "std_dev_5m_pct", "p50_abs_pct", "p75_abs_pct",
                "p90_abs_pct", "p95_abs_pct", "pct_flat_at_0_25_pct_threshold",
                "recommended_spot_threshold_pct", "recommended_premium_threshold_pct"
            ])
            writer.writeheader()
            writer.writerows(report_rows)
        log.info(f"Analysis Report successfully exported in CSV format to: {os.path.abspath(REPORT_CSV_PATH)}")
    except Exception as e:
        log.error(f"Failed to export report to CSV: {e}")

if __name__ == "__main__":
    main()
