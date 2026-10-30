"""
Live check that the real local Ollama `mistral` model drives the orchestrator correctly.
Skipped automatically when Ollama is not running or the model is not pulled.
Senders are fakes, so nothing is ever emailed or messaged.   Run: python -m pytest tests/test_ollama_live.py -q
"""
import asyncio
import logging

import pytest

from app.services.ollama_client import OllamaClient
from tests.test_agents import FakeEmail, FakeWA, make_orch, say, sid

logging.disable(logging.CRITICAL)


def _live_client():
    c = OllamaClient()
    c.timeout = 120
    st = asyncio.run(c.status(max_age=0))
    return (c, st)


_client, _status = _live_client()
pytestmark = pytest.mark.skipif(not (_status["reachable"] and _status["model_pulled"]),
                                reason="Ollama/mistral not available")


@pytest.fixture(scope="module")
def orch():
    o = make_orch(email=FakeEmail(), wa=FakeWA(), confirm=True)
    o.llm = _client
    o.draft_timeout = 120
    return o


def test_status_reports_the_model():
    assert _status["reachable"] and _status["model_pulled"] and _status["model"] == "mistral"


def test_mistral_drafts_an_email_from_a_short_instruction(orch):
    r = say(orch, sid(), "send an email to ops@acme.com about tonight's planned maintenance window from 10pm to 11pm")
    p = r["metadata"]["preview"]
    assert p["emails"] == ["ops@acme.com"] and p["drafted_by_ai"] is True
    assert "10" in p["body"] and "[" not in p["body"] and p["subject"]


def test_mistral_extracts_a_whatsapp_message(orch):
    r = say(orch, sid(), "whatsapp +919876543210 that the delivery will be delayed by one day")
    p = r["metadata"]["preview"]
    assert p["channel"] == "whatsapp" and p["phones"] == ["+919876543210"]
    assert "delay" in p["body"].lower() and "[" not in p["body"]


def test_mistral_knows_a_how_to_question_is_not_a_send_request(orch):
    r = say(orch, sid(), "how do I send an email from the customer portal?")
    assert r["handled"] is False


def test_mistral_picks_the_invite_template_or_equivalent(orch):
    r = say(orch, sid(), "send an email inviting team@acme.com to a meeting about Q3 planning on Friday at 3pm")
    text = (r["response"] + str(r["metadata"])).lower()
    assert r["metadata"].get("preview") or r["metadata"].get("missing_params")
    assert "q3" in text or "planning" in text or "date_time" in text


def test_full_flow_with_the_real_model_sends_after_confirmation():
    email = FakeEmail()
    o = make_orch(email=email, wa=FakeWA(), confirm=True)
    o.llm, o.draft_timeout = _client, 120
    s = sid()
    say(o, s, "send an email to hr@acme.com about the office being closed on Friday for maintenance")
    r = say(o, s, "yes")
    assert r["success"] and email.calls and email.calls[0]["to"] == ["hr@acme.com"]
    assert "friday" in email.calls[0]["body"].lower()
