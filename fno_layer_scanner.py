#!/usr/bin/env python3
"""
fno_layer_scanner.py
====================
Standalone adhoc CLI scanner script for TradeSignal F&O Universe.
Queries existing TradeSignal APIs to extract Layer 1 (Futures Commitment & OI),
Total Traded Volume, Delivery Volume %, and Layer 3 (ATM+-5 Immediate Barriers,
Level Strengths, Air Pocket & Vacuum Risk Flags, Global Walls).

Saves results directly into a CSV file.

Usage:
    python fno_layer_scanner.py
    python fno_layer_scanner.py --output fno_market_layers.csv
    python fno_layer_scanner.py --server http://127.0.0.1:5000
"""

import sys
import os
import argparse
import urllib.request
import urllib.parse
import json
import csv
import datetime
import requests
import io
from concurrent.futures import ThreadPoolExecutor, as_completed


def load_fno_symbols():
    """Load F&O symbol list from fno_symbols.json."""
    fno_path = os.path.join(os.path.dirname(__file__), "fno_symbols.json")
    symbols = []
    if os.path.exists(fno_path):
        try:
            with open(fno_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                symbols = data.get("symbols", [])
        except Exception as e:
            print(f"[Warning] Failed to parse fno_symbols.json: {e}")

    if not symbols:
        symbols = [
            "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY",
            "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN",
            "BHARTIARTL", "TVSMOTOR", "TATAMOTORS", "MARUTI", "M&M",
            "TATASTEEL", "HINDALCO", "SUNPHARMA", "ITC", "LT"
        ]

    unique = []
    seen = set()
    for s in symbols:
        clean = s.strip().upper()
        if clean and clean not in seen:
            seen.add(clean)
            unique.append(clean)
    return sorted(unique)


def fetch_nse_bhavcopy_delivery_data():
    """Fetch latest NSE daily Bhavcopy delivery % and total traded volume.
    Step backward if weekend/holiday.
    Returns dict: { 'SYMBOL': {'total_traded_vol': int, 'delivery_pct': float} }
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
    }
    date_obj = datetime.date.today()
    bhavcopy_map = {}

    for _ in range(10):
        date_str = date_obj.strftime("%d%m%Y")
        url = f"https://archives.nseindia.com/products/content/sec_bhavdata_full_{date_str}.csv"
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code == 200:
                print(f"[Scanner] Loaded NSE Bhavcopy Delivery Data for trading date: {date_obj.strftime('%Y-%m-%d')}")
                f = io.StringIO(r.text)
                reader = csv.reader(f)
                header = [col.strip().upper() for col in next(reader)]

                if "SYMBOL" in header and "TTL_TRD_QNTY" in header and "DELIV_PER" in header and "SERIES" in header:
                    sym_idx = header.index("SYMBOL")
                    series_idx = header.index("SERIES")
                    trd_idx = header.index("TTL_TRD_QNTY")
                    deliv_pct_idx = header.index("DELIV_PER")

                    for row in reader:
                        if not row or len(row) <= max(sym_idx, series_idx, trd_idx, deliv_pct_idx):
                            continue
                        row_sym = row[sym_idx].strip().upper()
                        row_series = row[series_idx].strip().upper()
                        if row_series == "EQ":
                            try:
                                trd_vol = int(row[trd_idx].strip())
                                deliv_pct_val = float(row[deliv_pct_idx].strip())
                                bhavcopy_map[row_sym] = {
                                    "total_traded_vol": trd_vol,
                                    "delivery_pct": deliv_pct_val
                                }
                            except ValueError:
                                pass
                break
        except Exception:
            pass
        date_obj -= datetime.timedelta(days=1)

    return bhavcopy_map


def fetch_symbol_detail_from_api(server_url, symbol, bhavcopy_map=None):
    """Query existing /api/oi/symbol/<symbol> endpoint."""
    url = f"{server_url.rstrip('/')}/api/oi/symbol/{urllib.parse.quote(symbol)}"
    req = urllib.request.Request(url, headers={"User-Agent": "TradeSignal-Standalone-Scanner"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                return parse_symbol_response(symbol, data, bhavcopy_map=bhavcopy_map)
    except Exception:
        pass
    return None


def fetch_symbol_detail_from_backend_direct(symbol, kite_instance=None, bhavcopy_map=None):
    """Fallback: query Python backend directly if Flask HTTP server is not responding."""
    try:
        backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "app", "backend"))
        if backend_dir not in sys.path:
            sys.path.insert(0, backend_dir)

        from oi_spurt_routes import _get_kite, get_ltp_and_pivots, get_option_chain, compute_max_pain, top_strikes, compute_atm_5_analysis, compute_pcr

        kite = kite_instance or _get_kite()
        sym = symbol.upper()
        ltp, price_change_pct, pivots, pivot_source, prev_close, open_gap_pct, perr = get_ltp_and_pivots(kite, sym)
        chain, expiry, futures_oi, futures_oi_prev, futures_ltp, futures_prev_close, cerr = get_option_chain(kite, sym)

        futures_data = {}
        futures_buildup = "–"
        if futures_oi > 0:
            f_oi = futures_oi or 0
            f_oi_prev = futures_oi_prev or f_oi
            f_oi_chg_pct = ((f_oi - f_oi_prev) / f_oi_prev * 100) if f_oi_prev > 0 else 0.0
            f_price_chg = ((futures_ltp - futures_prev_close) / futures_prev_close * 100) if futures_prev_close > 0 else price_change_pct

            if f_oi_chg_pct > 0 and f_price_chg > 0.25:
                futures_buildup = "Long Buildup"
            elif f_oi_chg_pct > 0 and f_price_chg < -0.25:
                futures_buildup = "Short Buildup"
            elif f_oi_chg_pct < 0 and f_price_chg > 0.25:
                futures_buildup = "Short Covering"
            elif f_oi_chg_pct < 0 and f_price_chg < -0.25:
                futures_buildup = "Long Unwinding"
            else:
                futures_buildup = "Flat"

            futures_data = {
                "ltp": round(futures_ltp, 2),
                "price_change_pct": round(f_price_chg, 2),
                "oi": f_oi,
                "oi_prev": f_oi_prev,
                "oi_change_pct": round(f_oi_chg_pct, 2),
                "buildup": futures_buildup
            }

        max_pain = compute_max_pain(chain) if chain else None
        strikes = top_strikes(chain, ltp) if chain else {"ce_wall": None, "pe_wall": None}

        if chain and ltp:
            chain_sorted = sorted(chain, key=lambda r: r["strike"])
            atm_idx = min(range(len(chain_sorted)), key=lambda i: abs(chain_sorted[i]["strike"] - ltp))
            strikes["atm_window_5"] = compute_atm_5_analysis(chain_sorted, atm_idx, ltp)

        raw_res = {
            "symbol": sym,
            "ltp": ltp,
            "price_change_pct": price_change_pct,
            "max_pain": max_pain,
            "pcr": compute_pcr(chain) if chain else None,
            "strikes": strikes,
            "futures_data": futures_data
        }
        return parse_symbol_response(symbol, raw_res, bhavcopy_map=bhavcopy_map)

    except Exception:
        return None


def parse_symbol_response(symbol, res_data, bhavcopy_map=None):
    """Extract Layer 1, Volume/Delivery, and Layer 3 metrics into a standardized CSV row dictionary."""
    if not res_data or "error" in res_data:
        return None

    spot = res_data.get("ltp", 0.0)
    px_chg = res_data.get("price_change_pct", 0.0)
    f_data = res_data.get("futures_data", {}) or {}

    f_ltp = f_data.get("ltp", spot)
    f_px_chg = f_data.get("price_change_pct", px_chg)
    f_oi = f_data.get("oi", 0)
    f_oi_prev = f_data.get("oi_prev", 0)
    f_oi_chg = f_data.get("oi_change_pct", 0.0)
    f_buildup = f_data.get("buildup", "–")

    # Delivery & Traded Volume
    bhav = (bhavcopy_map or {}).get(symbol.upper(), {})
    tot_vol = bhav.get("total_traded_vol")
    if tot_vol is None:
        tot_vol = res_data.get("volume", 0)

    deliv_pct = bhav.get("delivery_pct")
    deliv_pct_str = f"{deliv_pct:.2f}%" if deliv_pct is not None else "–"

    strikes = res_data.get("strikes", {}) or {}
    atm5 = strikes.get("atm_window_5", {}) or {}
    imm_res = atm5.get("immediate_resistance", {}) or {}
    imm_sup = atm5.get("immediate_support", {}) or {}
    risk = atm5.get("risk_analysis", {}) or {}

    return {
        "symbol":                  symbol,
        "spot_price":              spot,
        "spot_price_chg_pct":      px_chg,
        "total_traded_vol":        tot_vol if tot_vol else "–",
        "delivery_vol_pct":        deliv_pct_str,
        "futures_ltp":             f_ltp,
        "futures_price_chg_pct":  f_px_chg,
        "futures_oi":              f_oi,
        "futures_oi_prev":         f_oi_prev,
        "futures_oi_chg_pct":      f_oi_chg,
        "futures_buildup":         f_buildup,
        "pcr_ratio":               res_data.get("pcr"),
        "imm_resistance_strike":   imm_res.get("strike"),
        "imm_resistance_strength": imm_res.get("strength_score"),
        "imm_resistance_rating":   imm_res.get("strength_rating"),
        "imm_resistance_buildup":  imm_res.get("buildup"),
        "imm_support_strike":      imm_sup.get("strike"),
        "imm_support_strength":    imm_sup.get("strength_score"),
        "imm_support_rating":      imm_sup.get("strength_rating"),
        "imm_support_buildup":     imm_sup.get("buildup"),
        "global_ce_wall":          strikes.get("ce_wall"),
        "global_pe_wall":          strikes.get("pe_wall"),
        "max_pain":                res_data.get("max_pain"),
        "risk_flag_code":          risk.get("flag_code"),
        "risk_alert_title":        risk.get("alert_title"),
        "risk_description":        risk.get("short_desc")
    }


def scan_all_symbols(server_url, max_workers=6):
    """Scan all F&O symbols concurrently."""
    symbols = load_fno_symbols()
    print(f"[Scanner] Loaded {len(symbols)} F&O symbols to scan...")

    # Load NSE Delivery & Traded Volume Bhavcopy
    bhavcopy_map = fetch_nse_bhavcopy_delivery_data()

    test_res = fetch_symbol_detail_from_api(server_url, "NIFTY", bhavcopy_map=bhavcopy_map)
    use_api = test_res is not None

    kite_shared = None
    if use_api:
        print(f"[Scanner] Connected to TradeSignal Server at {server_url}")
    else:
        print(f"[Scanner] TradeSignal Server not responding at {server_url}. Initializing Direct Python Backend...")
        try:
            backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "app", "backend"))
            if backend_dir not in sys.path:
                sys.path.insert(0, backend_dir)
            from oi_spurt_routes import _get_kite
            kite_shared = _get_kite()
        except Exception as e:
            print(f"[Scanner Warning] Kite initialization error: {e}")

    results = []
    completed_count = 0

    def _worker(sym):
        if use_api:
            return fetch_symbol_detail_from_api(server_url, sym, bhavcopy_map=bhavcopy_map)
        else:
            return fetch_symbol_detail_from_backend_direct(sym, kite_instance=kite_shared, bhavcopy_map=bhavcopy_map)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_worker, sym): sym for sym in symbols}
        for future in as_completed(futures):
            completed_count += 1
            res = future.result()
            if res:
                results.append(res)
            pct = int((completed_count / len(symbols)) * 100)
            sys.stdout.write(f"\rScanning F&O Universe... [{completed_count}/{len(symbols)}] ({pct}%)")
            sys.stdout.flush()

    print("\n")
    results.sort(key=lambda r: r["symbol"])
    return results


def save_to_csv(rows, output_filepath):
    """Save scanned layer results to CSV file."""
    fieldnames = [
        "symbol", "spot_price", "spot_price_chg_pct",
        "total_traded_vol", "delivery_vol_pct",
        "futures_ltp", "futures_price_chg_pct", "futures_oi", "futures_oi_prev", "futures_oi_chg_pct", "futures_buildup",
        "pcr_ratio",
        "imm_resistance_strike", "imm_resistance_strength", "imm_resistance_rating", "imm_resistance_buildup",
        "imm_support_strike", "imm_support_strength", "imm_support_rating", "imm_support_buildup",
        "global_ce_wall", "global_pe_wall", "max_pain",
        "risk_flag_code", "risk_alert_title", "risk_description"
    ]

    with open(output_filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)

    print(f"✅ Saved CSV scan report to: {os.path.abspath(output_filepath)}")


def main():
    parser = argparse.ArgumentParser(description="Standalone F&O Layer 1 & Layer 3 Scanner CLI Script")
    parser.add_argument("--output", default="fno_layer_scan.csv", help="Output CSV filename (default: fno_layer_scan.csv)")
    parser.add_argument("--server", default="http://127.0.0.1:5000", help="TradeSignal Server URL (default: http://127.0.0.1:5000)")
    parser.add_argument("--workers", type=int, default=6, help="Number of concurrent worker threads (default: 6)")
    args = parser.parse_args()

    print("==================================================================")
    print("      TradeSignal F&O Universe Layer 1 & Layer 3 Scanner        ")
    print("==================================================================")
    print(f"Timestamp: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Output File: {args.output}\n")

    results = scan_all_symbols(args.server, max_workers=args.workers)

    if not results:
        print("❌ Scan returned 0 records. Please verify Kite session connection.")
        sys.exit(1)

    save_to_csv(results, args.output)
    print(f"Successfully processed {len(results)} F&O instruments.")
    print("==================================================================\n")


if __name__ == "__main__":
    main()
