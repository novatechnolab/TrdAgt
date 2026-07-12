#!/usr/bin/env python3
"""
Populate SQLite cache with sample TRENT data for testing cache strategy.
This allows testing replay mode without needing active Kite credentials.
"""

import sqlite3
from datetime import datetime
import sys

DB_PATH = '/home/rajk/Downloads/TradeSignal/tradesignal_cache.db'

# Sample TRENT 5-minute candles for 2026-04-15
# These are realistic values around price 3944
SAMPLE_CANDLES = [
    # (time, open, high, low, close, volume)
    ('2026-04-15T09:15:00', 3935, 3938, 3930, 3933, 1200),
    ('2026-04-15T09:20:00', 3933, 3941, 3932, 3940, 1500),
    ('2026-04-15T09:25:00', 3940, 3944, 3939, 3942, 1800),
    ('2026-04-15T09:30:00', 3942, 3948, 3940, 3945, 1900),
    ('2026-04-15T09:35:00', 3945, 3946, 3943, 3944, 1200),
    ('2026-04-15T09:40:00', 3944, 3948, 3942, 3946, 1600),
    ('2026-04-15T09:45:00', 3946, 3950, 3945, 3948, 1700),
    ('2026-04-15T09:50:00', 3948, 3952, 3946, 3950, 1900),
    ('2026-04-15T09:55:00', 3950, 3955, 3949, 3952, 2000),
    ('2026-04-15T10:00:00', 3952, 3956, 3950, 3954, 2100),
]

# TRENT instrument token (NSE equity)
TRENT_TOKEN = 3465729  # Common token for TRENT

def setup_cache():
    """Initialize database and insert sample data."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Create tables if they don't exist
        cursor.executescript('''
            CREATE TABLE IF NOT EXISTS ohlcv (
                instrument_token INTEGER NOT NULL,
                date TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume INTEGER NOT NULL,
                interval TEXT NOT NULL DEFAULT 'day',
                fetched_at TEXT NOT NULL,
                PRIMARY KEY (instrument_token, date, interval)
            );
        ''')
        
        # Insert sample data
        now = datetime.now().isoformat()
        for date_str, open_, high, low, close, volume in SAMPLE_CANDLES:
            cursor.execute(
                '''INSERT OR REPLACE INTO ohlcv
                   (instrument_token, date, open, high, low, close, volume, interval, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, '5minute', ?)''',
                (TRENT_TOKEN, date_str, open_, high, low, close, volume, now)
            )
        
        conn.commit()
        conn.close()
        
        print(f'✓ Populated cache with {len(SAMPLE_CANDLES)} TRENT candles')
        print(f'  Token: {TRENT_TOKEN}')
        print(f'  Date: 2026-04-15')
        print(f'  Interval: 5minute')
        print(f'  Range: 09:15 - 10:00')
        print(f'')
        print('Now when you test replay mode for TRENT on 2026-04-15 at 09:50,')
        print('the backend should return data_source: "sqlite_cache" (fast).')
        return True
    except Exception as e:
        print(f'❌ Error: {e}', file=sys.stderr)
        return False

if __name__ == '__main__':
    success = setup_cache()
    sys.exit(0 if success else 1)
