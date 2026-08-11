"""
Kite Data Agent for TradeSignal Agentic Framework.
Centralized owner of Kite WebSocket tick streaming and REST rate limiting.
Broadcasts ticks to 'ticks/{symbol}' topics on the MessageBus.
Fix2: Also feeds synergy_scanner state machine from agent ticks (single WS).
Fix5B: Handles tick batches delivered via inbox (non-blocking from WS thread).
"""

import logging
import time
import threading
from typing import Any, Dict, List, Optional
from .base_agent import BaseAgent, AgentStatus

logger = logging.getLogger(__name__)


class RateLimiter:
    """Token bucket rate limiter to prevent hitting Kite API limits (max requests per sec)."""
    def __init__(self, rate_per_sec: float = 8.0):
        self.rate = rate_per_sec
        self.capacity = rate_per_sec
        self.tokens = rate_per_sec
        self.last_update = time.time()
        self._lock = threading.Lock()

    def acquire(self, block: bool = True) -> bool:
        while True:
            with self._lock:
                now = time.time()
                elapsed = now - self.last_update
                self.last_update = now
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)

                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return True

            if not block:
                return False
            time.sleep(0.05)


class KiteDataAgent(BaseAgent):
    """
    Agent responsible for streaming tick data and servicing quote requests.
    Prevents duplicate WebSocket connections and rate-limit violations across scanners.
    """

    def __init__(self, name: str = "KiteDataAgent", bus=None, requests_per_sec: float = 8.0):
        super().__init__(name=name, bus=bus)
        self.rate_limiter = RateLimiter(rate_per_sec=requests_per_sec)
        self.quote_cache: Dict[str, Dict[str, Any]] = {}
        self.token_to_symbol_map: Dict[int, str] = {}
        self.symbol_to_token_map: Dict[str, int] = {}
        self._cache_lock = threading.RLock()
        self.is_connected = False
        self.ticks_processed = 0

    def register_symbol_tokens(self, token_map: Dict[str, int]):
        """Register mapping between symbol names and Kite instrument tokens."""
        with self._cache_lock:
            for symbol, token in token_map.items():
                self.symbol_to_token_map[symbol] = token
                self.token_to_symbol_map[token] = symbol

    def handle_message(self, message: Any):
        """
        Fix5B: Process tick batches delivered via inbox from the WS callback thread.
        The WS thread uses non-blocking put_inbox(); this method does the actual work
        in KiteDataAgent's own daemon thread — zero latency on the WS callback.
        """
        if isinstance(message, dict) and message.get("type") == "ticks":
            self.on_tick_received(message["data"])

    def on_tick_received(self, ticks: List[Dict[str, Any]]):
        """
        Process a batch of Kite WebSocket ticks.
        Called from agent's own thread via handle_message(inbox pop).
        Fix2: Also feeds synergy_scanner._on_ticks() so its state machine
        stays populated without a separate WebSocket connection.
        """
        now = time.time()
        for tick in ticks:
            self.ticks_processed += 1
            token = tick.get("instrument_token")
            symbol = self.token_to_symbol_map.get(token, str(token))
            ltp = tick.get("last_price", 0.0)
            volume = tick.get("volume_traded", 0)
            oi = tick.get("oi", 0)

            tick_payload = {
                "symbol": symbol,
                "token": token,
                "ltp": ltp,
                "volume": volume,
                "oi": oi,
                "change": tick.get("change", 0.0),
                "ohlc": tick.get("ohlc", {}),
                "timestamp": tick.get("timestamp", now),
            }

            with self._cache_lock:
                self.quote_cache[symbol] = tick_payload

            # Publish tick to message bus (feeds EMAAgent, PredictionAgent etc.)
            self.send(topic=f"ticks/{symbol}", payload=tick_payload)

        # Fix2: Feed synergy_scanner state machine from agent ticks.
        # Keeps _synergy_results populated without a separate KiteWS connection.
        # synergy_scanner._on_ticks(ws, ticks) only uses the ticks param; ws=None is safe.
        try:
            from synergy_scanner import _on_ticks as _syn_on_ticks
            _syn_on_ticks(ws=None, ticks=ticks)
        except Exception:
            pass  # scanner not yet initialized or import error — silent

    def get_latest_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Return latest cached quote for a symbol."""
        with self._cache_lock:
            return self.quote_cache.get(symbol)

    def execute_rate_limited_api_call(self, func, *args, **kwargs):
        """Execute a Kite REST API call through the token bucket rate limiter."""
        self.rate_limiter.acquire(block=True)
        return func(*args, **kwargs)

    def inject_simulated_tick(self, symbol: str, ltp: float, volume: int = 1000, oi: int = 5000):
        """Inject a simulated tick (used during testing or off-market simulation)."""
        fake_tick = [{
            "instrument_token": self.symbol_to_token_map.get(symbol, 99999),
            "last_price": ltp,
            "volume_traded": volume,
            "oi": oi,
            "change": 0.5,
            "timestamp": time.time(),
        }]
        if symbol not in self.symbol_to_token_map:
            self.register_symbol_tokens({symbol: 99999})
        self.on_tick_received(fake_tick)

    def on_tick(self):
        """Periodic housekeeping."""
        pass
