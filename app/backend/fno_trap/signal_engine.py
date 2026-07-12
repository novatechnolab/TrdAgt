"""
fno_trap/signal_engine.py
§5 Signal Engine — trap score, PCR, OI heatmap, max pain, VWAP, pivots.
Adapted from FNO Trap Dashboard/signal_engine.py for TradeSignal integration.
"""
import logging
import statistics
from datetime import date, datetime
from typing import Optional

from fno_trap.db import get_connection
from fno_trap.time_phase import now_ist, get_time_phase, trading_days_to_expiry, get_phase_multiplier

log = logging.getLogger(__name__)


# ── OI helpers ────────────────────────────────────────────────────────────

def _get_latest_oi(symbol: str, expiry: date) -> list:
    conn = get_connection()
    rows = conn.execute("""
        SELECT strike, option_type, oi, oi_change, volume, ltp, bid, ask
        FROM oi_snapshots
        WHERE symbol=? AND expiry=?
          AND snapshot_time >= datetime('now', '-12 minutes')
        ORDER BY strike ASC
    """, (symbol, expiry.isoformat())).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _split_chain(oi_rows):
    ce = sorted([r for r in oi_rows if r["option_type"] == "CE"], key=lambda x: x["strike"])
    pe = sorted([r for r in oi_rows if r["option_type"] == "PE"], key=lambda x: x["strike"], reverse=True)
    return ce, pe


# ── §6 Max Pain ───────────────────────────────────────────────────────────

def compute_max_pain(oi_rows: list) -> Optional[float]:
    if not oi_rows:
        return None
    strikes = sorted(set(r["strike"] for r in oi_rows))
    oi_map = {(r["strike"], r["option_type"]): r.get("oi", 0) or 0 for r in oi_rows}
    min_pain, mp_strike = float("inf"), None
    for s in strikes:
        pain = 0
        for r in oi_rows:
            k = r["strike"]
            oi = oi_map.get((k, r["option_type"]), 0)
            pain += max(0, s - k) * oi if r["option_type"] == "CE" else max(0, k - s) * oi
        if pain < min_pain:
            min_pain, mp_strike = pain, s
    return mp_strike


# ── §3.4 PCR ─────────────────────────────────────────────────────────────

def compute_pcr(ce_rows, pe_rows) -> dict:
    total_ce_oi  = sum(r.get("oi", 0) or 0 for r in ce_rows)
    total_pe_oi  = sum(r.get("oi", 0) or 0 for r in pe_rows)
    total_ce_vol = sum(r.get("volume", 0) or 0 for r in ce_rows)
    total_pe_vol = sum(r.get("volume", 0) or 0 for r in pe_rows)
    pcr_oi  = round(total_pe_oi / total_ce_oi, 3)  if total_ce_oi  else 0
    pcr_vol = round(total_pe_vol / total_ce_vol, 3) if total_ce_vol else 0
    return {"pcr_oi": pcr_oi, "pcr_vol": pcr_vol,
            "total_ce_oi": total_ce_oi, "total_pe_oi": total_pe_oi}


# ── §5.3 Trap score ───────────────────────────────────────────────────────

def compute_trap_score(ce_rows: list, pe_rows: list, spot: float, max_pain: float) -> dict:
    """
    §5.3 Compute directional trap score (0-100) and direction.
    PUT_BUYER_TRAP = bullish signal (put writers trapped, expect rise).
    CALL_BUYER_TRAP = bearish signal (call writers trapped, expect fall).
    """
    if not ce_rows or not pe_rows or not spot:
        return {"trap_score": 0, "trap_direction": None, "trap_score_trend": "FLAT"}

    # Find ATM ± 2 strikes
    all_strikes = sorted(set(r["strike"] for r in ce_rows + pe_rows))
    atm_idx = min(range(len(all_strikes)), key=lambda i: abs(all_strikes[i] - spot))
    near_range = all_strikes[max(0, atm_idx-3):atm_idx+4]

    atm_ce_oi = sum(r.get("oi", 0) or 0 for r in ce_rows if r["strike"] in near_range)
    atm_pe_oi = sum(r.get("oi", 0) or 0 for r in pe_rows if r["strike"] in near_range)

    total_near = atm_ce_oi + atm_pe_oi
    if total_near == 0:
        return {"trap_score": 0, "trap_direction": None, "trap_score_trend": "FLAT"}

    # Score = dominance of one side near ATM
    put_dominance  = atm_pe_oi / total_near   # PUT_BUYER_TRAP signal
    call_dominance = atm_ce_oi / total_near

    # Max pain proximity bonus (spot near max_pain = pinning risk)
    mp_bonus = 0
    if max_pain and abs(spot - max_pain) / (spot + 1) < 0.005:
        mp_bonus = 10

    if put_dominance > call_dominance:
        raw = int(put_dominance * 80) + mp_bonus
        direction = "PUT_BUYER_TRAP"
    else:
        raw = int(call_dominance * 80) + mp_bonus
        direction = "CALL_BUYER_TRAP"

    trap_score = min(raw, 100)

    # Trend: compare vs prior DB snapshot
    conn = get_connection()
    prev = conn.execute(
        "SELECT trap_score FROM signal_snapshot WHERE symbol=? ORDER BY computed_at DESC LIMIT 1",
        (ce_rows[0]["strike"] if ce_rows else "NIFTY",)  # fallback
    ).fetchone()
    conn.close()
    trend = "FLAT"
    if prev:
        delta = trap_score - (prev["trap_score"] or 0)
        if delta > 5:
            trend = "RISING"
        elif delta < -5:
            trend = "FALLING"

    return {"trap_score": trap_score, "trap_direction": direction, "trap_score_trend": trend}


# ── §5.6 OI heatmap rows ─────────────────────────────────────────────────

def build_oi_heatmap(ce_rows: list, pe_rows: list, spot: float, n: int = 8) -> list:
    """
    Return top-N ATM strikes with CE/PE OI (in Lakh) for frontend heatmap.
    hot_ce / hot_pe = highest OI on that side.
    """
    all_strikes = sorted(set(
        [r["strike"] for r in ce_rows] + [r["strike"] for r in pe_rows]
    ))
    if not all_strikes or not spot:
        return []
    atm_idx = min(range(len(all_strikes)), key=lambda i: abs(all_strikes[i] - spot))
    selected = all_strikes[max(0, atm_idx - n // 2): atm_idx + n // 2 + 1]

    ce_map = {r["strike"]: (r.get("oi", 0) or 0) for r in ce_rows}
    pe_map = {r["strike"]: (r.get("oi", 0) or 0) for r in pe_rows}

    rows = []
    for s in selected:
        ce_oi = ce_map.get(s, 0)
        pe_oi = pe_map.get(s, 0)
        rows.append({
            "strike": f"{int(s):,}",
            "ce":     round(ce_oi / 100000, 1),
            "pe":     round(pe_oi / 100000, 1),
            "hot_ce": False,
            "hot_pe": False,
        })

    if rows:
        max_ce = max(r["ce"] for r in rows)
        max_pe = max(r["pe"] for r in rows)
        for r in rows:
            r["hot_ce"] = r["ce"] == max_ce and max_ce > 0
            r["hot_pe"] = r["pe"] == max_pe and max_pe > 0

    return rows


# ── §5.4 VWAP (approximate from OI price proxy) ──────────────────────────

def compute_vwap_proxy(oi_rows: list, spot: float) -> Optional[float]:
    """Approximate VWAP using volume-weighted option LTPs as proxy."""
    if not oi_rows:
        return None
    total_vol = sum(r.get("volume", 0) or 0 for r in oi_rows)
    if total_vol == 0:
        return spot  # fallback
    vwap = sum((r.get("ltp", 0) or 0) * (r.get("volume", 0) or 0) for r in oi_rows) / total_vol
    # Scale proxy VWAP back to spot domain (crude approximation)
    return round(spot, 2)  # TODO: use intraday OHLCV for real VWAP


# ── §5.7 Pivot levels (classic floor pivots) ─────────────────────────────

def compute_pivots(spot: float) -> dict:
    """
    Approximate daily pivots from spot ± typical ATR.
    Real implementation needs previous day OHLC from Kite historical API.
    """
    atr_approx = spot * 0.007  # ~0.7% typical intraday ATR
    pp = round(spot, 2)
    r1 = round(pp + atr_approx, 2)
    r2 = round(pp + 2 * atr_approx, 2)
    s1 = round(pp - atr_approx, 2)
    s2 = round(pp - 2 * atr_approx, 2)
    return {"pivot": pp, "r1": r1, "r2": r2, "s1": s1, "s2": s2}


# ── §5.5 DTE rollover score ───────────────────────────────────────────────

def compute_rollover_score(oi_rows: list, expiry: date) -> float:
    """
    Approximate rollover activity as % of total OI that is low-volume
    (position carry vs. new trades). High score = lots of carryover.
    """
    dte = trading_days_to_expiry(expiry)
    if dte <= 1:
        return 90.0
    if dte <= 3:
        return 60.0
    if dte <= 5:
        return 30.0
    return 10.0


# ── §12.2 Confidence ─────────────────────────────────────────────────────

def compute_confidence(trap_score, phase, dte, candle_status, pcr_divergence_pct, rollover_score) -> int:
    confidence = trap_score * get_phase_multiplier(phase)
    if dte == 0:
        confidence *= 0.0
    elif dte == 1:
        confidence *= 0.70
    elif dte > 14:
        confidence *= 0.60
    if candle_status == "CONFIRMED":
        confidence *= 1.10
    elif candle_status == "UNCONFIRMED":
        confidence *= 0.80
    if pcr_divergence_pct and pcr_divergence_pct > 30:
        confidence *= 0.90
    if rollover_score and rollover_score > 60:
        confidence *= 0.88
    return min(int(confidence), 100)


# ── §18 RET modules ───────────────────────────────────────────────────────

def compute_data_confidence(oi_age_minutes: int) -> tuple:
    score = 100
    if oi_age_minutes > 10:
        score -= 40
    elif oi_age_minutes > 5:
        score -= 20
    elif oi_age_minutes > 2:
        score -= 10
    dot = "green" if score >= 80 else "amber" if score >= 50 else "red"
    return score, dot


def compute_survivability(ltp: float, wap: float, dte: int) -> int:
    if not ltp or not wap or dte <= 0:
        return 99
    daily_theta = ltp * 0.04 * (1 / max(dte, 1))
    per_snap = daily_theta / 72
    if per_snap <= 0:
        return 99
    return min(int(ltp / per_snap), 99)


def compute_psi(spot, ltp, atm_ltp) -> str:
    if not atm_ltp or atm_ltp == 0:
        return "NORMAL"
    ratio = ltp / atm_ltp if ltp else 0
    if ratio > 2.5:
        return "PSI_BLOCK"
    elif ratio > 1.8:
        return "PSI_WARN"
    return "NORMAL"


def compute_crowding(symbol, strike, option_type) -> int:
    conn = get_connection()
    row = conn.execute("""
        SELECT oi, volume FROM oi_snapshots
        WHERE symbol=? AND strike=? AND option_type=?
          AND snapshot_time >= datetime('now', '-12 minutes')
        ORDER BY snapshot_time DESC LIMIT 1
    """, (symbol, strike, option_type)).fetchone()
    conn.close()
    if not row or not row["oi"] or row["oi"] == 0:
        return 0
    return min(int((row["volume"] or 0) / row["oi"] * 50), 100)


def compute_stop(ltp, direction, spot, s1, r1) -> dict:
    stop_level = round(ltp * 0.68, 1) if ltp else None
    spot_anchor = s1 if direction == "PUT_BUYER_TRAP" else r1
    return {"stop_level": stop_level, "stop_spot_anchor": spot_anchor, "stop_pct": -32}


def compute_exit_time(phase) -> str:
    return "15:14" if phase in ("ACTIVE", "DEAD_ZONE") else "15:00"


def compute_lots(account_size_inr, lot_cost_inr, confidence_pct) -> int:
    if not lot_cost_inr or lot_cost_inr == 0:
        return 1
    max_risk = account_size_inr * 0.02
    lots = max(1, int(max_risk / lot_cost_inr))
    if confidence_pct < 75:
        lots = 1
    return lots


def compute_plain_language_why(trap_direction, trap_score, pcr_oi) -> str:
    templates_put = [
        "Put writers positioned defensively; market may squeeze higher.",
        "Put OI concentration near ATM — sellers trapped; potential upside.",
        "Strong put-side OI dominance; market trapped put buyers; bullish bias.",
        "Heavy put writing near support; sellers in control — bullish conviction.",
    ]
    templates_call = [
        "Call writers covering downside; market may drift lower.",
        "Call OI concentration near ATM — sellers trapped; potential downside.",
        "Strong call-side OI dominance; market trapped call buyers; bearish bias.",
        "Heavy call writing near resistance; sellers in control — bearish conviction.",
    ]
    templates = templates_put if trap_direction == "PUT_BUYER_TRAP" else templates_call
    idx = min(trap_score // 25, len(templates) - 1)
    return templates[idx]


def check_discipline_gate(session_date=None) -> dict:
    from datetime import date as Date
    if session_date is None:
        session_date = now_ist().date()
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM session_context WHERE session_date=?",
        (session_date.isoformat(),)
    ).fetchone()
    conn.close()
    if not row:
        return {"blocked": False, "reason": None, "cooldown_active": False, "cooldown_until": None}
    now = now_ist()
    result = {"blocked": False, "reason": None, "cooldown_active": False, "cooldown_until": None}
    if row["cooldown_until"]:
        try:
            cd = datetime.fromisoformat(row["cooldown_until"])
            if now < cd:
                result.update({"blocked": True, "reason": "Discipline block: cooldown active",
                                "cooldown_active": True, "cooldown_until": cd.strftime("%I:%M %p")})
                return result
        except Exception:
            pass
    if (row["daily_loss_so_far"] or 0) >= (row["max_daily_loss_inr"] or 5000):
        result.update({"blocked": True, "reason": "Discipline block: daily loss limit reached"})
        return result
    if (row["consecutive_losses"] or 0) >= (row["max_consecutive_losses"] or 2):
        result.update({"blocked": True, "reason": "Discipline block: consecutive losses — take a break"})
        return result
    return result
