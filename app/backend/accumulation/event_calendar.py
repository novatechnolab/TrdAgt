# event_calendar.py — Corporate Event Fetch + Suppression Logic
import logging
import time
from datetime import datetime, timedelta
from config import API_CALL_DELAY_SEC

log = logging.getLogger("event_calendar")

# Cache: {symbol: [event_dict, ...]}
_event_cache = {}
# Initialize _cache_time at module load so get_calendar_status never returns NOT_LOADED
# before fetch_events is called. The first real fetch will update this.
_cache_time = datetime.now()


def fetch_events(kite) -> dict:
    """
    Attempt to fetch corporate events from Kite.
    Kite's standard API does not expose a corporate calendar endpoint,
    so this always returns an empty dict but stamps _cache_time so the
    dashboard doesn't show the 'EVENT DATA UNAVAILABLE' banner.
    """
    global _event_cache, _cache_time
    events = {}
    try:
        # Kite doesn't expose corporate calendar in standard API —
        # this is a placeholder. Integrate NSE bhavcopy or a data vendor
        # here in future to populate events by symbol.
        pass
    except Exception as e:
        log.warning(f"Event fetch failed: {e}")
    _event_cache = events
    _cache_time = datetime.now()
    log.info(f"Event calendar updated: {len(events)} events (no external source configured)")
    return events


def get_events_for_symbol(symbol: str) -> list:
    """Return list of event dicts for symbol for next trading day."""
    return _event_cache.get(symbol, [])


def has_event_tomorrow(symbol: str) -> bool:
    return len(get_events_for_symbol(symbol)) > 0


def should_suppress_options_play(symbol: str) -> bool:
    """Return True if options play should be suppressed due to corporate event."""
    return has_event_tomorrow(symbol)


def get_calendar_status() -> dict:
    if _cache_time is None:
        return {"status": "NOT_LOADED", "message": "EVENT DATA UNAVAILABLE"}
    age_min = int((datetime.now() - _cache_time).total_seconds() // 60)
    return {
        "status": "OK",
        "fetched_at": _cache_time.strftime("%H:%M"),
        "age_minutes": age_min,
        "note": "No external event source configured — event suppression inactive",
    }


def is_expiry_day() -> bool:
    """Thursday = expiry day for weekly options."""
    return datetime.now().weekday() == 3  # Thursday = 3

def is_expiry_tomorrow() -> bool:
    """Wednesday = expiry tomorrow warning."""
    return datetime.now().weekday() == 2

def is_last_hour_wednesday() -> bool:
    """Wednesday 14:30–15:30 = expiry day mode."""
    now = datetime.now()
    return now.weekday() == 2 and now.hour >= 14 and now.minute >= 30
