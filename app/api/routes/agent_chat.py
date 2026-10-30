"""
Agent API - chat with the OrchestratorAgent, manage the address book, read send history.

  POST   /api/agent/chat                  natural-language request (orchestrator + mistral)
  GET    /api/agent/messages[/stats]      send history (own; admins may pass all=true)
  GET/POST /api/agent/contacts            address book (PUT/DELETE /contacts/{id})
  GET    /api/agent/templates             email templates the orchestrator can use
  GET    /api/agent/health | /stats | /conversation/{id}      DELETE /api/agent/conversation/{id}
  POST   /api/agent/email/send | /whatsapp/send   structured sends for services (X-API-Key)

User endpoints identify the caller from the Bearer token (AGENT_REQUIRE_AUTH=true, the default). The
structured service endpoints fail closed: 503 until AGENT_API_KEY is configured.
"""
import hmac
import logging
import os
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.agents import get_agent_manager
from app.agents.common import env_bool
from app.agents.email_templates import TEMPLATES
from app.api.routes.local_auth import CurrentUser, decode_token
from app.services.agent_store import DuplicateError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agent", tags=["agent-chat"])


# ----------------------------------------------------------------------------- models
class AgentChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000, description="User's message")
    session_id: Optional[str] = Field(None, max_length=128, description="Conversation session ID")
    # Only honoured when AGENT_REQUIRE_AUTH=false; authenticated calls always use the token identity.
    user_id: str = Field(default="default_user", max_length=255)
    user_roles: Optional[List[str]] = Field(default=["viewer"])


class AgentChatResponse(BaseModel):
    success: bool
    response: str
    session_id: str
    handled: bool = True
    routing: Optional[str] = None
    execution_result: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class ConversationStatusResponse(BaseModel):
    found: bool
    session_id: str
    state: Optional[Dict[str, Any]] = None
    message: Optional[str] = None


class AgentStatsResponse(BaseModel):
    initialized: bool
    total_requests: int
    active_sessions: int
    agents: Dict[str, Optional[str]]
    agent_state: Optional[Dict[str, Any]] = None
    initialization_time: Optional[str] = None


class ContactIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    email: Optional[str] = Field(None, max_length=255)
    phone: Optional[str] = Field(None, max_length=32)
    organization: Optional[str] = Field(None, max_length=160)
    notes: Optional[str] = Field(None, max_length=2000)


class EmailSendRequest(BaseModel):
    to: List[str] = Field(..., min_length=1, max_length=100)
    subject: str = Field(default="", max_length=300)
    body: str = Field(default="", max_length=100_000)
    cc: Optional[List[str]] = None
    bcc: Optional[List[str]] = None
    reply_to: Optional[str] = None
    template: Optional[str] = Field(None, description="meeting_invite, announcement, reminder, follow_up, "
                                                      "thank_you, outage_notice, welcome")
    variables: Dict[str, Any] = Field(default_factory=dict)
    user_id: str = "api"


class WhatsAppSendRequest(BaseModel):
    to: List[str] = Field(..., min_length=1, max_length=100)
    body: str = Field(default="", max_length=4096)
    template_name: Optional[str] = None
    template_params: Optional[List[str]] = None
    user_id: str = "api"


# ----------------------------------------------------------------------------- auth helpers
def agent_user(authorization: Optional[str] = Header(None)) -> CurrentUser:
    user = decode_token(authorization)
    if user:
        return user
    if env_bool("AGENT_REQUIRE_AUTH", True):
        raise HTTPException(status_code=401, detail="Authentication required")
    return CurrentUser(username="anonymous", role="viewer", roles=["viewer"], authenticated=False)


def _require_api_key(x_api_key: Optional[str]) -> None:
    expected = os.getenv("AGENT_API_KEY", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Direct send API is disabled: AGENT_API_KEY is not configured")
    if not x_api_key or not hmac.compare_digest(x_api_key.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


def _send_status(result: Dict[str, Any]) -> int:
    if result.get("success"):
        return 200
    if result.get("retry_after"):
        return 429
    results = result.get("results") or []
    if results and all(r.get("retryable") for r in results if not r.get("success")):
        return 502
    return 422


def _store():
    return get_agent_manager().store


# ----------------------------------------------------------------------------- chat
@router.post("/chat", response_model=AgentChatResponse)
async def agent_chat(request: AgentChatRequest, user: CurrentUser = Depends(agent_user)):
    """Send a natural-language request, e.g. 'email hr@acme.com that the office is closed tomorrow'."""
    session_id = request.session_id or str(uuid.uuid4())
    user_id = user.username if user.authenticated else request.user_id
    roles = user.roles if user.authenticated else (request.user_roles or ["viewer"])
    try:
        logger.info("📨 Agent chat | session=%s user=%s len=%d", session_id, user_id, len(request.message))
        result = await get_agent_manager().process_request(
            user_input=request.message, session_id=session_id, user_id=user_id, user_roles=roles)
        response = result.get("response", "")
        if not result.get("handled", True):
            response = ("I can send emails and WhatsApp messages for you. Try: "
                        "\"Email alice@example.com that the meeting moved to 4pm\" or "
                        "\"WhatsApp +919876543210 that the delivery is delayed\". "
                        "You can also use the names of saved contacts.")
        return AgentChatResponse(
            success=result.get("success", True), response=response, session_id=session_id,
            handled=result.get("handled", True), routing=result.get("routing"),
            execution_result=result.get("execution_result"), metadata=result.get("metadata"),
            error=result.get("error"))
    except Exception as e:
        logger.exception("❌ Agent chat failed")
        raise HTTPException(status_code=500, detail="Agent chat processing failed") from e


# ----------------------------------------------------------------------------- history
@router.get("/messages")
async def list_messages(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                        channel: Optional[str] = Query(None, pattern="^(email|whatsapp)$"),
                        status: Optional[str] = Query(None, pattern="^(sent|failed|dry_run|duplicate)$"),
                        all: bool = False, user: CurrentUser = Depends(agent_user)):
    store = _store()
    owner = None if (all and user.role == "admin") else user.username
    items = await store.run(store.list_messages, owner, limit, offset, channel, status)
    return {"items": items, "limit": limit, "offset": offset}


@router.get("/messages/stats")
async def message_stats(all: bool = False, user: CurrentUser = Depends(agent_user)):
    store = _store()
    return await store.run(store.message_stats, None if (all and user.role == "admin") else user.username)


# ----------------------------------------------------------------------------- contacts
@router.get("/contacts")
async def list_contacts(q: Optional[str] = Query(None, max_length=100), user: CurrentUser = Depends(agent_user)):
    store = _store()
    return {"items": await store.run(store.list_contacts, user.username, q)}


@router.post("/contacts", status_code=201)
async def create_contact(body: ContactIn, user: CurrentUser = Depends(agent_user)):
    store = _store()
    try:
        return await store.run(store.add_contact, user.username, body.name, body.email, body.phone,
                               body.organization, body.notes)
    except DuplicateError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.put("/contacts/{contact_id}")
async def update_contact(contact_id: int, body: ContactIn, user: CurrentUser = Depends(agent_user)):
    store = _store()
    try:
        updated = await store.run(store.update_contact, user.username, contact_id, **body.model_dump())
    except DuplicateError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if updated is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    return updated


@router.delete("/contacts/{contact_id}")
async def delete_contact(contact_id: int, user: CurrentUser = Depends(agent_user)):
    store = _store()
    if not await store.run(store.delete_contact, user.username, contact_id):
        raise HTTPException(status_code=404, detail="Contact not found")
    return {"success": True}


@router.get("/templates")
async def list_templates(user: CurrentUser = Depends(agent_user)):
    return {"items": [{"name": n, "description": t["description"], "required": t["required"]}
                      for n, t in TEMPLATES.items()]}


# ----------------------------------------------------------------------------- structured sends (services)
@router.post("/email/send")
async def send_email(request: EmailSendRequest, x_api_key: Optional[str] = Header(None)):
    _require_api_key(x_api_key)
    payload = request.model_dump()
    payload["emails"] = payload.pop("to")
    result = await get_agent_manager().send_direct("email", payload, user_id=request.user_id)
    if not result["success"]:
        raise HTTPException(status_code=_send_status(result), detail=result)
    return result


@router.post("/whatsapp/send")
async def send_whatsapp(request: WhatsAppSendRequest, x_api_key: Optional[str] = Header(None)):
    _require_api_key(x_api_key)
    payload = {"phones": request.to, "body": request.body, "whatsapp_template": request.template_name,
               "whatsapp_template_params": request.template_params}
    result = await get_agent_manager().send_direct("whatsapp", payload, user_id=request.user_id)
    if not result["success"]:
        raise HTTPException(status_code=_send_status(result), detail=result)
    return result


# ----------------------------------------------------------------------------- state & ops
@router.get("/conversation/{session_id}", response_model=ConversationStatusResponse)
async def get_conversation_status(session_id: str, user: CurrentUser = Depends(agent_user)):
    status = await get_agent_manager().get_conversation_status(session_id, user.username)
    return ConversationStatusResponse(found=status.get("found", False), session_id=session_id,
                                      state=status.get("state"), message=status.get("message"))


@router.delete("/conversation/{session_id}")
async def reset_conversation(session_id: str, user: CurrentUser = Depends(agent_user)):
    result = await get_agent_manager().reset_conversation(session_id, user.username)
    return {"success": result.get("success", False), "message": result.get("message", ""),
            "session_id": session_id}


@router.get("/stats", response_model=AgentStatsResponse)
async def get_agent_stats(user: CurrentUser = Depends(agent_user)):
    return AgentStatsResponse(**get_agent_manager().get_stats())


@router.post("/cleanup")
async def cleanup_old_sessions(max_age_hours: int = 24, x_api_key: Optional[str] = Header(None)):
    _require_api_key(x_api_key)
    cleaned = get_agent_manager().cleanup_old_sessions(max_age_hours)
    return {"success": True, "cleaned_sessions": cleaned, "max_age_hours": max_age_hours,
            "timestamp": datetime.utcnow().isoformat()}


@router.get("/health")
async def agent_health_check():
    """Public readiness: Ollama (reachable + model pulled), both sub-agents, database. Never exposes secrets."""
    try:
        health = await get_agent_manager().health()
        health["database"].pop("url", None)  # do not advertise the DB host publicly
        health["llm"].pop("base_url", None)
        subs = health["sub_agents"]
        llm = health["llm"]
        degraded = (not (llm["reachable"] and llm["model_pulled"])
                    or any(not (s and s["ready"]) for s in subs.values())
                    or not health["database"].get("healthy"))
        return {"status": "degraded" if degraded else "healthy", **health,
                "timestamp": datetime.utcnow().isoformat()}
    except Exception as e:
        logger.exception("❌ Agent health check failed")
        return {"status": "unhealthy", "error": str(e), "timestamp": datetime.utcnow().isoformat()}
