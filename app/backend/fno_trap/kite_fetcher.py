"""
fno_trap/kite_fetcher.py
Kite REST data layer — reads credentials from .kite_session.json (written by server.py).
Supports: NFO (NSE stocks + indices), BFO (SENSEX, BANKEX).
"""
import logging
import os
import json
from datetime import date
from typing import Optional

from fno_trap.db import get_connection
from fno_trap.time_phase import now_ist

log = logging.getLogger(__name__)

# ── Kite session — read from disk token file (written by server.py on auth) ──
_HERE = os.path.dirname(os.path.abspath(__file__))
_SESSION_FILE = os.path.join(_HERE, '..', '.kite_session.json')

_kite_instance = None


def get_kite():
    """Return a live KiteConnect instance using credentials from .kite_session.json."""
    global _kite_instance
    try:
        session_path = os.path.abspath(_SESSION_FILE)
        if not os.path.isfile(session_path):
            log.debug("FNO Trap: no .kite_session.json at %s", session_path)
            return None
        with open(session_path) as f:
            data = json.load(f)
        api_key      = data.get('api_key', '').strip()
        access_token = data.get('access_token', '').strip()
        if not api_key or not access_token:
            return None
        if (_kite_instance is not None
                and getattr(_kite_instance, 'access_token', None) == access_token):
            return _kite_instance
        from kiteconnect import KiteConnect
        _kite_instance = KiteConnect(api_key=api_key)
        _kite_instance.set_access_token(access_token)
        log.info("FNO Trap: KiteConnect initialised from disk session")
        return _kite_instance
    except Exception as e:
        log.warning("FNO Trap: get_kite() error: %s", e)
        return None


# ── Exchange routing ──────────────────────────────────────────────────────

# BSE indices trade options on BFO; everything else on NFO
_BSE_SYMBOLS = {"SENSEX", "BANKEX"}

# Spot price tokens — what to pass to kite.ltp()
INDEX_TOKENS = {
    # NSE indices
    "NIFTY":      "NSE:NIFTY 50",
    "BANKNIFTY":  "NSE:NIFTY BANK",
    "FINNIFTY":   "NSE:NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NSE:NIFTY MID SELECT",
    # BSE indices
    "SENSEX":     "BSE:SENSEX",
    "BANKEX":     "BSE:BANKEX",
}


def _exchange_for(symbol: str) -> str:
    """Return the F&O exchange for a symbol: BFO for BSE indices, NFO for everything else."""
    return "BFO" if symbol.upper() in _BSE_SYMBOLS else "NFO"


def _spot_token(symbol: str) -> str:
    """Return the LTP token string for kite.ltp()."""
    return INDEX_TOKENS.get(symbol.upper(), f"NSE:{symbol.upper()}")


# ── Instrument cache — keyed by exchange ─────────────────────────────────
_instruments_cache: dict = {}


def get_instruments(exchange: str = "NFO") -> list:
    import sqlite3
    from datetime import datetime
    db_path = os.path.join(_HERE, "..", "tradesignal_cache.db")
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT * FROM instruments WHERE exchange = ?", (exchange,)
        )
        rows = []
        for r in cursor.fetchall():
            d = dict(r)
            if "expiry" in d and d["expiry"]:
                if isinstance(d["expiry"], str):
                    try:
                        d["expiry"] = datetime.strptime(d["expiry"][:10], "%Y-%m-%d").date()
                    except Exception:
                        pass
            rows.append(d)
        conn.close()
        return rows
    except Exception as e:
        log.error("FNO Trap: get_instruments(%s) DB read failed: %s", exchange, e)
        return []


def get_option_chain_instruments(symbol: str, expiry: date) -> list:
    exchange = _exchange_for(symbol)
    instruments = get_instruments(exchange)
    return [
        inst for inst in instruments
        if inst.get("name") == symbol.upper()
        and inst.get("expiry") == expiry
        and inst.get("instrument_type") in ("CE", "PE")
    ]


def _chunked(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


# ── Spot ──────────────────────────────────────────────────────────────────

def fetch_spot(symbol: str) -> Optional[float]:
    kite = get_kite()
    if not kite:
        return None
    token = _spot_token(symbol)
    try:
        q = kite.ltp([token])
        ltp = q.get(token, {}).get("last_price")
        if ltp:
            _persist_spot(symbol, ltp)
        return ltp
    except Exception as e:
        log.error("FNO Trap: spot fetch error for %s: %s", symbol, e)
        return None


def _persist_spot(symbol: str, ltp: float):
    try:
        conn = get_connection()
        conn.execute(
            "INSERT INTO spot_tick(symbol, ltp, tick_time) VALUES(?,?,?)",
            (symbol, ltp, now_ist().isoformat())
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def get_latest_spot(symbol: str) -> Optional[float]:
    conn = get_connection()
    row = conn.execute(
        "SELECT ltp FROM spot_tick WHERE symbol=? ORDER BY tick_time DESC LIMIT 1",
        (symbol,)
    ).fetchone()
    conn.close()
    return row["ltp"] if row else None


# ── OI snapshot ───────────────────────────────────────────────────────────

def fetch_oi_snapshot(symbol: str, expiry: date) -> list:
    kite = get_kite()
    if not kite:
        return []
    exchange = _exchange_for(symbol)
    chain = get_option_chain_instruments(symbol, expiry)
    if not chain:
        log.warning("FNO Trap: no instruments for %s %s on %s", symbol, expiry, exchange)
        return []

    trading_symbols = [inst["tradingsymbol"] for inst in chain]
    quotes = {}
    for chunk in _chunked(trading_symbols, 400):
        try:
            quotes.update(kite.quote([f"{exchange}:" + s for s in chunk]))
        except Exception as e:
            log.error("FNO Trap: OI quote fetch error: %s", e)
            return []

    rows = []
    snap_time = now_ist().isoformat()
    for inst in chain:
        ts = inst["tradingsymbol"]
        q = quotes.get(f"{exchange}:{ts}", {})
        rows.append({
            "symbol":        symbol,
            "expiry":        expiry.isoformat(),
            "strike":        inst["strike"],
            "option_type":   inst["instrument_type"],
            "oi":            q.get("oi", 0) or 0,
            "oi_change":     (q.get("oi_day_high", 0) or 0) - (q.get("oi_day_low", 0) or 0),
            "volume":        q.get("volume", 0) or 0,
            "ltp":           q.get("last_price", 0) or 0,
            "iv":            None,
            "bid":           (q.get("depth", {}).get("buy", [{}]) or [{}])[0].get("price"),
            "ask":           (q.get("depth", {}).get("sell", [{}]) or [{}])[0].get("price"),
            "snapshot_time": snap_time,
        })

    _persist_oi(rows)
    return rows


def _persist_oi(rows: list):
    if not rows:
        return
    try:
        conn = get_connection()
        conn.executemany("""
            INSERT INTO oi_snapshots
            (symbol,expiry,strike,option_type,oi,oi_change,volume,ltp,iv,bid,ask,snapshot_time)
            VALUES(:symbol,:expiry,:strike,:option_type,:oi,:oi_change,:volume,:ltp,:iv,:bid,:ask,:snapshot_time)
        """, rows)
        conn.commit()
        conn.close()
    except Exception as e:
        log.error("FNO Trap: persist_oi error: %s", e)


# ── Futures ───────────────────────────────────────────────────────────────

def fetch_futures_tick(symbol: str, expiry: date) -> Optional[dict]:
    kite = get_kite()
    if not kite:
        return None
    exchange = _exchange_for(symbol)
    instruments = get_instruments(exchange)
    fut = next(
        (i for i in instruments
         if i.get("name") == symbol.upper()
         and i.get("instrument_type") == "FUT"
         and i.get("expiry") == expiry),
        None
    )
    if not fut:
        return None
    ts = fut["tradingsymbol"]
    try:
        q = kite.quote([f"{exchange}:{ts}"])
        data = q.get(f"{exchange}:{ts}", {})
        spot = get_latest_spot(symbol) or data.get("last_price", 0)
        fut_ltp = data.get("last_price", 0)
        basis_pct = ((fut_ltp - spot) / spot * 100) if spot else None
        row = {
            "symbol":    symbol,
            "expiry":    expiry.isoformat(),
            "ltp":       fut_ltp,
            "spot":      spot,
            "basis_pct": basis_pct,
            "oi":        data.get("oi", 0),
            "oi_change": (data.get("oi_day_high", 0) or 0) - (data.get("oi_day_low", 0) or 0),
            "volume":    data.get("volume", 0),
            "tick_time": now_ist().isoformat(),
        }
        conn = get_connection()
        conn.execute("""
            INSERT INTO futures_tick(symbol,expiry,ltp,spot,basis_pct,oi,oi_change,volume,tick_time)
            VALUES(:symbol,:expiry,:ltp,:spot,:basis_pct,:oi,:oi_change,:volume,:tick_time)
        """, row)
        conn.commit()
        conn.close()
        return row
    except Exception as e:
        log.error("FNO Trap: futures fetch for %s: %s", symbol, e)
        return None


# ── Expiry helpers ────────────────────────────────────────────────────────

def get_near_expiry(symbol: str) -> Optional[date]:
    """Get the nearest upcoming expiry from the correct exchange."""
    exchange = _exchange_for(symbol)
    instruments = get_instruments(exchange)
    today = now_ist().date()
    expiries = sorted(set(
        inst["expiry"] for inst in instruments
        if inst.get("name") == symbol.upper()
        and inst.get("instrument_type") in ("CE", "PE")
        and inst.get("expiry")
        and inst["expiry"] >= today
    ))
    return expiries[0] if expiries else None


def get_all_expiries(symbol: str) -> list:
    """Return all upcoming expiries for a symbol (weekly + monthly)."""
    exchange = _exchange_for(symbol)
    instruments = get_instruments(exchange)
    today = now_ist().date()
    expiries = sorted(set(
        inst["expiry"] for inst in instruments
        if inst.get("name") == symbol.upper()
        and inst.get("instrument_type") in ("CE", "PE")
        and inst.get("expiry")
        and inst["expiry"] >= today
    ))
    return expiries


# ── F&O universe search ───────────────────────────────────────────────────

def search_fno_universe(query: str, limit: int = 20) -> list:
    """
    Search all F&O symbols across NFO + BFO that match query.
    Returns list of {symbol, exchange, lot_size, type, segment}.
    Prioritises exact prefix matches, then partial.
    Works from instrument cache — no extra Kite call.
    """
    q = query.upper().strip()
    if not q:
        return []

    results = {}   # symbol → dict, deduplicated

    for exchange in ("NFO", "BFO"):
        instruments = get_instruments(exchange)
        for inst in instruments:
            name = (inst.get("name") or "").upper()
            if not name:
                continue
            # Only include if F&O (has options)
            if inst.get("instrument_type") not in ("CE", "PE", "FUT"):
                continue
            # Match query against symbol name
            if q not in name:
                continue
            if name in results:
                continue

            # Determine type label
            seg = inst.get("segment", "")
            if name in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"):
                sym_type = "Index"
            else:
                sym_type = "Stock"

            # Get lot size from a FUT or option instrument
            lot_size = inst.get("lot_size") or 0

            results[name] = {
                "symbol":   name,
                "exchange": exchange,
                "lot_size": lot_size,
                "type":     sym_type,
                "segment":  seg,
            }

    # Sort: exact match first, then prefix, then partial
    def rank(item):
        s = item["symbol"]
        if s == q:
            return 0
        if s.startswith(q):
            return 1
        return 2

    sorted_results = sorted(results.values(), key=rank)
    return sorted_results[:limit]


# ── Lot size lookup ───────────────────────────────────────────────────────

def get_lot_size(symbol: str) -> int:
    """Return lot size for symbol from instrument list."""
    exchange = _exchange_for(symbol)
    instruments = get_instruments(exchange)
    for inst in instruments:
        if inst.get("name") == symbol.upper() and inst.get("lot_size"):
            return inst["lot_size"]
    # Fallback defaults
    defaults = {"NIFTY": 50, "BANKNIFTY": 15, "FINNIFTY": 40, "MIDCPNIFTY": 75,
                "SENSEX": 10, "BANKEX": 15}
    return defaults.get(symbol.upper(), 50)
