"""
Prediction Agent for TradeSignal Agentic Framework.
Synthesizes multi-scanner signals into high-conviction trade setups using a 5-minute rolling confluence matrix.
Publishes unified prediction setups to 'alerts/prediction/{symbol}'.
"""

import logging
import time
from typing import Any, Dict, List
from .base_agent import BaseAgent

logger = logging.getLogger(__name__)


class PredictionAgent(BaseAgent):
    """
    Meta-agent that score-weights multi-scanner signal confluence.
    Combines votes from SynergyAgent, EMAAgent, FNOTrapAgent, and MarketAgent.
    """

    def __init__(self, name: str = "PredictionAgent", bus=None, window_seconds: float = 300.0):
        super().__init__(name=name, bus=bus)
        self.window_seconds = window_seconds  # 5-minute rolling window
        self.signal_store: Dict[str, List[Dict[str, Any]]] = {}  # symbol -> list of signal events
        self.market_bias = "NEUTRAL"
        self.predictions_emitted = 0

    def on_start(self):
        """Subscribe to all scanner signals and market context."""
        if self.bus:
            self.bus.subscribe("signals/*", self)
            self.bus.subscribe("signals/#", self)
            self.bus.subscribe("context/market_bias", self)
            logger.info(f"[{self.name}] Subscribed to 'signals/#' and 'context/market_bias'.")

    def handle_message(self, message: Any):
        """Process incoming signal or context updates."""
        if not hasattr(message, "topic"):
            return

        topic = message.topic
        payload = getattr(message, "payload", {})

        if topic == "context/market_bias":
            self.market_bias = payload.get("nifty_bias", "NEUTRAL")
            return

        if topic.startswith("signals/"):
            symbol = payload.get("symbol")
            if not symbol:
                return

            if symbol not in self.signal_store:
                self.signal_store[symbol] = []

            # Append signal to symbol store
            self.signal_store[symbol].append(payload)
            self._prune_and_evaluate(symbol)

    def _prune_and_evaluate(self, symbol: str):
        """Prune stale signals outside window_seconds and evaluate confluence."""
        now = time.time()
        # Keep signals within the 5-minute rolling window
        valid_signals = [
            sig for sig in self.signal_store[symbol]
            if now - sig.get("timestamp", now) <= self.window_seconds
        ]
        self.signal_store[symbol] = valid_signals

        if not valid_signals:
            return

        # Count direction votes
        bullish_sources = set()
        bearish_sources = set()
        setup_types = []

        for sig in valid_signals:
            setup = sig.get("setup_type", "UNKNOWN")
            setup_types.append(setup)
            direction = sig.get("direction") or ("BULLISH" if "BUY" in setup or "BULL" in setup else "BEARISH")
            
            sender = sig.get("agent_name") or sig.get("setup_type", "SCANNER").split("_")[0]
            if direction == "BULLISH":
                bullish_sources.add(sender)
            elif direction == "BEARISH":
                bearish_sources.add(sender)

        # Determine dominant direction and confluence score
        dominant_direction = "NEUTRAL"
        confluence_count = 0

        if len(bullish_sources) >= len(bearish_sources) and len(bullish_sources) > 0:
            dominant_direction = "BULLISH"
            confluence_count = len(bullish_sources)
        elif len(bearish_sources) > len(bullish_sources):
            dominant_direction = "BEARISH"
            confluence_count = len(bearish_sources)

        if dominant_direction == "NEUTRAL":
            return

        # Base score by number of agreeing independent scanner agents
        base_score = 60 if confluence_count == 1 else (75 if confluence_count == 2 else 90)

        # Bonus score if aligned with market context
        market_bonus = 10 if dominant_direction == self.market_bias else (0 if self.market_bias == "NEUTRAL" else -15)
        conviction_score = min(100, max(0, base_score + market_bonus))

        # Only emit prediction setup if conviction is HIGH (>= 75)
        if conviction_score >= 75:
            self.predictions_emitted += 1
            latest_price = 0.0
            target_1 = None
            target_2 = None
            stop_loss = None
            strike = None
            expiry = None
            why_notes = []

            for sig in reversed(valid_signals):
                p = float(sig.get("ltp") or 0.0)
                if p > 0 and latest_price == 0.0:
                    latest_price = p
                if not target_1 and sig.get("target_1"):
                    target_1 = sig.get("target_1")
                if not target_2 and sig.get("target_2"):
                    target_2 = sig.get("target_2")
                if not stop_loss and sig.get("stop_loss"):
                    stop_loss = sig.get("stop_loss")
                if not strike and sig.get("strike"):
                    strike = sig.get("strike")
                if not expiry and sig.get("expiry"):
                    expiry = sig.get("expiry")
                if sig.get("why"):
                    why_notes.append(sig.get("why"))

            agreeing_list = list(bullish_sources if dominant_direction == "BULLISH" else bearish_sources)
            base_rationale = f"Confluence of {confluence_count} scanner agent(s) ({', '.join(agreeing_list)}) aligned {dominant_direction} under {self.market_bias} market regime."
            full_rationale = f"{base_rationale} {' | '.join(why_notes)}" if why_notes else base_rationale

            prediction_payload = {
                "symbol": symbol,
                "direction": dominant_direction,
                "conviction_score": conviction_score,
                "confluence_count": confluence_count,
                "agreeing_agents": agreeing_list,
                "setup_types": list(set(setup_types)),
                "ltp": latest_price,
                "strike": strike,
                "expiry": expiry,
                "target_1": target_1,
                "target_2": target_2,
                "stop_loss": stop_loss,
                "market_context": self.market_bias,
                "timestamp": now,
                "rationale": full_rationale
            }

            self.send(topic=f"alerts/prediction/{symbol}", payload=prediction_payload)
            logger.info(f"[{self.name}] EMITTED PREDICTION SETUP for {symbol}: {dominant_direction} (Score: {conviction_score}%, LTP: {latest_price})")
