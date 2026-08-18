"""
EMA Convergence Watchlist Agent for TradeSignal Agentic Framework.

Scans all F&O symbols from get_ema_crossover_state() every 30 seconds and
ranks them by convergence score — how close EMA 9 and EMA 21 are to crossing.

Scoring Formula (5-minute candles only):
  gap_score   (40%) = how close EMAs are now relative to session baseline (2% ceiling)
  slope_score (60%) = rate at which EMA9 gap is narrowing toward EMA21 (leading indicator)

  combined = gap_score * 0.40 + slope_score * 0.60
  + modifiers: squeeze bonus (+15), collision zone bonus (+20), diverging cap (30)

Output: top 50 symbols sorted descending by convergence score.
Both bear_setup (bull->bear cross approaching) and bull_setup (bear->bull) are included.
"""

import logging
import time
import threading
from typing import Any, Dict, List, Optional
from .base_agent import BaseAgent

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
REFRESH_INTERVAL_SEC = 30      # re-score every 30s
TOP_N                = 50      # return top 50 by score
COLLISION_THRESHOLD  = 0.15    # % gap — in-zone bonus trigger (matches scanner)
SLOPE_NORMALISER     = 500     # scaling constant: gap_delta_pct * N -> slope_score
MAX_GAP_PCT          = 2.0     # gaps wider than this score ~0 (session ceiling)


def _score_symbol(symbol: str, data: Dict[str, Any],
                  prev_gap: Optional[float]) -> Optional[Dict[str, Any]]:
    """
    Compute convergence score for one symbol from its scanner data dict.
    prev_gap: gap_pct from previous cycle (enables slope calculation).
    Returns a scored record dict, or None if data is insufficient.
    """
    try:
        # ── Extract squeeze data (pre-computed by scanner on 5m candles) ─────
        squeeze      = data.get("squeeze") or {}
        current_gap  = squeeze.get("ema_gap", None)   # |EMA9-EMA21|/LTP * 100
        in_squeeze   = squeeze.get("in_squeeze", False)

        if current_gap is None or current_gap < 0:
            return None

        ltp = data.get("last_candle_close") or 0.0
        if ltp <= 0:
            return None

        trend_5m = data.get("state_5m", "neutral")   # 'bullish'|'bearish'|'neutral'
        cross_5m = data.get("cross_5m", "none")       # 'bullish'|'bearish'|'none'

        # ── Gap Score (40%) ───────────────────────────────────────────────────
        # Position vs 2% ceiling (session baseline approximation).
        gap_score = max(0.0, 100.0 - (current_gap / MAX_GAP_PCT) * 100.0)

        # ── Slope Score (60%) — leading indicator ─────────────────────────────
        # gap_delta < 0 means gap narrowing (converging) → high slope_score.
        if prev_gap is not None:
            gap_delta = current_gap - prev_gap          # negative = converging
            slope_score = max(0.0, min(100.0, -gap_delta * SLOPE_NORMALISER))
        else:
            slope_score = gap_score   # bootstrap: no history yet, mirror gap score
            gap_delta   = None

        # ── Modifiers ─────────────────────────────────────────────────────────
        bonus = 0.0
        if in_squeeze:
            bonus += 15.0
        in_collision = current_gap < COLLISION_THRESHOLD
        if in_collision:
            bonus += 20.0

        # ── Combined Score ────────────────────────────────────────────────────
        combined = (gap_score * 0.40) + (slope_score * 0.60) + bonus

        # Cap already-crossed-and-diverging stocks
        if cross_5m != "none":
            final_score = min(30.0, combined)
        else:
            final_score = min(100.0, combined)

        # ── Direction classification ──────────────────────────────────────────
        # bear_setup: bullish trend (EMA9 > EMA21) but gap closing → near bearish cross
        # bull_setup: bearish trend (EMA9 < EMA21) but gap closing → near bullish cross
        if trend_5m == "bullish":
            direction = "bear_setup"
        elif trend_5m == "bearish":
            direction = "bull_setup"
        else:
            direction = "neutral"

        return {
            "symbol":       symbol,
            "score":        round(final_score, 2),
            "gap_pct":      round(current_gap, 4),
            "gap_delta":    round(gap_delta, 5) if gap_delta is not None else None,
            "gap_score":    round(gap_score, 2),
            "slope_score":  round(slope_score, 2),
            "direction":    direction,
            "trend_5m":     trend_5m,
            "cross_5m":     cross_5m,
            "in_squeeze":   in_squeeze,
            "in_collision": in_collision,
            "ltp":          round(ltp, 2),
            "alignment":    data.get("alignment", "mixed"),
            "ema9_hold":    data.get("ema9_hold"),
        }

    except Exception as exc:
        logger.debug("[EMAConvergenceAgent] Score failed for %s: %s", symbol, exc)
        return None


class EMAConvergenceAgent(BaseAgent):
    """
    Autonomous agent that ranks all F&O symbols by EMA 9/21 convergence score.

    Data source: get_ema_crossover_state() — ZERO new Kite API calls.
    Publishes top-50 ranked list to 'watchlist/ema_convergence' on the MessageBus.
    Exposes get_watchlist() for direct REST access from server.py.
    """

    def __init__(self, name: str = "EMAConvergenceAgent", bus=None):
        super().__init__(name=name, bus=bus)
        self._watchlist: List[Dict[str, Any]] = []
        self._watchlist_lock  = threading.Lock()
        self._prev_gap: Dict[str, float] = {}     # symbol -> gap_pct from last cycle
        self._last_score_time = 0.0
        self._cycles          = 0

    # ── Public API ─────────────────────────────────────────────────────────────

    def get_watchlist(self) -> List[Dict[str, Any]]:
        """Thread-safe snapshot of the current ranked watchlist."""
        with self._watchlist_lock:
            return list(self._watchlist)

    def get_stats(self) -> Dict[str, Any]:
        """Diagnostic summary for health endpoint."""
        with self._watchlist_lock:
            count = len(self._watchlist)
            top   = self._watchlist[0] if self._watchlist else None
        return {
            "cycles":        self._cycles,
            "watchlist_size": count,
            "top_symbol":    top["symbol"] if top else None,
            "top_score":     top["score"] if top else None,
        }

    # ── Agent Lifecycle ────────────────────────────────────────────────────────

    def on_start(self):
        logger.info(
            "[%s] Started. Re-score every %ds, publishing top-%d.",
            self.name, REFRESH_INTERVAL_SEC, TOP_N
        )

    def on_tick(self):
        """Called every ~200ms by BaseAgent loop; self-throttles to REFRESH_INTERVAL_SEC."""
        if time.time() - self._last_score_time < REFRESH_INTERVAL_SEC:
            return
        self._last_score_time = time.time()
        self._run_scoring_cycle()

    # ── Core Scoring Logic ─────────────────────────────────────────────────────

    def _run_scoring_cycle(self):
        """Score all symbols, rank, store top-50, publish to bus."""
        try:
            from ema_crossover_scanner import get_ema_crossover_state
            state      = get_ema_crossover_state()
            crossovers = state.get("crossovers", {})

            if not crossovers:
                logger.debug("[%s] No scanner data yet — skipping cycle.", self.name)
                return

            scored: List[Dict[str, Any]] = []
            new_gaps: Dict[str, float]   = {}

            for symbol, data in crossovers.items():
                prev_gap = self._prev_gap.get(symbol)
                record   = _score_symbol(symbol, data, prev_gap)
                if record is None:
                    continue
                new_gaps[symbol] = record["gap_pct"]
                scored.append(record)

            # Persist gap history for slope calculation next cycle
            self._prev_gap = new_gaps

            # Sort descending, top 50
            scored.sort(key=lambda r: r["score"], reverse=True)
            top50 = scored[:TOP_N]
            for i, r in enumerate(top50, start=1):
                r["rank"] = i

            with self._watchlist_lock:
                self._watchlist = top50

            self._cycles += 1

            # Publish to MessageBus
            ts = _now_ist_str()
            payload = {
                "watchlist":      top50,
                "total_scored":   len(scored),
                "cycle":          self._cycles,
                "timestamp":      ts,
                "scanner_status": state.get("status", "unknown"),
            }
            self.send(topic="watchlist/ema_convergence", payload=payload)
            logger.info(
                "[%s] Cycle %d | scored=%d | top50 leader: %s (score=%.1f)",
                self.name, self._cycles, len(scored),
                top50[0]["symbol"] if top50 else "—",
                top50[0]["score"]  if top50 else 0.0,
            )

        except Exception as exc:
            logger.exception("[%s] Scoring cycle error: %s", self.name, exc)


def _now_ist_str() -> str:
    """Return current IST timestamp as a string."""
    try:
        from session_utils import now_ist
        return now_ist().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        import datetime
        return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
