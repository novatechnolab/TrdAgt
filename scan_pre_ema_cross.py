#!/usr/bin/env python3
"""
Standalone Pre-EMA Cross Alert Scanner & CSV Exporter.
Scans F&O symbols for 5M Pre-EMA Crossover momentum alerts for current or latest closed trading session.
Outputs results to pre_ema_cross_alerts.csv and prints a formatted summary table.
"""

import os
import sys
import csv
import time
import logging
from datetime import datetime, timezone, timedelta

root_dir = os.path.abspath(os.path.dirname(__file__))
backend_dir = os.path.join(root_dir, "app", "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from indicators import compute_ema
from server import get_historical_candles, get_kite
from ema_crossover_scanner import _get_active_trading_date
from db_instruments import get_cached_instruments
import volume_baseline

IST = timezone(timedelta(hours=5, minutes=30))

def now_ist():
    return datetime.now(IST)

INDEX_MAP = {
    'NIFTY':      'NIFTY 50',
    'BANKNIFTY':  'NIFTY BANK',
    'FINNIFTY':   'NIFTY FIN SERVICE',
    'MIDCPNIFTY': 'NIFTY MID SELECT',
    'NIFTYNXT50': 'NIFTY NEXT 50',
}

def scan_pre_ema_cross(csv_filename="pre_ema_cross_alerts.csv"):
    print("🚀 Initializing Pre-EMA Cross Scanner...")
    volume_baseline._load_from_db()
    kite = get_kite()
    if not kite:
        print("❌ Error: Failed to initialize Kite Connect session.")
        return

    now = now_ist()
    target_date = _get_active_trading_date(now)
    print(f"📅 Target Trading Date: {target_date} (Current Time: {now.strftime('%Y-%m-%d %H:%M:%S IST')})")

    nfo_instruments = get_cached_instruments("NFO")
    symbols = sorted(list({
        i["name"].upper() for i in nfo_instruments
        if i.get("instrument_type") in ["CE", "PE"] and i.get("name")
    }))
    if not symbols:
        print("❌ Error: No F&O symbols resolved from NFO instrument cache.")
        return

    print(f"🔍 Scanning {len(symbols)} F&O symbols for Pre-EMA Cross alerts...\n")

    alerts = []

    for idx, symbol in enumerate(symbols, 1):
        sys.stdout.write(f"\r[{idx}/{len(symbols)}] Scanning {symbol:<15}")
        sys.stdout.flush()

        fetch_sym = INDEX_MAP.get(symbol, symbol)
        candles = get_historical_candles(kite, fetch_sym, "5minute", days_back=5, limit=500)
        if not candles or len(candles) < 25:
            continue

        closes = [c.get('close', 0.0) for c in candles]
        ema9 = compute_ema(closes, 9)
        ema21 = compute_ema(closes, 21)

        if not (ema9 and ema21 and len(ema9) == len(closes) and len(ema21) == len(closes)):
            continue

        # Helper to extract YYYY-MM-DD date string
        def _get_candle_date(c):
            d_val = c.get('date')
            if isinstance(d_val, str):
                return d_val.split('T')[0] if 'T' in d_val else d_val.split(' ')[0]
            elif hasattr(d_val, 'strftime'):
                return d_val.strftime("%Y-%m-%d")
            return ""

        # 1. Resolve 23rd July (prev_date) and 24th July (target_date)
        all_dates = sorted(list({_get_candle_date(c) for c in candles if _get_candle_date(c)}))
        prev_dates = [d for d in all_dates if d < target_date]
        if not prev_dates:
            continue
        prev_date = prev_dates[-1]

        # 2. Fetch official daily candles for CPR calculation (previous closed session - 1)
        daily_candles = get_historical_candles(kite, fetch_sym, "day", days_back=10, limit=100)
        if not daily_candles:
            continue

        prev_daily = [c for c in daily_candles if _get_candle_date(c) < target_date]
        if not prev_daily:
            continue
        prev_day = prev_daily[-1]

        prev_high = prev_day.get('high', 0.0) or 0.0
        prev_low = prev_day.get('low', 0.0) or 0.0
        prev_close = prev_day.get('close', 0.0) or 0.0
        if prev_high <= 0 or prev_low <= 0 or prev_close <= 0:
            continue

        pivot = (prev_high + prev_low + prev_close) / 3.0
        bc_raw = (prev_high + prev_low) / 2.0
        tc_raw = (2.0 * pivot) - bc_raw

        cpr_bottom = min(tc_raw, bc_raw)  # Bottom Central boundary
        cpr_top = max(tc_raw, bc_raw)     # Top Central boundary

        # 3. Filter candles strictly for 24th July target session
        target_items = [
            {'candle': c, 'ema9': ema9[idx], 'ema21': ema21[idx]}
            for idx, c in enumerate(candles)
            if _get_candle_date(c) == target_date
        ]
        if len(target_items) < 2:
            continue

        # 4. Scan ONLY 24th July 5-minute candles (09:20 AM onwards)
        last_alert_mins = None

        for i in range(1, len(target_items)):
            c_prev = target_items[i - 1]['candle']  # 24th July candle
            c_curr = target_items[i]['candle']      # 24th July candle
            e9_val = target_items[i]['ema9']
            e21_val = target_items[i]['ema21']

            dt_val = c_curr.get('date')
            slot_str = None
            date_str = target_date
            if isinstance(dt_val, str):
                if 'T' in dt_val:
                    slot_str = dt_val[11:16]
                else:
                    slot_str = dt_val[11:16]
            elif hasattr(dt_val, 'strftime'):
                slot_str = dt_val.strftime("%H:%M")

            if not slot_str or slot_str > "15:15":
                continue

            # 15-minute cooldown check per symbol
            hh, mm = map(int, slot_str.split(':'))
            slot_mins = hh * 60 + mm
            if last_alert_mins is not None and (slot_mins - last_alert_mins) < 15:
                continue

            o1 = c_prev.get('open', 0.0) or 0.0
            c1 = c_prev.get('close', 0.0) or 0.0
            o2 = c_curr.get('open', 0.0) or 0.0
            c2 = c_curr.get('close', 0.0) or 0.0
            ltp = c2
            vol = c_curr.get('volume', 0) or 0

            if o2 <= 0 or ltp <= 0:
                continue

            move_pct_c1 = ((abs(c1 - o1) / o1) * 100.0) if o1 > 0 else 0.0
            move_pct_c2 = (abs(c2 - o2) / o2) * 100.0

            if move_pct_c1 >= 0.20 and move_pct_c2 >= 0.30:
                # Bullish: C1 green, C2 green, C2 close > C1 close & > CPR Bottom (cpr_bottom)
                # Bearish: C1 red, C2 red, C2 close < C1 close & < CPR Bottom (cpr_bottom)
                is_bull = (c1 > o1) and (c2 > o2) and (c2 > c1) and (c2 > cpr_bottom)
                is_bear = (c1 < o1) and (c2 < o2) and (c2 < c1) and (c2 < cpr_bottom)
                if e9_val is not None and e21_val is not None:
                    valid_side = (is_bull and e9_val < e21_val) or (is_bear and e9_val > e21_val)
                    if valid_side:
                        gap_pct = (abs(e9_val - e21_val) / ltp) * 100.0
                        if gap_pct <= 0.30:
                            vol_ratio, base_val = volume_baseline.get_vol_ratio(symbol, '5m', slot_str, vol)
                            direction = "BULLISH" if is_bull else "BEARISH"
                            last_alert_mins = slot_mins
                            alerts.append({
                                "Symbol": symbol,
                                "Direction": direction,
                                "Date": date_str,
                                "Slot_Time": slot_str,
                                "LTP": round(ltp, 2),
                                "C1_Move_Pct": round(move_pct_c1, 2),
                                "C2_Move_Pct": round(move_pct_c2, 2),
                                "Vol_Ratio": round(vol_ratio, 2) if vol_ratio else 0.0,
                                "EMA_Gap_Pct": round(gap_pct, 3)
                            })

    sys.stdout.write("\r" + " " * 60 + "\r")
    sys.stdout.flush()

    csv_path = os.path.abspath(csv_filename)
    fieldnames = ["Symbol", "Direction", "Date", "Slot_Time", "LTP", "C1_Move_Pct", "C2_Move_Pct", "Vol_Ratio", "EMA_Gap_Pct"]

    with open(csv_path, mode="w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(alerts)

    print(f"✅ Scan Complete! Found {len(alerts)} Pre-EMA Cross Alert(s) for target date {target_date}.")
    print(f"📁 CSV exported to: {csv_path}\n")

    if alerts:
        print("┌" + "─" * 86 + "┐")
        print(f"│ {'SYMBOL':<12} │ {'DIR':<7} │ {'TIME':<5} │ {'LTP':<8} │ {'C1 %':<6} │ {'C2 %':<6} │ {'VOL':<6} │ {'GAP %':<7} │")
        print("├" + "─" * 86 + "┤")
        for a in alerts:
            dir_str = "🟢 BULL" if a["Direction"] == "BULLISH" else "🔴 BEAR"
            print(f"│ {a['Symbol']:<12} │ {dir_str:<7} │ {a['Slot_Time']:<5} │ ₹{a['LTP']:<7.2f} │ {a['C1_Move_Pct']:<5.2f}% │ {a['C2_Move_Pct']:<5.2f}% │ {a['Vol_Ratio']:<5.1f}x │ {a['EMA_Gap_Pct']:<6.3f}% │")
        print("└" + "─" * 86 + "┘")

if __name__ == "__main__":
    csv_file = sys.argv[1] if len(sys.argv) > 1 else "pre_ema_cross_alerts.csv"
    scan_pre_ema_cross(csv_file)
