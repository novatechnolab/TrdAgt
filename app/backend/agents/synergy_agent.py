"""
Synergy Agent for TradeSignal Agentic Framework.
Bridges to the real synergy_scanner module to observe F&O Synergy BUY-class
profile transitions and emit signals to 'signals/synergy/{symbol}'.
"""

import logging
import time
from typing import Any, Dict
from .base_agent import BaseAgent

logger = logging.getLogger(__name__)


class SynergyAgent(BaseAgent):
    """
    Autonomous agent bridging to the real F&O Synergy Scanner.
    Polls synergy_scanner.get_buy_alerts() each tick cycle and emits bus
    signals only when a BUY-class profile state *transitions* for a symbol.
    Zero duplication of detection logic — the real scanner owns all computation.
    """

    def __init__(self, name: str = "SynergyAgent", bus=None):
        super().__init__(name=name, bus=bus)
        self.symbol_profiles: Dict[str, str] = {}  # symbol -> last seen profile
        self.signals_emitted = 0

    def on_start(self):
        """No tick subscription needed — we poll real scanner state in on_tick()."""
        logger.info(f"[{self.name}] Started. Bridging to synergy_scanner.get_buy_alerts().")

    def on_tick(self):
        """
        Poll real synergy_scanner BUY-class alerts every ~200ms cycle during market hours.
        Emits a signal on the bus when a symbol transitions to a new BUY-class profile.
        """
        try:
            from session_utils import is_market_hours
            if not is_market_hours():
                return

            from synergy_scanner import get_buy_alerts
            alerts = get_buy_alerts()
        except Exception:
            return  # scanner not yet started or import error — silent

        for symbol, result in alerts.items():
            new_profile = result.get("synergy_profile", "")
            prev_profile = self.symbol_profiles.get(symbol, "")

            if new_profile and new_profile != prev_profile:
                self.symbol_profiles[symbol] = new_profile
                self.signals_emitted += 1

                signal_payload = {
                    "symbol": symbol,
                    "profile": new_profile,
                    "action": result.get("action", ""),
                    "is_buy_signal": result.get("is_buy_signal", True),
                    "ltp": result.get("spot_ltp", 0.0),
                    "setup_type": "SYNERGY_BUY",
                    "direction": "BULLISH",
                    "conviction": 90,
                    "agent_name": self.name,
                    "timestamp": time.time(),
                }
                self.send(topic=f"signals/synergy/{symbol}", payload=signal_payload)
                logger.info(
                    f"[{self.name}] Synergy profile transition for {symbol}: "
                    f"'{prev_profile}' → '{new_profile}'"
                )
