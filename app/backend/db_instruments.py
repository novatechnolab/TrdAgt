import sqlite3
import os
import logging
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tradesignal_cache.db')
log = logging.getLogger(__name__)

def _parse_instrument_row(row):
    """Helper to convert database row to dict and parse 'expiry' string to datetime.date object."""
    d = dict(row)
    if "expiry" in d and d["expiry"]:
        if isinstance(d["expiry"], str):
            try:
                # Handle both YYYY-MM-DD and YYYY-MM-DD HH:MM:SS formats
                d["expiry"] = datetime.strptime(d["expiry"][:10], "%Y-%m-%d").date()
            except Exception:
                pass
    return d

def get_cached_instruments(exchange="NFO"):
    """Fetch cached instruments from SQLite database for the specified exchange."""
    if not os.path.exists(DB_PATH):
        log.warning(f"[DB Instruments] Database file not found at {DB_PATH}. Returning empty list.")
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT * FROM instruments WHERE exchange = ?", (exchange,)
        )
        rows = cursor.fetchall()
        conn.close()
        return [_parse_instrument_row(r) for r in rows]
    except Exception as e:
        log.error(f"[DB Instruments] Error loading instruments for {exchange}: {e}")
        return []

def get_instrument_by_symbol(symbol, exchange="NFO"):
    """Fetch a single instrument by tradingsymbol and exchange."""
    if not os.path.exists(DB_PATH):
        return None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT * FROM instruments WHERE tradingsymbol = ? AND exchange = ? LIMIT 1",
            (symbol, exchange)
        )
        row = cursor.fetchone()
        conn.close()
        return _parse_instrument_row(row) if row else None
    except Exception as e:
        log.error(f"[DB Instruments] Error loading symbol {symbol} ({exchange}): {e}")
        return None


def get_fno_symbols():
    """Fetch list of all F&O stock underlying names (from NFO futures)."""
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.execute(
            "SELECT DISTINCT name FROM instruments WHERE exchange = 'NFO' AND segment = 'NFO-FUT'"
        )
        names = [r[0].upper() for r in cursor.fetchall() if r[0]]
        conn.close()
        # Exclude common index names if only looking for stocks, but let's keep them and filter when needed
        return names
    except Exception as e:
        log.error(f"[DB Instruments] Error loading F&O symbols: {e}")
        return []


def get_cash_only_symbols():
    """Fetch list of all Cash-only (non-F&O) stock trading symbols from NSE."""
    if not os.path.exists(DB_PATH):
        return []
    try:
        fno_names = set(get_fno_symbols())
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.execute(
            "SELECT DISTINCT tradingsymbol FROM instruments WHERE exchange = 'NSE' AND instrument_type = 'EQ'"
        )
        symbols = []
        for r in cursor.fetchall():
            sym = r[0].upper()
            if sym in fno_names:
                continue
            if any(sym.endswith(suffix) for suffix in ["-BE", "-BZ", "-ST", "-TF", "-DE", "-SG"]):
                continue
            symbols.append(sym)
        conn.close()
        return sorted(symbols)
    except Exception as e:
        log.error(f"[DB Instruments] Error loading cash symbols: {e}")
        return []
