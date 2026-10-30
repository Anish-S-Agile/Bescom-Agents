"""
Orchestrator Agent - the only coordinating agent. It understands a user's request (local Ollama
`mistral`), collects whatever is missing over several turns, asks for confirmation, and hands the
work to its sub-agents:

    OrchestratorAgent
      |- EmailAgent      (SMTP email: announcements, invites, reminders, notices ...)
      `- WhatsAppAgent   (WhatsApp Cloud API / Twilio)

Safety rules baked in:
  * recipients (emails / phone numbers) are taken ONLY from the user's own text - never from the LLM;
  * anything the LLM wrote or rephrased is shown to the user for confirmation before sending;
  * per-user rate limit, optional role gate, optional confirmation for every send.
If Ollama is down the orchestrator degrades to rule-based extraction and asks for what is missing.

Environment:
    AGENT_REQUIRE_CONFIRMATION (true)   always preview before sending
    AGENT_RATE_LIMIT_PER_MINUTE (10)    sends per user per minute (0 = unlimited)
    AGENT_SEND_ALLOWED_ROLES            comma list of roles allowed to send (empty = anyone)
    AGENT_MAX_CLARIFICATIONS (5)        questions asked before giving up
    AGENT_DRAFT_TIMEOUT (60)            seconds allowed for the LLM to draft a message
"""
import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional

from app.agents.base_agent import BaseAgent
from app.agents.common import (AgentResult, RateLimiter, env_bool, env_float, env_int, env_list, find_emails,
                               find_phones, mask_recipient, normalize_email, normalize_phone, strip_addresses)
from app.agents.email_templates import TEMPLATES, missing_variables
from app.agents.state.conversation_state import ConversationState, ConversationStatus, conversation_state_manager

logger = logging.getLogger(__name__)

CHANNELS = ("email", "whatsapp", "both")

EXTRACT_PROMPT = """You are the routing brain of a messaging assistant. Decide whether the user wants to SEND an email, SEND a WhatsApp message, both, or neither, and extract the fields.

Reply with ONLY one JSON object:
{"channel": "email" | "whatsapp" | "both" | "none", "subject": "", "body": "", "template": "", "variables": {}}

Rules:
- "none" for questions, documentation requests, or anything that is not a request to send a message now ("how do I send an email?", "what is WhatsApp Business?").
- Put the message text only if the user wrote it. Otherwise leave body "" (it will be drafted later).
- template is one of: meeting_invite, announcement, reminder, follow_up, thank_you, outage_notice, welcome - or "" when none fits.
- variables hold template fields the user gave: topic, date_time, location, agenda, details, due, system, start_time, impact, resolution, organization, recipient_name, sender_name.
- Never invent email addresses or phone numbers.

Examples:
"send an email to hr@acme.com saying the office is closed tomorrow" -> {"channel":"email","subject":"Office closed tomorrow","body":"The office is closed tomorrow.","template":"","variables":{}}
"whatsapp +919876543210 that the delivery is delayed" -> {"channel":"whatsapp","subject":"","body":"The delivery is delayed.","template":"","variables":{}}
"invite team@acme.com to a meeting about Q3 planning on Friday 3pm" -> {"channel":"email","subject":"","body":"","template":"meeting_invite","variables":{"topic":"Q3 planning","date_time":"Friday 3pm"}}
"email ops@acme.com and whatsapp +14155550100 an outage notice for the billing system from 9pm" -> {"channel":"both","subject":"","body":"","template":"outage_notice","variables":{"system":"billing system","start_time":"9pm"}}
"how do I send an email from the portal?" -> {"channel":"none","subject":"","body":"","template":"","variables":{}}"""

DRAFT_PROMPT = """You write short, professional {channel} messages for an organisation. Write the message the user asked for, using only facts they gave you. Do not invent names, dates, numbers or links. Never use placeholders such as [Name] or [Organization], and do not add a sign-off or signature. WhatsApp messages are one to three short sentences.
Reply with ONLY one JSON object: {{"subject": "<short subject, empty for whatsapp>", "body": "<message text>"}}"""

KNOWLEDGE_PREFIXES = ("how do i", "how to", "how can i", "how does", "what is ", "what's ", "what are ", "what does ",
                      "explain", "why ", "tutorial", "tell me about", "can you describe", "is it possible")
_VERB_CHANNEL = re.compile(
    r"\b(send|compose|draft|write|forward|notify|ping|shoot|fire off|drop|invite|remind|inform|alert|announce)\b.*\b(e-?mails?|mails?|whatsapp|wa|"
    r"messages?|msgs?|text)\b", re.I | re.S)
# these verbs address a person, so an email address / phone number is enough ("invite team@acme.com to ...")
_VERB_ADDRESS = re.compile(r"\b(send|forward|notify|ping|invite|remind|inform|alert|announce|tell|message|thank)\b", re.I)
_CHANNEL_FIRST = re.compile(r"^\s*(e-?mail|mail|whatsapp)\b\s+\S", re.I)
# "email Alice ..." / "whatsapp Bob ..." as a command, but not "email is down" / "whatsapp has ..."
_IMPERATIVE_FIRST = re.compile(r"^\s*(e-?mail|whatsapp)\s+(?!(?:is|are|was|were|will|has|have|had|can|does|do|not|and|or|but|if)\b)\S", re.I)
# "email and whatsapp Alice ..." - both channels named up front, but not "email and whatsapp are down"
_BOTH_FIRST = re.compile(r"^\s*(?:e-?mail|mail)\s+and\s+whatsapp\s+(?!(?:is|are|was|were|will|has|have|had|can|does|do|not)\b)\S"
                         r"|^\s*whatsapp\s+and\s+(?:e-?mail|mail)\s+(?!(?:is|are|was|were|will|has|have|had|can|does|do|not)\b)\S",
                         re.I)
_CANCEL = {"cancel", "stop", "abort", "no", "nope", "never mind", "nevermind", "discard", "dont send", "don't send"}
_CONFIRM = {"yes", "y", "yep", "yeah", "send", "send it", "confirm", "ok", "okay", "sure", "go ahead", "do it",
            "proceed", "please send"}

_BODY_SAYING = re.compile(
    r"\b(?:saying|says|telling them|tell them|that says|with (?:the )?(?:message|text|body|content))\b\s*(?:is|:|-)?\s*"
    r"(?P<body>.+)$", re.I | re.S)
_BODY_LABEL = re.compile(r"\b(?:message|body|text|content)\b\s*(?:is|:|-)\s*(?P<body>.+)$", re.I | re.S)
_SUBJECT = re.compile(
    r"\bsubject\b\s*(?:line)?\s*(?:is|to|:|-)?\s*[\"“'‘]?(?P<subject>.+?)[\"”'’]?"
    r"(?=\s+(?:body|message|saying|says|and then|with (?:the )?(?:message|body|text))\b|\s*$|\n)", re.I | re.S)
_QUOTED = re.compile(r"[\"“](?P<q>[^\"”]{3,})[\"”]")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower()).strip(" .!")


def looks_like_send_request(text: str) -> bool:
    """Cheap pre-filter so ordinary RAG questions never pay for an LLM call."""
    t = (text or "").strip()
    if not t or t.lower().startswith(KNOWLEDGE_PREFIXES):
        return False
    if _CHANNEL_FIRST.search(t) and (find_emails(t) or find_phones(t)):
        return True
    if _IMPERATIVE_FIRST.search(t) or _BOTH_FIRST.search(t):
        return True
    if _VERB_ADDRESS.search(t) and (find_emails(t) or find_phones(t)):
        return True
    return bool(_VERB_CHANNEL.search(t))


class OrchestratorAgent(BaseAgent):
    def __init__(self, email_agent=None, whatsapp_agent=None, store=None) -> None:
        super().__init__(
            "OrchestratorAgent",
            "Coordinates the Email Service Agent and the WhatsApp Agent: understands the request, collects "
            "missing details, confirms, and dispatches.")
        self.email_agent = email_agent
        self.whatsapp_agent = whatsapp_agent
        self.store = store  # AgentStore: address book + send history (None = run without persistence)
        self.require_confirmation = env_bool("AGENT_REQUIRE_CONFIRMATION", True)
        self.rate_limiter = RateLimiter(env_int("AGENT_RATE_LIMIT_PER_MINUTE", 10))
        self.allowed_roles = env_list("AGENT_SEND_ALLOWED_ROLES")
        self.max_clarifications = env_int("AGENT_MAX_CLARIFICATIONS", 5)
        self.draft_timeout = env_float("AGENT_DRAFT_TIMEOUT", 60.0)
        from app.services.ollama_client import ollama_client
        self.llm = ollama_client
        self.llm.warm_up()

    def set_sub_agents(self, email_agent, whatsapp_agent) -> None:
        self.email_agent = email_agent
        self.whatsapp_agent = whatsapp_agent
        logger.info("✅ Orchestrator connected to EmailServiceAgent and WhatsAppAgent")

    def health(self) -> Dict[str, Any]:
        return {"agent": self.agent_name, "ready": bool(self.email_agent and self.whatsapp_agent),
                "llm": {"provider": "ollama", "model": self.llm.model, "available": self.llm.available},
                "sub_agents": {"email": self.email_agent.health() if self.email_agent else None,
                               "whatsapp": self.whatsapp_agent.health() if self.whatsapp_agent else None}}

    # ================================================================== routing gate
    def should_handle(self, user_input: str, session_id: str) -> bool:
        """True when this message belongs to the orchestrator (active conversation or a send request)."""
        state = conversation_state_manager.get_session(session_id)
        if state and self._is_active(state):
            return True
        return looks_like_send_request(user_input)

    @staticmethod
    def _is_active(state: ConversationState) -> bool:
        return (state.resource_type in CHANNELS and state.operation == "send"
                and state.status in (ConversationStatus.COLLECTING_PARAMS, ConversationStatus.READY_TO_EXECUTE))

    # ================================================================== main entry
    async def orchestrate(self, user_input: str, session_id: str, user_id: str,
                          user_roles: Optional[List[str]] = None) -> Dict[str, Any]:
        started = time.time()
        user_input = (user_input or "").strip()
        try:
            result = await self._orchestrate(user_input, session_id, user_id, user_roles or [])
            self._record("orchestrate", started, result.get("success", True), result.get("error"))
            return result
        except Exception as e:
            logger.exception(f"❌ Orchestration failed: {e}")
            self._record("orchestrate", started, False, str(e))
            return {"success": False, "handled": True, "routing": "error", "error": str(e),
                    "response": "Sorry, something went wrong while handling your request. Please try again."}

    async def _orchestrate(self, text: str, session_id: str, user_id: str, roles: List[str]) -> Dict[str, Any]:
        state = conversation_state_manager.get_session(session_id)
        if state and state.status in (ConversationStatus.COMPLETED, ConversationStatus.FAILED,
                                      ConversationStatus.CANCELLED):
            conversation_state_manager.delete_session(session_id)
            state = None
        if state and state.user_id != user_id:  # never let one user drive another user's conversation
            return {"success": False, "handled": True, "routing": "error", "error": "session_mismatch",
                    "response": "This conversation belongs to another user. Please start a new one."}

        # ---------------- continuing a conversation
        if state and self._is_active(state):
            state.add_message("user", text)
            reply = await self._continue(state, text, user_id, roles)
            return self._finish(state, reply)

        # ---------------- new request
        if not looks_like_send_request(text):
            return {"success": True, "handled": False, "routing": "none", "response": ""}
        plan = await self._understand(text, user_id)
        if plan["channel"] not in CHANNELS:
            return {"success": True, "handled": False, "routing": "none", "response": ""}

        denied = self._check_role(roles)
        if denied:
            return {"success": False, "handled": True, "routing": plan["channel"], "error": "forbidden",
                    "response": denied}

        state = conversation_state_manager.create_session(session_id, user_id)
        state.user_query = text
        state.add_message("user", text)
        state.handoff_to_agent(self.agent_name, self._target_name(plan["channel"]), "send request detected")
        state.resource_type, state.operation, state.intent = plan["channel"], "send", f"send_{plan['channel']}"
        state.status = ConversationStatus.COLLECTING_PARAMS
        drafted = bool(plan.pop("llm_body", False))
        state.collected_params = {**plan, "drafted": drafted, "_confirm": False, "_asked": None}
        reply = await self._advance(state, user_id, roles)
        return self._finish(state, reply)

    # ================================================================== conversation flow
    async def _continue(self, state: ConversationState, text: str, user_id: str, roles: List[str]) -> Dict[str, Any]:
        params = state.collected_params
        norm = _norm(text)

        if norm in _CANCEL:
            state.cancel("User cancelled")
            return self._reply("Okay, I've cancelled that. Nothing was sent.", state, routing="cancelled")

        if params.get("_confirm"):
            if norm in _CONFIRM:
                return await self._dispatch(state, user_id, roles)
            changed = self._apply_edit(params, text)
            if not changed:
                return self._reply("I didn't catch a change. Reply **yes** to send, **cancel** to discard, or tell "
                                   "me what to change (e.g. *subject: New subject*).", state, awaiting=True)
            params["_confirm"] = False

        else:
            asked = params.get("_asked")
            found = self._baseline(text)
            self._merge(params, found)
            ch = params["channel"]
            if (ch in ("email", "both") and not params["emails"]) or (ch in ("whatsapp", "both")
                                                                     and not params["phones"]):
                self._apply_contacts(params, await self._resolve_contacts(user_id, text))
            # a bare answer to the single thing we asked for
            if asked == "body" and not params.get("body") and not found["emails"] and not found["phones"]:
                if norm in _CONFIRM or len(norm) < 3:  # "yes"/"ok" is not a message
                    return self._reply("What should the message say? (or reply **cancel** to stop)", state,
                                       missing=["body"])
                params["body"], params["drafted"] = text.strip().strip("\"“”'"), False
            elif asked == "subject" and not params.get("subject"):
                params["subject"] = text.strip().strip("\"“”'")[:150]
            elif asked and asked.startswith("var:"):
                var = asked[4:]
                if not params["variables"].get(var):
                    params["variables"][var] = text.strip()

        return await self._advance(state, user_id, roles)

    async def _advance(self, state: ConversationState, user_id: str, roles: List[str]) -> Dict[str, Any]:
        """Work out what is still missing; ask for it, or preview/send once complete."""
        params = state.collected_params
        channel = params["channel"]

        wants_email = channel in ("email", "both")
        wants_wa = channel in ("whatsapp", "both")
        missing: List[str] = []
        if wants_email and not params["emails"]:
            missing.append("recipient_email")
        if wants_wa and not params["phones"]:
            missing.append("recipient_phone")

        # ---- draft content when the user gave none (and no template) - only once recipients are known
        if (not missing and not params.get("body") and not params.get("template") and not params.get("_draft_failed")
                and self._has_content_cue(state.user_query or "")):
            drafted = await self._draft(state.user_query or "", channel)
            if drafted:
                params["body"], params["drafted"] = drafted["body"], True
                if drafted.get("subject") and not params.get("subject"):
                    params["subject"] = drafted["subject"]
            else:
                params["_draft_failed"] = True

        if params.get("template"):
            missing += [f"var:{v}" for v in missing_variables(params["template"], params["variables"])]
        elif not params.get("body"):
            missing.append("body")
        if wants_email and not params.get("subject") and not params.get("template") and params.get("body"):
            params["subject"] = self._derive_subject(params["body"])

        state.required_params = set(missing) | set(state.required_params)
        state.missing_params = set(missing)

        if missing:
            state.clarification_count += 1
            if state.clarification_count > self.max_clarifications:
                state.cancel("too many clarifications")
                return self._reply("I couldn't gather everything needed, so I've stopped without sending anything. "
                                   "Please start again with the recipient and the message.", state, routing="cancelled")
            first = missing[0]
            params["_asked"] = "body" if first == "body" else first
            state.status = ConversationStatus.COLLECTING_PARAMS
            return self._reply(self._question(first, params), state, missing=missing)

        # ---- everything present
        if self.require_confirmation or params.get("drafted"):
            params["_confirm"], params["_asked"] = True, None
            state.status = ConversationStatus.READY_TO_EXECUTE
            return self._reply(self._preview(params), state, awaiting=True)
        return await self._dispatch(state, user_id, roles)

    async def _dispatch(self, state: ConversationState, user_id: str, roles: List[str]) -> Dict[str, Any]:
        params = state.collected_params
        denied = self._check_role(roles)
        if denied:
            return self._reply(denied, state, routing=params["channel"], success=False, awaiting=True)
        allowed, wait = self.rate_limiter.allow(user_id or "anonymous")
        if not allowed:
            return self._reply(f"You've reached the sending limit. Please wait about {int(wait)} seconds and reply "
                               "**yes** to send.", state, routing=params["channel"], success=False, awaiting=True)

        state.status = ConversationStatus.EXECUTING
        results = await self.execute_plan(params)
        await self._log_results(user_id, state.session_id, params, results)
        ok = [r for r in results if r.success]
        failed = [r for r in results if not r.success]
        data = {"success": bool(ok) and not failed, "data": {"results": [r.to_dict() for r in results]}}
        if failed:
            data["error"] = "; ".join(f"{r.channel}: {r.error}" for r in failed)
        retryable = bool(failed) and all(r.retryable for r in failed) and not ok

        lines = []
        for r in results:
            icon = ("🧪" if r.dry_run else "✅") if r.success else "❌"
            lines.append(f"{icon} **{r.channel.title()}**: {r.message if r.success else r.error}")
        if retryable:
            state.status = ConversationStatus.READY_TO_EXECUTE
            params["_confirm"] = True
            lines.append("\nThe provider looks temporarily unavailable. Reply **yes** to try again or **cancel**.")
            return self._reply("\n".join(lines), state, routing=params["channel"], success=False, awaiting=True,
                               execution_result=data)
        state.set_execution_result(data)
        return self._reply("\n".join(lines), state, routing=params["channel"], success=bool(ok),
                           execution_result=data)

    async def _log_results(self, owner: str, session_id: Optional[str], params: Dict[str, Any],
                           results: List[AgentResult]) -> None:
        """Persist one history row per channel. Logging problems must never affect the send result."""
        subject, body = self._rendered(params)
        for r in results:
            status = ("dry_run" if r.dry_run else "duplicate" if r.duplicate else "sent" if r.success else "failed")
            try:
                from app.services.metrics_recorder import metrics
                metrics.track_message(r.channel, status)
            except Exception:
                pass
            if not self.store:
                continue
            try:
                await self.store.run(
                    self.store.log_message, owner, r.channel, status, r.recipients,
                    subject if r.channel == "email" else None, body, session_id, r.provider_ids,
                    None if r.success else r.error, bool(params.get("drafted")))
            except Exception as e:
                logger.warning(f"⚠️ Could not record message history: {e}")

    async def _resolve_contacts(self, owner: str, text: str) -> List[Dict[str, Any]]:
        """Address-book lookup for names in the recipient part of the request (never the message body)."""
        if not self.store or not text:
            return []
        segment = re.split(r"\b(?:saying|says|that|about|regarding|subject|telling|tell them)\b|:", text,
                           maxsplit=1, flags=re.I)[0]
        segment = strip_addresses(segment)  # never match a name inside an address the user already typed
        try:
            return await self.store.run(self.store.find_in_text, owner, segment)
        except Exception as e:
            logger.warning(f"⚠️ Contact lookup failed: {e}")
            return []

    @staticmethod
    def _apply_contacts(params: Dict[str, Any], contacts: List[Dict[str, Any]], only: Optional[str] = None) -> None:
        """Add matched contacts' addresses for the chosen channel (or just `only`); remember who had no usable address."""
        channel = only or params["channel"]
        unresolved = list(params.get("_unresolved", []))
        for c in contacts:
            if channel in ("email", "both"):
                if c.get("email"):
                    if c["email"] not in params["emails"]:
                        params["emails"].append(c["email"])
                else:
                    unresolved.append(f"{c['name']} (no email saved)")
            if channel in ("whatsapp", "both"):
                if c.get("phone"):
                    if c["phone"] not in params["phones"]:
                        params["phones"].append(c["phone"])
                else:
                    unresolved.append(f"{c['name']} (no WhatsApp number saved)")
        params["_unresolved"] = unresolved

    # ================================================================== sub-agent dispatch
    async def execute_plan(self, params: Dict[str, Any]) -> List[AgentResult]:
        """Run the email and/or WhatsApp sub-agents (concurrently for 'both')."""
        channel = params["channel"]
        jobs = []
        if channel in ("email", "both"):
            jobs.append(self.email_agent.send(
                params["emails"], params.get("subject", ""), params.get("body", ""),
                template=params.get("template") or None, variables=params.get("variables") or {},
                cc=params.get("cc"), bcc=params.get("bcc"), reply_to=params.get("reply_to")))
        if channel in ("whatsapp", "both"):
            body = params.get("body", "")
            if not body and params.get("template"):  # reuse the rendered email text for WhatsApp
                from app.agents.email_templates import render_template
                try:
                    body = (render_template(params["template"], params.get("variables") or {}) or {}).get("body", "")
                except ValueError:
                    body = ""
            jobs.append(self.whatsapp_agent.send(params["phones"], body,
                                                 template_name=params.get("whatsapp_template") or None,
                                                 template_params=params.get("whatsapp_template_params")))
        outcomes = await asyncio.gather(*jobs, return_exceptions=True)
        results: List[AgentResult] = []
        for name, out in zip([c for c in ("email", "whatsapp") if channel in (c, "both")], outcomes):
            if isinstance(out, Exception):
                logger.exception(f"❌ {name} sub-agent crashed", exc_info=out)
                results.append(AgentResult(False, name, error=f"{name} agent error: {out}"))
            else:
                results.append(out)
        return results

    async def send_direct(self, channel: str, payload: Dict[str, Any], user_id: str = "api",
                          roles: Optional[List[str]] = None) -> Dict[str, Any]:
        """Structured (no-LLM) send used by the REST API: validates, rate-limits, dispatches."""
        channel = (channel or "").lower()
        if channel not in CHANNELS:
            return {"success": False, "error": f"unknown channel '{channel}'", "results": []}
        denied = self._check_role(roles or [])
        if denied:
            return {"success": False, "error": denied, "results": []}
        allowed, wait = self.rate_limiter.allow(user_id)
        if not allowed:
            return {"success": False, "error": f"rate limit exceeded; retry in {int(wait)}s", "results": [],
                    "retry_after": int(wait)}
        params = {"channel": channel, "emails": payload.get("emails") or payload.get("to_emails") or [],
                  "phones": payload.get("phones") or payload.get("to_phones") or [],
                  "subject": payload.get("subject", ""), "body": payload.get("body", ""),
                  "template": payload.get("template"), "variables": payload.get("variables") or {},
                  "cc": payload.get("cc"), "bcc": payload.get("bcc"), "reply_to": payload.get("reply_to"),
                  "whatsapp_template": payload.get("whatsapp_template"),
                  "whatsapp_template_params": payload.get("whatsapp_template_params")}
        results = await self.execute_plan(params)
        await self._log_results(user_id, None, params, results)
        return {"success": all(r.success for r in results), "results": [r.to_dict() for r in results]}

    # ================================================================== understanding the request
    async def _understand(self, text: str, user_id: str = "") -> Dict[str, Any]:
        """Understand the request, then resolve any saved-contact names the user mentioned."""
        plan = await self._understand_core(text)
        if plan["channel"] in CHANNELS:
            split = self._split_by_channel(text) if plan["channel"] == "both" else None
            if split:  # "email Alice and whatsapp Bob": each name only on the channel it was attached to
                before = (list(plan["emails"]), list(plan["phones"]))
                for channel, segment in split.items():
                    self._apply_contacts(plan, await self._resolve_contacts(user_id, segment), only=channel)
                if not (plan["emails"] and plan["phones"]):  # a part did not resolve: fall back to the plain rules
                    plan["emails"], plan["phones"] = before
                    plan.pop("_unresolved", None)
                    split = None
            if not split:
                self._apply_contacts(plan, await self._resolve_contacts(user_id, text))
        return plan

    @staticmethod
    def _split_by_channel(text: str) -> Optional[Dict[str, str]]:
        """{"email": "<who>", "whatsapp": "<who>"} when the recipient part names each channel separately
        ("Email Alice and WhatsApp Bob about ..."); None otherwise."""
        head = re.split(r"\b(?:saying|says|that|about|regarding|subject|telling|tell them)\b|:", text, maxsplit=1,
                        flags=re.I)[0]
        e, w = re.search(r"\be-?mails?\b|\bmail\b", head, re.I), re.search(r"\bwhatsapp\b|\bwa\b", head, re.I)
        if not (e and w):
            return None
        if e.start() < w.start():
            segments = {"email": head[e.end():w.start()], "whatsapp": head[w.end():]}
        else:
            segments = {"whatsapp": head[w.end():e.start()], "email": head[e.end():]}
        segments = {ch: re.sub(r"\band\b", " ", seg, flags=re.I).strip() for ch, seg in segments.items()}
        return segments if all(re.search(r"[A-Za-z]{2,}", strip_addresses(s)) for s in segments.values()) else None

    async def _understand_core(self, text: str) -> Dict[str, Any]:
        """Regex baseline + (when useful) one JSON-mode mistral call. Recipients never come from the LLM."""
        plan = self._baseline(text)
        plan["llm_body"] = False
        need_llm = plan["channel"] not in CHANNELS or (not plan["body"] and not plan["template"])
        if not need_llm:
            return plan
        try:
            # generous timeout: the first call after a cold start has to load the model
            data = await self.llm.chat_json(EXTRACT_PROMPT, text, max_tokens=300, timeout=self.draft_timeout,
                                            operation="orchestrator_extract")
        except Exception as e:
            logger.warning(f"⚠️ LLM extraction unavailable ({e}); using rule-based extraction")
            return plan
        channel = str(data.get("channel", "")).strip().lower()
        if channel == "none":
            plan["channel"] = "none"
            return plan
        if plan["channel"] not in CHANNELS and channel in CHANNELS:
            plan["channel"] = channel
        template = str(data.get("template", "") or "").strip()
        if template in TEMPLATES and not plan["template"]:
            plan["template"] = template
        if isinstance(data.get("variables"), dict):
            for k, v in data["variables"].items():
                if str(v).strip() and k not in plan["variables"]:
                    plan["variables"][k] = str(v).strip()
        if not plan["subject"] and isinstance(data.get("subject"), str):
            plan["subject"] = data["subject"].strip()[:150]
        if not plan["body"] and isinstance(data.get("body"), str) and self._clean_draft(data["body"]):
            plan["body"], plan["llm_body"] = self._clean_draft(data["body"]), True
        # a channel implied by the addresses we found beats a vague answer
        if plan["channel"] == "email" and plan["phones"] and not plan["emails"]:
            plan["channel"] = "whatsapp"
        return plan

    def _baseline(self, text: str) -> Dict[str, Any]:
        """Deterministic extraction: recipients, channel keywords, explicit subject/body."""
        emails = [e for e in (normalize_email(a) for a in find_emails(text)) if e]
        phones = find_phones(text)
        low = text.lower()
        has_email = bool(re.search(r"\be-?mails?\b|\bmail\b", low)) or bool(emails)
        has_wa = bool(re.search(r"\bwhatsapp\b|\bwa\b", low))
        channel = "both" if (has_email and has_wa) else "whatsapp" if has_wa else "email" if has_email else ""
        if phones and not emails and channel != "both" and not re.search(r"\be-?mails?\b|\bmail\b", low):
            channel = "whatsapp"  # a bare phone number implies WhatsApp
        subject = body = ""
        m = _SUBJECT.search(text)
        if m:
            subject = m.group("subject").strip()[:150]
        rest = text[m.end():] if m else text
        mb = _BODY_SAYING.search(rest) or _BODY_LABEL.search(rest)
        if mb:
            body = mb.group("body").strip().strip("\"“”'‘’").strip()
        else:
            quoted = sorted(_QUOTED.findall(rest), key=len, reverse=True)
            if quoted:
                body = quoted[0].strip()
        if len(body) < 2:
            body = ""
        return {"channel": channel, "emails": emails, "phones": phones, "subject": subject, "body": body,
                "template": "", "variables": {}}

    @staticmethod
    def _merge(params: Dict[str, Any], found: Dict[str, Any]) -> None:
        for key in ("emails", "phones"):
            for v in found[key]:
                if v not in params[key]:
                    params[key].append(v)
        if found["subject"]:
            params["subject"] = found["subject"]
        if found["body"]:
            params["body"], params["drafted"] = found["body"], False
        if params["channel"] == "email" and found["channel"] in ("whatsapp", "both") and found["phones"]:
            params["channel"] = "both" if params["emails"] else "whatsapp"

    def _apply_edit(self, params: Dict[str, Any], text: str) -> bool:
        before = (list(params["emails"]), list(params["phones"]), params.get("subject"), params.get("body"))
        found = self._baseline(text)
        self._merge(params, found)
        if not found["body"]:
            m = re.search(r"\b(?:change|make|set|update)\b.*\b(?:message|body|text)\b\s*(?:to|:)\s*(.+)$", text, re.I | re.S)
            if m:
                params["body"], params["drafted"] = m.group(1).strip().strip("\"“”'"), False
        return before != (params["emails"], params["phones"], params.get("subject"), params.get("body"))

    async def _draft(self, instruction: str, channel: str) -> Optional[Dict[str, str]]:
        label = "email" if channel in ("email", "both") else "WhatsApp"
        try:
            data = await self.llm.chat_json(DRAFT_PROMPT.format(channel=label), instruction, max_tokens=500,
                                            timeout=self.draft_timeout, operation="orchestrator_draft")
        except Exception as e:
            logger.warning(f"⚠️ LLM drafting unavailable ({e})")
            return None
        body = self._clean_draft(str(data.get("body", "")))
        if len(body) < 5:
            return None
        return {"subject": self._clean_draft(str(data.get("subject", "")))[:150], "body": body[:4000]}

    @staticmethod
    def _clean_draft(text: str) -> str:
        """Remove template placeholders ([Name], [Organization]) and dangling sign-offs the model tends to add."""
        text = re.sub(r"\[[^\]\n]{2,40}\]", "", text or "")
        lines = [ln.rstrip() for ln in text.strip().splitlines()]
        while lines and re.fullmatch(r"(best |kind |warm )?(regards|sincerely|thanks|thank you|cheers)[,.!]?\s*",
                                     lines[-1].strip(), re.I) or (lines and not lines[-1].strip()):
            lines.pop()
        return re.sub(r"[ \t]{2,}", " ", "\n".join(lines)).strip()

    # ================================================================== text helpers
    @staticmethod
    def _has_content_cue(query: str) -> bool:
        """True when the request says what the message is about. "send an email to hr@acme.com" has nothing for
        the model to draft from (it would invent a message), so the user is asked instead."""
        words = re.sub(r"[^\w\s]", " ", strip_addresses(query)).lower().split()
        filler = {"send", "an", "a", "the", "email", "e", "mail", "emails", "whatsapp", "wa", "message", "msg", "to",
                  "text", "please", "pls", "can", "you", "could", "compose", "draft", "write", "and", "also", "on",
                  "forward", "notify", "ping", "tell", "them", "him", "her", "my", "new", "quick", "short", "now"}
        return len([w for w in words if w not in filler]) >= 2

    @staticmethod
    def _derive_subject(body: str) -> str:
        first = re.split(r"(?<=[.!?])\s|\n", body.strip(), maxsplit=1)[0]
        return (first[:67] + "...") if len(first) > 70 else first or "Message"

    @staticmethod
    def _question(field: str, params: Dict[str, Any]) -> str:
        unresolved = params.get("_unresolved") or []
        hint = f" ({'; '.join(unresolved)})" if unresolved else ""
        if field == "recipient_email":
            return f"Who should I email? Please give the recipient's email address or a saved contact name.{hint}"
        if field == "recipient_phone":
            return ("Which WhatsApp number should I send to? Please include the country code "
                    f"(e.g. +919876543210) or give a saved contact name.{hint}")
        if field == "body":
            return "What should the message say?"
        var = field[4:].replace("_", " ")
        return f"Please provide the {var} for the {params.get('template', '').replace('_', ' ')}."

    @staticmethod
    def _rendered(params: Dict[str, Any]):
        """(subject, body) exactly as they will be sent - templates are rendered."""
        body, subject = params.get("body"), params.get("subject")
        if params.get("template") and not body:
            from app.agents.email_templates import render_template
            try:
                r = render_template(params["template"], params.get("variables") or {})
                body, subject = r["body"], subject or r["subject"]
            except (ValueError, TypeError):
                pass
        return subject, body

    @staticmethod
    def _preview_data(params: Dict[str, Any]) -> Dict[str, Any]:
        subject, body = OrchestratorAgent._rendered(params)
        return {"channel": params["channel"], "emails": params["emails"], "phones": params["phones"],
                "subject": subject, "body": body, "drafted_by_ai": bool(params.get("drafted"))}

    @staticmethod
    def _preview(params: Dict[str, Any]) -> str:
        channel = params["channel"]
        subject, body = OrchestratorAgent._rendered(params)
        parts = []
        if channel in ("email", "both"):
            parts.append("📧 **Email**\n**To:** " + ", ".join(params["emails"]) + f"\n**Subject:** {subject}")
        if channel in ("whatsapp", "both"):
            parts.append("💬 **WhatsApp**\n**To:** " + ", ".join(params["phones"]))
        quoted = "\n".join("> " + ln for ln in (body or "").splitlines())
        note = "\n\n*This text was written by the AI assistant from your request - please review it.*" \
            if params.get("drafted") else ""
        return ("Here's what I'm about to send:\n\n" + "\n\n".join(parts) + f"\n\n**Message:**\n{quoted}{note}\n\n"
                "Reply **yes** to send, **cancel** to discard, or tell me what to change.")

    def _check_role(self, roles: List[str]) -> Optional[str]:
        if self.allowed_roles and not ({r.lower() for r in roles or []} & set(self.allowed_roles)):
            return "You don't have permission to send emails or WhatsApp messages."
        return None

    @staticmethod
    def _target_name(channel: str) -> str:
        return {"email": "EmailServiceAgent", "whatsapp": "WhatsAppAgent"}.get(channel, "EmailServiceAgent+WhatsAppAgent")

    def _reply(self, text: str, state: ConversationState, *, routing: Optional[str] = None, success: bool = True,
               missing: Optional[List[str]] = None, awaiting: bool = False,
               execution_result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        meta = {"channel": state.resource_type, "missing_params": [m.replace("var:", "") for m in missing or []],
                "awaiting_confirmation": awaiting, "status": state.status.value}
        if awaiting and state.collected_params.get("channel"):
            meta["preview"] = self._preview_data(state.collected_params)
        return {"success": success, "handled": True, "response": text,
                "routing": routing or state.resource_type or "orchestrator",
                "execution_result": execution_result, "metadata": meta}

    @staticmethod
    def _finish(state: ConversationState, reply: Dict[str, Any]) -> Dict[str, Any]:
        state.add_message("assistant", reply.get("response", ""), {"routing": reply.get("routing")})
        conversation_state_manager.update_session(state)
        return reply
