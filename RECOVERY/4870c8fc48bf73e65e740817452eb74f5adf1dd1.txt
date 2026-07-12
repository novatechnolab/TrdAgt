# error_handler.py — Degraded Mode Logic + Stale Data Detection + Logging
import logging
import logging.handlers
import os
from datetime import datetime
from collections import deque

LOG_FILE = "scanner_errors.log"
_recent_errors = deque(maxlen=5)


def setup_logging():
    """Configure daily-rotated error log."""
    handler = logging.handlers.TimedRotatingFileHandler(
        LOG_FILE, when="midnight", backupCount=7
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    ))

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)

    # Also log to console
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
    root.addHandler(console)

    return root


def log_error(symbol: str, error_type: str, message: str, scan_cycle: int = 0):
    """Record an error for dashboard display and file logging."""
    entry = {
        "timestamp": datetime.now().isoformat(),
        "symbol": symbol,
        "error_type": error_type,
        "message": message,
        "scan_cycle": scan_cycle,
    }
    _recent_errors.appendleft(entry)
    logging.getLogger("error_handler").error(
        f"[cycle={scan_cycle}] [{symbol}] {error_type}: {message}"
    )


def get_recent_errors() -> list:
    return list(_recent_errors)


def is_data_stale(last_update: datetime, threshold_minutes: int) -> bool:
    if last_update is None:
        return True
    age = (datetime.now() - last_update).total_seconds() / 60
    return age > threshold_minutes


def get_stale_banner(last_scan_time) -> dict:
    from config import STALE_DATA_THRESHOLD_MIN
    if last_scan_time is None:
        return {"stale": True, "message": "No scan data — scanner starting up"}
    if is_data_stale(last_scan_time, STALE_DATA_THRESHOLD_MIN):
        age = int((datetime.now() - last_scan_time).total_seconds() / 60)
        return {"stale": True, "message": f"DATA STALE — last scan {age}m ago"}
    return {"stale": False, "message": ""}


def classify_scan_health(payload: dict) -> dict:
    """Return overall health status of the current scan."""
    warnings = []

    if payload.get("oi_degraded"):
        warnings.append("OI DATA DEGRADED — grades based on price/volume only")

    if payload.get("scan_error") == "SCAN_FAILED":
        warnings.append("SCAN FAILED — showing previous results")

    dur = payload.get("scan_duration_sec")
    if dur and dur > 240:
        warnings.append(f"SCAN DELAYED ({dur:.0f}s)")

    cal = payload.get("calendar_status", {})
    if cal.get("status") == "NOT_LOADED":
        warnings.append("EVENT DATA UNAVAILABLE")

    sym_status = {}  # Would check freshness from sector_map
    
    return {
        "ok": len(warnings) == 0,
        "warnings": warnings,
    }
