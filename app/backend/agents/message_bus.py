"""
Thread-safe MessageBus for TradeSignal Agentic Framework.
Supports topic-based pub/sub pattern matching (e.g., 'ticks/RELIANCE', 'ticks/*', 'signals/#').
"""

import fnmatch
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Set, Union

logger = logging.getLogger(__name__)


@dataclass
class Message:
    """Standardized message envelope exchanged across agents."""
    topic: str
    payload: Dict[str, Any]
    sender: str = "system"
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "topic": self.topic,
            "payload": self.payload,
            "sender": self.sender,
            "timestamp": self.timestamp,
        }


class MessageBus:
    """
    In-memory, thread-safe pub/sub broker.
    Allows agents to publish and subscribe to topics with wildcard support.
    """

    def __init__(self):
        self._subscriptions: Dict[str, Set[Any]] = {}  # pattern -> set of subscriber objects (BaseAgent or queue)
        self._lock = threading.RLock()
        self._total_published = 0

    def subscribe(self, pattern: str, subscriber: Any):
        """
        Subscribe an agent or inbox to a topic pattern.
        Examples of pattern: 'ticks/RELIANCE', 'ticks/*', 'signals/*'
        """
        with self._lock:
            if pattern not in self._subscriptions:
                self._subscriptions[pattern] = set()
            self._subscriptions[pattern].add(subscriber)
            logger.debug(f"Subscribed {getattr(subscriber, 'name', subscriber)} to pattern '{pattern}'")

    def unsubscribe(self, pattern: str, subscriber: Any):
        """Unsubscribe an agent or inbox from a topic pattern."""
        with self._lock:
            if pattern in self._subscriptions:
                self._subscriptions[pattern].discard(subscriber)
                if not self._subscriptions[pattern]:
                    del self._subscriptions[pattern]

    def publish(self, topic: str, payload: Dict[str, Any], sender: str = "system") -> int:
        """
        Publish a message envelope to all subscribers matching the topic pattern.
        Returns the number of subscribers notified.
        """
        msg = Message(topic=topic, payload=payload, sender=sender)
        recipients = set()

        with self._lock:
            self._total_published += 1
            for pattern, subscribers in self._subscriptions.items():
                if self._topic_matches(topic, pattern):
                    recipients.update(subscribers)

        delivered = 0
        for sub in recipients:
            try:
                if hasattr(sub, "put_inbox"):
                    sub.put_inbox(msg)
                    delivered += 1
                elif hasattr(sub, "put"):
                    sub.put(msg, block=False)
                    delivered += 1
            except Exception as e:
                logger.error(f"Failed to deliver message to {sub}: {e}")

        return delivered

    @staticmethod
    def _topic_matches(topic: str, pattern: str) -> bool:
        """
        Match topic against fnmatch pattern or wildcard string.
        Supports standard glob patterns (e.g. 'ticks/*' matches 'ticks/RELIANCE').
        Converts MQTT-style '#' to '*' for convenience.
        """
        if pattern == "*" or pattern == "#":
            return True
        norm_pattern = pattern.replace("#", "*")
        return fnmatch.fnmatch(topic, norm_pattern)

    def get_stats(self) -> Dict[str, Any]:
        """Return diagnostic metrics for the message bus."""
        with self._lock:
            return {
                "active_patterns": len(self._subscriptions),
                "total_subscriptions": sum(len(subs) for subs in self._subscriptions.values()),
                "total_published": self._total_published,
            }
