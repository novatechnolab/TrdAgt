"""
EMA Agent for TradeSignal Agentic Framework.
Fix3: Bridges to ema_crossover_scanner.get_ema_crossover_state() instead of
computing its own tick-based EMA. This ensures signals come from the real
candle-based scanner (EMA 9/21 crossover with volume confirmation) rather
than a noisy 50-tick LTP approximation.

Emits signals to 'signals/ema/{symbol}' only on confirmed new crossovers
detected by the production scanner.
"""

import logging
import time
from typing import Any, Dict
from .base_agent import BaseAgent

logger = logging.getLogger(__name__)


class EMAAgent(BaseAgent):
    """
    Autonomous agent wrapping EMA 9/21 crossover scanner results.
    Polls get_ema_crossover_state() every ~200ms for new crossover events
    and emits them as structured signals to the MessageBus.
    """

    def __init__(self, name: str = "EMAAgent", bus=None):
        super().__init__(name=name, bus=bus)
        # Track last emitted state per symbol to avoid duplicate signals
        self._last_emitted: Dict[str, str] = {}  # symbol -> "BULLISH"/"BEARISH"/"none"
        self.signals_emitted = 0

    def on_tick(self):
        """
        Poll the real EMA crossover scanner state every ~200ms.
        Emit a signal only when a new confirmed crossover is detected.
        """
        try:
            from ema_crossover_scanner import get_ema_crossover_state
            state = get_ema_crossover_state()
            crossovers = state.get("crossovers", {})
            if not crossovers:
                return

            for symbol, data in crossovers.items():
                # Use the scanner's multi-timeframe alignment field for direction
                # alignment: "bullish" | "bearish" | "mixed" | None
                alignment = (data.get("alignment") or "").lower()
                if alignment not in ("bullish", "bearish"):
                    continue

                direction = "BULLISH" if alignment == "bullish" else "BEARISH"

                # Only emit if direction changed since last signal for this symbol
                if self._last_emitted.get(symbol) == direction:
                    continue

                self._last_emitted[symbol] = direction
                self.signals_emitted += 1

                # Prefer 15m cross-over ltp; fall back to any available ltp field
                ltp = (data.get("ltp_15m") or data.get("ltp_1h")
                       or data.get("ltp_day") or data.get("ltp", 0.0))

                signal_payload = {
                    "symbol": symbol,
                    "direction": direction,
                    "ema9": data.get("ema9_15m", 0.0),
                    "ema21": data.get("ema21_15m", 0.0),
                    "ltp": float(ltp),
                    "conviction": 85,
                    "setup_type": f"EMA_{direction}_CROSSOVER",
                    "agent_name": self.name,
                    "alignment": alignment,
                    "state_15m": data.get("state_15m"),
                    "state_1h": data.get("state_1h"),
                    "timestamp": time.time(),
                }
                self.send(topic=f"signals/ema/{symbol}", payload=signal_payload)
                logger.info(
                    f"[{self.name}] EMA {direction} crossover for {symbol} "
                    f"(alignment={alignment}, ltp={ltp})"
                )

        except Exception as e:
            # Scanner may not be initialized yet (pre-market) — silent
            logger.debug(f"[{self.name}] on_tick poll skipped: {e}")
