"""
live_breakouts_eod_builder.py
==============================
Standalone script and module for building / rebuilding the Live Breakouts EOD Snapshot
outside active market hours.

Key Features:
  - Zero impact on live market streaming (completely isolated standalone execution).
  - Out-of-hours enforcement: automatically skips during active market hours (09:15-15:30 IST).
  - Dynamic date resolution: handles weekends, holidays, and historical sessions.
  - Full 160+ F&O stock coverage: scans all symbols and generates complete indicator state.
  - Auto-cleanup: removes stale snapshot files matching ema_eod_snapshot_*.json.
  - Seamless dashboard integration: outputs standard ema_eod_snapshot_DDMMYYYY.json.

Usage:
  CLI: python app/backend/live_breakouts_eod_builder.py [--force] [--date YYYY-MM-DD]
  API: build_eod_snapshot(target_date_str=None, force=False)
"""

import os
import sys
import glob
import json
import time
import logging
import datetime
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

# Ensure parent directory is on PATH for module imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from session_utils import now_ist, is_market_hours
from server import get_kite
from ema_crossover_scanner import (
    _get_token_map,
    _get_expected_trading_date,
    _scan_single_symbol,
    _ema_crossover_state,
    _state_lock,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("LiveBreakoutsEODBuilder")


def build_eod_snapshot(target_date_str=None, force=False):
    """
    Builds the complete EOD snapshot for all Live Breakout symbols outside market hours.
    
    Args:
        target_date_str (str|None): Optional YYYY-MM-DD date string. If None, resolves
                                   the latest completed trading session date.
        force (bool): If True, bypasses the active market hours safety check.
    
    Returns:
        dict|None: The generated snapshot data dictionary or None on error/skipped.
    """
    now = now_ist()
    
    # Safety Check: Do not execute during active market hours unless forced
    if is_market_hours() and not force:
        logger.warning("[LiveBreakouts Builder] Active market hours detected — skipping EOD rebuild to protect live board.")
        return None

    # Resolve target trading date
    if target_date_str:
        try:
            target_date = datetime.datetime.strptime(target_date_str, "%Y-%m-%d").date()
        except ValueError:
            logger.error(f"[LiveBreakouts Builder] Invalid target_date_str format: '{target_date_str}'. Expected YYYY-MM-DD.")
            return None
    else:
        target_date = _get_expected_trading_date(now)

    date_formatted = target_date.strftime("%Y-%m-%d")
    date_suffix = target_date.strftime("%d%m%Y")
    snapshot_filename = f"ema_eod_snapshot_{date_suffix}.json"
    snapshot_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), snapshot_filename)

    logger.info(f"[LiveBreakouts Builder] Starting EOD rebuild for session: {date_formatted} (File: {snapshot_filename})")

    # Obtain Kite session
    try:
        kite = get_kite()
        if not kite:
            logger.error("[LiveBreakouts Builder] Kite session not connected. Unable to fetch historical candles.")
            return None
    except Exception as e:
        logger.error(f"[LiveBreakouts Builder] Failed getting Kite session: {e}")
        return None

    # Obtain F&O token map
    try:
        token_map = _get_token_map()
        if not token_map:
            logger.error("[LiveBreakouts Builder] F&O token map is empty.")
            return None
    except Exception as e:
        logger.error(f"[LiveBreakouts Builder] Failed loading token map: {e}")
        return None

    total_symbols = len(token_map)
    logger.info(f"[LiveBreakouts Builder] Scanning {total_symbols} F&O stock symbols...")

    crossovers_dict = {}
    completed_count = 0

    # Scan symbols concurrently with worker pool pacing
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures_map = {
            executor.submit(_scan_single_symbol, symbol, token, kite): symbol
            for symbol, token in token_map.items()
        }

        for fut in as_completed(futures_map):
            sym = futures_map[fut]
            try:
                symbol, res = fut.result()
                if res and res.get("status") == "ok":
                    crossovers_dict[symbol] = res
            except Exception as ex:
                logger.warning(f"[LiveBreakouts Builder] Failed scanning {sym}: {ex}")
            
            completed_count += 1
            if completed_count % 25 == 0 or completed_count == total_symbols:
                logger.info(f"[LiveBreakouts Builder] Progress: {completed_count}/{total_symbols} symbols processed.")

    if not crossovers_dict:
        logger.error("[LiveBreakouts Builder] EOD scan produced 0 valid results. Snapshot aborted.")
        return None

    snapshot_data = {
        "date": date_formatted,
        "last_update": f"{date_formatted} 15:30:00",
        "symbols_count": len(crossovers_dict),
        "crossovers": crossovers_dict,
    }

    # Write snapshot file to disk
    try:
        with open(snapshot_path, "w") as f:
            json.dump(snapshot_data, f, indent=2)
        logger.info(f"[LiveBreakouts Builder] Successfully saved EOD snapshot: {snapshot_path} ({len(crossovers_dict)} symbols)")
    except Exception as e:
        logger.error(f"[LiveBreakouts Builder] Failed writing snapshot file {snapshot_path}: {e}")
        return None

    # Auto-cleanup: remove any old/stale ema_eod_snapshot_*.json files
    try:
        pattern = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ema_eod_snapshot_*.json")
        for fpath in glob.glob(pattern):
            if os.path.basename(fpath) != snapshot_filename:
                try:
                    os.remove(fpath)
                    logger.info(f"[LiveBreakouts Builder] Auto-deleted old snapshot file: {os.path.basename(fpath)}")
                except Exception:
                    pass
    except Exception as ex:
        logger.warning(f"[LiveBreakouts Builder] Old snapshot cleanup failed: {ex}")

    # Update in-memory scanner state if active
    try:
        with _state_lock:
            _ema_crossover_state["last_update"] = snapshot_data["last_update"]
            _ema_crossover_state["crossovers"] = snapshot_data["crossovers"]
            _ema_crossover_state["symbols_count"] = snapshot_data["symbols_count"]
            _ema_crossover_state["status"] = "completed"
        logger.info("[LiveBreakouts Builder] In-memory scanner state synchronized.")
    except Exception as ex:
        logger.warning(f"[LiveBreakouts Builder] In-memory state sync warning: {ex}")

    return snapshot_data


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Live Breakouts EOD Snapshot Builder")
    parser.add_argument("--force", action="store_true", help="Bypass active market hours safety check")
    parser.add_argument("--date", type=str, default=None, help="Target session date in YYYY-MM-DD format")
    args = parser.parse_args()

    result = build_eod_snapshot(target_date_str=args.date, force=args.force)
    if result:
        print(f"✅ Success! Rebuilt EOD snapshot for {result['date']} with {result['symbols_count']} symbols.")
        sys.exit(0)
    else:
        print("❌ Rebuild skipped or failed. Check logs for details.")
        sys.exit(1)
