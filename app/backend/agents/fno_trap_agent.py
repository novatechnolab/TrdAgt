"""
FNO Trap Agent for TradeSignal Agentic Framework.
Bridges to the real fno_trap.trap_engine module to observe trap card state
transitions and emit signals to 'signals/trap/{symbol}'.
"""

import logging
import time
from typing import Any, Dict
from .base_agent import BaseAgent

logger = logging.getLogger(__name__)


class FNOTrapAgent(BaseAgent):
    """
    Autonomous agent bridging to the real FNO Trap Engine.
    Polls fno_trap.trap_engine.get_cached_card() each tick cycle and emits
    bus signals only when a trap_direction state *transitions* for a symbol.
    Zero duplication of detection logic — the real trap engine owns all computation.
    """

    def __init__(self, name: str = "FNOTrapAgent", bus=None):
        super().__init__(name=name, bus=bus)
        self.trap_states: Dict[str, str] = {}  # symbol -> last seen trap_direction
        self.signals_emitted = 0

    def on_start(self):
        """No tick subscription needed — we poll real trap engine state in on_tick()."""
        logger.info(f"[{self.name}] Started. Bridging to fno_trap.trap_engine.get_cached_card().")

    def on_tick(self):
        """
        Poll real trap engine cached cards each ~200ms cycle.
        Emits a signal when a symbol's trap_direction transitions.
        """
        try:
            from fno_trap.trap_engine import _card_cache, _card_lock
            with _card_lock:
                cards_snapshot = dict(_card_cache)
        except ImportError:
            return  # trap engine not available — silent
        except Exception:
            return

        for symbol, card in cards_snapshot.items():
            new_direction = card.get("trap_direction") or card.get("trap_dir", "")
            if not new_direction:
                continue

            prev_direction = self.trap_states.get(symbol, "")

            if new_direction != prev_direction:
                self.trap_states[symbol] = new_direction
                self.signals_emitted += 1

                # Map trap engine direction to standard BULLISH/BEARISH convention
                # PUT_BUYER_TRAP → CE buy → BULLISH
                # CALL_BUYER_TRAP → PE buy → BEARISH
                direction = "BULLISH" if new_direction == "PUT_BUYER_TRAP" else "BEARISH"
                trap_score = card.get("trap_score", card.get("score", 80))
                ltp = card.get("spot_ltp", card.get("spot", 0.0))

                signal_payload = {
                    "symbol": symbol,
                    "trap_direction": new_direction,
                    "direction": direction,
                    "trap_score": trap_score,
                    "ltp": ltp,
                    "setup_type": "FNO_TRAP_CLEAR",
                    "agent_name": self.name,
                    "timestamp": time.time(),
                }
                self.send(topic=f"signals/trap/{symbol}", payload=signal_payload)
                logger.info(
                    f"[{self.name}] Trap direction transition for {symbol}: "
                    f"'{prev_direction}' → '{new_direction}' (Score: {trap_score})"
                )
