"""
fno_short_covering_scanner.py — F&O Short Covering Scanner Script

Scans all F&O stock underlyings for:
  1. Futures Buildup == Short Covering (Price UP + OI DOWN from intraday peak)
  2. Call Options (CE) at ATM to ATM+5 strikes for CE Short Covering
     (CE price UP + CE OI declining from intraday peak)
  3. Output columns: Symbol | %Change | SC_Strikes_Count | ATM_Strike_PCR
  4. Exports output to CSV format, sorted by SC_Strikes_Count descending.

Buildup classification uses Enhanced B+ noise floors:
  PRICE_NOISE_FLOOR = 0.10%  (below this = noise)
  OI_NOISE_FLOOR    = 0.05%  (below this = noise)

Usage:
  python app/backend/fno_short_covering_scanner.py [--csv output.csv] [--min-strikes N]
"""

import os
import sys
import json
import sqlite3
import csv
import argparse
from datetime import datetime, date

# ── Include backend path in sys.path ──────────────────────────────────────────
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

try:
    from session_utils import now_ist, now_ist_str
except ImportError:
    def now_ist(): return datetime.now()
    def now_ist_str(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# ── Kite connection helper ───────────────────────────────────────────────────
def get_kite_client():
    """Load KiteConnect client using session saved at .kite_session.json."""
    session_path = os.path.join(BACKEND_DIR, ".kite_session.json")
    if not os.path.exists(session_path):
        print(f"❌ Error: Kite session file not found at {session_path}")
        print("Please log in to TradeSignal backend or save credentials to .kite_session.json.")
        sys.exit(1)

    try:
        with open(session_path, "r") as f:
            sess = json.load(f)
        api_key = sess.get("api_key")
        access_token = sess.get("access_token")
        if not api_key or not access_token:
            print("❌ Error: Invalid credentials in .kite_session.json")
            sys.exit(1)

        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token)
        return kite
    except Exception as e:
        print(f"❌ Error initializing KiteConnect: {e}")
        sys.exit(1)


# ── Load F&O Instruments from Cache DB ───────────────────────────────────────
def load_fno_instruments():
    """Load active F&O instruments (futures & options) from local tradesignal_cache.db."""
    db_path = os.path.join(BACKEND_DIR, "tradesignal_cache.db")
    if not os.path.exists(db_path):
        print(f"❌ Error: Database file not found at {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.execute("SELECT * FROM instruments WHERE exchange = 'NFO'")
    rows = cursor.fetchall()
    conn.close()

    futures_map = {}   # symbol -> near month fut instrument dict
    options_map = {}   # (symbol, expiry) -> list of option instrument dicts

    today = date.today()

    for r in rows:
        d = dict(r)
        exp_str = d.get("expiry")
        if not exp_str:
            continue
        try:
            exp_date = datetime.strptime(exp_str[:10], "%Y-%m-%d").date()
        except Exception:
            continue

        if exp_date < today:
            continue

        d["expiry_date"] = exp_date
        sym = d.get("name", "").upper()
        if not sym:
            continue

        seg = d.get("segment", "")
        itype = d.get("instrument_type", "")

        # Futures
        if seg == "NFO-FUT" or itype == "FUT":
            if sym not in futures_map or exp_date < futures_map[sym]["expiry_date"]:
                futures_map[sym] = d
        # Options
        elif itype in ("CE", "PE"):
            key = (sym, exp_date)
            if key not in options_map:
                options_map[key] = []
            options_map[key].append(d)

    return futures_map, options_map


def chunk_list(lst, size=500):
    for i in range(0, len(lst), size):
        yield lst[i : i + size]


# ── Enhanced B+ buildup classifier (mirrors server.py fix) ──────────────────
PRICE_NOISE_FLOOR = 0.10   # % — below this, price move is tick noise
OI_NOISE_FLOOR    = 0.05   # % — below this, OI move is sub-lot rounding noise

def _classify_buildup(oi_chg_pct: float, price_chg_pct: float) -> str:
    """Classify buildup using Enhanced B+ dual noise floor logic.
    OI direction is measured from intraday peak (oi_day_high) for Short Covering.
    """
    price_meaningful = abs(price_chg_pct) >= PRICE_NOISE_FLOOR
    oi_meaningful    = abs(oi_chg_pct)    >= OI_NOISE_FLOOR
    if not price_meaningful and not oi_meaningful:
        return "Flat"
    oi_up    = oi_chg_pct   > 0
    price_up = price_chg_pct > 0
    if oi_meaningful:
        if oi_up and price_up:     return "Long Buildup"
        if oi_up and not price_up: return "Short Buildup"
        if not oi_up and price_up: return "Short Covering"
        return "Long Unwinding"
    # OI silent but price meaningful → price-direction signal
    return "Short Covering" if price_up else "Long Unwinding"


# ── Main Scanner Logic ───────────────────────────────────────────────────────
def run_scan(min_sc_strikes=0):
    print(f"🚀 Starting F&O Short Covering Scan at {now_ist_str()}...")
    kite = get_kite_client()
    
    # Import API functions directly from oi_spurt_routes (NO modification to oi_spurt_routes.py)
    try:
        from oi_spurt_routes import get_option_chain, fetch_oi_spurt
    except ImportError:
        print("❌ Error: Unable to import get_option_chain / fetch_oi_spurt from oi_spurt_routes")
        sys.exit(1)

    # Fetch NSE OI spurt dataset via fetch_oi_spurt scraper
    spurt_dict = {}
    try:
        spurt_data, source, spurt_err = fetch_oi_spurt(min_pct=-500.0)
        if spurt_data and isinstance(spurt_data, list):
            spurt_dict = {
                item["symbol"].upper(): float(item.get("oi_change_pct", 0.0))
                for item in spurt_data if isinstance(item, dict) and "symbol" in item
            }
            print(f"📡 Loaded {len(spurt_dict)} NSE OI spurt records via scraper ({source}).")
    except Exception as e:
        print(f"⚠ Warning fetching NSE OI spurt scraper data: {e}")

    try:
        from db_instruments import get_fno_symbols
        symbols = get_fno_symbols()
    except Exception:
        futures_map, _ = load_fno_instruments()
        symbols = list(futures_map.keys())

    print(f"📊 Found {len(symbols)} F&O stock underlyings to scan via oi_spurt_routes API.")

    results = []

    for idx, sym in enumerate(symbols, start=1):
        try:
            # Layer 1: Call get_option_chain API for Futures & Option Chain data
            rows, nearest, fut_oi, fut_oi_prev, fut_ltp, fut_prev_close, err = get_option_chain(kite, sym)
            if err or not rows or fut_ltp <= 0:
                continue

            # Compute Futures metrics
            fut_price_chg_pct = ((fut_ltp - fut_prev_close) / fut_prev_close * 100) if fut_prev_close else 0.0
            fut_oi_chg_pct    = ((fut_oi - fut_oi_prev) / fut_oi_prev * 100) if fut_oi_prev else 0.0

            # Futures Buildup classification
            fut_buildup = _classify_buildup(fut_oi_chg_pct, fut_price_chg_pct)

            # Layer 2: Find ATM strike
            chain_sorted = sorted(rows, key=lambda r: r["strike"])
            atm_idx = min(range(len(chain_sorted)), key=lambda i: abs(chain_sorted[i]["strike"] - fut_ltp))
            atm_strike = chain_sorted[atm_idx]["strike"]

            matched_count = 0
            formatted_strikes_tag = ""

            # Scenario 1: Futures = Short Covering -> Primary: SC at/above ATM; Also check LB at/above ATM
            if fut_buildup == "Short Covering":
                ce_window = chain_sorted[atm_idx : min(len(chain_sorted), atm_idx + 6)]
                sc_cnt = 0
                lb_cnt = 0
                for r in ce_window:
                    ce_ltp      = r.get("ce_ltp", 0)
                    ce_prev_ltp = r.get("ce_prev_ltp", ce_ltp) or ce_ltp
                    ce_oi_chg   = r.get("ce_oi_chg", 0)

                    price_up = ce_ltp > (ce_prev_ltp * 1.005) if ce_prev_ltp > 0 else False
                    if price_up:
                        if ce_oi_chg < 0:
                            sc_cnt += 1
                        elif ce_oi_chg > 0:
                            lb_cnt += 1

                matched_count = sc_cnt
                formatted_strikes_tag = f"SC{sc_cnt} LB{lb_cnt}"

            # Scenario 2: Futures = Long Buildup -> Primary: LB at/above ATM; Also check SC at/above ATM
            elif fut_buildup == "Long Buildup":
                ce_window = chain_sorted[atm_idx : min(len(chain_sorted), atm_idx + 6)]
                lb_cnt = 0
                sc_cnt = 0
                for r in ce_window:
                    ce_ltp      = r.get("ce_ltp", 0)
                    ce_prev_ltp = r.get("ce_prev_ltp", ce_ltp) or ce_ltp
                    ce_oi_chg   = r.get("ce_oi_chg", 0)

                    price_up = ce_ltp > (ce_prev_ltp * 1.005) if ce_prev_ltp > 0 else False
                    if price_up:
                        if ce_oi_chg > 0:
                            lb_cnt += 1
                        elif ce_oi_chg < 0:
                            sc_cnt += 1

                matched_count = lb_cnt
                formatted_strikes_tag = f"LB{lb_cnt} SC{sc_cnt}"

            # Scenario 3: Futures = Short Buildup -> PE Long Buildup at/below ATM (ATM-5 to ATM)
            elif fut_buildup == "Short Buildup":
                start_idx = max(0, atm_idx - 5)
                pe_window = chain_sorted[start_idx : atm_idx + 1]
                sb_cnt = 0
                for r in pe_window:
                    pe_ltp      = r.get("pe_ltp", 0)
                    pe_prev_ltp = r.get("pe_prev_ltp", pe_ltp) or pe_ltp
                    pe_oi_chg   = r.get("pe_oi_chg", 0)

                    price_up = pe_ltp > (pe_prev_ltp * 1.005) if pe_prev_ltp > 0 else False
                    oi_up    = pe_oi_chg > 0
                    if price_up and oi_up:
                        sb_cnt += 1

                matched_count = sb_cnt
                formatted_strikes_tag = f"SB{sb_cnt}"

            # Scenario 4: Futures = Long Unwinding -> PE Long Buildup OR PE Long Unwinding at/below ATM (ATM-5 to ATM)
            elif fut_buildup == "Long Unwinding":
                start_idx = max(0, atm_idx - 5)
                pe_window = chain_sorted[start_idx : atm_idx + 1]
                lb_cnt = 0
                lu_cnt = 0
                for r in pe_window:
                    pe_ltp      = r.get("pe_ltp", 0)
                    pe_prev_ltp = r.get("pe_prev_ltp", pe_ltp) or pe_ltp
                    pe_oi_chg   = r.get("pe_oi_chg", 0)

                    pe_lb = (pe_ltp > (pe_prev_ltp * 1.005)) and (pe_oi_chg > 0) if pe_prev_ltp > 0 else False
                    pe_lu = (pe_ltp < (pe_prev_ltp * 0.995)) and (pe_oi_chg < 0) if pe_prev_ltp > 0 else False
                    if pe_lb:
                        lb_cnt += 1
                    if pe_lu:
                        lu_cnt += 1

                matched_count = lb_cnt + lu_cnt
                formatted_strikes_tag = f"LB{lb_cnt} LU{lu_cnt}"
            else:
                continue

            # Single-strike ATM PCR calculation
            atm_row = chain_sorted[atm_idx]
            atm_ce_oi = atm_row.get("ce_oi", 0) or 0
            atm_pe_oi = atm_row.get("pe_oi", 0) or 0
            atm_strike_pcr = round(atm_pe_oi / atm_ce_oi, 2) if atm_ce_oi > 0 else 0.0

            bias = "Bullish" if fut_buildup in ("Long Buildup", "Short Covering") else "Bearish"

            # OI Spurt % from NSE scraper dictionary (fallback to calculated fut_oi_chg_pct)
            oi_spurt_pct = spurt_dict.get(sym.upper(), round(fut_oi_chg_pct, 2))

            if matched_count >= min_sc_strikes:
                results.append({
                    "Symbol":            sym,
                    "Pct_Change":        round(fut_price_chg_pct, 2),
                    "OI_Spurt_Pct":      oi_spurt_pct,
                    "Fut_Buildup":        fut_buildup,
                    "Bias":              bias,
                    "Option_Strikes_Count": formatted_strikes_tag,
                    "raw_count":         matched_count,
                    "ATM_Strike_PCR":    atm_strike_pcr,
                })
        except Exception as e:
            print(f"⚠ Warning scanning {sym}: {e}")

    # Custom order for sorting by Fut_Buildup
    BUILDUP_ORDER = {
        "Long Buildup": 1,
        "Short Covering": 2,
        "Short Buildup": 3,
        "Long Unwinding": 4,
    }

    # Sort results by Fut_Buildup order ascending, then raw_count descending, then Pct_Change descending
    results.sort(key=lambda x: (BUILDUP_ORDER.get(x["Fut_Buildup"], 99), -x["raw_count"], -x["Pct_Change"]))
    for r in results:
        del r["raw_count"]
    return results


# ── CSV Export & CLI Runner ──────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="F&O Buildup Multi-Scenario Scanner")
    parser.add_argument("--csv", type=str, default="fno_short_covering_scan.csv", help="Path to save CSV output")
    parser.add_argument("--min-strikes", type=int, default=0, help="Minimum number of option strikes (0 to 6)")
    args = parser.parse_args()

    scan_data = run_scan(min_sc_strikes=args.min_strikes)

    print("\n" + "=" * 90)
    print(f"📈 F&O BUILDUP MULTI-SCENARIO SCAN RESULTS ({len(scan_data)} stocks matched)")
    print("=" * 90)

    # 7-column output
    fieldnames = [
        "Symbol",
        "Pct_Change",
        "OI_Spurt_Pct",
        "Fut_Buildup",
        "Bias",
        "Option_Strikes_Count",
        "ATM_Strike_PCR",
    ]

    # Write to single CSV file
    csv_file = args.csv
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(scan_data)

    print(f"✅ Saved CSV output to: {os.path.abspath(csv_file)}\n")

    # Print CSV output to terminal
    writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(scan_data)


if __name__ == "__main__":
    main()
