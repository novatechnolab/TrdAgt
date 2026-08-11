"""
Orchestrator Supervisor for TradeSignal Agentic Framework.
Coordinates agent registration, startup order, health watchdog monitoring, and dead-agent recovery.
"""

import logging
import threading
import time
from typing import Dict, List, Optional

from .base_agent import BaseAgent, AgentStatus
from .message_bus import MessageBus

logger = logging.getLogger(__name__)


class Orchestrator:
    """
    Central supervisor that manages the lifecycle of all domain agents.
    Provides watchdog monitoring to auto-restart crashed or unresponsive agents.
    """

    def __init__(self, bus: Optional[MessageBus] = None):
        self.bus = bus or MessageBus()
        self.agents: Dict[str, BaseAgent] = {}
        self._lock = threading.RLock()
        self._watchdog_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.watchdog_interval = 10.0  # seconds between health checks
        self.heartbeat_timeout = 60.0  # max seconds without heartbeat before restart

    def register_agent(self, agent: BaseAgent) -> BaseAgent:
        """Register an agent with the orchestrator and inject the shared message bus."""
        with self._lock:
            agent.bus = self.bus
            self.agents[agent.name] = agent
            logger.info(f"Registered agent [{agent.name}] with Orchestrator.")
        return agent

    def get_agent(self, name: str) -> Optional[BaseAgent]:
        """Get registered agent by name."""
        with self._lock:
            return self.agents.get(name)

    def start_all(self):
        """Start all registered agents and launch the watchdog loop."""
        with self._lock:
            logger.info("Orchestrator starting all registered agents...")
            for name, agent in self.agents.items():
                try:
                    agent.start()
                except Exception as e:
                    logger.exception(f"Failed to start agent [{name}]: {e}")

            self._stop_event.clear()
            if not self._watchdog_thread or not self._watchdog_thread.is_alive():
                self._watchdog_thread = threading.Thread(
                    target=self._watchdog_loop,
                    name="OrchestratorWatchdog",
                    daemon=True
                )
                self._watchdog_thread.start()
            logger.info("Orchestrator active with watchdog daemon.")

    def stop_all(self, timeout: float = 2.0):
        """Stop all agents and shutdown the watchdog supervisor."""
        self._stop_event.set()
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            self._watchdog_thread.join(timeout=timeout)

        with self._lock:
            logger.info("Orchestrator stopping all agents...")
            for name, agent in self.agents.items():
                try:
                    agent.stop(timeout=timeout)
                except Exception as e:
                    logger.exception(f"Error stopping agent [{name}]: {e}")
            logger.info("Orchestrator shutdown complete.")

    def _watchdog_loop(self):
        """Watchdog loop checking agent heartbeats every `watchdog_interval` seconds."""
        while not self._stop_event.is_set():
            time.sleep(self.watchdog_interval)
            now = time.time()

            with self._lock:
                for name, agent in list(self.agents.items()):
                    # 1. Restart failed agents
                    if agent.status == AgentStatus.FAILED:
                        logger.warning(f"[Watchdog] Agent [{name}] is in FAILED state. Restarting...")
                        self._restart_agent(agent)

                    # 2. Check stale heartbeats for running agents
                    elif agent.status == AgentStatus.RUNNING:
                        elapsed = now - agent.last_heartbeat
                        if elapsed > self.heartbeat_timeout:
                            logger.warning(f"[Watchdog] Agent [{name}] heartbeat stale ({elapsed:.1f}s). Restarting...")
                            self._restart_agent(agent)

    def _restart_agent(self, agent: BaseAgent):
        """Attempt to gracefully stop and restart an agent."""
        try:
            agent.stop(timeout=1.0)
            agent.error_count = 0
            agent.start()
            logger.info(f"[Watchdog] Successfully restarted agent [{agent.name}].")
        except Exception as e:
            logger.exception(f"[Watchdog] Failed to restart agent [{agent.name}]: {e}")

    def get_system_health(self) -> Dict:
        """Return diagnostic overview of message bus and all managed agents."""
        with self._lock:
            agents_info = {name: agent.get_info() for name, agent in self.agents.items()}
            healthy_count = sum(1 for a in agents_info.values() if a["status"] == AgentStatus.RUNNING.value)
            return {
                "total_agents": len(self.agents),
                "healthy_agents": healthy_count,
                "bus_stats": self.bus.get_stats(),
                "agents": agents_info,
            }
