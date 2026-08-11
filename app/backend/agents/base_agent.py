"""
Base Agent Abstract Class for TradeSignal Agentic Framework.
Provides thread isolation, lifecycle callbacks, heartbeat reporting, and error boundaries.
"""

import logging
import queue
import threading
import time
from enum import Enum
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class AgentStatus(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class BaseAgent:
    """
    Autonomous non-LLM agent base class.
    Communicates asynchronously via an inbox queue and message bus.
    """

    def __init__(self, name: str, bus=None, inbox_maxsize: int = 1000):
        self.name = name
        self.bus = bus
        self.inbox = queue.Queue(maxsize=inbox_maxsize)
        self.status = AgentStatus.CREATED
        self.last_heartbeat = time.time()
        self.error_count = 0
        self.last_error: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self):
        """Start the agent's worker thread."""
        if self._thread and self._thread.is_alive():
            logger.warning(f"Agent [{self.name}] is already running.")
            return

        self._stop_event.clear()
        self.status = AgentStatus.RUNNING
        self.last_heartbeat = time.time()
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"AgentThread-{self.name}",
            daemon=True
        )
        self._thread.start()
        logger.info(f"Agent [{self.name}] started.")

    def stop(self, timeout: float = 2.0):
        """Signal the agent thread to stop gracefully."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self.status = AgentStatus.STOPPED
        logger.info(f"Agent [{self.name}] stopped.")

    def send(self, topic: str, payload: Dict[str, Any]):
        """Publish a message to the shared bus from this agent."""
        if self.bus:
            self.bus.publish(topic=topic, payload=payload, sender=self.name)
        else:
            logger.error(f"Agent [{self.name}] has no bus configured.")

    def put_inbox(self, message: Any, block: bool = False, timeout: Optional[float] = None):
        """Put a incoming message envelope into this agent's inbox."""
        try:
            self.inbox.put(message, block=block, timeout=timeout)
        except queue.Full:
            logger.warning(f"Agent [{self.name}] inbox full. Dropping message.")

    def _run_loop(self):
        """Main internal loop for the worker thread with exception boundaries."""
        try:
            self.on_start()
        except Exception as e:
            logger.exception(f"Error in [{self.name}] on_start: {e}")
            self.status = AgentStatus.FAILED
            self.last_error = str(e)
            return

        while not self._stop_event.is_set():
            self.last_heartbeat = time.time()
            try:
                # Poll inbox with short timeout to allow periodic tick and clean shutdown
                try:
                    msg = self.inbox.get(timeout=0.2)
                    self.handle_message(msg)
                    self.inbox.task_done()
                except queue.Empty:
                    pass

                # Run agent-specific periodic tick logic
                self.on_tick()

            except Exception as e:
                self.error_count += 1
                self.last_error = str(e)
                logger.exception(f"Exception in agent [{self.name}] execution loop: {e}")
                # If persistent errors occur, transition status to FAILED
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

    # --- Lifecycle Hooks to be overridden by concrete agents ---

    def on_start(self):
        """Hook called when agent starts up."""
        pass

    def handle_message(self, message: Any):
        """Hook called when a message is received from the inbox."""
        pass

    def on_tick(self):
        """Hook called periodically (every ~200ms when idle)."""
        pass

    def on_stop(self):
        """Hook called when agent shuts down."""
        pass

    def get_info(self) -> Dict[str, Any]:
        """Return diagnostic snapshot of agent status."""
        return {
            "name": self.name,
            "status": self.status.value,
            "last_heartbeat": self.last_heartbeat,
            "inbox_size": self.inbox.qsize(),
            "error_count": self.error_count,
            "last_error": self.last_error,
        }
