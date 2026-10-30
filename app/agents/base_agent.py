"""
Base class for the orchestrator and its sub-agents.

Agents are plain async components: the LLM (local Ollama / mistral) is used by the
orchestrator for routing and drafting, while the sub-agents perform deterministic,
validated actions (send an email, send a WhatsApp message).
"""
import logging
import time
from abc import ABC
from datetime import datetime
from typing import Any, Dict

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    def __init__(self, agent_name: str, agent_description: str):
        self.agent_name = agent_name
        self.agent_description = agent_description
        self.state: Dict[str, Any] = {
            "agent_name": agent_name,
            "created_at": datetime.utcnow().isoformat(),
            "execution_count": 0,
            "success_count": 0,
            "failure_count": 0,
            "last_execution": None,
            "last_error": None,
        }

    def _record(self, operation: str, started: float, success: bool, error: str = None) -> None:
        """Update counters and record the run for Grafana. Never raises."""
        duration = time.time() - started
        self.state["execution_count"] += 1
        self.state["success_count" if success else "failure_count"] += 1
        self.state["last_execution"] = datetime.utcnow().isoformat()
        if error:
            self.state["last_error"] = error[:300]
        try:
            from app.services.metrics_recorder import metrics
            metrics.track_agent_run(self.agent_name, operation, duration, success)
        except Exception as e:  # metrics must never break a send
            logger.debug(f"metrics unavailable: {e}")

    def get_state(self) -> Dict[str, Any]:
        return dict(self.state)

    def health(self) -> Dict[str, Any]:
        return {"agent": self.agent_name, "ready": True}

    async def close(self) -> None:
        """Release resources (HTTP clients etc.)."""

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}(name={self.agent_name}, executions={self.state['execution_count']})>"
