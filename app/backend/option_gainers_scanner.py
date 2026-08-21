"""
================================================================================
📈 F&O Option Premium Gainers Board Scanner
================================================================================
Objective:
  Maintain a live "performance board" of F&O option premium gainers during
  the trading session. The dashboard is served at /api/option-gainers-board;
  Telegram/Discord alerts have been disabled for this board.

Board Architecture — Two Layers:

  1. Opening Layer (⭐ Permanent):
     ATM, OTM1, OTM2 CE/PE locked at 09:15 AM using opening spot prices.
     NEVER removed from the board even if temporarily losing.
     The anchor — always shows how the morning setup is performing.

  2. Running Layer (🏃 Accumulated every 5 minutes):
     Current ATM, OTM1, OTM2 CE/PE recalculated every 5 minutes using fresh
     spot prices. New contracts (not already on the board) are added permanently.
     This ensures fast-moving stocks like KAYNES adding +10% in 30 minutes
     are captured immediately — not missed due to an hourly refresh lag.

  NFO instrument list is fetched ONCE at startup and cached for the day.
  Each 5-minute poll only needs a lightweight spot price refresh (~1-2 seconds).

Performance per 5-minute poll cycle:
  - Spot refresh (210 underlyings):   ~1-2 seconds
  - ATM/OTM calculation (Python):     ~0.1 seconds
  - Open premium fetch (new tokens):  ~0.5 seconds
  - Board LTP fetch (~1,500 tokens):  ~1-2 seconds
  - Total:                            ~5-6 seconds per 300-second window
================================================================================
"""

import os
import json
import threading
import time
import logging
from collections import Counter
from session_utils import now_ist

import sqlite3

_gainers_thread = None

MIN_OPEN_PREMIUM = 0.10  # Min opening premium (₹) — allows low-priced OTM breakout options down to ₹0.10
TOP_N            = 25    # Max gainers shown per report
POLL_INTERVAL    = 300   # 5 minutes between reports (seconds)

# In-memory board state — raw data read by /api/option-gainers-board
# The API endpoint fetches live LTPs on each request using these dicts.
_board_state = {
    "date":            None,
    "last_discovery":  None,   # timestamp of last board update
    "total_tracked":   0,
    "open_premiums":   {},     # {token_int: open_prem} — daily baseline
    "board_contracts": {},     # {token_int: {symbol, opt_type, strike, is_opening}}
}


def _get_db_conn():
    db_path = os.path.join(os.path.dirname(__file__), "tradesignal_cache.db")
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def _init_gainers_db():
    try:
        conn = _get_db_conn()
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS fno_gainers_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_date TEXT UNIQUE NOT NULL,
                snapshot_json TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_fgs_date ON fno_gainers_snapshots(snapshot_date)")
        cursor.execute("DELETE FROM fno_gainers_snapshots WHERE snapshot_date < date('now', '-30 days')")
        conn.commit()
        conn.close()
    except Exception as e:
        logging.warning(f"[Gainers DB] Init failed: {e}")


_init_gainers_db()


def _save_snapshot_to_db(snapshot_data):
    if not snapshot_data:
        return
    try:
        snap_date = snapshot_data.get("date") or now_ist().strftime("%Y-%m-%d")
        snap_json = json.dumps(snapshot_data)
        conn = _get_db_conn()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM fno_gainers_snapshots WHERE snapshot_date < date('now', '-30 days')")
        cursor.execute("""
            INSERT OR REPLACE INTO fno_gainers_snapshots (snapshot_date, snapshot_json, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
        """, (snap_date, snap_json))
        conn.commit()
        conn.close()
        logging.info(f"[Gainers DB] Saved EOD snapshot for {snap_date} to SQLite (30-day retention).")
    except Exception as e:
        logging.warning(f"[Gainers DB] Save to DB failed: {e}")


def get_gainers_snapshot_from_db(date_str):
    try:
        _init_gainers_db()
        conn = _get_db_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT snapshot_json FROM fno_gainers_snapshots WHERE snapshot_date = ?", (date_str,))
        row = cursor.fetchone()
        conn.close()
        if row and row[0]:
            return json.loads(row[0])
    except Exception as e:
        logging.warning(f"[Gainers DB] Fetch by date failed: {e}")
    return None


def get_board_state():
    """Returns raw board data for the API endpoint's live LTP fetch."""
    return {
        "date":            _board_state["date"],
        "last_discovery":  _board_state["last_discovery"],
        "total_tracked":   _board_state["total_tracked"],
        "open_premiums":   dict(_board_state["open_premiums"]),
        "board_contracts": dict(_board_state["board_contracts"]),
    }


def _get_kite():
    from server import get_kite
    return get_kite()





def _is_active_window(now):
    """Returns True between 09:15 and 15:40 IST."""
    if now.hour == 9 and now.minute >= 15: return True
    if 10 <= now.hour <= 14: return True
    if now.hour == 15 and now.minute <= 40: return True
    return False


def _is_eod_window(now):
    return now.hour > 15 or (now.hour == 15 and now.minute >= 40)


_prev_close_cache = {}

_PREV_CLOSE_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prev_close_cache.json")

def _load_prev_close_cache():
    global _prev_close_cache
    from session_utils import now_ist
    try:
        now_val = now_ist()
        expected_date = _get_expected_trading_date(now_val)
        expected_date_str = expected_date.strftime("%Y-%m-%d")
        if os.path.exists(_PREV_CLOSE_CACHE_FILE):
            with open(_PREV_CLOSE_CACHE_FILE, 'r') as f:
                data = json.load(f)
            if data.get("date") == expected_date_str:
                _prev_close_cache = data.get("cache", {})
                logging.info(f"[Gainers EOD] Loaded {len(_prev_close_cache)} previous closes from disk cache.")
            else:
                logging.info(f"[Gainers EOD] Disk cache date ({data.get('date')}) differs from expected ({expected_date_str}). Ignoring.")
    except Exception as e:
        logging.warning(f"[Gainers EOD] Failed to load disk prev close cache: {e}")

def _save_prev_close_cache():
    from session_utils import now_ist
    try:
        now_val = now_ist()
        expected_date = _get_expected_trading_date(now_val)
        expected_date_str = expected_date.strftime("%Y-%m-%d")
        data = {
            "date": expected_date_str,
            "cache": _prev_close_cache
        }
        with open(_PREV_CLOSE_CACHE_FILE, 'w') as f:
            json.dump(data, f)
        logging.info(f"[Gainers EOD] Saved {len(_prev_close_cache)} previous closes to disk cache.")
    except Exception as e:
        logging.warning(f"[Gainers EOD] Failed to save disk prev close cache: {e}")

def _fetch_spot_prices(kite, underlying_names):
    """
    Fetches current spot prices for a set of underlying names.
    Returns Dict[sym -> ltp].
    """
    from oi_spurt_routes import EXCHANGE_MAP
    queries = [EXCHANGE_MAP.get(u, f"NSE:{u}") for u in underlying_names]
    raw = {}
    for b in range(0, len(queries), 500):
        try:
            raw.update(kite.quote(queries[b:b+500]))
        except Exception as e:
            logging.warning(f"[Gainers Scanner] Spot price batch failed: {e}")
            
    # Capture previous day's close if we are during market hours
    # (during market hours, ohlc.close is the correct yesterday's close)
    from session_utils import is_market_hours
    if is_market_hours():
        updated = False
        for k, v in raw.items():
            sym = k.replace("NSE:", "")
            prev = v.get("ohlc", {}).get("close", 0) or 0
            if prev > 0 and _prev_close_cache.get(sym) != prev:
                _prev_close_cache[sym] = prev
                updated = True
        if updated:
            _save_prev_close_cache()

    return {
        k.replace("NSE:", ""): v["last_price"]
        for k, v in raw.items()
        if v.get("last_price", 0) > 0
    }



def _build_atm_otm2_contracts(nfo_data, spot_prices, mode):
    """
    Pure-Python: resolves ATM ± 2 OTM contracts from cached NFO instruments
    and fresh spot prices. No Kite API call needed.

    Returns Dict[token -> {symbol, tradingsymbol, opt_type, strike, label, is_opening}]
    """
    indices = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}

    by_underlying = {}
    for i in nfo_data:
        name = i.get("name", "")
        if i.get("instrument_type") in ["CE", "PE"] and name and name.upper() not in indices:
            u = name.upper()
            by_underlying.setdefault(u, []).append(i)

    contracts = {}
    for u, insts in by_underlying.items():
        if u not in spot_prices:
            continue
        spot = spot_prices[u]

        strikes = sorted(set(i["strike"] for i in insts))
        if len(strikes) < 2:
            continue
        diffs = [strikes[j+1] - strikes[j] for j in range(len(strikes)-1)]
        step = Counter(diffs).most_common(1)[0][0]
        atm_strike = round(spot / step) * step

        expiries = [i["expiry"] for i in insts if i.get("expiry")]
        from session_utils import now_ist
        ref_date = _get_expected_trading_date(now_ist())
        active_expiries = [e for e in expiries if e >= ref_date]
        if not active_expiries:
            continue
        near_expiry = min(active_expiries)

        targets = {
            "ATM_CE":   (atm_strike,            "CE"),
            "ATM_PE":   (atm_strike,            "PE"),
            "OTM1_CE":  (atm_strike + step,     "CE"),
            "OTM1_PE":  (atm_strike - step,     "PE"),
            "OTM2_CE":  (atm_strike + 2*step,   "CE"),
            "OTM2_PE":  (atm_strike - 2*step,   "PE"),
            "OTM3_CE":  (atm_strike + 3*step,   "CE"),
            "OTM3_PE":  (atm_strike - 3*step,   "PE"),
            "OTM4_CE":  (atm_strike + 4*step,   "CE"),
            "OTM4_PE":  (atm_strike - 4*step,   "PE"),
            "OTM5_CE":  (atm_strike + 5*step,   "CE"),
            "OTM5_PE":  (atm_strike - 5*step,   "PE"),
            "OTM6_CE":  (atm_strike + 6*step,   "CE"),
            "OTM6_PE":  (atm_strike - 6*step,   "PE"),
            "OTM7_CE":  (atm_strike + 7*step,   "CE"),
            "OTM7_PE":  (atm_strike - 7*step,   "PE"),
            "OTM8_CE":  (atm_strike + 8*step,   "CE"),
            "OTM8_PE":  (atm_strike - 8*step,   "PE"),
            "OTM9_CE":  (atm_strike + 9*step,   "CE"),
            "OTM9_PE":  (atm_strike - 9*step,   "PE"),
            "OTM10_CE": (atm_strike + 10*step,  "CE"),
            "OTM10_PE": (atm_strike - 10*step,  "PE"),
        }

        contract_map = {
            (i["strike"], i["instrument_type"]): i
            for i in insts if i.get("expiry") == near_expiry
        }

        for label, (target_strike, opt_type) in targets.items():
            match = contract_map.get((target_strike, opt_type))
            if match:
                token = int(match["instrument_token"])
                contracts[token] = {
                    "symbol":        u,
                    "tradingsymbol": match["tradingsymbol"],
                    "opt_type":      opt_type,
                    "strike":        target_strike,
                    "label":         label,
                    "is_opening":    (mode == "opening"),
                }

    return contracts


def _fetch_open_premiums(kite, tokens):
    """
    Fetches ohlc.open for a list of integer tokens.
    Returns Dict[token -> open_premium] — only tokens with valid open_prem >= MIN_OPEN_PREMIUM.
    """
    open_premiums = {}
    for b in range(0, len(tokens), 500):
        batch = tokens[b:b+500]
        try:
            quotes = kite.quote(batch)
            for token_key, q in quotes.items():
                t = int(token_key)
                open_prem = q.get("ohlc", {}).get("open", 0) or 0
                vol = q.get("volume", 0) or 0
                if open_prem >= MIN_OPEN_PREMIUM:
                    open_premiums[t] = open_prem
        except Exception as e:
            logging.warning(f"[Gainers Scanner] Open premium fetch failed: {e}")
        time.sleep(0.2)
    return open_premiums


def _fetch_current_ltps(kite, tokens):
    """
    Fetches current LTP for all board tokens using integer token keys.
    Returns Dict[token -> ltp].
    """
    ltps = {}
    for b in range(0, len(tokens), 500):
        batch = tokens[b:b+500]
        try:
            quotes = kite.quote(batch)
            for token_key, q in quotes.items():
                t = int(token_key)
                ltp = q.get("last_price", 0) or 0
                vol = q.get("volume", 0) or 0
                if ltp > 0:
                    ltps[t] = ltp
        except Exception as e:
            logging.warning(f"[Gainers Scanner] LTP fetch failed: {e}")
        time.sleep(0.2)
    return ltps


# ── EOD Snapshot (on-demand, outside market hours) ────────────────────────────
_eod_snapshot_cache = {"result": None, "ts": 0.0, "running": False}
_EOD_CACHE_TTL      = 28800  # 8 hours — EOD data is static after market close, no need to re-fetch
# EOD snapshots now use _DDMMYYYYHHMM.json suffix instead of a static filename


def _fetch_missing_prev_closes(kite, token_map, expected_date):
    """
    Fetches the correct previous day's close for symbols from historical daily data.
    """
    from datetime import timedelta
    # Fetch a small window of daily candles (20 days is plenty to find the previous trading day)
    from_dt = expected_date - timedelta(days=20)
    to_dt = expected_date
    
    missing = [sym for sym in token_map if sym not in _prev_close_cache]
    if not missing:
        return
        
    logging.info(f"[Gainers EOD] Fetching missing prev closes for {len(missing)} symbols...")
    
    for idx, sym in enumerate(missing):
        token = token_map[sym]
        time.sleep(0.35)  # Respect Zerodha 3 req/s limit
        for attempt in range(2):
            try:
                hist = kite.historical_data(int(token), from_dt, to_dt, "day")
                if not hist:
                    break
                
                # Find the candle matching expected_date
                match_idx = -1
                for i, c in enumerate(hist):
                    c_date = c["date"].date() if hasattr(c["date"], "date") else c["date"]
                    if c_date == expected_date:
                        match_idx = i
                        break
                        
                if match_idx > 0:
                    _prev_close_cache[sym] = hist[match_idx - 1]["close"]
                elif len(hist) >= 1:
                    # If target date's candle is not in hist (e.g. running live/pre-EOD),
                    # the last candle represents the previous session close.
                    _prev_close_cache[sym] = hist[-1]["close"]
                break
            except Exception as e:
                if attempt == 0:
                    time.sleep(1.0)
                else:
                    logging.warning(f"[Gainers EOD] Failed daily hist for {sym}: {e}")
    if missing:
        _save_prev_close_cache()


def _run_eod_snapshot_bg():
    """
    Background: fetches NFO + spot OHLC + option quotes,
    computes % gains for all ATM ± 2 OTM contracts, and stores result.
    Called when the board is empty (server restart / outside 09:15-15:30).
    """
    _eod_snapshot_cache["running"] = True
    logging.info("[Gainers EOD] Starting on-demand snapshot fetch...")
    try:
        kite = _get_kite()
        if not kite:
            return

        # ── 1. NFO instruments ───────────────────────────────────────────────
        from db_instruments import get_cached_instruments
        nfo_data = get_cached_instruments("NFO")
        indices  = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}
        underlying_names = {
            i["name"].upper() for i in nfo_data
            if i.get("instrument_type") in ["CE", "PE"]
            and i.get("name") and i["name"].upper() not in indices
        }

        # ── 2. Spot prices (OHLC open + last_price in one call) ───────────────
        from oi_spurt_routes import EXCHANGE_MAP
        queries = [EXCHANGE_MAP.get(u, f"NSE:{u}") for u in underlying_names]
        raw_quotes = {}
        for b in range(0, len(queries), 500):
            try:
                raw_quotes.update(kite.quote(queries[b:b+500]))
            except Exception as e:
                logging.warning(f"[Gainers EOD] Spot batch failed: {e}")

        # Resolve instrument tokens first
        token_map_eod = {}
        for exch_sym, d in raw_quotes.items():
            sym = exch_sym.split(":")[-1]
            token = d.get("instrument_token")
            if token:
                token_map_eod[sym] = token

        # Warm missing prev closes first
        from session_utils import now_ist
        now_val = now_ist()
        expected_date = _get_expected_trading_date(now_val)
        _fetch_missing_prev_closes(kite, token_map_eod, expected_date)

        opening_spot, current_spot, spot_change_map = {}, {}, {}
        gap_map_eod = {}
        volume_map_eod = {}   # {sym: full_day_volume}
        for exch_sym, d in raw_quotes.items():
            sym        = exch_sym.split(":")[-1]
            open_price = d.get("ohlc", {}).get("open", 0) or 0
            last_price = d.get("last_price", 0) or 0
            prev_close = _prev_close_cache.get(sym) or d.get("ohlc", {}).get("close", 0) or 0
            vol        = d.get("volume", 0) or 0
            if open_price > 0:
                opening_spot[sym] = open_price
            if last_price > 0:
                current_spot[sym] = last_price
            if last_price > 0 and prev_close > 0:
                spot_change_map[sym] = round(((last_price - prev_close) / prev_close) * 100, 2)
            if prev_close > 0:
                gap_map_eod[sym] = round(((open_price - prev_close) / prev_close) * 100, 2)
            else:
                gap_map_eod[sym] = 0.0
            if vol > 0:
                volume_map_eod[sym] = vol

        # ── EOD RVOL: full_day_volume / 20D avg (no time-normalisation needed at EOD) ──
        # Only warm symbols missing from cache — partial cache won't block uncached symbols.
        if not _avg_volume_cache:
            _load_avg_volume_cache_from_db()
        missing_syms = {sym: tok for sym, tok in token_map_eod.items() if sym not in _avg_volume_cache}
        if missing_syms:
            logging.info(f"[Gainers EOD] Warming avg volume for {len(missing_syms)} missing symbols...")
            _warm_avg_volume_bg(kite, missing_syms)
        rvol_eod_map = {}
        for sym in underlying_names:
            day_vol = volume_map_eod.get(sym, 0.0)
            if day_vol <= 0:
                with _avg_volume_lock:
                    day_vol = _last_day_volume_cache.get(sym, 0.0)
            avg_vol = get_avg_volume(sym)
            if avg_vol and avg_vol > 0 and day_vol > 0:
                rvol_eod_map[sym] = round(day_vol / avg_vol, 1)

        # ── 3. Build contracts (opening + running + prev_close + intermediate steps) ──
        opening_contracts = _build_atm_otm2_contracts(nfo_data, opening_spot, mode="opening")
        running_contracts = _build_atm_otm2_contracts(nfo_data, current_spot, mode="running")
        prev_close_spot = {sym: val for sym, val in _prev_close_cache.items() if val > 0}
        prev_close_contracts = _build_atm_otm2_contracts(nfo_data, prev_close_spot, mode="prev_close")

        # Interpolate intermediate spot levels for trending stocks to capture intermediate breakout strikes
        for sym, open_px in opening_spot.items():
            curr_px = current_spot.get(sym, open_px)
            if open_px > 0 and curr_px > 0 and abs(curr_px - open_px) / open_px >= 0.01:
                for factor in [0.2, 0.4, 0.6, 0.8]:
                    mid_px = open_px + (curr_px - open_px) * factor
                    mid_contracts = _build_atm_otm2_contracts(nfo_data, {sym: mid_px}, mode="running")
                    running_contracts.update(mid_contracts)

        board = {}
        board.update(prev_close_contracts)
        board.update(running_contracts)
        board.update(opening_contracts)   # opening wins for duplicate tokens
        logging.info(f"[Gainers EOD] Board: {len(board)} contracts.")

        # ── 4. Option quotes (ohlc.open baseline + last_price LTP) ───────────
        open_premiums = _fetch_open_premiums(kite, list(board.keys()))
        ltps          = _fetch_current_ltps(kite, list(open_premiums.keys()))
        logging.info(f"[Gainers EOD] {len(open_premiums)} baselines, {len(ltps)} LTPs.")

        # ── 5. Compute gains, filter, group, sort ─────────────────────────────
        results = []
        for token, open_prem in open_premiums.items():
            ltp  = ltps.get(token)
            info = board.get(token)
            if not ltp or not info:
                continue
            gain_pct = ((ltp - open_prem) / open_prem) * 100
            if gain_pct <= 0:
                continue
            results.append({
                "symbol":     info["symbol"],
                "opt_type":   info["opt_type"],
                "strike":     info["strike"],
                "is_opening": info["is_opening"],
                "open_prem":  round(open_prem, 2),
                "ltp":        round(ltp, 2),
                "gain_pct":   round(gain_pct, 2),
            })

        by_symbol = {}
        for r in results:
            by_symbol.setdefault(r["symbol"], []).append(r)

        ranked_stocks = sorted(
            by_symbol.items(),
            key=lambda kv: max(r["gain_pct"] for r in kv[1]),
            reverse=True
        )

        from session_utils import now_ist
        now_val = now_ist()
        now_str  = now_val.strftime("%Y-%m-%dT%H:%M:%S")
        date_str = _get_expected_trading_date(now_val).strftime("%Y-%m-%d")

        from ema_crossover_scanner import get_ema_crossover_state, get_daily_dxcnt_map, _compute_ema9_hold
        from server import get_historical_candles
        ema_state = get_ema_crossover_state().get("crossovers", {})
        dxcnt_map = get_daily_dxcnt_map()

        stocks_list = []
        for sym, contracts in ranked_stocks:
            # First try in-memory crossover state
            sym_ema = ema_state.get(sym, {})
            e9h_state = sym_ema.get("ema9_hold")
            e9h_mins = sym_ema.get("ema9_hold_minutes", 0)

            # If unpopulated or missing, fetch 5m candles directly via Kite API
            if e9h_state is None or e9h_mins == 0:
                try:
                    candles_5m = get_historical_candles(kite, sym, "5minute", days_back=7, limit=500)
                    if candles_5m:
                        candles_today = [c for c in candles_5m if c.get('date', '')[:10] == date_str] or candles_5m
                        st, mins = _compute_ema9_hold(candles_today)
                        if st is not None:
                            e9h_state = st
                            e9h_mins = mins
                except Exception as ex:
                    logging.warning(f"[EOD Snapshot] Direct Kite API fetch for {sym} E9H failed: {ex}")

            stocks_list.append({
                "symbol":              sym,
                "best_gain":           round(max(r["gain_pct"] for r in contracts), 2),
                "spot_change_pct":     spot_change_map.get(sym),
                "gap_pct":             gap_map_eod.get(sym, 0.0),
                "rvol_ratio":          rvol_eod_map.get(sym),   # EOD: full_day_vol / 20D avg
                "linearity_score":     sym_ema.get("linearity_score", 0.0),
                "net_movement":        sym_ema.get("net_movement", 0.0),
                "dxcnt":               dxcnt_map.get(sym, 0),
                "ema9_hold":           e9h_state,
                "ema9_hold_minutes":   e9h_mins,
                "fh_spurt_ratio":      sym_ema.get("fh_spurt_ratio"),
                "fh_cumulative_ratio": sym_ema.get("fh_cumulative_ratio"),
                "fh_spurt_tag":        sym_ema.get("fh_spurt_tag"),
                "contracts":           sorted(contracts, key=lambda x: x["gain_pct"]),
            })

        _eod_snapshot_cache["result"] = {
            "stocks":          stocks_list,
            "total_tracked":   len(open_premiums),
            "total_positive":  len(results),
            "n_stocks":        len(ranked_stocks),
            "last_updated":    now_str,
            "date":            date_str,
            "is_eod_snapshot": True,
        }
        _eod_snapshot_cache["ts"] = time.time()
        _save_snapshot_to_db(_eod_snapshot_cache["result"])
        # Persist to disk so server restarts don't trigger a full re-fetch
        try:
            expected_date = _get_expected_trading_date(now_val)
            
            # GUARD: Never write the EOD snapshot file to disk before 15:30 IST on the target date.
            is_past_close = (now_val.hour > 15 or (now_val.hour == 15 and now_val.minute >= 40))
            if expected_date == now_val.date() and not is_past_close:
                logging.info(f"[Gainers EOD] Skipping saving to disk: target date is today ({expected_date}) but it is before 15:40 IST.")
            else:
                suffix = expected_date.strftime('%d%m%Y')
                snapshot_filename = f"gainers_eod_snapshot_{suffix}.json"
                snapshot_path = os.path.join(os.path.dirname(__file__), snapshot_filename)

                with open(snapshot_path, 'w') as f:
                    json.dump(_eod_snapshot_cache["result"], f)
                logging.info(f"[Gainers EOD] Snapshot saved to disk: {snapshot_filename}")

                # Clean up old files
                import glob
                pattern = os.path.join(os.path.dirname(__file__), "gainers_eod_snapshot_*.json")
                for fpath in glob.glob(pattern):
                    if os.path.basename(fpath) != snapshot_filename:
                        try:
                            os.remove(fpath)
                        except Exception:
                            pass
        except Exception as ex:
            logging.warning(f"[Gainers EOD] Disk save failed: {ex}")
        logging.info(f"[Gainers EOD] Snapshot complete: {len(ranked_stocks)} stocks, {len(results)} gainers.")
    except Exception as e:
        logging.error(f"[Gainers EOD] Snapshot failed: {e}")
    finally:
        _eod_snapshot_cache["running"] = False


def _get_expected_trading_date(now):
    """
    Returns the expected date (datetime.date) of the trading session that
    the current/latest EOD snapshot represents.
    """
    from datetime import timedelta
    from session_utils import NSE_HOLIDAYS
    target = now.date()
    if now.hour < 9:
        target = target - timedelta(days=1)
    while target.weekday() >= 5 or target in NSE_HOLIDAYS:
        target = target - timedelta(days=1)
    return target


def get_eod_snapshot():
    """
    Returns cached EOD snapshot if available and fresh.
    On cache miss: tries to load from disk (persisted across server restarts)
    before triggering a background re-fetch.
    """
    from datetime import datetime as _dt2, timedelta
    from session_utils import now_ist as _now_ist
    import glob
    now_ts = time.time()
    _now = _now_ist()
    expected_date = _get_expected_trading_date(_now)
    expected_date_str = expected_date.strftime("%Y-%m-%d")
    expected_suffix = expected_date.strftime("%d%m%Y")
    
    snapshot_filename = f"gainers_eod_snapshot_{expected_suffix}.json"
    snapshot_path = os.path.join(os.path.dirname(__file__), snapshot_filename)

    # Clean up any other old snapshot files
    try:
        pattern = os.path.join(os.path.dirname(__file__), "gainers_eod_snapshot_*.json")
        for fpath in glob.glob(pattern):
            if os.path.basename(fpath) != snapshot_filename:
                os.remove(fpath)
                logging.info(f"[Gainers EOD] Auto-deleted old snapshot file: {os.path.basename(fpath)}")
    except Exception as ex:
        logging.warning(f"[Gainers EOD] Old snapshot cleanup failed: {ex}")

    if _eod_snapshot_cache["result"] and (now_ts - _eod_snapshot_cache["ts"]) < _EOD_CACHE_TTL:
        if _eod_snapshot_cache["result"].get("date") == expected_date_str:
            return _eod_snapshot_cache["result"]
        else:
            _eod_snapshot_cache["result"] = None
            _eod_snapshot_cache["ts"]     = 0.0

    # Try loading from the expected file if it exists
    if not _eod_snapshot_cache["result"] and os.path.exists(snapshot_path):
        try:
            with open(snapshot_path, 'r') as f:
                saved = json.load(f)
            _eod_snapshot_cache["result"] = saved
            _eod_snapshot_cache["ts"]     = now_ts
            logging.info(f"[Gainers EOD] Loaded snapshot from disk ({snapshot_filename}).")

            # If the market for the expected date has closed, but the snapshot on disk
            # was generated before market close (e.g. premarket or midday) or is empty,
            # trigger a background rebuild so the user gets actual post-market data.
            snap_time = _dt2.strptime(saved.get('last_updated', ''), '%Y-%m-%dT%H:%M:%S')
            is_past_close = (_now.hour > 15 or (_now.hour == 15 and _now.minute >= 40))
            market_is_closed = (expected_date < _now.date()) or (expected_date == _now.date() and is_past_close)
            snap_is_pre_close = (snap_time.hour < 15 or (snap_time.hour == 15 and snap_time.minute < 40))
            is_incomplete = (len(saved.get("stocks", [])) == 0)
            if market_is_closed and (snap_is_pre_close or is_incomplete) and not _eod_snapshot_cache["running"]:
                logging.info("[Gainers EOD] Saved snapshot is pre-close or empty for a closed market. Triggering background rebuild...")
                threading.Thread(target=_run_eod_snapshot_bg, daemon=True).start()

            return saved
        except Exception as ex:
            logging.warning(f"[Gainers EOD] Disk load failed: {ex}")

    is_past_close = (_now.hour > 15 or (_now.hour == 15 and _now.minute >= 40))
    market_is_closed = (expected_date < _now.date()) or (expected_date == _now.date() and is_past_close)
    if market_is_closed and not _eod_snapshot_cache["running"]:
        threading.Thread(target=_run_eod_snapshot_bg, daemon=True).start()
    return None   # first call: still loading





def _option_gainers_loop():
    logging.info("[Gainers Scanner] Board thread active. Standing by for 09:15 AM IST.")

    nfo_cache       = None      # NFO instruments cached for the day (fetched once)
    underlying_names = set()    # Set of F&O underlying names (derived from nfo_cache)
    board_contracts = {}        # All contracts ever added to the board
    open_premiums   = {}        # {token: ohlc.open} — daily gain baseline
    opening_locked  = False     # True once opening layer is locked for the day
    last_reset_date = None
    last_poll_time  = 0
    eod_sent        = False

    while True:
        try:
            now = now_ist()
            today_date = now.date()

            # ── Midnight Reset ────────────────────────────────────────────────
            if last_reset_date != today_date:
                nfo_cache        = None
                underlying_names = set()
                board_contracts.clear()
                open_premiums.clear()
                opening_locked = False
                last_poll_time = 0
                eod_sent       = False
                last_reset_date = today_date
                logging.info(f"[Gainers Scanner] Daily board reset for {today_date}.")

            in_window = _is_active_window(now)

            # ── One-Time NFO Cache + Opening Layer Lock (09:15 AM) ───────────
            if in_window and not opening_locked:
                kite = _get_kite()
                if not kite:
                    time.sleep(15)
                    continue

                logging.info("[Gainers Scanner] Fetching NFO instrument cache and locking Opening layer...")
                indices = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}
                from db_instruments import get_cached_instruments
                nfo_cache = get_cached_instruments("NFO")
                underlying_names = {
                    i["name"].upper() for i in nfo_cache
                    if i.get("instrument_type") in ["CE", "PE"]
                    and i.get("name") and i["name"].upper() not in indices
                }

                # Fetch opening spot prices
                spot_prices = _fetch_spot_prices(kite, underlying_names)

                # Build opening layer (ATM ± 2 OTM at 09:15 AM spot prices)
                opening_contracts = _build_atm_otm2_contracts(nfo_cache, spot_prices, mode="opening")
                if not opening_contracts:
                    logging.warning("[Gainers Scanner] Opening layer discovery failed. Retrying in 30s.")
                    time.sleep(30)
                    continue

                prev_close_spot = {sym: val for sym, val in _prev_close_cache.items() if val > 0}
                prev_close_contracts = _build_atm_otm2_contracts(nfo_cache, prev_close_spot, mode="prev_close")

                board_contracts.update(prev_close_contracts)
                board_contracts.update(opening_contracts)

                # Capture opening premiums for all opening tokens
                combined_tokens = list(prev_close_contracts.keys()) + list(opening_contracts.keys())
                new_tokens = [t for t in combined_tokens if t not in open_premiums]
                prems = _fetch_open_premiums(kite, new_tokens)
                open_premiums.update(prems)

                opening_locked = True
                # Sync raw board state — API will fetch live LTPs on each request
                _board_state["date"]            = today_date.strftime("%Y-%m-%d")
                _board_state["last_discovery"]  = now.strftime("%Y-%m-%dT%H:%M:%S")
                _board_state["total_tracked"]   = len(open_premiums)
                _board_state["open_premiums"]   = dict(open_premiums)
                _board_state["board_contracts"] = dict(board_contracts)
                logging.info(
                    f"[Gainers Scanner] Opening layer locked: "
                    f"{len(opening_contracts)} contracts, "
                    f"{len(open_premiums)} with valid baselines."
                )

            # ── EOD Snapshot (once at 15:30 PM) ─────────────────────────────
            if _is_eod_window(now) and opening_locked and not eod_sent:
                # Trigger EOD snapshot generation to save to disk immediately on close.
                # _run_eod_snapshot_bg() handles its own Kite session internally.
                if not _eod_snapshot_cache["running"]:
                    threading.Thread(target=_run_eod_snapshot_bg, daemon=True).start()
                eod_sent = True
                logging.info("[Gainers Scanner] Session closed. EOD snapshot rebuild triggered.")

            # ── 5-Minute Poll: Accumulate Running Layer ──────────────────────
            current_time = time.time()
            if in_window and opening_locked and (current_time - last_poll_time >= POLL_INTERVAL):
                kite = _get_kite()
                if not kite:
                    logging.warning("[Gainers Scanner] Kite unavailable. Skipping poll.")
                else:
                    # Step 1: Refresh spot prices (fast — 1-2 seconds)
                    spot_prices = _fetch_spot_prices(kite, underlying_names)

                    # Step 2: Recalculate current ATM ± 2 OTM using cached NFO
                    running_contracts = _build_atm_otm2_contracts(nfo_cache, spot_prices, mode="running")

                    # Step 3: Accumulate new contracts onto the board permanently
                    new_tokens = [t for t in running_contracts if t not in board_contracts]
                    if new_tokens:
                        for t in new_tokens:
                            board_contracts[t] = running_contracts[t]
                        prems = _fetch_open_premiums(kite, new_tokens)
                        open_premiums.update(prems)
                        logging.info(
                            f"[Gainers Scanner] +{len(new_tokens)} new running strikes added "
                            f"(board now: {len(board_contracts)})."
                        )

                    # Step 4: Sync raw state so API always has latest board tokens.
                    # Live LTPs are fetched on-demand by /api/option-gainers-board.
                    _board_state["date"]            = today_date.strftime("%Y-%m-%d")
                    _board_state["last_discovery"]  = now.strftime("%Y-%m-%dT%H:%M:%S")
                    _board_state["total_tracked"]   = len(open_premiums)
                    _board_state["open_premiums"]   = dict(open_premiums)
                    _board_state["board_contracts"] = dict(board_contracts)

                    last_poll_time = current_time

            time.sleep(30)

        except Exception as e:
            logging.error(f"[Gainers Scanner] Exception in loop: {e}")
            time.sleep(30)


def start_option_gainers_scanner():
    global _gainers_thread, _rvol_eligible_after
    # Enforce EOD snapshot cleanup on startup
    try:
        import glob
        from session_utils import now_ist, NSE_HOLIDAYS
        now_val = now_ist()
        today = now_val.date()
        is_weekend = today.weekday() >= 5
        is_holiday = today in NSE_HOLIDAYS
        is_trading_day = not is_weekend and not is_holiday
        
        expected_date = _get_expected_trading_date(now_val)
        expected_suffix = expected_date.strftime("%d%m%Y")
        expected_filename = f"gainers_eod_snapshot_{expected_suffix}.json"
        
        pattern = os.path.join(os.path.dirname(__file__), "gainers_eod_snapshot_*.json")
        deleted_count = 0
        
        for fpath in glob.glob(pattern):
            fname = os.path.basename(fpath)
            should_delete = False
            
            if is_trading_day and now_val.hour >= 9:
                # Delete all snapshots post 09:00 hrs on a trading day
                should_delete = True
            else:
                # Delete only older snapshots (not matching the expected trading date)
                if fname != expected_filename:
                    should_delete = True
                    
            if should_delete:
                try:
                    os.remove(fpath)
                    deleted_count += 1
                    logging.info(f"[Gainers EOD] Startup cleanup: deleted stale/old snapshot {fname}")
                except Exception as e:
                    logging.warning(f"[Gainers EOD] Startup cleanup: failed to delete {fname}: {e}")
                    
        if deleted_count > 0:
            logging.info(f"[Gainers EOD] Startup cleanup completed. Deleted {deleted_count} file(s).")
    except Exception as ex:
        logging.warning(f"[Gainers EOD] Startup cleanup failed: {ex}")

    # Option A: record startup time; RVOL warm won't fire until 5 min have elapsed
    _rvol_eligible_after = time.time() + _RVOL_STARTUP_DELAY_SEC
    logging.info(
        f"[Gainers RVOL] Warm deferred until "
        f"{_dt.datetime.fromtimestamp(_rvol_eligible_after).strftime('%H:%M:%S')} "
        f"(+{_RVOL_STARTUP_DELAY_SEC}s startup delay)."
    )
    if _gainers_thread is None or not _gainers_thread.is_alive():
        logging.info("[Gainers Scanner] Spawning F&O Option Gainers Board background thread...")
        _gainers_thread = threading.Thread(target=_option_gainers_loop, daemon=True)
        _gainers_thread.start()


# ── RVOL: 20-day Average Daily Volume Cache ────────────────────────────────────
# Warmed ONCE per day in a background thread using instrument_tokens obtained
# from the existing spot quote batch (zero extra API calls per board refresh).
import datetime as _dt
import concurrent.futures as _cf

_avg_volume_cache   = {}   # {symbol: float}  — avg daily volume (shares)
_last_day_volume_cache = {} # {symbol: float} — last trading day's volume (shares)
_avg_volume_date    = None # date when cache was last successfully built
_avg_volume_lock    = threading.Lock()
_avg_volume_warming = False

# ── Option A: Startup delay ───────────────────────────────────────────────────
# RVOL warm is intentionally deferred 5 minutes after server start.
# This gives the EMA Crossover Scanner time to complete its first full pass
# (~4-5 min for 216 symbols × 4 intervals) before RVOL starts consuming
# the Zerodha historical API quota, preventing 429 TooManyRequests collisions.
_RVOL_STARTUP_DELAY_SEC = 300   # 5 minutes
_rvol_eligible_after    = 0.0   # set by start_option_gainers_scanner()


def _load_avg_volume_cache_from_db():
    global _avg_volume_date, _avg_volume_cache, _last_day_volume_cache
    try:
        import sqlite3
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tradesignal_cache.db")
        if not os.path.exists(db_path):
            return False
            
        today = _dt.date.today().strftime("%Y-%m-%d")
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Check if table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='rvol_baseline'")
        if not cursor.fetchone():
            conn.close()
            return False
            
        # Drop table if schema is outdated (missing last_day_volume)
        cursor.execute("PRAGMA table_info(rvol_baseline)")
        columns = [col[1] for col in cursor.fetchall()]
        if "last_day_volume" not in columns:
            cursor.execute("DROP TABLE rvol_baseline")
            conn.commit()
            conn.close()
            return False
            
        cursor.execute("SELECT symbol, average_volume, last_day_volume, date FROM rvol_baseline")
        rows = cursor.fetchall()
        conn.close()
        
        if not rows:
            return False
            
        # Load the cache regardless of the date so we have a baseline immediately
        first_date = rows[0][3]
        with _avg_volume_lock:
            _avg_volume_cache.clear()
            _last_day_volume_cache.clear()
            for symbol, avg_vol, last_vol, _ in rows:
                _avg_volume_cache[symbol] = avg_vol
                if last_vol is not None:
                    _last_day_volume_cache[symbol] = last_vol
            if first_date == today:
                _avg_volume_date = _dt.date.today()
        
        logging.info(f"[Gainers RVOL] Loaded {len(_avg_volume_cache)} symbols from SQLite cache (cache date: {first_date}).")
        return first_date == today
    except Exception as e:
        logging.warning(f"[Gainers RVOL] Failed to load SQLite cache: {e}")
    return False

def _save_avg_volume_cache_to_db():
    try:
        import sqlite3
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tradesignal_cache.db")
        today_str = _avg_volume_date.strftime("%Y-%m-%d") if _avg_volume_date else _dt.date.today().strftime("%Y-%m-%d")
        
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Ensure table exists
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS rvol_baseline (
                symbol TEXT PRIMARY KEY,
                average_volume REAL NOT NULL,
                last_day_volume REAL,
                date TEXT NOT NULL
            )
        ''')
        
        with _avg_volume_lock:
            items = []
            for sym, avg in _avg_volume_cache.items():
                l_vol = _last_day_volume_cache.get(sym)
                items.append((sym, avg, l_vol, today_str))
            
        if items:
            cursor.execute("DELETE FROM rvol_baseline")
            cursor.executemany(
                "INSERT OR REPLACE INTO rvol_baseline (symbol, average_volume, last_day_volume, date) VALUES (?, ?, ?, ?)",
                items
            )
            conn.commit()
            
        conn.close()
        logging.info(f"[Gainers RVOL] Saved {len(items)} symbols to SQLite cache.")
    except Exception as e:
        logging.warning(f"[Gainers RVOL] Failed to save SQLite cache: {e}")

def _save_single_avg_volume_to_db(symbol, avg, last_vol):
    try:
        import sqlite3
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tradesignal_cache.db")
        today_str = _dt.date.today().strftime("%Y-%m-%d")
        
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS rvol_baseline (
                symbol TEXT PRIMARY KEY,
                average_volume REAL NOT NULL,
                last_day_volume REAL,
                date TEXT NOT NULL
            )
        ''')
        cursor.execute(
            "INSERT OR REPLACE INTO rvol_baseline (symbol, average_volume, last_day_volume, date) VALUES (?, ?, ?, ?)",
            (symbol, avg, last_vol, today_str)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logging.warning(f"[Gainers RVOL] Failed to save single symbol {symbol} to SQLite: {e}")


def get_avg_volume(symbol):
    """Thread-safe read. Returns None if cache not yet ready for this symbol."""
    with _avg_volume_lock:
        return _avg_volume_cache.get(symbol)


def _warm_avg_volume_bg(kite, token_map):
    """Background: fetch 20-day daily candles per symbol and store avg volume.
    token_map = {symbol: instrument_token} — passed in from spot quote response,
    so NO extra kite.quote() calls are needed."""
    global _avg_volume_date, _avg_volume_warming
    import random
    try:
        today   = _dt.date.today()
        from_dt = today - _dt.timedelta(days=30)
        to_dt   = today - _dt.timedelta(days=1)

        # Prune old day baselines from database once before warming
        try:
            import sqlite3
            db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tradesignal_cache.db")
            today_str = today.strftime("%Y-%m-%d")
            conn = sqlite3.connect(db_path)
            conn.execute("DELETE FROM rvol_baseline WHERE date != ?", (today_str,))
            conn.commit()
            conn.close()
        except Exception as e:
            logging.warning(f"[Gainers RVOL] DB prune error: {e}")

        pending_symbols = list(token_map.keys())
        results = {}
        last_vols = {}

        while pending_symbols:
            sym = pending_symbols.pop(0)  # FIFO queue: pop from front
            token = token_map[sym]
            try:
                # Option B: 0.6s gap → ~1.67 req/s, leaving ~1.33 req/s headroom
                # for the EMA Crossover Scanner's concurrent historical fetches.
                # (Zerodha hard limit: 3 req/s across the entire API key session.)
                time.sleep(0.6)
                hist = kite.historical_data(int(token), from_dt, to_dt, "day")
                vols = [c["volume"] for c in hist if c.get("volume", 0) > 0]
                avg = (sum(vols) / len(vols)) if vols else None
                last_vol = hist[-1].get("volume", 0.0) if hist else 0.0

                if avg is not None:
                    # Update local cache immediately
                    with _avg_volume_lock:
                        _avg_volume_cache[sym] = avg
                        _last_day_volume_cache[sym] = last_vol
                        _avg_volume_date = today
                    
                    # Save immediately to SQLite
                    _save_single_avg_volume_to_db(sym, avg, last_vol)

            except Exception as e:
                is_rate_limit = "429" in str(e) or "too many" in str(e).lower()
                if is_rate_limit:
                    backoff = 5.0 + random.uniform(0.5, 2.0)
                    logging.warning(f"[Gainers RVOL] Rate limited for {sym}, appending to back of queue. Sleeping {backoff:.1f}s...")
                    pending_symbols.append(sym)  # Send to back to retry later
                    time.sleep(backoff)
                else:
                    logging.warning(f"[Gainers RVOL] Error for {sym}: {e}")

        logging.info(f"[Gainers RVOL] Average volume warmup complete.")
    except Exception as e:
        logging.warning(f"[Gainers RVOL] Warm failed: {e}")
    finally:
        _avg_volume_warming = False


def ensure_avg_volume_warm(kite, token_map):
    """Non-blocking: triggers background warm only if cache is missing/stale."""
    global _avg_volume_warming
    if not token_map:
        return

    # Check if SQLite database cache has today's data first (avoids unnecessary historical API requests)
    today = _dt.date.today()
    if not _avg_volume_cache or _avg_volume_date != today:
        _load_avg_volume_cache_from_db()

    with _avg_volume_lock:
        if _avg_volume_date == today and _avg_volume_cache:
            # Cache was warmed today — check for per-symbol gaps
            missing = {sym: tok for sym, tok in token_map.items()
                       if sym not in _avg_volume_cache}
            if not missing:
                return  # All symbols present — nothing to do
            if _avg_volume_warming:
                return  # Warm already in progress — it will fill gaps
            
            # Targeted re-warm for missing symbols only
            _avg_volume_warming = True
            threading.Thread(target=_warm_avg_volume_bg, args=(kite, missing),
                             daemon=True).start()
            return

    # If startup delay is active, defer full warm (unless we loaded existing today's cache above or outside market hours)
    from session_utils import is_market_hours
    if is_market_hours() and time.time() < _rvol_eligible_after:
        remaining = int(_rvol_eligible_after - time.time())
        logging.debug(f"[Gainers RVOL] Warm deferred — {remaining}s startup delay remaining.")
        return

    with _avg_volume_lock:
        if _avg_volume_warming:
            return  # Full warm already in progress
        _avg_volume_warming = True

    # Full warm — first run of the day
    threading.Thread(target=_warm_avg_volume_bg, args=(kite, dict(token_map)),
                     daemon=True).start()


def resolve_all_spot_tokens():
    """
    Query the SQLite instruments cache to resolve the NSE/BSE cash instrument tokens
    for all F&O underlying assets. No Kite network requests are made.
    """
    import sqlite3
    db_path = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tradesignal_cache.db"))
    if not os.path.exists(db_path):
        return {}

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 1. Retrieve all unique F&O underlying names
    try:
        cursor.execute("SELECT DISTINCT name FROM instruments WHERE exchange = 'NFO' AND segment = 'NFO-FUT'")
        fno_symbols = [row[0].upper() for row in cursor.fetchall() if row[0]]
    except Exception as e:
        logging.warning(f"[Gainers RVOL] Error loading F&O symbols: {e}")
        conn.close()
        return {}

    token_map = {}

    # 2. Map standard index names
    SPOT_MAP = {
        "NIFTY":      ("NSE", "NIFTY 50"),
        "BANKNIFTY":  ("NSE", "NIFTY BANK"),
        "FINNIFTY":   ("NSE", "NIFTY FIN SERVICE"),
        "MIDCPNIFTY": ("NSE", "NIFTY MID SELECT"),
        "SENSEX":     ("BSE", "SENSEX"),
        "BANKEX":     ("BSE", "BANKEX"),
    }

    try:
        # Load indices
        for sym, (exch, ts) in SPOT_MAP.items():
            cursor.execute(
                "SELECT instrument_token FROM instruments WHERE exchange = ? AND tradingsymbol = ? LIMIT 1",
                (exch, ts)
            )
            row = cursor.fetchone()
            if row:
                token_map[sym] = row[0]

        # Load F&O stocks
        for sym in fno_symbols:
            if sym in SPOT_MAP:
                continue
            cursor.execute(
                "SELECT instrument_token FROM instruments WHERE exchange = 'NSE' AND tradingsymbol = ? LIMIT 1",
                (sym,)
            )
            row = cursor.fetchone()
            if row:
                token_map[sym] = row[0]
    except Exception as e:
        logging.warning(f"[Gainers RVOL] Error mapping cash tokens: {e}")

    conn.close()
    _load_prev_close_cache()
    return token_map

_load_prev_close_cache()

# ── 20% Milestone Timeline Computation ──
_milestone_cache = {}
_MILESTONE_CACHE_TTL = 15.0

def get_contract_milestones(token=None, symbol=None, strike=None, opt_type=None, date_str=None, step_pct=20.0):
    """
    Computes cumulative +20% incremental milestones from 09:15 AM opening baseline.
    Returns structured timeline for live session or EOD replay.
    """
    from session_utils import IST, now_ist, is_market_hours
    from datetime import datetime, date, time as dt_time

    now_val = now_ist()
    today_str = now_val.strftime("%Y-%m-%d")
    target_date_str = date_str or today_str

    cache_key = f"{token}_{symbol}_{strike}_{opt_type}_{target_date_str}_{step_pct}"
    now_ts = time.time()
    if cache_key in _milestone_cache:
        cached_ts, cached_res = _milestone_cache[cache_key]
        if (now_ts - cached_ts) < _MILESTONE_CACHE_TTL or (target_date_str < today_str and cached_res.get("success")):
            return cached_res

    # 1. Resolve Instrument Token and Tradingsymbol from DB if needed
    tradingsymbol = None
    sym_name = symbol
    resolved_strike = float(strike) if strike is not None else None
    resolved_type = (opt_type or "").upper()
    resolved_token = int(token) if token else None

    conn = _get_db_conn()
    c = conn.cursor()
    try:
        if resolved_token:
            c.execute(
                "SELECT instrument_token, tradingsymbol, strike, instrument_type, name FROM instruments WHERE instrument_token = ? LIMIT 1",
                (resolved_token,)
            )
            row = c.fetchone()
            if row:
                resolved_token, tradingsymbol, resolved_strike, resolved_type, sym_name = row
        elif sym_name and resolved_strike and resolved_type:
            c.execute(
                "SELECT instrument_token, tradingsymbol, strike, instrument_type, name FROM instruments WHERE name = ? AND strike = ? AND instrument_type = ? ORDER BY expiry ASC LIMIT 1",
                (sym_name, resolved_strike, resolved_type)
            )
            row = c.fetchone()
            if row:
                resolved_token, tradingsymbol, resolved_strike, resolved_type, sym_name = row

        # Resolve spot token
        spot_token = None
        if sym_name:
            c.execute(
                "SELECT instrument_token FROM instruments WHERE exchange = 'NSE' AND tradingsymbol = ? LIMIT 1",
                (sym_name,)
            )
            s_row = c.fetchone()
            if s_row:
                spot_token = s_row[0]
    finally:
        conn.close()

    if not resolved_token:
        return {"success": False, "error": "Option contract not found"}

    kite = _get_kite()
    if not kite:
        return {"success": False, "error": "Kite session not connected"}

    # 2. Fetch Historical 1-Minute Candles for Option & Spot
    from datetime import timedelta
    try:
        y, m, d = map(int, target_date_str.split("-"))
        t_date = date(y, m, d)
    except Exception:
        t_date = now_val.date()
        target_date_str = t_date.strftime("%Y-%m-%d")

    from_dt = datetime.combine(t_date, dt_time(9, 15), tzinfo=IST)
    to_dt = datetime.combine(t_date, dt_time(15, 40), tzinfo=IST)

    opt_candles = []
    try:
        opt_candles = kite.historical_data(resolved_token, from_dt, to_dt, "minute")
    except Exception as e:
        logging.warning(f"[Milestones] Error fetching option candles: {e}")

    # Fallback to most recent available trading day if target date has no data (e.g. midnight rollover / weekend)
    if not opt_candles:
        for delta in range(1, 7):
            fallback_date = t_date - timedelta(days=delta)
            f_from = datetime.combine(fallback_date, dt_time(9, 15), tzinfo=IST)
            f_to = datetime.combine(fallback_date, dt_time(15, 40), tzinfo=IST)
            try:
                test_cds = kite.historical_data(resolved_token, f_from, f_to, "minute")
                if test_cds:
                    opt_candles = test_cds
                    t_date = fallback_date
                    target_date_str = fallback_date.strftime("%Y-%m-%d")
                    from_dt = f_from
                    to_dt = f_to
                    break
            except Exception:
                pass

    if not opt_candles:
        return {"success": False, "error": f"No option candle data available for {target_date_str}"}

    spot_map = {}
    open_spot = 0.0
    if spot_token:
        try:
            s_candles = kite.historical_data(spot_token, from_dt, to_dt, "minute")
            if s_candles:
                open_spot = s_candles[0].get("open", 0.0)
                for sc in s_candles:
                    t_str = sc["date"].strftime("%H:%M")
                    spot_map[t_str] = sc
        except Exception as e:
            logging.warning(f"[Milestones] Error fetching spot candles: {e}")

    # 3. Compute Step-by-Step Milestones
    open_prem = opt_candles[0].get("open", 0.0)
    if open_prem <= 0:
        for cd in opt_candles:
            if cd.get("open", 0) > 0:
                open_prem = cd["open"]
                break
    if open_prem <= 0:
        open_prem = 0.10

    curr_target_pct = float(step_pct)
    milestones = []
    prev_milestone_time = None
    step_num = 1

    for cd in opt_candles:
        high = cd.get("high", 0.0)
        close = cd.get("close", 0.0)
        t_str = cd["date"].strftime("%H:%M")
        t_mins = (cd["date"].hour * 60 + cd["date"].minute) - (9 * 60 + 15)

        while True:
            target_price = round(open_prem * (1.0 + curr_target_pct / 100.0), 2)
            if high >= target_price:
                if prev_milestone_time is None:
                    delta_mins = max(0, t_mins)
                else:
                    prev_h, prev_m = map(int, prev_milestone_time.split(":"))
                    delta_mins = max(0, (cd["date"].hour * 60 + cd["date"].minute) - (prev_h * 60 + prev_m))

                sc = spot_map.get(t_str)
                spot_price = sc["close"] if sc else None
                spot_move_pct = round(((spot_price - open_spot) / open_spot) * 100.0, 2) if (spot_price and open_spot > 0) else None
                spot_volume = sc.get("volume", 0) if sc else 0
                opt_volume = cd.get("volume", 0)

                multiplier = round(1.0 + curr_target_pct / 100.0, 1)
                mult_label = f"{multiplier:g}x" if multiplier >= 2.0 else ""

                milestones.append({
                    "step": step_num,
                    "milestone_pct": round(curr_target_pct, 1),
                    "mult_label": mult_label,
                    "target_price": target_price,
                    "time": t_str,
                    "candle_high": high,
                    "candle_close": close,
                    "volume": opt_volume,
                    "opt_volume": opt_volume,
                    "spot_volume": spot_volume,
                    "spot_price": spot_price,
                    "spot_move_pct": spot_move_pct,
                    "elapsed_mins": max(0, t_mins),
                    "delta_mins": delta_mins,
                })

                prev_milestone_time = t_str
                curr_target_pct += step_pct
                step_num += 1
            else:
                break

    last_candle = opt_candles[-1]
    current_ltp = last_candle.get("close", open_prem)
    total_gain_pct = round(((current_ltp - open_prem) / open_prem) * 100.0, 2) if open_prem > 0 else 0.0

    # In-progress next milestone
    next_target_pct = round(curr_target_pct, 1)
    next_target_price = round(open_prem * (1.0 + next_target_pct / 100.0), 2)
    points_needed = max(0.0, round(next_target_price - current_ltp, 2))
    pct_needed = max(0.0, round(((next_target_price - current_ltp) / current_ltp) * 100.0, 2)) if current_ltp > 0 else 0.0

    res = {
        "success": True,
        "symbol": sym_name,
        "tradingsymbol": tradingsymbol,
        "strike": resolved_strike,
        "opt_type": resolved_type,
        "token": resolved_token,
        "date": target_date_str,
        "open_price": open_prem,
        "current_ltp": current_ltp,
        "max_high": max(c.get("high", 0) for c in opt_candles),
        "total_gain_pct": total_gain_pct,
        "milestones": milestones,
        "in_progress": {
            "is_market_hours": is_market_hours() if target_date_str == today_str else False,
            "next_milestone_pct": next_target_pct,
            "next_target_price": next_target_price,
            "points_needed": points_needed,
            "pct_needed": pct_needed,
        }
    }

    _milestone_cache[cache_key] = (now_ts, res)
    return res

