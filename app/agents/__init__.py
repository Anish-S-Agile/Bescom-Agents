"""
Agent system: one OrchestratorAgent coordinating an Email Service Agent and a WhatsApp Agent.
"""
from app.agents.agent_manager import AgentManager, get_agent_manager
from app.agents.base_agent import BaseAgent
from app.agents.email_agent import EmailAgent
from app.agents.orchestrator_agent import OrchestratorAgent
from app.agents.whatsapp_agent import WhatsAppAgent

__all__ = ["AgentManager", "get_agent_manager", "BaseAgent", "OrchestratorAgent", "EmailAgent", "WhatsAppAgent"]
