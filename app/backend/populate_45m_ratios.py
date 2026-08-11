"""
================================================================================
 populate_45m_ratios.py  —  First-45-Min Volume Ratio Pre-computation
================================================================================
Batch-safe script that populates `first_45m_ratios` for all F&O stocks.

 Kite API safety:
   - Processes stocks in BATCH_SIZE groups (default 10)
   - 0.6 s delay between every API call (Zerodha rate limit = 3 req/s)
   - 5 s pause after every batch
   - Exponential back-off on 429 / NetworkError (up to 3 retries)
   - already_populated() check skips stocks already in DB — safe to re-run

 Ratio logic: exact mirror of fh_cumulative_ratio in _compute_fh_spurt():
   slots       = 09:15, 09:20, …, 09:55  (9 × 5m = 45 min)
   running_avg      = sum(actual[s]) / len(completed_slots)
   running_baseline = sum(baseline[s]) / len(completed_slots)
   ratio            = running_avg / running_baseline

 Run modes:
   python populate_45m_ratios.py            # full: last 90 days (one-time)
   python populate_45m_ratios.py --daily    # incremental: last 2 days
================================================================================
"""

import os, sys, time, sqlite3, json, logging
from datetime import datetime, timedelta, date as dt_date

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BACKEND_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(BACKEND_DIR, '../../.env'))
load_dotenv(os.path.join(BACKEND_DIR, '../.env'))
load_dotenv(os.path.join(BACKEND_DIR, '.env'))

DB_PATH = os.path.join(BACKEND_DIR, 'tradesignal_cache.db')

logging.basicConfig(level=logging.INFO, format='%(asctime)s  %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────
SLOTS_45M        = frozenset(["09:15","09:20","09:25","09:30","09:35","09:40","09:45","09:50","09:55"])
SLOTS_45M_SORTED = sorted(SLOTS_45M)
BATCH_SIZE       = 10          # stocks per batch
INTER_CALL_SLEEP = 0.65        # seconds between every Kite API call
INTER_BATCH_SLEEP = 5.0        # seconds pause after each batch
MAX_RETRIES      = 3           # retries on 429 / network error
RETRY_BASE_SLEEP = 10.0        # base seconds for exponential back-off


# ── DB helpers ─────────────────────────────────────────────────────────────────

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS first_45m_ratios (
            instrument_token INTEGER NOT NULL,
            trade_date       TEXT    NOT NULL,
            ratio            REAL,
            PRIMARY KEY (instrument_token, trade_date)
        )
    """)
    conn.commit()
    log.info("[DB] first_45m_ratios table ready.")

def get_fno_stocks(conn):
    rows = conn.execute("""
        SELECT DISTINCT i.tradingsymbol, i.instrument_token
        FROM instruments i
        WHERE i.segment = 'NSE' AND i.instrument_type = 'EQ'
        AND EXISTS (SELECT 1 FROM instruments f
                    WHERE f.segment = 'NFO-FUT' AND f.name = i.tradingsymbol)
        AND EXISTS (SELECT 1 FROM allday_baselines b
                    WHERE b.symbol = i.tradingsymbol AND b.timeframe = '5m')
        ORDER BY i.tradingsymbol
    """).fetchall()
    return [(r['tradingsymbol'], r['instrument_token']) for r in rows]

def get_baseline_5m(conn, symbol):
    row = conn.execute(
        "SELECT baselines FROM allday_baselines WHERE symbol=? AND timeframe='5m' LIMIT 1",
        (symbol,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row['baselines'])
    except Exception:
        return None

def already_populated(conn, token, from_date_str, to_date_str):
    """True if we have at least 5 ratio rows for this token in the date range."""
    count = conn.execute(
        "SELECT COUNT(*) FROM first_45m_ratios WHERE instrument_token=? AND trade_date BETWEEN ? AND ?",
        (token, from_date_str, to_date_str)).fetchone()[0]
    return count >= 5

def store_ratios(conn, rows):
    conn.executemany(
        "INSERT OR REPLACE INTO first_45m_ratios (instrument_token, trade_date, ratio) VALUES (?, ?, ?)",
        rows)
    conn.commit()


# ── Core computation ───────────────────────────────────────────────────────────

def compute_45m_ratios(hist_5m, baselines):
    """
    Exact mirror of fh_cumulative_ratio (ema_crossover_scanner.py L193-196)
    applied to every historical day in hist_5m.
    """
    by_date = {}
    for c in hist_5m:
        dt_val = c.get('date')
        if hasattr(dt_val, 'strftime'):
            d_str, t_str = dt_val.strftime('%Y-%m-%d'), dt_val.strftime('%H:%M')
        elif isinstance(dt_val, str):
            d_str, t_str = dt_val[:10], dt_val[11:16]
        else:
            continue
        if t_str not in SLOTS_45M:
            continue
        by_date.setdefault(d_str, {})[t_str] = c.get('volume', 0) or 0

    ratios = {}
    for d_str, day_slots in by_date.items():
        completed = [s for s in SLOTS_45M_SORTED if s in day_slots]
        if not completed:
            continue
        running_avg      = sum(day_slots[s] for s in completed) / len(completed)
        running_baseline = sum(baselines.get(s, 0) for s in completed) / len(completed)
        if running_baseline > 0:
            ratios[d_str] = round(running_avg / running_baseline, 1)
    return ratios


# ── Kite fetch with retry ──────────────────────────────────────────────────────

def kite_fetch_5m(kite, token, from_dt, to_dt):
    """
    Fetch 5m historical data with exponential back-off on rate-limit errors.
    Returns candle list or None on failure.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            time.sleep(INTER_CALL_SLEEP)
            data = kite.historical_data(int(token), from_dt, to_dt, '5minute')
            return data
        except Exception as e:
            err_str = str(e).lower()
            is_rate_limit = '429' in err_str or 'too many' in err_str or 'rate' in err_str
            is_network    = 'network' in err_str or 'timeout' in err_str or 'connection' in err_str
            if (is_rate_limit or is_network) and attempt < MAX_RETRIES:
                sleep_sec = RETRY_BASE_SLEEP * (2 ** (attempt - 1))
                log.warning(f"    [Retry {attempt}/{MAX_RETRIES}] Rate/Network error — sleeping {sleep_sec}s: {e}")
                time.sleep(sleep_sec)
            else:
                log.error(f"    [Failed] Kite error after {attempt} attempt(s): {e}")
                return None
    return None


# ── Kite session ───────────────────────────────────────────────────────────────

def get_kite():
    try:
        from kiteconnect import KiteConnect
        session_file = os.path.join(BACKEND_DIR, '.kite_session.json')
        if not os.path.exists(session_file):
            log.error("No .kite_session.json. Start server.py first.")
            return None
        with open(session_file) as f:
            sess = json.load(f)
        api_key      = os.environ.get('KITE_API_KEY', sess.get('api_key', ''))
        access_token = sess.get('access_token', '')
        if not api_key or not access_token:
            log.error("Missing api_key or access_token.")
            return None
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token)
        log.info("[Kite] Session loaded.")
        return kite
    except Exception as e:
        log.error(f"[Kite] Load failed: {e}")
        return None


# ── Main ───────────────────────────────────────────────────────────────────────

def run(daily_mode=False):
    conn = get_conn()
    init_table(conn)

    kite = get_kite()
    if not kite:
        conn.close()
        return

    stocks = get_fno_stocks(conn)
    log.info(f"[Stocks] F&O universe with 5m baseline: {len(stocks)} symbols")

    today         = dt_date.today()
    from_dt       = today - timedelta(days=2 if daily_mode else 90)
    to_dt         = today
    from_date_str = from_dt.strftime('%Y-%m-%d')
    to_date_str   = to_dt.strftime('%Y-%m-%d')
    mode_label    = "DAILY (last 2 days)" if daily_mode else "FULL (last 90 days)"

    log.info(f"[Mode] {mode_label}  |  {from_date_str} -> {to_date_str}")
    log.info(f"[Batch] size={BATCH_SIZE}  call_gap={INTER_CALL_SLEEP}s  batch_pause={INTER_BATCH_SLEEP}s")
    log.info("=" * 64)

    total       = len(stocks)
    success     = 0
    skipped     = 0
    no_baseline = 0
    errors      = 0

    # Process in batches
    for batch_start in range(0, total, BATCH_SIZE):
        batch = stocks[batch_start: batch_start + BATCH_SIZE]
        batch_num = batch_start // BATCH_SIZE + 1
        total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
        log.info(f"--- Batch {batch_num}/{total_batches} ({len(batch)} stocks) ---")

        for symbol, token in batch:
            idx    = stocks.index((symbol, token)) + 1
            prefix = f"[{idx:>3}/{total}] {symbol:<20}"

            baselines = get_baseline_5m(conn, symbol)
            if not baselines:
                log.warning(f"{prefix} No 5m baseline — skip")
                no_baseline += 1
                continue

            if sum(baselines.get(s, 0) for s in SLOTS_45M_SORTED) <= 0:
                log.warning(f"{prefix} Baseline has no 45m slots — skip")
                no_baseline += 1
                continue

            if not daily_mode and already_populated(conn, token, from_date_str, to_date_str):
                log.info(f"{prefix} Already in DB — skip")
                skipped += 1
                continue

            hist_5m = kite_fetch_5m(kite, token, from_dt, to_dt)
            if not hist_5m:
                errors += 1
                continue

            ratios = compute_45m_ratios(hist_5m, baselines)
            if not ratios:
                log.warning(f"{prefix} No 45m slot data in candles")
                errors += 1
                continue

            store_ratios(conn, [(token, d, r) for d, r in ratios.items()])
            log.info(f"{prefix} OK  {len(hist_5m):>4} candles -> {len(ratios):>2} days")
            success += 1

        # Pause between batches to avoid hammering Kite
        if batch_start + BATCH_SIZE < total:
            log.info(f"    [Pause] {INTER_BATCH_SLEEP}s between batches...")
            time.sleep(INTER_BATCH_SLEEP)

    conn.close()
    log.info("=" * 64)
    log.info(f"[Done] Success={success}  Skipped={skipped}  NoBaseline={no_baseline}  Errors={errors}")
    log.info(f"[DB]   Rows in first_45m_ratios: {_count_rows()}")


def _count_rows():
    try:
        conn = sqlite3.connect(DB_PATH)
        n = conn.execute("SELECT COUNT(*) FROM first_45m_ratios").fetchone()[0]
        conn.close()
        return n
    except Exception:
        return '?'


if __name__ == '__main__':
    run(daily_mode='--daily' in sys.argv)
