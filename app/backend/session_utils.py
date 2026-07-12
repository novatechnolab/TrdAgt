"""
session_utils.py — IST-aware session helpers for TradeSignal backend.

Single source of truth for:
  - IST timezone constant
  - now_ist() — always returns current time in IST
  - Session mode detection: is_market_hours(), is_premarket(), is_live_session()
  - Session mode label: get_session_mode() → 'live' | 'premarket' | 'historical'

ALL business logic files and server.py should use these helpers
instead of datetime.now() (which is system-local / UTC on cloud servers).
"""
from datetime import datetime, timezone, timedelta, date

# Official NSE Holidays for 2025–2026
NSE_HOLIDAYS = {
    date(2025, 1, 14), date(2025, 2, 19), date(2025, 3, 14), date(2025, 3, 31),
    date(2025, 4, 10), date(2025, 4, 14), date(2025, 4, 18), date(2025, 5, 1),
    date(2025, 8, 15), date(2025, 8, 27), date(2025, 10, 2), date(2025, 10, 20),
    date(2025, 10, 24), date(2025, 11, 5), date(2025, 12, 25),
    # 2026 Trading Holidays
    date(2026, 1, 26),   # Republic Day
    date(2026, 3, 3),    # Holi
    date(2026, 3, 26),   # Shri Ram Navami
    date(2026, 3, 31),   # Shri Mahavir Jayanti
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 5, 28),   # Bakri Id
    date(2026, 6, 26),   # Muharram (Today)
    date(2026, 9, 14),   # Ganesh Chaturthi
    date(2026, 10, 2),   # Mahatma Gandhi Jayanti
    date(2026, 10, 20),  # Dussehra
    date(2026, 11, 10),  # Diwali-Balipratipada
    date(2026, 11, 24),  # Guru Nanak Jayanti
    date(2026, 12, 25),  # Christmas
}

# ── IST Timezone ─────────────────────────────────────────────────────────────
IST = timezone(timedelta(hours=5, minutes=30))

# ── NSE Market Hours (IST) ────────────────────────────────────────────────────
_MARKET_OPEN_H,  _MARKET_OPEN_M  =  9, 15
_MARKET_CLOSE_H, _MARKET_CLOSE_M = 15, 30
_PREMARKET_START_H, _PREMARKET_START_M =  9,  0   # Pre-open starts 9:00 AM
_LATE_ENTRY_H,   _LATE_ENTRY_M   = 14, 45          # No new entries after 2:45 PM


def now_ist() -> datetime:
    """Current datetime in IST. Always use this instead of datetime.now()."""
    return datetime.now(tz=IST)


def now_ist_str(fmt: str = '%Y-%m-%d %H:%M:%S') -> str:
    """Current IST datetime as formatted string."""
    return now_ist().strftime(fmt)


def today_ist() -> str:
    """Today's date in IST as 'YYYY-MM-DD'."""
    return now_ist().strftime('%Y-%m-%d')


def is_market_hours() -> bool:
    """True during live NSE session: 9:15 AM – 3:30 PM IST, Mon–Fri (excluding holidays)."""
    n = now_ist()
    if n.weekday() >= 5 or n.date() in NSE_HOLIDAYS:      # Saturday/Sunday or Holiday
        return False
    minutes = n.hour * 60 + n.minute
    return (_MARKET_OPEN_H * 60 + _MARKET_OPEN_M
            <= minutes <=
            _MARKET_CLOSE_H * 60 + _MARKET_CLOSE_M)


def is_premarket() -> bool:
    """True during pre-open: 9:00 AM – 9:15 AM IST (excluding holidays)."""
    n = now_ist()
    if n.weekday() >= 5 or n.date() in NSE_HOLIDAYS:      # Saturday/Sunday or Holiday
        return False
    minutes = n.hour * 60 + n.minute
    return (_PREMARKET_START_H * 60 + _PREMARKET_START_M
            <= minutes <
            _MARKET_OPEN_H * 60 + _MARKET_OPEN_M)


def is_late_session() -> bool:
    """True after 2:45 PM IST (excluding holidays)."""
    n = now_ist()
    if n.weekday() >= 5 or n.date() in NSE_HOLIDAYS:      # Saturday/Sunday or Holiday
        return False
    return n.hour * 60 + n.minute >= _LATE_ENTRY_H * 60 + _LATE_ENTRY_M


def get_session_mode() -> str:
    """Classify current moment into one of three modes.

    Returns:
        'live'       — active market session (9:15–15:30 IST, weekday)
        'premarket'  — pre-open window (9:00–9:15 IST, weekday)
        'historical' — outside market hours (evening, weekend, holiday)
    """
    if is_market_hours():
        return 'live'
    if is_premarket():
        return 'premarket'
    return 'historical'


def ist_timestamp() -> str:
    """ISO-8601 timestamp with IST offset (+05:30). Use for all API responses."""
    return now_ist().isoformat()


def to_ist(dt_obj: datetime) -> datetime:
    """Convert any tz-aware datetime to IST. Naive datetimes assumed IST."""
    if dt_obj.tzinfo is None:
        return dt_obj.replace(tzinfo=IST)
    return dt_obj.astimezone(IST)
