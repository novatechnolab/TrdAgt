"""
Shared pytest fixtures for TradeSignal backend tests.
"""
import json, os, pytest

GOLDEN_FILE = os.path.join(
    os.path.dirname(__file__), '../../tests/regression/golden/golden.json'
)

@pytest.fixture(scope='session')
def golden_data():
    """Load golden outputs captured from JS validator."""
    if not os.path.exists(GOLDEN_FILE):
        return {}
    with open(GOLDEN_FILE) as f:
        return json.load(f)

@pytest.fixture(scope='session')
def sample_ohlcv_uptrend():
    """30-candle deterministic uptrend (mirrors S1 fixture)."""
    candles = []
    steps = [+10,+8,-2,+12,+7,-3,+9,+11,-1,+13,
             +6,-2,+10,+8,-3,+11,+5,-2,+9,+7,
             +12,-2,+8,-4,-3,-2,+6,+8,+5,+7]
    base = 2800.0
    for i, step in enumerate(steps):
        mins = 9*60+15 + i*5
        h, m = divmod(mins, 60)
        o = round(base, 2)
        c = round(base + step, 2)
        candles.append({
            'date':   f'2026-04-15T{h:02d}:{m:02d}:00+05:30',
            'open':   o,
            'high':   round(max(o, c)+1.5, 2),
            'low':    round(min(o, c)-1.5, 2),
            'close':  c,
            'volume': 400000 + i*10000,
        })
        base = c
    return candles

@pytest.fixture(scope='session')
def sample_ohlcv_downtrend():
    """30-candle deterministic downtrend."""
    candles = []
    steps = [-10,-8,+2,-12,-7,+3,-9,-11,+1,-13,
             -6,+2,-10,-8,+3,-11,-5,+2,-9,-7,
             -12,+2,-8,+4,+3,+2,-6,-8,-5,-7]
    base = 1150.0
    for i, step in enumerate(steps):
        mins = 9*60+15 + i*5
        h, m = divmod(mins, 60)
        o = round(base, 2)
        c = round(base + step, 2)
        candles.append({
            'date':   f'2026-04-15T{h:02d}:{m:02d}:00+05:30',
            'open':   o,
            'high':   round(max(o, c)+1.5, 2),
            'low':    round(min(o, c)-1.5, 2),
            'close':  c,
            'volume': 320000 + i*8000,
        })
        base = c
    return candles
