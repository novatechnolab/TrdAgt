"""
Market Agent for TradeSignal Agentic Framework.
Bridges to the real bias-score computation in server.py rather than
introducing new index-change threshold logic.
Publishes market context to 'context/market_bias'.
"""

import logging
import time
from typing import Any, Dict
from .base_agent import BaseAgent

logger = logging.getLogger(__name__)


class MarketAgent(BaseAgent):
    """
    Autonomous agent tracking market regime by polling the real
    compute_bias_score() / nifty_cache_get() from server.py.
    No new thresholds or business logic introduced — reads existing cache.
    """

    def __init__(self, name: str = "MarketAgent", bus=None):
        super().__init__(name=name, bus=bus)
        self.market_bias = "NEUTRAL"
        self.bias_score = 50
        self.nifty_ltp = 0.0
        self.last_update = time.time()
        self._last_published_bias = ""

    def on_start(self):
        """No tick subscription needed — we poll server bias cache in on_tick()."""
        logger.info(f"[{self.name}] Started. Bridging to server.compute_bias_score() cache.")

    def on_tick(self):
        """
        Poll the real bias-score cache from server.py every ~200ms cycle during market hours.
        Only publishes a market_bias context message when bias zone changes.
        Falls back to Nifty tick-based change if cache is empty (market just opened).
        """
        try:
            from session_utils import is_market_hours
            if not is_market_hours():
                return

            from server import nifty_cache_get, zone_for_score
            cached = nifty_cache_get("bias_score_data", max_age_seconds=60)
            if cached:
                score = cached.get("score", 50)
                zone = zone_for_score(score)
                new_bias = (
                    "BULLISH" if zone == "bullish" else
                    "BEARISH" if zone == "bearish" else
                    "NEUTRAL"
                )
                self.bias_score = score
                self.nifty_ltp = cached.get("nifty_ltp", self.nifty_ltp)
                self._publish_if_changed(new_bias)
        except Exception:
            pass  # server cache not populated yet — silent until data arrives

    def _publish_if_changed(self, new_bias: str):
        """Publish context/market_bias only when bias zone actually changes."""
        if new_bias != self._last_published_bias:
            self.market_bias = new_bias
            self._last_published_bias = new_bias
            self.last_update = time.time()
            context_payload = {
                "nifty_bias": self.market_bias,
                "bias_score": self.bias_score,
                "nifty_ltp": self.nifty_ltp,
                "timestamp": self.last_update,
            }
            self.send(topic="context/market_bias", payload=context_payload)
            logger.info(
                f"[{self.name}] Market context updated: "
                f"{self.market_bias} (Score: {self.bias_score})"
            )

    def set_manual_bias(self, bias: str, score: int = 80):
        """Helper to force market bias for testing or initialization."""
        self.market_bias = bias
        self.bias_score = score
        self._last_published_bias = bias
        context_payload = {
            "nifty_bias": self.market_bias,
            "bias_score": self.bias_score,
            "nifty_ltp": self.nifty_ltp,
            "timestamp": time.time(),
        }
        self.send(topic="context/market_bias", payload=context_payload)
