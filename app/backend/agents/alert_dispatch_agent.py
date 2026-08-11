"""
Alert Dispatch Agent for TradeSignal Agentic Framework.
Centralized router for all outward alerts (Telegram, Socket.IO web dashboards).
Enforces cross-scanner deduplication, symbol cooldowns, and high-conviction priority escalation.
"""

import logging
import queue
import time
from typing import Any, Dict, Optional
from .base_agent import BaseAgent

logger = logging.getLogger(__name__)


class AlertDispatchAgent(BaseAgent):
    """
    Autonomous agent handling alert deduplication, cooldowns, Telegram routing, and dashboard broadcasts.
    """

    def __init__(
        self,
        name: str = "AlertDispatchAgent",
        bus=None,
        telegram_token: Optional[str] = None,
        telegram_chat_id: Optional[str] = None,
        discord_webhook_url: Optional[str] = None,
        cooldown_seconds: float = 900.0  # 15-minute symbol cooldown
    ):
        super().__init__(name=name, bus=bus)
        self.telegram_token = telegram_token
        self.telegram_chat_id = telegram_chat_id
        self.discord_webhook_url = (discord_webhook_url or "").replace('discordapp.com', 'discord.com') or None
        self.cooldown_seconds = cooldown_seconds
        self.symbol_last_alert: Dict[str, float] = {}  # symbol -> timestamp of last dispatch
        self.dispatched_alerts: Dict[str, Any] = {}
        self.alerts_dispatched_count = 0
        self.alerts_suppressed_count = 0
        self.socketio = None

    def attach_socketio(self, socketio):
        """Attach Socket.IO server instance for real-time web dashboard emission."""
        if socketio is None:
            logger.warning(f"[{self.name}] attach_socketio called with None — skipping.")
            return
        self.socketio = socketio
        logger.info(f"[{self.name}] Socket.IO attached for real-time alert broadcasting.")

    def on_start(self):
        """Subscribe to all prediction and raw alert topics."""
        if self.bus:
            self.bus.subscribe("alerts/#", self)
            self.bus.subscribe("signals/#", self)
            logger.info(f"[{self.name}] Subscribed to 'alerts/#' and 'signals/#'.")

    def _run_loop(self):
        """
        Fix4: Override BaseAgent._run_loop with a tighter 50ms inbox poll.
        Default BaseAgent uses 200ms; for alert dispatch each hop adds latency.
        50ms poll reduces end-to-end alert delivery from ~400ms to ~100ms.
        """
        try:
            self.on_start()
        except Exception as e:
            logger.exception(f"Error in [{self.name}] on_start: {e}")
            from .base_agent import AgentStatus
            self.status = AgentStatus.FAILED
            self.last_error = str(e)
            return

        from .base_agent import AgentStatus
        while not self._stop_event.is_set():
            self.last_heartbeat = time.time()
            try:
                try:
                    msg = self.inbox.get(timeout=0.05)  # 50ms — 4× faster than default
                    self.handle_message(msg)
                    self.inbox.task_done()
                except queue.Empty:
                    pass
                self.on_tick()
            except Exception as e:
                self.error_count += 1
                self.last_error = str(e)
                logger.exception(f"Exception in agent [{self.name}] execution loop: {e}")
                if self.error_count > 10:
                    self.status = AgentStatus.FAILED
                    logger.error(f"Agent [{self.name}] failed due to excessive errors.")
                    break

        try:
            self.on_stop()
        except Exception as e:
            logger.exception(f"Error in [{self.name}] on_stop: {e}")

        if self.status != AgentStatus.FAILED:
            self.status = AgentStatus.STOPPED

    def handle_message(self, message: Any):
        """Process incoming alert or signal message."""
        if not hasattr(message, "topic"):
            return

        topic = message.topic
        payload = getattr(message, "payload", {})
        symbol = payload.get("symbol")

        if not symbol:
            return

        # Prioritize prediction confluence setups over raw scanner pings
        is_prediction = topic.startswith("alerts/prediction")
        conviction = payload.get("conviction_score", payload.get("conviction", 60))

        # Deduplication check
        now = time.time()
        last_time = self.symbol_last_alert.get(symbol, 0.0)
        elapsed = now - last_time

        # Priority Escalation: High conviction prediction (>= 85%) bypasses cooldown
        bypass_cooldown = is_prediction and conviction >= 85

        if elapsed < self.cooldown_seconds and not bypass_cooldown:
            self.alerts_suppressed_count += 1
            logger.debug(f"[{self.name}] Suppressed duplicate alert for {symbol} (Cooldown active: {int(elapsed)}s < {int(self.cooldown_seconds)}s)")
            return

        # Record alert dispatch
        self.symbol_last_alert[symbol] = now
        self.alerts_dispatched_count += 1

        formatted_msg = self._format_telegram_message(topic, payload)
        self.dispatched_alerts[f"{symbol}_{int(now)}"] = {
            "topic": topic,
            "payload": payload,
            "formatted_message": formatted_msg,
            "timestamp": now,
        }

        # Dispatch via Telegram if credentials available
        if self.telegram_token and self.telegram_chat_id:
            self._send_telegram(formatted_msg)

        # Dispatch via Discord webhook if configured
        if self.discord_webhook_url:
            self._send_discord(formatted_msg)

        # Broadcast over Socket.IO if attached
        if self.socketio:
            try:
                self.socketio.emit("tradesignal_alert", payload, namespace="/")
            except Exception as e:
                logger.error(f"[{self.name}] Error broadcasting over Socket.IO: {e}")

        logger.info(f"[{self.name}] DISPATCHED ALERT for {symbol} (Conviction: {conviction}%, Topic: {topic})")

    def _format_telegram_message(self, topic: str, payload: Dict[str, Any]) -> str:
        """Format rich markdown text for Telegram notifications."""
        symbol = payload.get("symbol", "N/A")
        direction = payload.get("direction", "NEUTRAL")
        icon = "🚀 BULLISH" if direction == "BULLISH" else ("🔻 BEARISH" if direction == "BEARISH" else "⚠️ ALERT")
        conviction = payload.get("conviction_score", payload.get("conviction", 0))
        ltp = payload.get("ltp", 0.0)
        rationale = payload.get("rationale", payload.get("setup_type", "Signal Triggered"))
        agreeing = payload.get("agreeing_agents", [])

        lines = [
            f"⚡ *TRADESIGNAL AGENTIC ALERT* | {icon}",
            f"*Symbol:* `{symbol}` | *LTP:* `₹{ltp:.2f}`",
            f"*Conviction Score:* `{conviction}%`",
            f"*Rationale:* {rationale}",
        ]
        if agreeing:
            lines.append(f"*Confluence Agents:* `{', '.join(agreeing)}`")
        lines.append(f"*Time:* `{time.strftime('%H:%M:%S IST')}`")

        return "\n".join(lines)

    def _send_telegram(self, text: str):
        """Internal helper to dispatch text to Telegram Bot API."""
        try:
            import requests
            url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
            data = {"chat_id": self.telegram_chat_id, "text": text, "parse_mode": "Markdown"}
            requests.post(url, data=data, timeout=3.0)
        except Exception as e:
            logger.error(f"[{self.name}] Telegram send failed: {e}")

    def _send_discord(self, text: str):
        """Internal helper to dispatch text to Discord webhook."""
        try:
            import json
            import urllib.request
            payload = json.dumps({
                'content': text[:2000],
                'username': 'TradeSignal Agentic Alerts'
            }).encode('utf-8')
            req = urllib.request.Request(
                self.discord_webhook_url,
                data=payload,
                headers={'Content-Type': 'application/json', 'User-Agent': 'TradeSignalAlerts/1.0'},
                method='POST'
            )
            import ssl
            try:
                import certifi
                ctx = ssl.create_default_context(cafile=certifi.where())
            except ImportError:
                ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
                if not (200 <= resp.status < 300):
                    logger.error(f"[{self.name}] Discord webhook returned {resp.status}")
        except Exception as e:
            logger.error(f"[{self.name}] Discord send failed: {e}")
