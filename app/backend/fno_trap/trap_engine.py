"""
fno_trap/trap_engine.py
Snapshot cycle orchestrator — runs full pipeline per symbol on demand.
Calls: kite_fetcher → signal_engine → output_reducer → persist → broadcast.
"""
import logging
import threading
from datetime import datetime
from typing import Optional

from fno_trap.db import get_connection
from fno_trap.time_phase import now_ist, get_time_phase, trading_days_to_expiry
from fno_trap.kite_fetcher import (
    fetch_spot, fetch_oi_snapshot, fetch_futures_tick,
    get_latest_spot, get_near_expiry
)
from fno_trap.signal_engine import (
    _split_chain, compute_max_pain, compute_pcr, compute_trap_score,
    build_oi_heatmap, compute_vwap_proxy, compute_pivots,
    compute_rollover_score, compute_confidence, compute_data_confidence,
    compute_survivability, compute_psi, compute_crowding,
    compute_stop, compute_exit_time, compute_lots,
    compute_plain_language_why, check_discipline_gate
)
from fno_trap.output_reducer import reduce_to_action_card, persist_action_card

log = logging.getLogger(__name__)

# ── In-memory snapshot cache for fast /api/card serving ──────────────────
_card_cache: dict = {}   # symbol → card_payload dict
_card_lock = threading.Lock()

_LOT_SIZE_DEFAULTS = {
    "NIFTY": 50, "BANKNIFTY": 15, "FINNIFTY": 40, "MIDCPNIFTY": 75,
    "SENSEX": 10, "BANKEX": 15,
}


def _get_lot_size(symbol: str) -> int:
    """Dynamic lot size from Kite instruments; falls back to hardcoded defaults."""
    try:
        from fno_trap.kite_fetcher import get_lot_size
        ls = get_lot_size(symbol)
        if ls and ls > 0:
            return ls
    except Exception:
        pass
    return _LOT_SIZE_DEFAULTS.get(symbol.upper(), 50)



def get_cached_card(symbol: str) -> Optional[dict]:
    with _card_lock:
        return _card_cache.get(symbol)


def set_cached_card(symbol: str, payload: dict):
    with _card_lock:
        _card_cache[symbol] = payload


def run_cycle(symbol: str, account_size: float = 200000) -> dict:
    """
    Full snapshot cycle for one symbol.
    Returns the API-ready card payload dict.
    """
    now = now_ist()
    phase = get_time_phase(now)

    if phase == "MARKET_CLOSED":
        card = _run_prev_session_cycle(symbol, now, account_size)
        set_cached_card(symbol, card)
        return card

    if phase == "SETTLEMENT_EARLY":
        card = _blocked_payload(symbol, now, phase, "Market in pre-open settlement — no entries allowed")
        set_cached_card(symbol, card)
        return card

    # ── 1. Fetch spot ─────────────────────────────────────────────────────
    spot = fetch_spot(symbol) or get_latest_spot(symbol) or 0

    # ── 2. Get expiry ─────────────────────────────────────────────────────
    expiry = get_near_expiry(symbol)
    if not expiry:
        card = _wait_payload(symbol, now, phase, spot,
                             "Instrument list unavailable — cannot determine expiry", 10)
        set_cached_card(symbol, card)
        return card

    dte = trading_days_to_expiry(expiry)

    # ── 3. Fetch OI snapshot ──────────────────────────────────────────────
    oi_rows = fetch_oi_snapshot(symbol, expiry)
    if not oi_rows:
        # Fall back to cached DB rows
        oi_rows = _get_db_oi(symbol, expiry)

    # ── 4. OI age ─────────────────────────────────────────────────────────
    oi_age_min = _oi_age_minutes(symbol, expiry)
    data_conf_score, data_conf_dot = compute_data_confidence(oi_age_min)

    # ── 5. Fetch futures / basis ──────────────────────────────────────────
    fut = fetch_futures_tick(symbol, expiry)
    basis_pct = fut["basis_pct"] if fut else None

    # ── 6. Signal engine ──────────────────────────────────────────────────
    ce_rows, pe_rows = _split_chain(oi_rows)
    pcr = compute_pcr(ce_rows, pe_rows)
    max_pain = compute_max_pain(oi_rows)
    trap = compute_trap_score(ce_rows, pe_rows, spot, max_pain)
    trap_score     = trap["trap_score"]
    trap_direction = trap["trap_direction"]
    trap_trend     = trap["trap_score_trend"]
    pivots         = compute_pivots(spot)
    vwap           = compute_vwap_proxy(oi_rows, spot)
    rollover       = compute_rollover_score(oi_rows, expiry)

    # Candle status — stub (would need OHLCV data)
    candle_status = "UNKNOWN"

    confidence = compute_confidence(
        trap_score, phase, dte, candle_status,
        abs(pcr["pcr_oi"] - pcr["pcr_vol"]) / max(pcr["pcr_oi"], 0.01) * 100,
        rollover
    )

    why = compute_plain_language_why(trap_direction, trap_score, pcr["pcr_oi"])

    # ── 7. Discipline gate ────────────────────────────────────────────────
    discipline = check_discipline_gate()

    # ── 8. Recommendation ────────────────────────────────────────────────
    lot_size = _get_lot_size(symbol)
    option_type = "CE" if trap_direction == "PUT_BUYER_TRAP" else "PE"
    atm_strike = _find_atm_strike(ce_rows + pe_rows, spot)

    # Find ATM LTP for this option type
    rec_rows = ce_rows if option_type == "CE" else pe_rows
    atm_ltp = next(
        (r.get("ltp", 0) for r in rec_rows if r.get("strike") == atm_strike), 0
    ) or 0

    lot_cost = round(atm_ltp * lot_size, 2) if atm_ltp else None
    lots = compute_lots(account_size, lot_cost or 0, confidence)
    stop_info = compute_stop(atm_ltp, trap_direction, spot, pivots["s1"], pivots["r1"])
    exit_time = compute_exit_time(phase)

    # Targets — T1 = 1× stop distance, T2 = 2×
    if atm_ltp:
        t1 = round(atm_ltp + (atm_ltp - (stop_info["stop_level"] or atm_ltp * 0.68)), 1)
        t2 = round(atm_ltp + 2 * (atm_ltp - (stop_info["stop_level"] or atm_ltp * 0.68)), 1)
    else:
        t1 = t2 = None

    psi = compute_psi(spot, atm_ltp, atm_ltp)  # ATM vs ATM = NORMAL baseline
    crowding = compute_crowding(symbol, atm_strike, option_type) if atm_strike else 0

    # Survivability
    pos = _get_active_position(symbol)
    wap = pos.get("wap") if pos else None
    surv = compute_survivability(atm_ltp, wap, dte) if (pos and wap) else 99

    # Correlated positions
    corr_count = _count_correlated_positions(symbol)

    snap = {
        "symbol": symbol,
        "expiry": expiry.isoformat(),
        "spot": spot,
        "phase": phase,
        "dte": dte,
        "trap_score": trap_score,
        "trap_direction": trap_direction,
        "trap_score_trend": trap_trend,
        "candle_confirmation_status": candle_status,
        "confidence_pct": confidence,
        "plain_language_why": why,
        "pcr_oi": pcr["pcr_oi"],
        "pcr_vol": pcr["pcr_vol"],
        "pcr_divergence_pct": abs(pcr["pcr_oi"] - pcr["pcr_vol"]) / max(pcr["pcr_oi"], 0.01) * 100,
        "rollover_score_pct": rollover,
        "absorption_score": 0,
        "market_regime": _infer_regime(trap_score, pcr["pcr_oi"], dte),
        "crowding_risk_score": crowding,
        "execution_gate_status": "GOOD" if (basis_pct or 0) < 0.2 else "FAIR",
        "psi_gate_status": psi,
        "basis_pct": basis_pct,
        "no_trade_zone_active": phase == "DEAD_ZONE",
        "correlated_position_count": corr_count,
        "max_pain": max_pain,
        "vwap": vwap,
        "pivot_r1": pivots["r1"],
        "pivot_r2": pivots["r2"],
        "pivot_s1": pivots["s1"],
        "pivot_s2": pivots["s2"],
        "oi_age_minutes": oi_age_min,
        "data_confidence_score": data_conf_score,
        "pipeline_status": "HEALTHY",
        "survivability_snapshots": surv,
        "computed_at": now.isoformat(),
        # Extras for output_reducer
        "volatility_transition_state": None,
        "liquidity_shock_detected": False,
        "dealer_gamma_regime": None,
        "intent_gate_status": None,
        "fresh_oi_ratio_pct": 100,
    }

    rec = {
        "symbol": symbol,
        "expiry": expiry.isoformat(),
        "recommended_strike": atm_strike,
        "recommended_option_type": option_type,
        "recommended_expiry": expiry.isoformat(),
        "vol_adjusted_lots": lots,
        "lot_cost_inr": lot_cost,
        "stop_type": "PREMIUM_STOP",
        "stop_level": stop_info["stop_level"],
        "stop_spot_anchor": stop_info["stop_spot_anchor"],
        "stop_pct": stop_info["stop_pct"],
        "target_1": t1,
        "target_2": t2,
        "exit_time": exit_time,
    }

    # ── 9. Output reducer → card ──────────────────────────────────────────
    card_row = reduce_to_action_card(symbol, expiry.isoformat(), snap, rec, discipline, account_size)
    persist_action_card(card_row)

    # ── 10. Build API payload ─────────────────────────────────────────────
    oi_heatmap = build_oi_heatmap(ce_rows, pe_rows, spot)
    nifty_spot, nifty_dir = (spot, "up") if symbol == "NIFTY" else (get_latest_spot("NIFTY") or 0, "up")

    payload = _build_api_payload(
        card_row, snap, rec, pos, oi_heatmap, data_conf_dot,
        discipline, nifty_spot, nifty_dir
    )

    set_cached_card(symbol, payload)
    log.info("FNO Trap cycle OK: %s → %s (score=%d conf=%d)",
             symbol, card_row["card_state"], trap_score, confidence)
    return payload


# ── Helpers ───────────────────────────────────────────────────────────────

def _find_atm_strike(oi_rows, spot):
    if not oi_rows or not spot:
        return None
    strikes = list(set(r["strike"] for r in oi_rows))
    return min(strikes, key=lambda s: abs(s - spot))


def _infer_regime(trap_score, pcr_oi, dte):
    if dte <= 1:
        return "EVENT_COMPRESSION"
    if trap_score >= 70 and pcr_oi > 1.2:
        return "TRENDING_UP"
    if trap_score >= 70 and pcr_oi < 0.8:
        return "TRENDING_DOWN"
    return "RANGE_BOUND"


def _count_correlated_positions(symbol):
    CORR = {"NIFTY": ["FINNIFTY","MIDCPNIFTY"], "BANKNIFTY": ["FINNIFTY"], "FINNIFTY": ["NIFTY","BANKNIFTY"]}
    related = CORR.get(symbol, [])
    if not related:
        return 0
    conn = get_connection()
    count = conn.execute(
        f"SELECT COUNT(*) FROM position_entries WHERE symbol IN ({','.join('?'*len(related))}) AND is_open=1",
        related
    ).fetchone()[0]
    conn.close()
    return count


def _get_active_position(symbol) -> Optional[dict]:
    conn = get_connection()
    pos = conn.execute(
        "SELECT * FROM position_entries WHERE symbol=? AND is_open=1 ORDER BY opened_at DESC LIMIT 1",
        (symbol,)
    ).fetchone()
    if not pos:
        conn.close()
        return None
    pos_dict = dict(pos)
    fills = conn.execute(
        "SELECT lots, fill_price FROM position_fills WHERE position_id=? AND fill_source='ENTRY'",
        (pos["id"],)
    ).fetchall()
    conn.close()
    total_lots = sum(f["lots"] for f in fills)
    if total_lots > 0:
        pos_dict["wap"] = sum(f["lots"] * f["fill_price"] for f in fills) / total_lots
    else:
        pos_dict["wap"] = pos_dict.get("entry_price")
    return pos_dict


def _get_db_oi(symbol, expiry, max_age_hours: int = 1) -> list:
    """Load most recent OI snapshot from DB. max_age_hours=0 means any age."""
    conn = get_connection()
    if max_age_hours > 0:
        rows = conn.execute("""
            SELECT strike, option_type, oi, oi_change, volume, ltp, bid, ask
            FROM oi_snapshots WHERE symbol=? AND expiry=?
            AND snapshot_time >= datetime('now', ? || ' hours')
            ORDER BY snapshot_time DESC, strike ASC
        """, (symbol, expiry.isoformat(), f'-{max_age_hours}')).fetchall()
    else:
        # Any age — pull from latest batch (same snapshot_time as newest row)
        latest = conn.execute(
            "SELECT snapshot_time FROM oi_snapshots WHERE symbol=? AND expiry=? ORDER BY snapshot_time DESC LIMIT 1",
            (symbol, expiry.isoformat())
        ).fetchone()
        if not latest:
            conn.close()
            return []
        rows = conn.execute("""
            SELECT strike, option_type, oi, oi_change, volume, ltp, bid, ask
            FROM oi_snapshots WHERE symbol=? AND expiry=?
            AND snapshot_time >= datetime(?, '-5 minutes')
            ORDER BY strike ASC
        """, (symbol, expiry.isoformat(), latest["snapshot_time"])).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _get_last_session_expiry(symbol: str):
    """Return the expiry used in the most recent OI snapshot for symbol."""
    conn = get_connection()
    row = conn.execute(
        "SELECT expiry FROM oi_snapshots WHERE symbol=? ORDER BY snapshot_time DESC LIMIT 1",
        (symbol,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    try:
        from datetime import date
        return date.fromisoformat(row["expiry"])
    except Exception:
        return None


def _prev_session_label(symbol: str) -> str:
    """Human-readable label for the last snapshot time, e.g. 'Last session: Fri 3:25 PM'"""
    conn = get_connection()
    row = conn.execute(
        "SELECT snapshot_time FROM oi_snapshots WHERE symbol=? ORDER BY snapshot_time DESC LIMIT 1",
        (symbol,)
    ).fetchone()
    conn.close()
    if not row:
        return "Previous session data"
    try:
        dt = datetime.fromisoformat(row["snapshot_time"].replace("Z", "+00:00"))
        return dt.strftime("Last session: %a %-I:%M %p")
    except Exception:
        return "Previous session data"


def _run_prev_session_cycle(symbol: str, now, account_size: float = 200000) -> dict:
    """
    Build a MARKET_CLOSED card populated with real signal data from
    the most recent DB OI snapshot (previous closing session).
    No live Kite calls — pure DB replay.
    """
    # Load last stored expiry from DB
    expiry = _get_last_session_expiry(symbol)
    if not expiry:
        # No historical data at all — return minimal closed card
        return _market_closed_payload(symbol, now)

    oi_rows = _get_db_oi(symbol, expiry, max_age_hours=0)  # any age
    if not oi_rows:
        return _market_closed_payload(symbol, now)

    # Use last stored spot (or 0)
    spot = get_latest_spot(symbol) or 0

    dte = trading_days_to_expiry(expiry)
    oi_age_min = _oi_age_minutes(symbol, expiry)
    _, data_conf_dot = compute_data_confidence(oi_age_min)
    data_conf_dot = "red"   # always red — market is closed, data is stale

    ce_rows, pe_rows = _split_chain(oi_rows)
    pcr       = compute_pcr(ce_rows, pe_rows)
    max_pain  = compute_max_pain(oi_rows)
    trap      = compute_trap_score(ce_rows, pe_rows, spot, max_pain)
    trap_score     = trap["trap_score"]
    trap_direction = trap["trap_direction"]
    trap_trend     = trap["trap_score_trend"]
    pivots    = compute_pivots(spot)
    vwap      = compute_vwap_proxy(oi_rows, spot)
    rollover  = compute_rollover_score(oi_rows, expiry)

    regime = _infer_regime(trap_score, pcr["pcr_oi"], dte)
    oi_heatmap = build_oi_heatmap(ce_rows, pe_rows, spot)

    why = compute_plain_language_why(trap_direction, trap_score, pcr["pcr_oi"]) if trap_direction else None
    session_label = _prev_session_label(symbol)

    pos = _get_active_position(symbol)
    nifty_spot = get_latest_spot("NIFTY") or 0 if symbol != "NIFTY" else spot

    return {
        # Card identity
        "card_state":    "MARKET_CLOSED",
        "trap_dir":      trap_direction,
        "action":        None,
        "strike":        None,
        "expiry":        expiry.isoformat(),
        "lots":          1,
        "lot_cost":      None,
        "premium":       None,
        "stop":          None,
        "spot_inval":    None,
        "exit_time":     "09:15",   # next open
        # Prev-session signals (shown in Details accordion)
        "why":           f"⏸ {session_label}. {why}" if why else f"⏸ {session_label} — market closed.",
        "spot_t1":       None,
        "spot_t2":       None,
        "spot":          spot,
        "spot_dir":      "up",
        "phase":         "MARKET_CLOSED",
        "dte":           f"DTE {dte}" if dte is not None else "—",
        "data_conf":     data_conf_dot,
        "snapshot_time": now.strftime("%H:%M"),
        "confidence":    0,
        "wait_reason":   None,
        "wait_mins":     0,
        "wait_total_mins": 0,
        "avoid_reason":  None,
        "block_reason":  None,
        "warnings":      [],
        # Position
        "has_position":  bool(pos),
        "position_state": pos.get("position_state") if pos else None,
        "pos_strike":    f"{int(pos['strike']):,} {pos.get('option_type','')}" if pos else None,
        "pos_wap":       pos.get("wap") if pos else None,
        "pos_now":       0,
        "pos_lots":      pos.get("lots_total") if pos else None,
        "survivability_snaps": 99,
        "trail":         None,
        # REAL prev-session signals ↓
        "trap_score":    trap_score,
        "pcr_oi":        pcr["pcr_oi"],
        "pcr_vol":       pcr["pcr_vol"],
        "max_pain":      max_pain or 0,
        "vwap":          vwap or 0,
        "pivot_r1":      pivots["r1"],
        "pivot_s1":      pivots["s1"],
        "oi_data":       oi_heatmap,
        "regime":        regime,
        "vix":           None,
        "exec_gate":     None,
        "psi":           None,
        "crowding":      0,
        "conditions":    [],
        "ws_bandwidth":  None,
        "oi_age":        oi_age_min,
        "pipeline":      "prev_session",
        "cooldown_active": False,
        "cooldown_expiry": None,
        "has_event":     False,
        "event_text":    None,
        "nifty_spot":    nifty_spot or 0,
        "nifty_dir":     "up",
        # Extra field for frontend banner
        "prev_session_label": session_label,
    }


def _oi_age_minutes(symbol, expiry) -> int:
    conn = get_connection()
    row = conn.execute(
        "SELECT snapshot_time FROM oi_snapshots WHERE symbol=? AND expiry=? ORDER BY snapshot_time DESC LIMIT 1",
        (symbol, expiry.isoformat())
    ).fetchone()
    conn.close()
    if not row:
        return 999
    try:
        snap_dt = datetime.fromisoformat(row["snapshot_time"].replace("Z", "+00:00"))
        delta = (now_ist() - snap_dt).total_seconds() / 60
        return int(delta)
    except Exception:
        return 0


def _build_api_payload(card, snap, rec, pos, oi_heatmap, data_conf_dot, discipline, nifty_spot, nifty_dir) -> dict:
    """Assemble the full API payload shape expected by fno_dashboard.html."""
    action = None
    if card.get("action_verb") == "BUY_CALL":
        action = "BUY CALL"
    elif card.get("action_verb") == "BUY_PUT":
        action = "BUY PUT"

    strike_str = None
    if card.get("strike") and card.get("recommended_expiry"):
        exp = card["recommended_expiry"]
        try:
            from datetime import date
            d = date.fromisoformat(exp)
            exp_label = d.strftime("%-d %b")
        except Exception:
            exp_label = exp
        ot = card.get("option_type", "")
        strike_str = f"{int(card['strike']):,} {ot} · {exp_label}"

    warnings = []
    for line, key in [
        (card.get("warning_line_1"), card.get("warning_key_1")),
        (card.get("warning_line_2"), card.get("warning_key_2")),
        (card.get("warning_line_correlated"), "w-correlated"),
    ]:
        if line:
            warnings.append({"text": line, "bucket": "c", "key": key or ""})

    has_position = bool(pos)
    pos_state = pos.get("position_state") if pos else None
    pos_now_ltp = 0  # Would need live option LTP sub — stub for Phase 2

    return {
        "card_state":    card["card_state"],
        "trap_dir":      snap.get("trap_direction"),
        "action":        action,
        "strike":        strike_str,
        "expiry":        card.get("recommended_expiry"),
        "lots":          card.get("lot_count") or 1,
        "lot_cost":      card.get("lot_cost_inr"),
        "premium":       None,
        "stop":          card.get("stop_level"),
        "spot_inval":    f"{int(card['stop_spot_anchor']):,}" if card.get("stop_spot_anchor") else None,
        "exit_time":     card.get("exit_time") or "15:00",
        "why":           card.get("why_line") or snap.get("plain_language_why"),
        "spot_t1":       f"{int(card['target_1']):,}" if card.get("target_1") else None,
        "spot_t2":       f"{int(card['target_2']):,}" if card.get("target_2") else None,
        "spot":          snap.get("spot") or 0,
        "spot_dir":      "up",
        "phase":         snap.get("phase"),
        "dte":           f"DTE {snap.get('dte')}" if snap.get("dte") is not None else "—",
        "data_conf":     data_conf_dot,
        "snapshot_time": snap.get("computed_at", "")[-8:][:5],
        "confidence":    snap.get("confidence_pct") or 0,
        "wait_reason":   card.get("wait_reason"),
        "wait_mins":     0,
        "wait_total_mins": 0,
        "avoid_reason":  card.get("avoid_reason"),
        "block_reason":  card.get("block_reason"),
        "warnings":      warnings,
        "has_position":  has_position,
        "position_state": pos_state,
        "pos_strike":    f"{int(pos['strike']):,} {pos.get('option_type','')}" if pos else None,
        "pos_wap":       pos.get("wap") if pos else None,
        "pos_now":       pos_now_ltp,
        "pos_lots":      pos.get("lots_total") if pos else None,
        "survivability_snaps": snap.get("survivability_snapshots"),
        "trail":         pos.get("trail_stop_level") if pos else None,
        "trap_score":    snap.get("trap_score") or 0,
        "pcr_oi":        snap.get("pcr_oi") or 0,
        "pcr_vol":       snap.get("pcr_vol") or 0,
        "max_pain":      snap.get("max_pain") or 0,
        "vwap":          snap.get("vwap") or 0,
        "pivot_r1":      snap.get("pivot_r1") or 0,
        "pivot_s1":      snap.get("pivot_s1") or 0,
        "oi_data":       oi_heatmap,
        "regime":        snap.get("market_regime"),
        "vix":           None,
        "exec_gate":     snap.get("execution_gate_status"),
        "psi":           snap.get("psi_gate_status"),
        "crowding":      snap.get("crowding_risk_score"),
        "conditions":    [],
        "ws_bandwidth":  None,
        "oi_age":        snap.get("oi_age_minutes"),
        "pipeline":      snap.get("pipeline_status"),
        "cooldown_active":  discipline.get("cooldown_active"),
        "cooldown_expiry":  discipline.get("cooldown_until"),
        "has_event":     False,
        "event_text":    None,
        "nifty_spot":    nifty_spot or 0,
        "nifty_dir":     nifty_dir or "up",
    }


def _market_closed_payload(symbol, now) -> dict:
    """Fallback minimal closed card when no DB data exists yet."""
    return {
        "card_state": "MARKET_CLOSED", "trap_dir": None, "action": None,
        "strike": None, "expiry": None, "lots": 1, "lot_cost": None,
        "premium": None, "stop": None, "spot_inval": None, "exit_time": "09:15",
        "why": "⏸ Market closed — no previous session data available yet.",
        "spot_t1": None, "spot_t2": None,
        "spot": get_latest_spot(symbol) or 0, "spot_dir": "up",
        "phase": "MARKET_CLOSED", "dte": "—", "data_conf": "red",
        "snapshot_time": now.strftime("%H:%M"), "confidence": 0,
        "wait_reason": None, "wait_mins": 0, "wait_total_mins": 0,
        "avoid_reason": None, "block_reason": None, "warnings": [],
        "has_position": False, "position_state": None,
        "pos_strike": None, "pos_wap": None, "pos_now": 0, "pos_lots": None,
        "survivability_snaps": 99, "trail": None,
        "trap_score": 0, "pcr_oi": 0, "pcr_vol": 0, "max_pain": 0, "vwap": 0,
        "pivot_r1": 0, "pivot_s1": 0, "oi_data": [], "regime": None,
        "vix": None, "exec_gate": None, "psi": None, "crowding": 0, "conditions": [],
        "ws_bandwidth": None, "oi_age": None, "pipeline": "no_data",
        "cooldown_active": False, "cooldown_expiry": None,
        "has_event": False, "event_text": None, "nifty_spot": 0, "nifty_dir": "up",
        "prev_session_label": None,
    }


def _blocked_payload(symbol, now, phase, reason) -> dict:
    p = _market_closed_payload(symbol, now)
    p["card_state"] = "BLOCKED"
    p["phase"] = phase
    p["block_reason"] = reason
    p["data_conf"] = "amber"
    return p


def _wait_payload(symbol, now, phase, spot, reason, wait_mins) -> dict:
    p = _market_closed_payload(symbol, now)
    p["card_state"] = "WAIT"
    p["phase"] = phase
    p["spot"] = spot
    p["data_conf"] = "amber"
    p["wait_reason"] = reason
    p["wait_mins"] = wait_mins
    return p
