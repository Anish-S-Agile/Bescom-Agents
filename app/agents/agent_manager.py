"""
Agent Manager - owns the agent hierarchy and its lifecycle:

    OrchestratorAgent
      |- EmailAgent
      `- WhatsAppAgent
"""
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.agents.email_agent import EmailAgent
from app.agents.orchestrator_agent import OrchestratorAgent
from app.agents.state.conversation_state import conversation_state_manager
from app.agents.whatsapp_agent import WhatsAppAgent
from app.services.agent_store import AgentStore, agent_store

logger = logging.getLogger(__name__)


class AgentManager:
    def __init__(self) -> None:
        self.orchestrator: Optional[OrchestratorAgent] = None
        self.email_agent: Optional[EmailAgent] = None
        self.whatsapp_agent: Optional[WhatsAppAgent] = None
        self.store: Optional[AgentStore] = None
        self.initialized = False
        self.initialization_time: Optional[datetime] = None
        self.total_requests = 0

    def initialize(self) -> None:
        if self.initialized:
            return
        self.store = agent_store
        self.store.init()  # tables + (fake postgres only) demo seed
        self.email_agent = EmailAgent()
        self.whatsapp_agent = WhatsAppAgent()
        self.orchestrator = OrchestratorAgent(self.email_agent, self.whatsapp_agent, self.store)
        self.initialized = True
        self.initialization_time = datetime.utcnow()
        logger.info("✅ Agents ready: %s -> [%s, %s]", self.orchestrator.agent_name,
                    self.email_agent.agent_name, self.whatsapp_agent.agent_name)
        for agent in (self.email_agent, self.whatsapp_agent):
            h = agent.health()
            if not h["ready"]:
                logger.warning("⚠️ %s is not configured - sends through it will fail until it is", agent.agent_name)

    def should_handle(self, user_input: str, session_id: str) -> bool:
        if not self.initialized:
            self.initialize()
        return self.orchestrator.should_handle(user_input, session_id)

    async def process_request(self, user_input: str, session_id: str, user_id: str,
                              user_roles: Optional[List[str]] = None) -> Dict[str, Any]:
        """Run one user message through the orchestrator. `handled=False` means: not for the agents (use RAG)."""
        if not self.initialized:
            self.initialize()
        self.total_requests += 1
        start = datetime.utcnow()
        result = await self.orchestrator.orchestrate(
            user_input=user_input, session_id=session_id, user_id=user_id, user_roles=user_roles or [])
        end = datetime.utcnow()
        result["metadata"] = {**(result.get("metadata") or {}), "request_number": self.total_requests,
                              "duration_seconds": (end - start).total_seconds(), "timestamp": end.isoformat(),
                              "session_id": session_id, "user_id": user_id}
        return result

    async def send_direct(self, channel: str, payload: Dict[str, Any], user_id: str = "api",
                          roles: Optional[List[str]] = None) -> Dict[str, Any]:
        if not self.initialized:
            self.initialize()
        return await self.orchestrator.send_direct(channel, payload, user_id, roles)

    async def get_conversation_status(self, session_id: str, user_id: Optional[str] = None) -> Dict[str, Any]:
        state = conversation_state_manager.get_session(session_id)
        if not state or (user_id is not None and state.user_id != user_id):
            return {"found": False, "message": "No active conversation found for this session"}
        return {"found": True, "state": state.to_dict()}

    async def reset_conversation(self, session_id: str, user_id: Optional[str] = None) -> Dict[str, Any]:
        state = conversation_state_manager.get_session(session_id)
        if state and user_id is not None and state.user_id != user_id:
            state = None  # not yours: behave as if it does not exist
        deleted = bool(state) and conversation_state_manager.delete_session(session_id)
        return {"success": deleted,
                "message": "Conversation reset successfully" if deleted else "No conversation found"}

    async def health(self) -> Dict[str, Any]:
        """Orchestrator + sub-agents + Ollama (reachable / model pulled) + database."""
        if not self.initialized:
            self.initialize()
        health = self.orchestrator.health()
        health["llm"] = await self.orchestrator.llm.status()
        health["database"] = await self.store.run(self.store.describe)
        return health

    def get_stats(self) -> Dict[str, Any]:
        agents = {"orchestrator": self.orchestrator.agent_name if self.orchestrator else None,
                  "email": self.email_agent.agent_name if self.email_agent else None,
                  "whatsapp": self.whatsapp_agent.agent_name if self.whatsapp_agent else None}
        return {"initialized": self.initialized,
                "initialization_time": self.initialization_time.isoformat() if self.initialization_time else None,
                "total_requests": self.total_requests,
                "active_sessions": len(conversation_state_manager.get_active_sessions()),
                "agents": agents,
                "agent_state": {a.agent_name: a.get_state()
                                for a in (self.orchestrator, self.email_agent, self.whatsapp_agent) if a}}

    def cleanup_old_sessions(self, max_age_hours: int = 24) -> int:
        return conversation_state_manager.cleanup_old_sessions(max_age_hours)

    async def shutdown(self) -> None:
        for agent in (self.whatsapp_agent, self.email_agent, self.orchestrator):
            if agent:
                try:
                    await agent.close()
                except Exception as e:
                    logger.warning("⚠️ Error closing %s: %s", agent.agent_name, e)


agent_manager: Optional[AgentManager] = None


def get_agent_manager(vector_service=None, ai_service=None) -> AgentManager:
    """Get or create the process-wide AgentManager (arguments kept for backward compatibility)."""
    global agent_manager
    if agent_manager is None:
        manager = AgentManager()
        manager.initialize()  # may raise: do not cache a half-initialised manager, the next call retries
        agent_manager = manager
    return agent_manager
