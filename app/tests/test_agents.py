"""
Orchestrator + Email/WhatsApp sub-agents. No network: the LLM is faked, SMTP is patched and the
WhatsApp HTTP layer uses httpx.MockTransport.
"""
import asyncio
import json
import logging
import uuid

import aiosmtplib
import httpx
import pytest

from app.agents import common
from app.agents.base_agent import BaseAgent
from app.agents.common import AgentResult, find_phones, normalize_phone
from app.agents.email_agent import EmailAgent
from app.agents.orchestrator_agent import OrchestratorAgent, looks_like_send_request
from app.agents.whatsapp_agent import WhatsAppAgent

logging.disable(logging.CRITICAL)


def run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------------- fakes
class FakeLLM:
    model = "mistral"
    available = True

    def __init__(self, extract=None, draft=None, down=False):
        self.extract, self.draft, self.down, self.calls = extract, draft, down, []

    async def chat_json(self, system, user, max_tokens=120, timeout=None, operation="x"):
        self.calls.append(operation)
        if self.down:
            raise RuntimeError("ollama down")
        return (self.draft if operation == "orchestrator_draft" else self.extract) or {}

    def warm_up(self):
        pass


class FakeEmail:
    agent_name = "EmailServiceAgent"

    def __init__(self, ok=True, retryable=False):
        self.calls, self.ok, self.retryable = [], ok, retryable

    async def send(self, to, subject="", body="", **kw):
        self.calls.append({"to": to, "subject": subject, "body": body, **kw})
        if self.ok:
            return AgentResult(True, "email", message="Email sent", recipients=to)
        return AgentResult(False, "email", error="smtp down", retryable=self.retryable, recipients=to)

    def health(self):
        return {"ready": True}


class FakeWA(FakeEmail):
    agent_name = "WhatsAppAgent"

    async def send(self, to, body="", **kw):
        self.calls.append({"to": to, "body": body, **kw})
        return AgentResult(True, "whatsapp", message="WhatsApp sent", recipients=to)


def make_orch(llm=None, confirm=True, email=None, wa=None, store=None):
    o = OrchestratorAgent.__new__(OrchestratorAgent)  # skip __init__ (it starts an Ollama warm-up thread)
    BaseAgent.__init__(o, "OrchestratorAgent", "test")
    o.email_agent, o.whatsapp_agent = email or FakeEmail(), wa or FakeWA()
    o.store = store
    o.require_confirmation = confirm
    o.rate_limiter = common.RateLimiter(0)
    o.allowed_roles, o.max_clarifications, o.draft_timeout = [], 5, 5
    o.llm = llm or FakeLLM(extract={"channel": "email"})
    return o


def say(o, session, text, roles=("viewer",)):
    return run(o.orchestrate(text, session, "u1", list(roles)))


def sid():
    return "t-" + uuid.uuid4().hex


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    async def nosleep(_):
        pass

    monkeypatch.setattr(common.asyncio, "sleep", nosleep)


# ----------------------------------------------------------------------------- common helpers
def test_phone_normalisation():
    assert normalize_phone("+91 98765-43210") == "+919876543210"
    assert normalize_phone("9876543210") == "+919876543210"  # default country code
    assert normalize_phone("0044 7911 123456") == "+447911123456"
    assert normalize_phone("12345") is None


def test_find_phones_ignores_digits_inside_emails():
    assert find_phones("mail user1234567890@x.com") == []
    assert find_phones("whatsapp +14155550100 now") == ["+14155550100"]


@pytest.mark.parametrize("text,expected", [
    ("send an email to a@b.com saying hi", True),
    ("whatsapp +919876543210 that I am late", True),
    ("email hr@acme.com the report", True),
    ("how do I send an email from the portal?", False),
    ("what is whatsapp business", False),
    ("email Alice saying the meeting moved", True),
    ("whatsapp Bob the delivery update", True),
    ("email is not working", False),
    ("mail delivery is slow", False),
    ("list my clusters", False),
    ("explain load balancing", False),
    ("invite team@acme.com to a meeting about Q3 planning on Friday 3pm", True),
    ("remind +919876543210 about the payment due Friday", True),
    ("notify ops@acme.com that the server is down", True),
    ("what is the best way to invite team@acme.com", False),
    ("email and whatsapp Alice saying the review moved", True),
    ("email and whatsapp are down", False),
])
def test_send_request_prefilter(text, expected):
    assert looks_like_send_request(text) is expected


def test_baseline_extraction():
    o = make_orch()
    p = o._baseline('Send an email to Bob@Acme.com subject: Q3 plan saying "See you Monday at 10"')
    assert p["emails"] == ["Bob@acme.com"] and p["channel"] == "email"
    assert p["subject"] == "Q3 plan" and p["body"] == "See you Monday at 10"
    p = o._baseline("send a message to +919876543210 saying running late")
    assert p["phones"] == ["+919876543210"] and p["body"] == "running late" and p["channel"] == "whatsapp"


# ----------------------------------------------------------------------------- orchestrator
def test_non_send_message_is_not_handled():
    assert say(make_orch(), sid(), "how many clusters in delhi")["handled"] is False


def test_llm_says_none_means_not_handled():
    o = make_orch(FakeLLM(extract={"channel": "none"}))
    assert say(o, sid(), "write a message about the policy")["handled"] is False


def test_email_preview_then_confirm_sends():
    email = FakeEmail()
    o = make_orch(email=email)
    s = sid()
    r = say(o, s, 'email bob@acme.com subject: Hello saying "Office closed tomorrow"')
    assert r["metadata"]["awaiting_confirmation"] and "bob@acme.com" in r["response"] and not email.calls
    r = say(o, s, "yes")
    assert r["success"] and len(email.calls) == 1
    assert email.calls[0]["to"] == ["bob@acme.com"] and email.calls[0]["subject"] == "Hello"
    assert say(o, s, "yes")["handled"] is False  # finished; a bare "yes" is not a new request


def test_bare_send_request_asks_for_the_message_instead_of_inventing_one():
    llm = FakeLLM(extract={"channel": "email"}, draft={"subject": "Application", "body": "Thank you for considering my application"})
    o = make_orch(llm)
    s = sid()
    r = say(o, s, "send an email to hr@acme.com")
    assert "orchestrator_draft" not in llm.calls and r["metadata"]["missing_params"] == ["body"]
    r = say(o, s, "the payroll is delayed by two days")  # the answer is taken as the message, not rejected
    assert r["metadata"]["awaiting_confirmation"] and "payroll is delayed" in r["response"]


def test_request_with_a_topic_is_still_drafted():
    llm = FakeLLM(extract={"channel": "email"}, draft={"subject": "Thanks", "body": "Thank you for the contract review."})
    r = say(make_orch(llm), sid(), "email legal@acme.com thanking them for the contract review")
    assert "orchestrator_draft" in llm.calls and r["metadata"]["preview"]["drafted_by_ai"]


def test_cancel_sends_nothing():
    email = FakeEmail()
    o = make_orch(email=email)
    s = sid()
    say(o, s, 'email bob@acme.com saying "hi there"')
    assert "cancelled" in say(o, s, "cancel")["response"].lower()
    assert not email.calls


def test_no_confirmation_mode_sends_immediately_for_explicit_text():
    email = FakeEmail()
    o = make_orch(confirm=False, email=email)
    r = say(o, sid(), 'email bob@acme.com subject: Hi saying "Lunch at 1pm"')
    assert r["success"] and len(email.calls) == 1


def test_missing_recipient_is_asked_then_collected():
    email = FakeEmail()
    o = make_orch(FakeLLM(extract={"channel": "email", "body": "The meeting is at 4pm."}), confirm=False,
                  email=email)
    s = sid()
    r = say(o, s, "send an email about the meeting at 4pm")
    assert "recipient_email" in r["metadata"]["missing_params"] and not email.calls
    r = say(o, s, "carol@acme.com")
    # the text came from the LLM, so it must be previewed even though confirmation is off
    assert r["metadata"]["awaiting_confirmation"] and not email.calls
    assert say(o, s, "yes")["success"] and email.calls[0]["to"] == ["carol@acme.com"]


def test_llm_never_supplies_recipients():
    o = make_orch(FakeLLM(extract={"channel": "email", "emails": ["evil@x.com"], "body": "hello there"}))
    r = say(o, sid(), "send an email saying hello there")
    assert "evil@x.com" not in r["response"] and "recipient_email" in r["metadata"]["missing_params"]


def test_llm_draft_requires_review():
    email = FakeEmail()
    llm = FakeLLM(extract={"channel": "email"}, draft={"subject": "Delay", "body": "Your order is delayed by a day."})
    o = make_orch(llm, confirm=False, email=email)
    s = sid()
    r = say(o, s, "send an email to dan@acme.com about the delayed order")
    assert r["metadata"]["awaiting_confirmation"] and "written by the AI" in r["response"]
    assert "orchestrator_draft" in llm.calls and not email.calls
    assert say(o, s, "yes")["success"] and email.calls[0]["subject"] == "Delay"


def test_llm_down_falls_back_to_asking_for_the_body():
    o = make_orch(FakeLLM(down=True))
    s = sid()
    r = say(o, s, "send an email to dan@acme.com")
    assert r["metadata"]["missing_params"] == ["body"]
    r = say(o, s, "Please review the attached policy by Friday.")
    assert r["metadata"]["awaiting_confirmation"]


def test_edit_during_confirmation():
    email = FakeEmail()
    o = make_orch(email=email)
    s = sid()
    say(o, s, 'email bob@acme.com subject: Old saying "Same text here"')
    say(o, s, "subject: New subject")
    say(o, s, "yes")
    assert email.calls[0]["subject"] == "New subject" and email.calls[0]["body"] == "Same text here"


def test_both_channels_dispatch_to_both_sub_agents():
    email, wa = FakeEmail(), FakeWA()
    o = make_orch(email=email, wa=wa, confirm=False)
    r = say(o, sid(), 'email ops@acme.com and whatsapp +14155550100 saying "Server maintenance at 9pm"')
    assert r["success"] and email.calls and wa.calls
    assert wa.calls[0]["to"] == ["+14155550100"]


def test_template_asks_for_missing_variables():
    email = FakeEmail()
    llm = FakeLLM(extract={"channel": "email", "template": "meeting_invite", "variables": {"topic": "Q3 planning"}})
    o = make_orch(llm, email=email)
    s = sid()
    r = say(o, s, "invite team@acme.com to a meeting about Q3 planning by email")
    assert r["metadata"]["missing_params"] == ["date_time"]
    r = say(o, s, "Friday 3pm")
    assert "Friday 3pm" in r["response"] and "Meeting invitation" in r["response"]
    say(o, s, "yes")
    assert email.calls[0]["variables"]["date_time"] == "Friday 3pm" and email.calls[0]["template"] == "meeting_invite"


def test_retryable_failure_keeps_conversation_for_retry():
    email = FakeEmail(ok=False, retryable=True)
    o = make_orch(email=email)
    s = sid()
    say(o, s, 'email bob@acme.com saying "Status update attached"')
    r = say(o, s, "yes")
    assert not r["success"] and "try again" in r["response"].lower()
    email.ok = True
    assert say(o, s, "yes")["success"] and len(email.calls) == 2


def test_rate_limit_blocks_excess_sends():
    email = FakeEmail()
    o = make_orch(email=email, confirm=False)
    o.rate_limiter = common.RateLimiter(1)
    say(o, sid(), 'email a@acme.com saying "first message"')
    r = say(o, sid(), 'email a@acme.com saying "second message"')
    assert "limit" in r["response"].lower() and len(email.calls) == 1


def test_role_gate():
    o = make_orch()
    o.allowed_roles = ["admin"]
    r = say(o, sid(), 'email a@acme.com saying "hello there"', roles=("viewer",))
    assert r["error"] == "forbidden"
    assert say(o, sid(), 'email a@acme.com saying "hello there"', roles=("admin",))["handled"]


def test_send_direct_validates_channel():
    o = make_orch()
    assert run(o.send_direct("sms", {}))["success"] is False
    r = run(o.send_direct("email", {"emails": ["a@acme.com"], "subject": "s", "body": "b"}))
    assert r["success"] and o.email_agent.calls


# ----------------------------------------------------------------------------- EmailAgent
@pytest.fixture
def mail(monkeypatch):
    a = EmailAgent()
    a.host, a.sender, a.username, a.password = "smtp.test", "noreply@acme.com", "u", "p"
    a.max_attempts = 3
    sent = []

    async def fake_send(msg, **kw):
        sent.append((msg, kw))

    monkeypatch.setattr(aiosmtplib, "send", fake_send)
    a.sent = sent
    return a


def test_email_sends_multipart_message(mail):
    r = run(mail.send(["bob@acme.com"], "Hi", "Line1\nLine2 <b>"))
    assert r.success and len(mail.sent) == 1
    msg, kw = mail.sent[0]
    assert msg["Subject"] == "Hi" and msg["To"] == "bob@acme.com" and kw["recipients"] == ["bob@acme.com"]
    assert msg.get_body(("plain",)).get_content().startswith("Line1")
    assert "&lt;b&gt;" in msg.get_body(("html",)).get_content()  # html body is escaped


def test_email_rejects_invalid_and_missing_fields(mail):
    assert "invalid recipient" in run(mail.send(["not-an-email"], "s", "b")).error
    assert "subject" in run(mail.send(["a@acme.com"], "", "b")).error
    assert "body" in run(mail.send(["a@acme.com"], "s", "")).error
    assert not mail.sent


def test_email_header_injection_is_neutralised(mail):
    run(mail.send(["a@acme.com"], "Hi\r\nBcc: evil@x.com", "body"))
    msg, _ = mail.sent[0]
    assert msg["Bcc"] is None and "\n" not in msg["Subject"]


def test_email_domain_allowlist_and_recipient_cap(mail):
    mail.allowed_domains = ["acme.com"]
    assert "not allowed" in run(mail.send(["x@other.com"], "s", "b")).error
    mail.allowed_domains, mail.max_recipients = [], 2
    assert "too many" in run(mail.send(["a@acme.com", "b@acme.com", "c@acme.com"], "s", "b")).error


def test_email_retries_transient_then_succeeds(mail, monkeypatch):
    attempts = {"n": 0}

    async def flaky(msg, **kw):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise aiosmtplib.SMTPServerDisconnected("dropped")

    monkeypatch.setattr(aiosmtplib, "send", flaky)
    assert run(mail.send(["a@acme.com"], "s", "b")).success and attempts["n"] == 3


def test_email_gives_up_after_retries_as_retryable(mail, monkeypatch):
    async def down(msg, **kw):
        raise aiosmtplib.SMTPResponseException(451, "try later")

    monkeypatch.setattr(aiosmtplib, "send", down)
    r = run(mail.send(["a@acme.com"], "s", "b"))
    assert not r.success and r.retryable


def test_email_auth_failure_is_permanent_and_not_retried(mail, monkeypatch):
    n = {"n": 0}

    async def bad(msg, **kw):
        n["n"] += 1
        raise aiosmtplib.SMTPAuthenticationError(535, "bad creds")

    monkeypatch.setattr(aiosmtplib, "send", bad)
    r = run(mail.send(["a@acme.com"], "s", "b"))
    assert not r.success and not r.retryable and n["n"] == 1


def test_email_duplicate_is_suppressed(mail):
    run(mail.send(["a@acme.com"], "s", "b"))
    r = run(mail.send(["a@acme.com"], "s", "b"))
    assert r.success and r.duplicate and len(mail.sent) == 1


def test_email_template_rendering_and_errors(mail):
    r = run(mail.send(["a@acme.com"], template="outage_notice", variables={"system": "Billing", "start_time": "9pm"}))
    assert r.success
    msg, _ = mail.sent[0]
    assert "Billing" in msg["Subject"] and "9pm" in msg.get_body(("plain",)).get_content()
    assert "missing template variables" in run(mail.send(["a@acme.com"], template="reminder", variables={})).error
    assert "unknown" in run(mail.send(["a@acme.com"], template="nope")).error


def test_email_unconfigured_and_dry_run():
    a = EmailAgent()
    a.host = ""
    assert "not configured" in run(a.send(["a@acme.com"], "s", "b")).error
    a.dry_run = True
    r = run(a.send(["a@acme.com"], "s", "b"))
    assert r.success and r.dry_run


# ----------------------------------------------------------------------------- WhatsAppAgent
def wa_agent(handler, provider="meta"):
    a = WhatsAppAgent()
    a.provider = provider
    a.token, a.phone_number_id = "tok", "123"
    a.twilio_sid, a.twilio_token, a.twilio_from = "AC1", "secret", "+14155238886"
    a._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return a


def test_whatsapp_meta_text_payload():
    seen = {}

    def handler(req):
        seen["url"], seen["auth"], seen["json"] = str(req.url), req.headers["authorization"], json.loads(req.content)
        return httpx.Response(200, json={"messages": [{"id": "wamid.1"}]})

    async def go():
        a = wa_agent(handler)
        try:
            return await a.send(["9876543210"], "Hello")
        finally:
            await a.close()

    r = run(go())
    assert r.success and r.provider_ids == ["wamid.1"]
    assert seen["url"].endswith("/123/messages") and seen["auth"] == "Bearer tok"
    assert seen["json"]["to"] == "919876543210" and seen["json"]["text"]["body"] == "Hello"


def test_whatsapp_meta_template_payload():
    seen = {}

    def handler(req):
        seen["json"] = json.loads(req.content)
        return httpx.Response(200, json={"messages": [{"id": "x"}]})

    run(wa_agent(handler).send(["+14155550100"], "", template_name="order_update", template_params=["A1"]))
    t = seen["json"]
    assert t["type"] == "template" and t["template"]["name"] == "order_update"
    assert t["template"]["components"][0]["parameters"][0]["text"] == "A1"


def test_whatsapp_retries_429_honouring_retry_after():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1"}, json={"error": {"message": "slow down"}})
        return httpx.Response(200, json={"messages": [{"id": "ok"}]})

    assert run(wa_agent(handler).send(["+14155550100"], "hi")).success and calls["n"] == 2


def test_whatsapp_4xx_is_permanent_and_surfaces_provider_error():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(400, json={"error": {"message": "Recipient not in allowed list", "code": 131030}})

    r = run(wa_agent(handler).send(["+14155550100"], "hi"))
    assert not r.success and not r.retryable and calls["n"] == 1 and "131030" in r.error


def test_whatsapp_5xx_exhausts_retries_as_retryable():
    r = run(wa_agent(lambda req: httpx.Response(503, text="down")).send(["+14155550100"], "hi"))
    assert not r.success and r.retryable


def test_whatsapp_network_error_is_retried():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={"messages": [{"id": "ok"}]})

    assert run(wa_agent(handler).send(["+14155550100"], "hi")).success and calls["n"] == 3


def test_whatsapp_validation():
    a = wa_agent(lambda req: httpx.Response(200, json={}))
    assert "invalid phone" in run(a.send(["abc"], "hi")).error
    assert "required" in run(a.send(["+14155550100"], "  ")).error
    assert "too long" in run(a.send(["+14155550100"], "x" * 5000)).error


def test_whatsapp_partial_failure_reports_per_recipient():
    def handler(req):
        body = json.loads(req.content)
        if body["to"] == "14155550101":
            return httpx.Response(400, json={"error": {"message": "bad", "code": 1}})
        return httpx.Response(200, json={"messages": [{"id": "ok"}]})

    r = run(wa_agent(handler).send(["+14155550100", "+14155550101"], "hi"))
    assert r.success and len(r.details["failed"]) == 1 and "failed for 1" in r.message


def test_whatsapp_twilio_payload():
    seen = {}

    def handler(req):
        seen["url"], seen["body"] = str(req.url), req.content.decode()
        return httpx.Response(201, json={"sid": "SM1"})

    r = run(wa_agent(handler, provider="twilio").send(["+14155550100"], "Hi there"))
    assert r.success and r.provider_ids == ["SM1"]
    assert "Accounts/AC1/Messages.json" in seen["url"] and "whatsapp%3A%2B14155550100" in seen["body"]


def test_whatsapp_duplicate_suppressed_and_unconfigured():
    n = {"n": 0}

    def handler(req):
        n["n"] += 1
        return httpx.Response(200, json={"messages": [{"id": "ok"}]})

    a = wa_agent(handler)

    async def twice():
        await a.send(["+14155550100"], "same")
        return await a.send(["+14155550100"], "same")

    assert run(twice()).duplicate and n["n"] == 1
    b = WhatsAppAgent()
    b.token = ""
    assert "not configured" in run(b.send(["+14155550100"], "hi")).error


# ----------------------------------------------------------------------------- REST API
def test_direct_send_endpoints_fail_closed_and_check_key(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.agents import agent_manager as am
    from app.api.routes import agent_chat

    mgr = am.AgentManager()
    mgr.email_agent, mgr.whatsapp_agent = FakeEmail(), FakeWA()
    mgr.orchestrator = make_orch(email=mgr.email_agent, wa=mgr.whatsapp_agent)
    mgr.initialized = True
    monkeypatch.setattr(am, "agent_manager", mgr)

    app = FastAPI()
    app.include_router(agent_chat.router)
    c = TestClient(app)
    body = {"to": ["a@acme.com"], "subject": "s", "body": "b"}

    monkeypatch.setenv("AGENT_REQUIRE_AUTH", "false")
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    assert c.post("/api/agent/email/send", json=body).status_code == 503
    monkeypatch.setenv("AGENT_API_KEY", "k3y")
    assert c.post("/api/agent/email/send", json=body).status_code == 401
    assert c.post("/api/agent/email/send", json=body, headers={"X-API-Key": "wrong"}).status_code == 401
    ok = c.post("/api/agent/email/send", json=body, headers={"X-API-Key": "k3y"})
    assert ok.status_code == 200 and ok.json()["success"]
    wa = c.post("/api/agent/whatsapp/send", json={"to": ["+14155550100"], "body": "hi"},
                headers={"X-API-Key": "k3y"})
    assert wa.status_code == 200
    chat = c.post("/api/agent/chat", json={"message": "list my clusters"})
    assert chat.status_code == 200 and chat.json()["handled"] is False


# ----------------------------------------------------------------------------- regressions from real-model run
def test_bare_yes_is_not_accepted_as_the_message_body():
    o = make_orch(FakeLLM(down=True))
    s = sid()
    say(o, s, "send an email to dan@acme.com")
    for word in ("yes", "ok"):
        r = say(o, s, word)
        assert r["metadata"]["missing_params"] == ["body"] and not r["metadata"]["awaiting_confirmation"]


def test_draft_placeholders_and_signoffs_are_stripped():
    clean = OrchestratorAgent._clean_draft
    assert clean("Dear Customer, it is delayed.\n\nBest Regards,\n[Organization Name]") == \
        "Dear Customer, it is delayed."
    assert clean("Hello [Name], see you at 4pm.") == "Hello , see you at 4pm."


def test_bare_phone_number_implies_whatsapp():
    assert make_orch()._baseline("send a message to +919876543210 saying running late")["channel"] == "whatsapp"
