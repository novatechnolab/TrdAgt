"""
Fresh alert engine for the F&O Premium Gainers Board.

This module intentionally does not reuse the existing option premium scanner or
the FNO spike engine. Its universe is the live board state from
option_gainers_scanner, so every alert matches the contracts shown on the
Premium Gainers dashboard.
"""

import logging
import os
import json
import threading
import time
from collections import deque
from datetime import datetime, time as dt_time

from session_utils import IST, is_market_hours, is_premarket, now_ist


import sqlite3

logger = logging.getLogger("option_gainers_alerts")

POLL_INTERVAL_SECS = 15
WINDOW_SECS = 180
PREMIUM_SPIKE_PCT = 10.0
SPOT_SPIKE_PCT = 0.30
MIN_LTP = 0.10
COOLDOWN_SECS = 180
MAX_ALERTS = 500
MAX_TOKEN_HISTORY = 32

_thread = None
_lock = threading.Lock()
_alerts = deque(maxlen=MAX_ALERTS)
_token_history = {}
_cooldowns = {}
_seq = 0
_last_scan = None
_last_error = None
_tracked_contracts = 0
_sampled_contracts = 0
_eod_snapshot_cache = {"result": None, "ts": 0.0, "running": False}
_EOD_CACHE_TTL = 28800
_last_reset_date = None   # Tracks last day-start flush date (YYYY-MM-DD)


def _get_db_conn():
    db_path = os.path.join(os.path.dirname(__file__), "tradesignal_cache.db")
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def _init_alerts_db():
    try:
        conn = _get_db_conn()
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS premium_spike_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_date TEXT NOT NULL,
                alert_time TEXT NOT NULL,
                timestamp REAL NOT NULL,
                token INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                tradingsymbol TEXT NOT NULL,
                opt_type TEXT NOT NULL,
                strike REAL NOT NULL,
                label TEXT,
                layer TEXT,
                direction TEXT,
                open_prem REAL,
                old_ltp REAL,
                ltp REAL,
                premium_spike_pct REAL,
                board_gain_pct REAL,
                old_spot REAL,
                spot REAL,
                spot_spike_pct REAL,
                interval_volume INTEGER,
                consistency REAL,
                is_eod_snapshot INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(alert_date, token, alert_time) ON CONFLICT IGNORE
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_psa_date ON premium_spike_alerts(alert_date)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_psa_symbol ON premium_spike_alerts(symbol)")
        cursor.execute("DELETE FROM premium_spike_alerts WHERE alert_date < date('now', '-30 days')")
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"[Premium Alerts DB] Init failed: {e}")


_init_alerts_db()


def _save_alerts_to_db(alerts):
    if not alerts:
        return
    try:
        conn = _get_db_conn()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM premium_spike_alerts WHERE alert_date < date('now', '-30 days')")
        for a in alerts:
            alert_date = a.get("date") or (a.get("time") and a["time"][:10]) or now_ist().strftime("%Y-%m-%d")
            cursor.execute("""
                INSERT OR IGNORE INTO premium_spike_alerts (
                    alert_date, alert_time, timestamp, token, symbol, tradingsymbol,
                    opt_type, strike, label, layer, direction, open_prem, old_ltp, ltp,
                    premium_spike_pct, board_gain_pct, old_spot, spot, spot_spike_pct,
                    interval_volume, consistency, is_eod_snapshot
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                alert_date,
                a.get("time", ""),
                a.get("ts", time.time()),
                a.get("token", 0),
                a.get("symbol", ""),
                a.get("tradingsymbol", ""),
                a.get("opt_type", ""),
                a.get("strike", 0.0),
                a.get("label", ""),
                a.get("layer", ""),
                a.get("direction", ""),
                a.get("open_prem", 0.0),
                a.get("old_ltp", 0.0),
                a.get("ltp", 0.0),
                a.get("premium_spike_pct", 0.0),
                a.get("board_gain_pct", 0.0),
                a.get("old_spot", 0.0),
                a.get("spot", 0.0),
                a.get("spot_spike_pct", 0.0),
                a.get("interval_volume", 0),
                a.get("consistency", 0.0),
                1 if a.get("is_eod_snapshot") else 0
            ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"[Premium Alerts DB] Save failed: {e}")


def get_alerts_from_db_by_date(date_str):
    """Returns persisted alerts for a given date from SQLite.
    GUARD: Must not be called during live market hours to prevent stale-data leaks."""
    if is_market_hours():
        logger.warning("[Premium Alerts DB] get_alerts_from_db_by_date() called during market hours — blocked.")
        return []
    try:
        _init_alerts_db()
        conn = _get_db_conn()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT alert_date, alert_time, timestamp, token, symbol, tradingsymbol,
                   opt_type, strike, label, layer, direction, open_prem, old_ltp, ltp,
                   premium_spike_pct, board_gain_pct, old_spot, spot, spot_spike_pct,
                   interval_volume, consistency, is_eod_snapshot
            FROM premium_spike_alerts
            WHERE alert_date = ?
            ORDER BY timestamp DESC
        """, (date_str,))
        rows = cursor.fetchall()
        conn.close()
        alerts = []
        for r in rows:
            alerts.append({
                "date": r[0],
                "time": r[1],
                "ts": r[2],
                "token": r[3],
                "symbol": r[4],
                "tradingsymbol": r[5],
                "opt_type": r[6],
                "strike": r[7],
                "label": r[8],
                "layer": r[9],
                "direction": r[10],
                "open_prem": r[11],
                "old_ltp": r[12],
                "ltp": r[13],
                "premium_spike_pct": r[14],
                "board_gain_pct": r[15],
                "old_spot": r[16],
                "spot": r[17],
                "spot_spike_pct": r[18],
                "interval_volume": r[19],
                "consistency": r[20],
                "is_eod_snapshot": bool(r[21]),
            })
        return alerts
    except Exception as e:
        logger.warning(f"[Premium Alerts DB] Fetch by date failed: {e}")
        return []


def _get_kite():
    from server import get_kite
    return get_kite()


def start_option_gainers_alerts_scanner():
    """Starts the fresh Premium Gainers alert scanner once per process."""
    global _thread
    if _thread is None or not _thread.is_alive():
        logger.info("[Premium Alerts] Starting fresh Premium Gainers alert scanner.")
        _thread = threading.Thread(
            target=_scanner_loop,
            daemon=True,
            name="OptionGainersAlertsScanner",
        )
        _thread.start()


def get_alerts(after=None):
    """Returns alert history, newest first by default."""
    with _lock:
        if after is not None:
            return [a for a in _alerts if a.get("seq", 0) > after]
        return list(reversed(_alerts))


def get_status():
    with _lock:
        return {
            "status": "live" if is_market_hours() else "idle",
            "last_scan": _last_scan,
            "last_error": _last_error,
            "tracked_contracts": _tracked_contracts,
            "sampled_contracts": _sampled_contracts,
            "alert_count": len(_alerts),
            "poll_interval_secs": POLL_INTERVAL_SECS,
            "window_secs": WINDOW_SECS,
            "premium_spike_pct": PREMIUM_SPIKE_PCT,
            "spot_spike_pct": SPOT_SPIKE_PCT,
            "eod_snapshot_loading": _eod_snapshot_cache["running"],
        }


def clear_alerts():
    global _seq
    with _lock:
        _alerts.clear()
        _cooldowns.clear()
        _seq = 0


def _scanner_loop():
    global _last_scan, _last_error, _tracked_contracts, _sampled_contracts, _last_reset_date, _seq

    while True:
        try:
            if not is_market_hours():
                _last_scan = now_ist().strftime("%Y-%m-%dT%H:%M:%S")
                time.sleep(30)
                continue

            # ── Day-start flush: clear in-memory state when a new trading date begins ──
            # Prevents prior-day deque entries / cooldowns / history from leaking into
            # the new session's live feed. Does NOT touch the DB (DB is date-keyed).
            today_str = now_ist().strftime("%Y-%m-%d")
            if _last_reset_date != today_str:
                with _lock:
                    _alerts.clear()
                    _cooldowns.clear()
                    _token_history.clear()
                    _seq = 0
                _last_reset_date = today_str
                logger.info(f"[Premium Alerts] Day-start flush: cleared in-memory state for {today_str}")

            kite = _get_kite()
            if not kite:
                _set_error("Kite session unavailable")
                time.sleep(30)
                continue

            from option_gainers_scanner import get_board_state, start_option_gainers_scanner
            start_option_gainers_scanner()
            state = get_board_state()

            open_premiums = {
                int(token): value
                for token, value in (state.get("open_premiums", {}) or {}).items()
            }
            board_contracts = {
                int(token): info
                for token, info in (state.get("board_contracts", {}) or {}).items()
            }
            token_items = [
                (token, info)
                for token, info in board_contracts.items()
                if token in open_premiums
            ]

            _tracked_contracts = len(token_items)
            if not token_items:
                _last_scan = now_ist().strftime("%Y-%m-%dT%H:%M:%S")
                time.sleep(POLL_INTERVAL_SECS)
                continue

            option_quotes = _fetch_option_quotes(kite, [token for token, _ in token_items])
            spot_quotes = _fetch_spot_quotes(kite, {info.get("symbol") for _, info in token_items})

            now = now_ist()
            sampled = 0
            for token, info in token_items:
                quote = option_quotes.get(token)
                if not quote:
                    continue

                ltp = quote.get("last_price", 0) or 0
                if ltp < MIN_LTP:
                    continue

                symbol = info.get("symbol")
                spot = spot_quotes.get(symbol, {}).get("last_price", 0) or 0
                if spot <= 0:
                    continue

                sampled += 1
                hist = _token_history.setdefault(token, deque(maxlen=MAX_TOKEN_HISTORY))
                hist.append({
                    "ts": now.timestamp(),
                    "ltp": float(ltp),
                    "spot": float(spot),
                    "volume": quote.get("volume", 0) or 0,
                })

                alert = _maybe_build_alert(token, info, open_premiums[int(token)], hist, quote, spot_quotes.get(symbol, {}), now)
                if alert:
                    _push_alert(alert)

            _sampled_contracts = sampled
            _last_scan = now.strftime("%Y-%m-%dT%H:%M:%S")
            _last_error = None
            time.sleep(POLL_INTERVAL_SECS)

        except Exception as exc:
            logger.warning("[Premium Alerts] scanner loop failed: %s", exc, exc_info=True)
            _set_error(str(exc))
            time.sleep(30)


def _fetch_option_quotes(kite, tokens):
    out = {}
    for idx in range(0, len(tokens), 500):
        batch = tokens[idx:idx + 500]
        try:
            raw = kite.quote(batch)
            for key, quote in raw.items():
                try:
                    out[int(key)] = quote
                except Exception:
                    token = quote.get("instrument_token")
                    if token:
                        out[int(token)] = quote
        except Exception as exc:
            logger.warning("[Premium Alerts] option quote batch failed: %s", exc)
        time.sleep(0.05)
    return out


def _fetch_spot_quotes(kite, symbols):
    from oi_spurt_routes import EXCHANGE_MAP

    clean_symbols = sorted({s for s in symbols if s})
    queries = [EXCHANGE_MAP.get(sym, f"NSE:{sym}") for sym in clean_symbols]
    out = {}
    for idx in range(0, len(queries), 500):
        batch = queries[idx:idx + 500]
        try:
            raw = kite.quote(batch)
            for key, quote in raw.items():
                out[key.split(":")[-1]] = quote
        except Exception as exc:
            logger.warning("[Premium Alerts] spot quote batch failed: %s", exc)
        time.sleep(0.05)
    return out


def _maybe_build_alert(token, info, open_prem, hist, quote, spot_quote, now):
    if len(hist) < 3:
        return None

    window = [h for h in hist if now.timestamp() - h["ts"] <= WINDOW_SECS]
    if len(window) < 3:
        return None

    old = window[0]
    latest = window[-1]
    if old["ltp"] <= 0 or old["spot"] <= 0:
        return None

    premium_spike_pct = ((latest["ltp"] - old["ltp"]) / old["ltp"]) * 100
    spot_spike_pct = ((latest["spot"] - old["spot"]) / old["spot"]) * 100
    opt_type = info.get("opt_type")

    if premium_spike_pct < PREMIUM_SPIKE_PCT:
        return None
    if opt_type == "CE" and spot_spike_pct < SPOT_SPIKE_PCT:
        return None
    if opt_type == "PE" and spot_spike_pct > -SPOT_SPIKE_PCT:
        return None

    cooldown_key = f"{token}"
    last_alert = _cooldowns.get(cooldown_key, 0)
    if now.timestamp() - last_alert < COOLDOWN_SECS:
        return None

    pullbacks = 0
    for idx in range(1, len(window)):
        if window[idx]["ltp"] < window[idx - 1]["ltp"]:
            pullbacks += 1
    consistency = round((1 - (pullbacks / max(1, len(window) - 1))) * 100, 1)

    open_prem = float(open_prem)
    board_gain_pct = ((latest["ltp"] - open_prem) / open_prem) * 100 if open_prem > 0 else 0.0
    if board_gain_pct <= 0:
        return None
    interval_vol = max(0, (quote.get("volume", 0) or 0) - (old.get("volume", 0) or 0))
    ohlc = spot_quote.get("ohlc") or {}
    prev_close = ohlc.get("close", 0) or 0
    day_spot_change_pct = ((latest["spot"] - prev_close) / prev_close) * 100 if prev_close > 0 else None

    _cooldowns[cooldown_key] = now.timestamp()
    direction = "BULL" if opt_type == "CE" else "BEAR"
    layer = "opening" if info.get("is_opening") else "running"

    return {
        "time": now.strftime("%H:%M:%S"),
        "date": now.strftime("%Y-%m-%d"),
        "token": token,
        "symbol": info.get("symbol"),
        "tradingsymbol": info.get("tradingsymbol"),
        "opt_type": opt_type,
        "strike": info.get("strike"),
        "label": info.get("label"),
        "layer": layer,
        "direction": direction,
        "open_prem": round(open_prem, 2),
        "old_ltp": round(old["ltp"], 2),
        "ltp": round(latest["ltp"], 2),
        "premium_spike_pct": round(premium_spike_pct, 2),
        "board_gain_pct": round(board_gain_pct, 2),
        "old_spot": round(old["spot"], 2),
        "spot": round(latest["spot"], 2),
        "spot_spike_pct": round(spot_spike_pct, 2),
        "day_spot_change_pct": round(day_spot_change_pct, 2) if day_spot_change_pct is not None else None,
        "interval_volume": int(interval_vol),
        "consistency": consistency,
        "reason": "Tracked board contract: premium velocity confirmed by matching underlying move",
    }


def _push_alert(alert):
    global _seq
    with _lock:
        _seq += 1
        alert["seq"] = _seq
        _alerts.append(alert)

    _save_alerts_to_db([alert])

    logger.info(
        "[Premium Alerts] %s %s %s +%.2f%%, spot %.2f%%",
        alert["symbol"],
        alert["strike"],
        alert["opt_type"],
        alert["premium_spike_pct"],
        alert["spot_spike_pct"],
    )


def _set_error(message):
    global _last_error, _last_scan
    _last_error = message
    _last_scan = now_ist().strftime("%Y-%m-%dT%H:%M:%S")


# ── EOD Snapshot Reconstruction ─────────────────────────────────────────────

def get_eod_snapshot():
    """
    Returns a persisted/reconstructed EOD snapshot of fresh Premium Spike Alerts.
    On a cache miss it starts a background reconstruction and returns None.
    """
    if is_market_hours() or is_premarket():
        return None

    now_val = now_ist()
    now_ts = time.time()
    expected_date = _expected_trading_date()
    expected_date_str = expected_date.strftime("%Y-%m-%d")
    snapshot_path = _snapshot_path(expected_date)

    if (
        _eod_snapshot_cache["result"]
        and (now_ts - _eod_snapshot_cache["ts"]) < _EOD_CACHE_TTL
        and _eod_snapshot_cache["result"].get("trade_date") == expected_date_str
    ):
        return _eod_snapshot_cache["result"]

    if os.path.exists(snapshot_path):
        try:
            with open(snapshot_path, "r") as f:
                saved = json.load(f)
            if saved.get("trade_date") == expected_date_str:
                _eod_snapshot_cache["result"] = saved
                _eod_snapshot_cache["ts"] = now_ts
                return saved
        except Exception as exc:
            logger.warning("[Premium Alerts EOD] Disk load failed: %s", exc)

    is_past_close = (now_val.hour > 15 or (now_val.hour == 15 and now_val.minute >= 40))
    market_is_closed = (expected_date < now_val.date()) or (expected_date == now_val.date() and is_past_close)

    if market_is_closed and not _eod_snapshot_cache["running"]:
        threading.Thread(
            target=_run_eod_snapshot_bg,
            daemon=True,
            name="PremiumAlertsEODSnapshot",
        ).start()
    return None


def is_eod_snapshot_running():
    return _eod_snapshot_cache["running"]


def _run_eod_snapshot_bg():
    _eod_snapshot_cache["running"] = True
    try:
        kite = _get_kite()
        if not kite:
            return

        expected_date = _expected_trading_date()
        start_dt = datetime.combine(expected_date, dt_time(9, 15), tzinfo=IST)
        end_dt = datetime.combine(expected_date, dt_time(15, 30), tzinfo=IST)

        board = _build_eod_board(kite)
        if not board:
            _save_eod_snapshot(expected_date, [])
            return

        spot_token_map = _resolve_spot_tokens(kite, {info["symbol"] for info in board.values()})
        spot_candles = {}
        for idx, (symbol, token) in enumerate(spot_token_map.items()):
            spot_candles[symbol] = _fetch_hist_map(kite, token, start_dt, end_dt)
            if idx % 3 == 0:
                time.sleep(0.35)

        alerts = []
        last_alert_ts = {}
        for idx, (token, info) in enumerate(board.items()):
            symbol = info.get("symbol")
            if symbol not in spot_candles:
                continue
            option_map = _fetch_hist_map(kite, token, start_dt, end_dt)
            if not option_map:
                continue
            alerts.extend(_replay_contract_eod(token, info, option_map, spot_candles[symbol], last_alert_ts))
            if idx % 3 == 0:
                time.sleep(0.35)

        alerts.sort(key=lambda a: (a.get("date", ""), a.get("time", ""), a.get("symbol", "")), reverse=True)
        for seq, alert in enumerate(reversed(alerts), 1):
            alert["seq"] = seq
        alerts = list(reversed(alerts))
        _save_eod_snapshot(expected_date, alerts)
    except Exception as exc:
        logger.warning("[Premium Alerts EOD] Snapshot failed: %s", exc, exc_info=True)
    finally:
        _eod_snapshot_cache["running"] = False


def _build_eod_board(kite):
    """
    Rebuilds the same opening/current ATM +/- 2 OTM universe used by the
    Premium Gainers EOD board, but keeps token/tradingsymbol metadata for replay.
    """
    from db_instruments import get_cached_instruments
    from oi_spurt_routes import EXCHANGE_MAP
    from option_gainers_scanner import _build_atm_otm2_contracts

    nfo_data = get_cached_instruments("NFO")
    indices = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}
    underlying_names = {
        i["name"].upper()
        for i in nfo_data
        if i.get("instrument_type") in ["CE", "PE"]
        and i.get("name")
        and i["name"].upper() not in indices
    }

    queries = [EXCHANGE_MAP.get(sym, f"NSE:{sym}") for sym in underlying_names]
    raw_quotes = {}
    for idx in range(0, len(queries), 500):
        try:
            raw_quotes.update(kite.quote(queries[idx:idx + 500]))
        except Exception as exc:
            logger.warning("[Premium Alerts EOD] Spot batch failed: %s", exc)

    opening_spot = {}
    current_spot = {}
    for exch_sym, quote in raw_quotes.items():
        sym = exch_sym.split(":")[-1]
        open_px = (quote.get("ohlc") or {}).get("open", 0) or 0
        ltp = quote.get("last_price", 0) or 0
        if open_px > 0:
            opening_spot[sym] = open_px
        if ltp > 0:
            current_spot[sym] = ltp

    opening_contracts = _build_atm_otm2_contracts(nfo_data, opening_spot, mode="opening")
    running_contracts = _build_atm_otm2_contracts(nfo_data, current_spot, mode="running")

    # Interpolate intermediate spot levels for trending stocks to capture intermediate breakout strikes
    for sym, open_px in opening_spot.items():
        curr_px = current_spot.get(sym, open_px)
        if open_px > 0 and curr_px > 0 and abs(curr_px - open_px) / open_px >= 0.01:
            for factor in [0.2, 0.4, 0.6, 0.8]:
                mid_px = open_px + (curr_px - open_px) * factor
                mid_contracts = _build_atm_otm2_contracts(nfo_data, {sym: mid_px}, mode="running")
                running_contracts.update(mid_contracts)

    board = {}
    board.update(running_contracts)
    board.update(opening_contracts)
    return board


def _resolve_spot_tokens(kite, symbols):
    from oi_spurt_routes import EXCHANGE_MAP

    queries = [EXCHANGE_MAP.get(sym, f"NSE:{sym}") for sym in sorted(symbols)]
    out = {}
    for idx in range(0, len(queries), 500):
        try:
            raw = kite.quote(queries[idx:idx + 500])
            for exch_sym, quote in raw.items():
                token = quote.get("instrument_token")
                if token:
                    out[exch_sym.split(":")[-1]] = int(token)
        except Exception as exc:
            logger.warning("[Premium Alerts EOD] Spot token batch failed: %s", exc)
    return out


def _fetch_hist_map(kite, token, start_dt, end_dt):
    try:
        candles = kite.historical_data(int(token), start_dt, end_dt, "minute")
    except Exception as exc:
        logger.debug("[Premium Alerts EOD] Historical fetch failed for %s: %s", token, exc)
        return {}

    out = {}
    for candle in candles or []:
        c_dt = candle.get("date")
        if not c_dt:
            continue
        if c_dt.tzinfo is not None:
            c_dt = c_dt.astimezone(IST)
        key = c_dt.strftime("%H:%M")
        out[key] = {
            "dt": c_dt,
            "open": float(candle.get("open", 0) or 0),
            "high": float(candle.get("high", 0) or 0),
            "low": float(candle.get("low", 0) or 0),
            "close": float(candle.get("close", 0) or 0),
            "volume": int(candle.get("volume", 0) or 0),
        }
    return out


def _replay_contract_eod(token, info, option_map, spot_map, last_alert_ts):
    common_keys = sorted(set(option_map) & set(spot_map))
    if len(common_keys) < 3:
        return []

    open_prem = option_map[common_keys[0]]["open"] or option_map[common_keys[0]]["close"]
    if open_prem < MIN_LTP:
        return []

    hist = deque(maxlen=MAX_TOKEN_HISTORY)
    alerts = []
    for key in common_keys:
        opt = option_map[key]
        spot = spot_map[key]
        hist.append({
            "ts": opt["dt"].timestamp(),
            "ltp": opt["close"],
            "spot": spot["close"],
            "volume": opt["volume"],
        })

        alert = _maybe_build_eod_alert(token, info, open_prem, hist, opt, spot, last_alert_ts)
        if alert:
            alerts.append(alert)
    return alerts


def _maybe_build_eod_alert(token, info, open_prem, hist, opt_candle, spot_candle, last_alert_ts):
    window_end = opt_candle["dt"].timestamp()
    window = [h for h in hist if window_end - h["ts"] <= WINDOW_SECS]
    if len(window) < 3:
        return None

    old = window[0]
    latest = window[-1]
    if old["ltp"] <= 0 or old["spot"] <= 0:
        return None

    premium_spike_pct = ((latest["ltp"] - old["ltp"]) / old["ltp"]) * 100
    spot_spike_pct = ((latest["spot"] - old["spot"]) / old["spot"]) * 100
    opt_type = info.get("opt_type")

    if premium_spike_pct < PREMIUM_SPIKE_PCT:
        return None
    if opt_type == "CE" and spot_spike_pct < SPOT_SPIKE_PCT:
        return None
    if opt_type == "PE" and spot_spike_pct > -SPOT_SPIKE_PCT:
        return None

    board_gain_pct = ((latest["ltp"] - open_prem) / open_prem) * 100 if open_prem > 0 else 0
    if board_gain_pct <= 0:
        return None

    cooldown_key = f"{token}"
    if window_end - last_alert_ts.get(cooldown_key, 0) < COOLDOWN_SECS:
        return None
    last_alert_ts[cooldown_key] = window_end

    pullbacks = sum(1 for idx in range(1, len(window)) if window[idx]["ltp"] < window[idx - 1]["ltp"])
    consistency = round((1 - (pullbacks / max(1, len(window) - 1))) * 100, 1)
    layer = "opening" if info.get("is_opening") else "running"

    return {
        "time": opt_candle["dt"].strftime("%H:%M:%S"),
        "date": opt_candle["dt"].strftime("%Y-%m-%d"),
        "token": token,
        "symbol": info.get("symbol"),
        "tradingsymbol": info.get("tradingsymbol"),
        "opt_type": opt_type,
        "strike": info.get("strike"),
        "label": info.get("label"),
        "layer": layer,
        "direction": "BULL" if opt_type == "CE" else "BEAR",
        "open_prem": round(open_prem, 2),
        "old_ltp": round(old["ltp"], 2),
        "ltp": round(latest["ltp"], 2),
        "premium_spike_pct": round(premium_spike_pct, 2),
        "board_gain_pct": round(board_gain_pct, 2),
        "old_spot": round(old["spot"], 2),
        "spot": round(latest["spot"], 2),
        "spot_spike_pct": round(spot_spike_pct, 2),
        "day_spot_change_pct": None,
        "interval_volume": int(max(0, latest["volume"] - old["volume"])),
        "consistency": consistency,
        "reason": "EOD replay: tracked board contract premium velocity matched underlying move",
        "is_eod_snapshot": True,
    }


def _save_eod_snapshot(trade_date, alerts):
    result = {
        "alerts": alerts[:MAX_ALERTS],
        "total_alerts": len(alerts),
        "trade_date": trade_date.strftime("%Y-%m-%d"),
        "reconstructed_at": now_ist().strftime("%Y-%m-%dT%H:%M:%S"),
        "is_eod_snapshot": True,
        "window_secs": WINDOW_SECS,
        "premium_spike_pct": PREMIUM_SPIKE_PCT,
        "spot_spike_pct": SPOT_SPIKE_PCT,
    }
    _eod_snapshot_cache["result"] = result
    _eod_snapshot_cache["ts"] = time.time()

    _save_alerts_to_db(alerts)

    try:
        # GUARD: Never write the EOD snapshot file to disk before 15:30 IST on the target date.
        now_val = now_ist()
        is_past_close = (now_val.hour > 15 or (now_val.hour == 15 and now_val.minute >= 40))
        if trade_date == now_val.date() and not is_past_close:
            logger.info("[Premium Alerts EOD] Skipping saving to disk: target date is today (%s) but it is before 15:40 IST.", trade_date)
            return

        _cleanup_old_snapshots(trade_date)
        with open(_snapshot_path(trade_date), "w") as f:
            json.dump(result, f)
    except Exception as exc:
        logger.warning("[Premium Alerts EOD] Disk save failed: %s", exc)


def _expected_trading_date():
    from option_gainers_scanner import _get_expected_trading_date
    return _get_expected_trading_date(now_ist())


def _snapshot_path(trade_date):
    suffix = trade_date.strftime("%d%m%Y")
    filename = f"premium_alerts_eod_snapshot_{suffix}.json"
    return os.path.join(os.path.dirname(__file__), filename)


def _cleanup_old_snapshots(keep_date):
    import glob

    keep_path = _snapshot_path(keep_date)
    pattern = os.path.join(os.path.dirname(__file__), "premium_alerts_eod_snapshot_*.json")
    for path in glob.glob(pattern):
        if path != keep_path:
            try:
                os.remove(path)
            except Exception:
                pass
