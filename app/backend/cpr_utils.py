import os
import json
import time
import logging
import threading
from datetime import datetime, date, timedelta, time as dt_time

log = logging.getLogger("CPRUtils")

CPR_CACHE_FILE = os.path.join(os.path.dirname(__file__), "cpr_cache.json")
_cpr_cache = {}
_cpr_lock = threading.Lock()


def _load_cpr_cache():
    global _cpr_cache
    if os.path.exists(CPR_CACHE_FILE):
        try:
            with open(CPR_CACHE_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    with _cpr_lock:
                        _cpr_cache.update(data)
                    log.info(f"[CPRUtils] Loaded {len(data)} CPR pivots from disk cache.")
        except Exception as e:
            log.warning(f"[CPRUtils] Failed to load CPR cache: {e}")


def _save_cpr_cache():
    try:
        with _cpr_lock:
            data = dict(_cpr_cache)
        with open(CPR_CACHE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        log.warning(f"[CPRUtils] Failed to save CPR cache: {e}")


# Auto-load disk cache on module import
_load_cpr_cache()


def get_current_session_date() -> str:
    """Returns ISO string YYYY-MM-DD for current session date (today if weekday, last Friday if weekend)."""
    today = date.today()
    if today.weekday() == 5:    # Saturday
        s_date = today - timedelta(days=1)
    elif today.weekday() == 6:  # Sunday
        s_date = today - timedelta(days=2)
    else:
        s_date = today
    return s_date.isoformat()


def calculate_cpr(pdh: float, pdl: float, pdc: float):
    """
    Pure mathematical calculation of Central Pivot Range (CPR):
    Pivot = (PDH + PDL + PDC) / 3
    BC    = (PDH + PDL) / 2
    TC    = 2 * Pivot - BC
    (If TC < BC, swap TC and BC so TC is the upper boundary)
    """
    if pdh <= 0 or pdl <= 0 or pdc <= 0:
        return None
    pivot = (pdh + pdl + pdc) / 3.0
    bc    = (pdh + pdl) / 2.0
    tc    = 2.0 * pivot - bc
    if tc < bc:
        tc, bc = bc, tc
    return {
        "pdh":   round(pdh, 2),
        "pdl":   round(pdl, 2),
        "pdc":   round(pdc, 2),
        "pivot": round(pivot, 2),
        "bc":    round(bc, 2),
        "tc":    round(tc, 2),
    }


def get_cpr_pivots(symbol: str):
    """Returns cached CPR pivots dict for symbol if valid for current session, or None."""
    cur_session = get_current_session_date()
    with _cpr_lock:
        item = _cpr_cache.get(symbol.upper()) or _cpr_cache.get(symbol)
        if item:
            # Guardrail: Check session date invalidation
            item_session = item.get("session_date")
            if item_session and item_session == cur_session:
                return item
            # Stale record from older session date
            return None
        return None


def compute_cpr_flags(spot_open: float, spot_ltp: float, tc: float, pivot: float, bc: float):
    """
    Evaluates the 6 Top/Bottom Y/N flags based on pure mathematical comparisons:
    TC:    Top (Open > TC),      Bottom (Spot >= TC)
    Pivot: Top (Open > Pivot),   Bottom (Spot >= Pivot)
    BC:    Top (Open < BC),      Bottom (Spot >= BC)
    """
    open_val = spot_open if spot_open > 0 else spot_ltp
    if open_val <= 0 or spot_ltp <= 0 or tc is None or pivot is None or bc is None:
        return None

    tc_top  = "Y" if open_val > tc else "N"
    tc_bot  = "Y" if spot_ltp >= tc else "N"

    piv_top = "Y" if open_val > pivot else "N"
    piv_bot = "Y" if spot_ltp >= pivot else "N"

    bc_top  = "Y" if open_val < bc else "N"
    bc_bot  = "Y" if spot_ltp >= bc else "N"

    return {
        "tc":  f"{tc_top}/{tc_bot}",
        "piv": f"{piv_top}/{piv_bot}",
        "bc":  f"{bc_top}/{bc_bot}",
    }


def _get_previous_day_bar_for_cpr(hist):
    """
    Returns the bar to use as CPR reference (previous trading day's OHLC).

    CPR for a session = previous trading day's OHLC. Logic:
    - Weekday (trading day / pre-market / holiday): ref_date = today
      → returns last bar strictly before today (e.g. Monday → Friday)
    - Weekend (Sat/Sun): last session = hist[-1] (e.g. Friday)
      → returns bar before that session (e.g. Friday → Thursday)

    Works correctly at all times: pre-market, intraday, post-market,
    weekends, and market holidays — no clock-time checks needed.
    """
    if not hist:
        return None

    today = date.today()
    is_weekend = today.weekday() >= 5  # 5=Saturday, 6=Sunday

    if is_weekend:
        # Last completed session = hist[-1] (e.g. Friday)
        # CPR for that session = bar before Friday = Thursday
        last_bar_date = hist[-1].get("date")
        if hasattr(last_bar_date, "date"):
            last_bar_date = last_bar_date.date()
        ref_date = last_bar_date
    else:
        # Weekday: CPR for today's session = bar before today
        ref_date = today

    for bar in reversed(hist):
        bar_date = bar.get("date")
        if hasattr(bar_date, "date"):
            bar_date = bar_date.date()
        if bar_date < ref_date:
            return bar
    return None


def warm_cpr_pivots_bg(kite, spot_tokens_map: dict):
    """
    Background worker that fetches previous day's OHLC from Kite for missing/stale symbols
    and populates the CPR cache + disk with active session guardrails.
    """
    cur_session = get_current_session_date()

    def _worker():
        with _cpr_lock:
            # Identify missing OR stale symbols whose session_date != cur_session
            missing = {}
            for tok, sym in spot_tokens_map.items():
                cached_item = _cpr_cache.get(sym.upper()) or _cpr_cache.get(sym)
                if not cached_item or cached_item.get("session_date") != cur_session:
                    missing[tok] = sym

        if not missing:
            return

        today   = date.today()
        from_dt = today - timedelta(days=7)
        to_dt   = today

        log.info(f"[CPRUtils] Warming CPR pivots for {len(missing)} missing/stale symbols (Session: {cur_session})...")
        updated = False
        for tok, sym in missing.items():
            try:
                hist = kite.historical_data(tok, from_dt, to_dt, "day")
                last = _get_previous_day_bar_for_cpr(hist)

                if last:
                    pdh = float(last.get("high") or 0)
                    pdl = float(last.get("low") or 0)
                    pdc = float(last.get("close") or 0)

                    # Guardrails on price sanity
                    if pdh > 0 and pdl > 0 and pdc > 0 and pdh >= pdl:
                        cpr = calculate_cpr(pdh, pdl, pdc)
                        if cpr:
                            ref_bar_date = last.get("date")
                            ref_date_str = ref_bar_date.strftime("%Y-%m-%d") if hasattr(ref_bar_date, 'strftime') else str(ref_bar_date)[:10]
                            cpr["ref_date"] = ref_date_str
                            cpr["session_date"] = cur_session
                            with _cpr_lock:
                                _cpr_cache[sym.upper()] = cpr
                            updated = True
                time.sleep(0.35)
            except Exception as e:
                log.warning(f"[CPRUtils] Failed to fetch history for {sym}: {e}")
                time.sleep(1.0)

        if updated:
            _save_cpr_cache()
            log.info(f"[CPRUtils] Finished warming CPR pivots. Total cached for session {cur_session}: {len(_cpr_cache)}")

    threading.Thread(target=_worker, daemon=True).start()
