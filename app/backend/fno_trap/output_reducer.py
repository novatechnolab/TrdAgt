"""
fno_trap/output_reducer.py
§19.3 Priority 1-4 state machine + warning bucket evaluator.
Adapted from FNO Trap Dashboard/output_reducer.py.
"""
import logging
from typing import Optional

from fno_trap.db import get_connection
from fno_trap.time_phase import now_ist

log = logging.getLogger(__name__)


# ── Priority 1-4 state machine ────────────────────────────────────────────

def reduce_to_action_card(symbol, expiry, snap, rec, discipline, account_size) -> dict:
    """
    §19.3 Priority reducer:
    P1 → BLOCKED (phase, discipline, ban, event)
    P2 → AVOID   (no edge, DTE=0)
    P3 → WAIT    (trap score < threshold, DEAD_ZONE, OI stale)
    P4 → TRADE   (all gates pass)
    """
    now = now_ist()
    phase = snap.get("phase", "MARKET_CLOSED")
    trap_score  = snap.get("trap_score", 0)
    confidence  = snap.get("confidence_pct", 0)
    oi_age      = snap.get("oi_age_minutes", 0)
    dte         = snap.get("dte", 0)
    psi         = snap.get("psi_gate_status", "NORMAL")

    prev_state = _get_prev_card_state(symbol)

    # ── P1: BLOCKED ───────────────────────────────────────────────────────
    if phase in ("SETTLEMENT_EARLY", "CLOSE_RISK", "MARKET_CLOSED"):
        return _make_card(symbol, expiry, "BLOCKED", snap, rec, prev_state,
                          block_reason=f"Phase {phase}: no fresh entries allowed")

    if discipline.get("blocked"):
        return _make_card(symbol, expiry, "BLOCKED", snap, rec, prev_state,
                          block_reason=discipline["reason"])

    if psi == "PSI_BLOCK":
        return _make_card(symbol, expiry, "BLOCKED", snap, rec, prev_state,
                          block_reason="Premium Stretch Index: option overextended — skip this cycle")

    # ── P2: AVOID ─────────────────────────────────────────────────────────
    if dte == 0:
        return _make_card(symbol, expiry, "AVOID", snap, rec, prev_state,
                          avoid_reason="Expiry day — avoid new entries (gamma risk)")

    if trap_score < 30 and confidence < 40:
        return _make_card(symbol, expiry, "AVOID", snap, rec, prev_state,
                          avoid_reason="Insufficient directional edge — no clear trap forming")

    # ── P3: WAIT ──────────────────────────────────────────────────────────
    if phase == "SETTLEMENT_LATE":
        return _make_card(symbol, expiry, "WAIT", snap, rec, prev_state,
                          wait_reason="Opening OI stabilising — wait until 10:00 AM")

    if oi_age > 15:
        return _make_card(symbol, expiry, "WAIT", snap, rec, prev_state,
                          wait_reason=f"OI data is {oi_age} min old — waiting for fresh snapshot")

    if trap_score < 50 or confidence < 55:
        return _make_card(symbol, expiry, "WAIT", snap, rec, prev_state,
                          wait_reason=f"Trap score {trap_score}% — signal forming but not confirmed yet")

    # DEAD_ZONE wait (lower threshold — require stronger signal)
    if phase == "DEAD_ZONE" and (trap_score < 65 or confidence < 65):
        return _make_card(symbol, expiry, "WAIT", snap, rec, prev_state,
                          wait_reason="Dead zone (11:30–1:30 PM) — require stronger signal to trade")

    # ── P4: TRADE ─────────────────────────────────────────────────────────
    return _make_card(symbol, expiry, "TRADE", snap, rec, prev_state)


def _make_card(symbol, expiry, state, snap, rec, prev_state,
               block_reason=None, avoid_reason=None, wait_reason=None) -> dict:
    trap_dir   = snap.get("trap_direction")
    action_verb = None
    if state == "TRADE":
        action_verb = "BUY_CALL" if trap_dir == "PUT_BUYER_TRAP" else "BUY_PUT"

    phase = snap.get("phase", "MARKET_CLOSED")
    w1, w2, w_corr, k1, k2 = _evaluate_warning_buckets(snap, rec, phase)

    return {
        "symbol":               symbol,
        "expiry":               expiry,
        "card_state":           state,
        "action_verb":          action_verb,
        "strike":               rec.get("recommended_strike") if state == "TRADE" else None,
        "option_type":          rec.get("recommended_option_type") if state == "TRADE" else None,
        "recommended_expiry":   rec.get("recommended_expiry") if state == "TRADE" else None,
        "lot_count":            rec.get("vol_adjusted_lots") if state == "TRADE" else None,
        "lot_cost_inr":         rec.get("lot_cost_inr") if state == "TRADE" else None,
        "stop_type":            rec.get("stop_type") if state == "TRADE" else None,
        "stop_level":           rec.get("stop_level") if state == "TRADE" else None,
        "stop_spot_anchor":     rec.get("stop_spot_anchor") if state == "TRADE" else None,
        "stop_pct":             rec.get("stop_pct") if state == "TRADE" else None,
        "exit_time":            rec.get("exit_time"),
        "target_1":             rec.get("target_1") if state == "TRADE" else None,
        "target_2":             rec.get("target_2") if state == "TRADE" else None,
        "block_reason":         block_reason,
        "avoid_reason":         avoid_reason,
        "wait_reason":          wait_reason,
        "wait_valid_until":     None,
        "wait_is_phase_duration": False,
        "previous_card_state":  prev_state,
        "warning_line_1":       w1,
        "warning_line_2":       w2,
        "warning_line_correlated": w_corr,
        "why_line":             snap.get("plain_language_why"),
        "warning_key_1":        k1,
        "warning_key_2":        k2,
        "source_trap_score":    snap.get("trap_score"),
        "source_confidence_pct": snap.get("confidence_pct"),
        "computed_at":          now_ist().isoformat(),
    }


# ── Warning buckets ───────────────────────────────────────────────────────

def _evaluate_warning_buckets(snap, rec, phase) -> tuple:
    bucket_a = _eval_bucket_a(snap, rec, phase)
    bucket_b = _eval_bucket_b(snap)
    bucket_c = _eval_bucket_c(snap)

    dead_zone_line = dead_zone_key = None
    if phase == "DEAD_ZONE":
        dead_zone_line = "⚠ Dead zone (11:30–1:30 PM) — lower reliability. Wait for Power Hour."
        dead_zone_key  = "w-deadzone"

    ranked = sorted(
        [b for b in [bucket_a, bucket_b, bucket_c] if b[0]],
        key=lambda x: x[2]
    )

    lines, keys = [], []
    if dead_zone_line:
        lines.append(dead_zone_line)
        keys.append(dead_zone_key)

    for line, key, _ in ranked:
        if len(lines) >= 2:
            break
        if line not in lines:
            lines.append(line)
            keys.append(key)

    w1 = lines[0] if lines else None
    w2 = lines[1] if len(lines) > 1 else None
    k1 = keys[0]  if keys  else None
    k2 = keys[1]  if len(keys) > 1 else None

    corr_count = snap.get("correlated_position_count") or 0
    w_corr = None
    if corr_count >= 2:
        w_corr = (f"⚠ {corr_count} correlated positions — combined exposure high"
                  if corr_count >= 3
                  else "⚠ You have 2 open positions in correlated symbols — consider exposure before adding.")

    return w1, w2, w_corr, k1, k2


def _eval_bucket_a(snap, rec, phase) -> tuple:
    surv      = snap.get("survivability_snapshots") or 99
    vol_state = snap.get("volatility_transition_state")
    exit_time = rec.get("exit_time") or "15:14"
    if surv == 0:
        return ("⛔ Theta critical — exit or T1 must hit now", "w-iv-crush", 1)
    if surv < 3:
        return (f"⏱ Theta: ~{surv*5} min viable — momentum must come soon", "w-iv-crush", 2)
    if vol_state == "CRUSHING":
        return ("⚠ Options losing value fast — exit earlier than planned", "w-iv-crush", 3)
    if surv < 6:
        return (f"⏱ Theta: ~{surv*5} min viable — monitor closely", "w-iv-crush", 4)
    now_str = now_ist().strftime("%H:%M")
    if exit_time and _minutes_diff(now_str, exit_time) <= 10:
        return (f"⏱ Time stop near — {_fmt_exit_time(exit_time)} approaching", "w-iv-crush", 5)
    return (None, None, 99)


def _eval_bucket_b(snap) -> tuple:
    if snap.get("liquidity_shock_detected"):
        return ("⚠ Spread widening — use limit orders, not market", "w-spread", 1)
    if (snap.get("crowding_risk_score") or 0) > 66:
        return ("⚠ Retail crowd heavy — slippage risk at this strike", "w-spread", 2)
    if snap.get("execution_gate_status") == "FAIR":
        return ("⚠ Spread elevated — check bid-ask before placing", "w-spread", 3)
    if abs(snap.get("basis_pct") or 0) > 0.3:
        return ("⚠ Futures price unusual — use a limit order, not market", "w-basis", 4)
    return (None, None, 99)


def _eval_bucket_c(snap) -> tuple:
    corr   = snap.get("correlated_position_count") or 0
    roll   = snap.get("rollover_score_pct") or 0
    gamma  = snap.get("dealer_gamma_regime")
    pcr_d  = snap.get("pcr_divergence_pct") or 0
    fresh  = snap.get("fresh_oi_ratio_pct") or 100
    if corr >= 3:
        return (f"⚠ {corr} correlated positions — combined exposure high", "w-correlated", 1)
    if roll > 60:
        return ("⚠ Many traders rolling contracts today — OI data less reliable", "w-rollover", 2)
    if gamma == "FLIP_ZONE":
        spot = snap.get("spot")
        level = f"{int(spot):,}" if spot else "key level"
        return (f"⚠ Big trader hedging likely near {level} — expect resistance", "w-gamma", 3)
    if corr == 2:
        return ("⚠ 2 correlated positions active — watch combined exposure", "w-correlated", 4)
    if 30 <= roll <= 60:
        return ("⚠ Rollover OI active — reduce conviction slightly", "w-rollover", 5)
    if pcr_d > 30:
        return ("⚠ Options flow and price are disagreeing — watch price carefully", "w-pcr-div", 6)
    if fresh < 5:
        return ("⚠ OI dominated by carryover today", "w-rollover", 7)
    return (None, None, 99)


# ── Persist ───────────────────────────────────────────────────────────────

def persist_action_card(row: dict):
    conn = get_connection()
    conn.execute("""
        INSERT INTO action_card (
            symbol, expiry, card_state, action_verb, strike, option_type,
            recommended_expiry, lot_count, lot_cost_inr,
            stop_type, stop_level, stop_spot_anchor, stop_pct,
            exit_time, target_1, target_2,
            block_reason, avoid_reason, wait_reason,
            wait_valid_until, wait_is_phase_duration, previous_card_state,
            warning_line_1, warning_line_2, warning_line_correlated,
            why_line, warning_key_1, warning_key_2,
            source_trap_score, source_confidence_pct, computed_at
        ) VALUES (
            :symbol, :expiry, :card_state, :action_verb, :strike, :option_type,
            :recommended_expiry, :lot_count, :lot_cost_inr,
            :stop_type, :stop_level, :stop_spot_anchor, :stop_pct,
            :exit_time, :target_1, :target_2,
            :block_reason, :avoid_reason, :wait_reason,
            :wait_valid_until, :wait_is_phase_duration, :previous_card_state,
            :warning_line_1, :warning_line_2, :warning_line_correlated,
            :why_line, :warning_key_1, :warning_key_2,
            :source_trap_score, :source_confidence_pct, :computed_at
        )
    """, row)
    conn.commit()
    conn.close()


def _get_prev_card_state(symbol) -> Optional[str]:
    conn = get_connection()
    row = conn.execute(
        "SELECT card_state FROM action_card WHERE symbol=? ORDER BY computed_at DESC LIMIT 1",
        (symbol,)
    ).fetchone()
    conn.close()
    return row["card_state"] if row else None


def _minutes_diff(t1, t2) -> int:
    try:
        h1, m1 = map(int, t1.split(":"))
        h2, m2 = map(int, t2.split(":"))
        return (h2*60+m2) - (h1*60+m1)
    except Exception:
        return 999


def _fmt_exit_time(t) -> str:
    try:
        h, m = map(int, t.split(":"))
        suffix = "AM" if h < 12 else "PM"
        h12 = h if h <= 12 else h - 12
        return f"{h12}:{m:02d} {suffix}"
    except Exception:
        return t
