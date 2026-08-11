#!/usr/bin/env python3
"""
EMA Squeeze Stock Scanner & CSV Exporter
Scans live TradeSignal backend for stocks within specified EMA Gap % range (Default: 0.03% to 0.06%).
Saves output to ema_squeeze_stocks.csv.
"""

import sys
import argparse
import json
import csv
import urllib.request
from datetime import datetime

def main():
    parser = argparse.ArgumentParser(description="Scan EMA Squeeze stocks within specified EMA gap range and export to CSV.")
    parser.add_argument("--min-gap", type=float, default=0.03, help="Minimum EMA Gap % (default: 0.03)")
    parser.add_argument("--max-gap", type=float, default=0.06, help="Maximum EMA Gap % (default: 0.06)")
    parser.add_argument("--csv", type=str, default="ema_squeeze_stocks.csv", help="CSV output filename (default: ema_squeeze_stocks.csv)")
    parser.add_argument("--url", type=str, default="http://localhost:5000/api/ema-crossovers", help="API Endpoint URL")
    args = parser.parse_args()

    print(f"🔍 Scanning Squeeze Stocks with EMA Gap % between {args.min_gap}% and {args.max_gap}%...\n")

    try:
        req = urllib.request.urlopen(args.url, timeout=10)
        data = json.loads(req.read().decode('utf-8'))
        crossovers = data.get('crossovers', {})
    except Exception as e:
        print(f"❌ Error connecting to API server ({args.url}): {e}")
        print("💡 Make sure the backend server (server.py) is running on port 5000.")
        return

    results = []
    scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for sym, v in crossovers.items():
        if not isinstance(v, dict):
            continue
        squeeze = v.get('squeeze', {})
        ema_gap = squeeze.get('ema_gap')
        if ema_gap is not None and args.min_gap <= ema_gap <= args.max_gap:
            results.append({
                'timestamp': scan_time,
                'symbol': sym,
                'ema_gap': round(ema_gap, 4),
                'ltp': v.get('last_candle_close'),
                'state_5m': v.get('state_5m'),
                'alignment': v.get('alignment'),
                'collision': v.get('ema_collision') or 'None'
            })

    results.sort(key=lambda x: x['ema_gap'])

    if not results:
        print(f"No stocks currently found in the EMA Gap range {args.min_gap}% - {args.max_gap}%.")
        return

    print(f"FOUND {len(results)} STOCKS IN GAP RANGE ({args.min_gap}% - {args.max_gap}%):\n")
    header = f"{'Symbol':15s} | {'EMA Gap (%)':12s} | {'LTP (₹)':10s} | {'5m State':10s} | {'Alignment':10s} | {'Collision':10s}"
    print(header)
    print("-" * len(header))

    for r in results:
        print(f"{r['symbol']:15s} | {r['ema_gap']:11.4f}% | {str(r['ltp']):10s} | {str(r['state_5m']):10s} | {str(r['alignment']):10s} | {str(r['collision']):10s}")

    # Export to CSV
    try:
        fieldnames = ['timestamp', 'symbol', 'ema_gap', 'ltp', 'state_5m', 'alignment', 'collision']
        with open(args.csv, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(results)
        print(f"\n✅ Results successfully exported to: {args.csv}")
    except Exception as e:
        print(f"\n❌ Error writing CSV file: {e}")

if __name__ == "__main__":
    main()
