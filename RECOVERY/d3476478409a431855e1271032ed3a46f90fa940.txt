"""
fno_trap/time_phase.py
§3.9 Time Phase Engine — IST market session classification.
Drives BLOCKED conditions, phase multipliers, WAIT timeouts.
"""
from datetime import datetime, time, timedelta

try:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")
    def now_ist():
        return datetime.now(IST)
except ImportError:
    def now_ist():
        return datetime.now(datetime.timezone(timedelta(hours=5, minutes=30)))

PHASES = [
    ("SETTLEMENT_EARLY", time(9, 0),   time(9, 14)),
    ("SETTLEMENT_LATE",  time(9, 15),  time(9, 59)),
    ("ACTIVE",           time(10, 0),  time(11, 29)),
    ("DEAD_ZONE",        time(11, 30), time(13, 29)),
    ("POWER_HOUR",       time(13, 30), time(15, 14)),
    ("CLOSE_RISK",       time(15, 15), time(15, 30)),
]

PHASE_MULTIPLIER = {
    "SETTLEMENT_EARLY": 0.0,
    "SETTLEMENT_LATE":  0.80,
    "ACTIVE":           1.0,
    "DEAD_ZONE":        0.75,
    "POWER_HOUR":       1.10,
    "CLOSE_RISK":       0.0,
}


def get_time_phase(dt=None):
    if dt is None:
        dt = now_ist()
    wd = dt.weekday()
    if wd >= 5:
        return "MARKET_CLOSED"
    t = dt.time()
    for phase, start, end in PHASES:
        if start <= t <= end:
            return phase
    return "MARKET_CLOSED"


def is_market_open(dt=None):
    return get_time_phase(dt) != "MARKET_CLOSED"


def get_phase_multiplier(phase):
    return PHASE_MULTIPLIER.get(phase, 0.0)


def trading_days_to_expiry(expiry_date, ref_date=None):
    from datetime import timedelta, date
    if ref_date is None:
        ref_date = now_ist().date()
    if isinstance(expiry_date, str):
        expiry_date = date.fromisoformat(expiry_date)
    count = 0
    d = ref_date
    while d <= expiry_date:
        if d.weekday() < 5:
            count += 1
        d += timedelta(days=1)
    return count
