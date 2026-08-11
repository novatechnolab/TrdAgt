#!/usr/bin/env python3
"""
Daily EMA 9 vs EMA 21 F&O Stock Scanner (Powered by Kite Connect API)
Scans F&O stocks on daily timeframe using official Kite Connect historical data,
calculates EMA 9 and EMA 21, determines crossover status, and exports results to CSV.
"""

import os
import sys
import csv
import sqlite3
import pandas as pd
import numpy as np

# Ensure backend directory is in sys.path
backend_dir = os.path.join(os.path.dirname(__file__), "app", "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from server import get_kite, get_historical_candles
from db_instruments import get_fno_symbols
from indicators import compute_ema

EXCLUDED_INDICES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}

def load_quarterly_results_map():
    """Fetch Q1 quarterly results and annotations for symbols from results_cache.db."""
    res_db = os.path.join(os.path.dirname(__file__), "app", "backend", "results_cache.db")
    if not os.path.exists(res_db):
        return {}

    try:
        conn = sqlite3.connect(res_db)
        query = """
            SELECT 
                c.symbol,
                c.quarter_label,
                COALESCE(a.est_pat, c.est_pat_consensus) AS est_pat,
                c.opm_yoy_delta AS ebitda_pts,
                c.revenue_yoy AS rev_yoy,
                c.profit_yoy AS pat_yoy,
                c.eps_yoy AS eps_yoy,
                c.verdict
            FROM results_cache c
            LEFT JOIN results_annotations a ON UPPER(c.symbol) = UPPER(a.symbol)
        """
        rows = conn.execute(query).fetchall()
        conn.close()

        res_map = {}
        for row in rows:
            sym = row[0].upper() if row[0] else ""
            if not sym:
                continue
            q_label = str(row[1]) if row[1] is not None else ""

            def fmt_val(val):
                if val is None:
                    return "–"
                try:
                    return round(float(val), 2)
                except (ValueError, TypeError):
                    return "–"

            res_map[sym] = {
                "Quarter": q_label if q_label else "Q1",
                "Est_PAT": fmt_val(row[2]),
                "EBITDA_Pts": fmt_val(row[3]),
                "Rev_YoY (%)": fmt_val(row[4]),
                "PAT_YoY (%)": fmt_val(row[5]),
                "EPS_YoY (%)": fmt_val(row[6]),
                "Verdict": row[7] or "–"
            }
        return res_map
    except Exception:
        return {}

def run_ema_scan(output_csv="fno_daily_ema_scan.csv"):
    print("🚀 Initializing Kite Connect Daily EMA Scanner...")
    kite = get_kite()
    if not kite:
        print("❌ Error: Failed to initialize Kite Connect session. Please verify Kite credentials.")
        return

    # Fetch official F&O stock list from database/instruments cache
    db_symbols = get_fno_symbols()
    stock_symbols = sorted(list({s.upper() for s in db_symbols if s.upper() not in EXCLUDED_INDICES}))

    if not stock_symbols:
        print("❌ Error: No F&O symbols found in database cache.")
        return

    print(f"🔍 Scanning {len(stock_symbols)} F&O stocks via Kite Connect API...\n")

    # Load Q1 quarterly results map from database
    q_results_map = load_quarterly_results_map()

    results = []
    total = len(stock_symbols)

    for idx, symbol in enumerate(stock_symbols, 1):
        sys.stdout.write(f"\r[{idx}/{total}] Fetching & processing {symbol:<15}")
        sys.stdout.flush()

        try:
            # Fetch ~300 daily candles for full EMA convergence & accurate crossover streak counts
            candles = get_historical_candles(kite, symbol, "day", days_back=300)
            if not candles or len(candles) < 21:
                continue

            closes = [c['close'] for c in candles if c.get('close') is not None]
            if len(closes) < 21:
                continue

            ema9_series = compute_ema(closes, 9)
            ema21_series = compute_ema(closes, 21)

            latest_close = float(closes[-1])
            latest_ema9 = round(float(ema9_series[-1]), 2)
            latest_ema21 = round(float(ema21_series[-1]), 2)

            gap = round(abs(latest_ema9 - latest_ema21), 2)
            gap_pct = round((gap / latest_ema21) * 100.0, 2) if latest_ema21 > 0 else 0.0

            if latest_ema9 > latest_ema21:
                status = "EMA 9 > EMA 21"
                is_bullish = True
            elif latest_ema9 < latest_ema21:
                status = "EMA 9 < EMA 21"
                is_bullish = False
            else:
                status = "EMA 9 = EMA 21"
                is_bullish = None

            # Count consecutive daily candles in current crossover state
            candles_count = 0
            if is_bullish is not None:
                for i in range(len(closes) - 1, -1, -1):
                    e9 = float(ema9_series[i])
                    e21 = float(ema21_series[i])
                    if is_bullish and (e9 > e21):
                        candles_count += 1
                    elif (not is_bullish) and (e9 < e21):
                        candles_count += 1
                    else:
                        break

            # Calculate cumulative % change over past 30 daily candles
            if len(closes) >= 30:
                close_30d = float(closes[-30])
                perf_30d_pct = round(((latest_close - close_30d) / close_30d) * 100.0, 2) if close_30d > 0 else 0.0
            elif len(closes) > 1:
                close_oldest = float(closes[0])
                perf_30d_pct = round(((latest_close - close_oldest) / close_oldest) * 100.0, 2) if close_oldest > 0 else 0.0
            else:
                perf_30d_pct = 0.0

            q_info = q_results_map.get(symbol, {
                "Quarter": "Q1",
                "Est_PAT": "–",
                "EBITDA_Pts": "–",
                "Rev_YoY (%)": "–",
                "PAT_YoY (%)": "–",
                "EPS_YoY (%)": "–",
                "Verdict": "–"
            })

            results.append({
                "Symbol": symbol,
                "Close": round(latest_close, 2),
                "EMA9": latest_ema9,
                "EMA21": latest_ema21,
                "Gap": gap,
                "Gap_Pct (%)": gap_pct,
                "Status": status,
                "Candles_Since_Cross": candles_count,
                "Quarter": q_info["Quarter"],
                "Est_PAT": q_info["Est_PAT"],
                "EBITDA_Pts": q_info["EBITDA_Pts"],
                "Rev_YoY (%)": q_info["Rev_YoY (%)"],
                "PAT_YoY (%)": q_info["PAT_YoY (%)"],
                "EPS_YoY (%)": q_info["EPS_YoY (%)"],
                "Verdict": q_info["Verdict"],
                "Return_30D (%)": perf_30d_pct
            })
        except Exception as err:
            continue

    print("\n")

    # Sort results by Symbol
    results.sort(key=lambda x: x["Symbol"])

    # Write to CSV
    csv_fields = [
        "Symbol", "Close", "EMA9", "EMA21", "Gap", "Gap_Pct (%)", "Status", "Candles_Since_Cross",
        "Quarter", "Est_PAT", "EBITDA_Pts", "Rev_YoY (%)", "PAT_YoY (%)", "EPS_YoY (%)", "Verdict", "Return_30D (%)"
    ]
    with open(output_csv, mode="w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=csv_fields)
        writer.writeheader()
        writer.writerows(results)

    print(f"✅ Scan complete! Scanned {len(results)} F&O stocks via Kite Connect API.")
    print(f"📁 Output saved to: {os.path.abspath(output_csv)}")

    # Print summary breakdown
    bullish = [r for r in results if r["Status"] == "EMA 9 > EMA 21"]
    bearish = [r for r in results if r["Status"] == "EMA 9 < EMA 21"]

    print(f"\n📊 Summary:")
    print(f"   • EMA 9 > EMA 21 (Bullish Crossover): {len(bullish)} stocks")
    print(f"   • EMA 9 < EMA 21 (Bearish Crossover): {len(bearish)} stocks")

    if results:
        print("\n📋 Sample Output (First 10 rows):")
        sample_df = pd.DataFrame(results[:10])
        print(sample_df.to_string(index=False))

if __name__ == "__main__":
    run_ema_scan()
