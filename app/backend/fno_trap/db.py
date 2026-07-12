"""
fno_trap/db.py
SQLite schema + connection for FNO Trap Dashboard.
DB file: app/backend/fno_trap_cache.db (separate from TradeSignal main DB)
"""
import os
import sqlite3
import logging

log = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(_HERE, "fno_trap_cache.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_connection()
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS watchlist (
        symbol      TEXT PRIMARY KEY,
        lot_size    INTEGER DEFAULT 50,
        is_index    BOOLEAN DEFAULT 1,
        added_at    DATETIME DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS oi_snapshots (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol        TEXT NOT NULL,
        expiry        TEXT NOT NULL,
        strike        REAL NOT NULL,
        option_type   TEXT NOT NULL,
        oi            INTEGER DEFAULT 0,
        oi_change     INTEGER DEFAULT 0,
        volume        INTEGER DEFAULT 0,
        ltp           REAL DEFAULT 0,
        iv            REAL,
        bid           REAL,
        ask           REAL,
        snapshot_time TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_oi ON oi_snapshots(symbol, expiry, snapshot_time DESC);

    CREATE TABLE IF NOT EXISTS spot_tick (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol    TEXT NOT NULL,
        ltp       REAL NOT NULL,
        tick_time TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_spot ON spot_tick(symbol, tick_time DESC);

    CREATE TABLE IF NOT EXISTS futures_tick (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol      TEXT NOT NULL,
        expiry      TEXT NOT NULL,
        ltp         REAL,
        spot        REAL,
        basis_pct   REAL,
        oi          INTEGER,
        oi_change   INTEGER,
        volume      INTEGER,
        tick_time   TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS signal_snapshot (
        id                          INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol                      TEXT NOT NULL,
        expiry                      TEXT NOT NULL,
        trap_score                  INTEGER DEFAULT 0,
        trap_direction              TEXT,
        trap_score_trend            TEXT,
        candle_confirmation_status  TEXT DEFAULT 'UNKNOWN',
        confidence_pct              INTEGER DEFAULT 0,
        plain_language_why          TEXT,
        pcr_oi                      REAL,
        pcr_vol                     REAL,
        pcr_divergence_pct          REAL,
        rollover_score_pct          REAL,
        absorption_score            INTEGER,
        market_regime               TEXT,
        crowding_risk_score         INTEGER DEFAULT 0,
        execution_gate_status       TEXT,
        psi_gate_status             TEXT,
        basis_pct                   REAL,
        no_trade_zone_active        BOOLEAN DEFAULT 0,
        correlated_position_count   INTEGER DEFAULT 0,
        max_pain                    REAL,
        vwap                        REAL,
        pivot_r1                    REAL,
        pivot_r2                    REAL,
        pivot_s1                    REAL,
        pivot_s2                    REAL,
        oi_age_minutes              INTEGER DEFAULT 0,
        data_confidence_score       INTEGER DEFAULT 100,
        pipeline_status             TEXT DEFAULT 'HEALTHY',
        survivability_snapshots     INTEGER DEFAULT 99,
        computed_at                 TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_signal ON signal_snapshot(symbol, computed_at DESC);

    CREATE TABLE IF NOT EXISTS action_card (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol                  TEXT NOT NULL,
        expiry                  TEXT NOT NULL,
        card_state              TEXT NOT NULL,
        action_verb             TEXT,
        strike                  REAL,
        option_type             TEXT,
        recommended_expiry      TEXT,
        lot_count               INTEGER,
        lot_cost_inr            REAL,
        stop_type               TEXT,
        stop_level              REAL,
        stop_spot_anchor        REAL,
        stop_pct                REAL,
        exit_time               TEXT,
        target_1                REAL,
        target_2                REAL,
        block_reason            TEXT,
        avoid_reason            TEXT,
        wait_reason             TEXT,
        wait_valid_until        TEXT,
        wait_is_phase_duration  BOOLEAN DEFAULT 0,
        previous_card_state     TEXT,
        warning_line_1          TEXT,
        warning_line_2          TEXT,
        warning_line_correlated TEXT,
        why_line                TEXT,
        warning_key_1           TEXT,
        warning_key_2           TEXT,
        source_trap_score       INTEGER,
        source_confidence_pct   INTEGER,
        computed_at             TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_action_card ON action_card(symbol, computed_at DESC);

    CREATE TABLE IF NOT EXISTS position_entries (
        id                              INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol                          TEXT NOT NULL,
        strike                          REAL NOT NULL,
        option_type                     TEXT NOT NULL,
        expiry                          TEXT NOT NULL,
        entry_price                     REAL,
        lots_total                      INTEGER NOT NULL DEFAULT 1,
        original_trap_direction_at_entry TEXT,
        target_1                        REAL,
        target_2                        REAL,
        stop_level                      REAL,
        stop_spot_anchor                REAL,
        exit_time                       TEXT,
        partial_exit_at_t1              BOOLEAN DEFAULT 0,
        peak_premium                    REAL,
        trail_stop_level                REAL,
        position_state                  TEXT DEFAULT 'STRENGTHENING',
        is_open                         BOOLEAN DEFAULT 1,
        opened_at                       TEXT DEFAULT CURRENT_TIMESTAMP,
        closed_at                       TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_pos ON position_entries(symbol, is_open);

    CREATE TABLE IF NOT EXISTS position_fills (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        position_id     INTEGER NOT NULL REFERENCES position_entries(id) ON DELETE CASCADE,
        fill_sequence   INTEGER NOT NULL,
        lots            INTEGER NOT NULL,
        fill_price      REAL NOT NULL,
        fill_time       TEXT NOT NULL,
        fill_source     TEXT DEFAULT 'ENTRY',
        notes           TEXT,
        UNIQUE(position_id, fill_sequence)
    );

    CREATE TABLE IF NOT EXISTS session_context (
        session_date            TEXT PRIMARY KEY,
        account_size_inr        REAL DEFAULT 200000,
        max_daily_loss_inr      REAL DEFAULT 5000,
        max_consecutive_losses  INTEGER DEFAULT 2,
        daily_loss_so_far       REAL DEFAULT 0,
        consecutive_losses      INTEGER DEFAULT 0,
        cooldown_until          TEXT
    );

    CREATE TABLE IF NOT EXISTS pipeline_health (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        check_time      TEXT DEFAULT CURRENT_TIMESTAMP,
        kite_api_status TEXT DEFAULT 'OK',
        oi_data_age_min INTEGER DEFAULT 0,
        pipeline_status TEXT DEFAULT 'HEALTHY'
    );
    """)

    # Default watchlist
    c.execute("SELECT COUNT(*) FROM watchlist")
    if c.fetchone()[0] == 0:
        defaults = [("NIFTY",50,1),("BANKNIFTY",15,1),("FINNIFTY",40,1),("MIDCPNIFTY",75,1)]
        c.executemany("INSERT OR IGNORE INTO watchlist(symbol,lot_size,is_index) VALUES(?,?,?)", defaults)
        log.info("FNO Trap: inserted default watchlist")

    # Default session row for today
    c.execute("INSERT OR IGNORE INTO session_context(session_date) VALUES(date('now','localtime'))")
    conn.commit()
    conn.close()
    log.info("FNO Trap DB initialised at %s", DB_PATH)
