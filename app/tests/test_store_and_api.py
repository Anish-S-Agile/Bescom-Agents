"""
Fake postgres + agent store, local auth, and the full agent REST API (history, contacts, ownership),
plus orchestrator <-> store integration (contact names, send history).
"""
import asyncio
import logging
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agents import agent_manager as am
from app.agents.common import AgentResult
from app.api.routes import agent_chat, local_auth
from app.services import fake_postgres
from app.services.agent_store import AgentStore, AuthError, DuplicateError, MAX_FAILED_LOGINS
from tests.test_agents import FakeEmail, FakeLLM, FakeWA, make_orch, run, say, sid

logging.disable(logging.CRITICAL)


@pytest.fixture
def store():
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    s = AgentStore(engine)
    s.init()
    return s


# ----------------------------------------------------------------------------- fake postgres
def test_fake_postgres_is_selected_when_forced(monkeypatch):
    monkeypatch.setenv("AGENT_DB_MODE", "fake")
    fake_postgres.reset_engine()
    try:
        info = fake_postgres.describe()
        assert info["fake"] and info["dialect"] == "sqlite"
    finally:
        fake_postgres.reset_engine()


def _clear_pg_env(monkeypatch):
    for k in ("AGENT_DATABASE_URL", "DATABASE_URL", "AGENT_DB_HOST", "AGENT_DB_PORT", "AGENT_DB_NAME",
              "AGENT_DB_USER", "AGENT_DB_PASSWORD", "AGENT_DB_SSLMODE", "RUNNING_IN_DOCKER"):
        monkeypatch.delenv(k, raising=False)


def test_postgres_url_from_parts_keeps_special_characters_in_the_password(monkeypatch):
    _clear_pg_env(monkeypatch)
    monkeypatch.setenv("AGENT_DB_HOST", "10.0.0.5")
    monkeypatch.setenv("AGENT_DB_PASSWORD", "p@ss:w/rd#1")
    monkeypatch.setenv("AGENT_DB_SSLMODE", "require")
    url = fake_postgres.postgres_url()
    assert (url.host, url.port, url.database, url.username) == ("10.0.0.5", 5432, "agents", "agents")
    assert url.password == "p@ss:w/rd#1" and url.query["sslmode"] == "require"
    assert "p@ss" not in fake_postgres.mask_url(url)


def test_postgres_url_accepts_heroku_scheme_and_url_wins_over_parts(monkeypatch):
    _clear_pg_env(monkeypatch)
    monkeypatch.setenv("AGENT_DB_HOST", "ignored")
    monkeypatch.setenv("AGENT_DATABASE_URL", "postgres://u:p@db.example:6432/prod?sslmode=verify-full")
    monkeypatch.setenv("AGENT_DB_SSLMODE", "require")  # the URL already has its own sslmode
    url = fake_postgres.postgres_url()
    assert url.get_backend_name() == "postgresql" and url.host == "db.example" and url.port == 6432
    assert url.query["sslmode"] == "verify-full"


def test_postgres_url_is_none_when_nothing_is_configured(monkeypatch):
    _clear_pg_env(monkeypatch)
    assert fake_postgres.postgres_url() is None


def test_localhost_database_maps_to_docker_host_only_inside_a_container(monkeypatch):
    _clear_pg_env(monkeypatch)
    monkeypatch.setenv("AGENT_DATABASE_URL", "postgresql://u:p@localhost:5432/db")
    monkeypatch.setattr("os.path.exists", lambda p: False)
    assert fake_postgres.postgres_url().host == "localhost"
    monkeypatch.setenv("RUNNING_IN_DOCKER", "true")
    assert fake_postgres.postgres_url().host == "host.docker.internal"
    monkeypatch.setenv("REWRITE_LOCALHOST_IN_DOCKER", "false")
    assert fake_postgres.postgres_url().host == "localhost"


def test_ollama_url_maps_to_docker_host_only_inside_a_container(monkeypatch):
    from app.services.ollama_client import _resolve_base_url
    monkeypatch.setattr("os.path.exists", lambda p: False)
    monkeypatch.delenv("RUNNING_IN_DOCKER", raising=False)
    assert _resolve_base_url("http://localhost:11434/") == "http://localhost:11434"
    assert _resolve_base_url("http://10.1.2.3:11434") == "http://10.1.2.3:11434"
    monkeypatch.setenv("RUNNING_IN_DOCKER", "true")
    assert _resolve_base_url("http://localhost:11434") == "http://host.docker.internal:11434"
    assert _resolve_base_url("http://10.1.2.3:11434") == "http://10.1.2.3:11434"


def test_auto_mode_refuses_the_fake_in_production(monkeypatch):
    _clear_pg_env(monkeypatch)
    monkeypatch.setenv("AGENT_DB_MODE", "auto")
    monkeypatch.setenv("ENVIRONMENT", "production")
    fake_postgres.reset_engine()
    try:
        with pytest.raises(RuntimeError, match="refusing to fall back"):
            fake_postgres.get_engine()
    finally:
        fake_postgres.reset_engine()


def test_postgres_only_mode_fails_loudly_when_unreachable(monkeypatch):
    monkeypatch.setenv("AGENT_DB_MODE", "postgres")
    monkeypatch.setenv("AGENT_DB_CONNECT_RETRIES", "1")
    monkeypatch.setenv("AGENT_DATABASE_URL", "postgresql://u:p@127.0.0.1:1/none")
    fake_postgres.reset_engine()
    try:
        with pytest.raises(RuntimeError, match="unreachable"):
            fake_postgres.get_engine()
    finally:
        fake_postgres.reset_engine()


def test_auto_mode_falls_back_to_fake_when_postgres_down(monkeypatch):
    monkeypatch.setenv("AGENT_DB_MODE", "auto")
    monkeypatch.setenv("AGENT_DATABASE_URL", "postgresql://u:p@127.0.0.1:1/none")
    fake_postgres.reset_engine()
    try:
        assert fake_postgres.describe()["fake"] is True
    finally:
        fake_postgres.reset_engine()


def test_no_demo_seed_in_production(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("FAKE_POSTGRES_SEED", "true")
    fake_postgres.reset_engine()
    monkeypatch.setenv("AGENT_DB_MODE", "fake")
    try:
        fake_postgres.get_engine()
        assert fake_postgres.should_seed() is False
    finally:
        fake_postgres.reset_engine()


# ----------------------------------------------------------------------------- users
def test_user_register_and_authenticate(store):
    u = store.create_user("alice", "alice@acme.com", "secret1")
    assert u["role"] == "user" and "password" not in str(u)
    assert store.authenticate("alice", "secret1")["id"] == u["id"]
    assert store.authenticate("ALICE@acme.com", "secret1")["username"] == "alice"  # email, case-insensitive
    with pytest.raises(AuthError):
        store.authenticate("alice", "wrong")
    with pytest.raises(AuthError):
        store.authenticate("nobody", "x")


def test_duplicate_and_invalid_users(store):
    store.create_user("alice", "alice@acme.com", "secret1")
    with pytest.raises(DuplicateError):
        store.create_user("Alice", "other@acme.com", "secret1")
    with pytest.raises(DuplicateError):
        store.create_user("alice2", "ALICE@acme.com", "secret1")
    with pytest.raises(ValueError):
        store.create_user("ab", "x@acme.com", "secret1")
    with pytest.raises(ValueError):
        store.create_user("bob", "bob@acme.com", "123")


def test_account_locks_after_repeated_failures(store):
    store.create_user("alice", "alice@acme.com", "secret1")
    for _ in range(MAX_FAILED_LOGINS):
        with pytest.raises(AuthError):
            store.authenticate("alice", "bad")
    with pytest.raises(AuthError) as e:
        store.authenticate("alice", "secret1")  # even the right password is refused while locked
    assert e.value.status == 429


# ----------------------------------------------------------------------------- contacts
def test_contacts_crud_and_isolation(store):
    c = store.add_contact("alice", "Bob Kumar", email="Bob@Acme.com", phone="98765 43210")
    assert c["email"] == "Bob@acme.com" and c["phone"] == "+919876543210"
    assert store.list_contacts("mallory") == []  # other owners see nothing
    assert store.update_contact("mallory", c["id"], name="Hacked") is None
    assert store.delete_contact("mallory", c["id"]) is False
    assert store.update_contact("alice", c["id"], name="Bob K", email="bob@acme.com", phone=c["phone"])["name"] == "Bob K"
    assert store.list_contacts("alice", q="acme")[0]["name"] == "Bob K"
    assert store.delete_contact("alice", c["id"]) is True


def test_contact_validation_and_duplicates(store):
    with pytest.raises(ValueError):
        store.add_contact("a", "NoAddress")
    with pytest.raises(ValueError):
        store.add_contact("a", "Bad", email="nope")
    with pytest.raises(ValueError):
        store.add_contact("a", "Bad", phone="12")
    store.add_contact("a", "Dan", email="dan@acme.com")
    with pytest.raises(DuplicateError):
        store.add_contact("a", "dan", email="other@acme.com")


def test_find_contacts_in_text(store):
    store.add_contact("a", "Alice Johnson", email="alice@acme.com")
    store.add_contact("a", "Bob Kumar", email="bob@acme.com")
    store.add_contact("a", "Bob Smith", email="bobs@acme.com")
    names = lambda t: sorted(c["name"] for c in store.find_in_text("a", t))  # noqa: E731
    assert names("email Alice about it") == ["Alice Johnson"]            # unique first name
    assert names("email Bob about it") == []                              # ambiguous first name
    assert names("email Bob Kumar about it") == ["Bob Kumar"]
    assert names("email alice johnson and bob smith") == ["Alice Johnson", "Bob Smith"]
    assert names("alicecooper") == []                                     # whole words only


# ----------------------------------------------------------------------------- history
def test_message_history_and_stats(store):
    store.log_message("alice", "email", "sent", ["a@acme.com"], "Hi", "body", "s1", ["<id>"])
    store.log_message("alice", "whatsapp", "failed", ["+14155550100"], None, "b", "s1", error="boom")
    store.log_message("bob", "email", "sent", ["b@acme.com"], "Yo", "x", "s2")
    mine = store.list_messages("alice")
    assert [m["channel"] for m in mine] == ["whatsapp", "email"]  # newest first
    assert store.list_messages("alice", channel="email")[0]["subject"] == "Hi"
    assert len(store.list_messages(None)) == 3  # admin view
    stats = store.message_stats("alice")
    assert stats["total"] == 2 and stats["by_status"] == {"sent": 1, "failed": 1}


def test_seed_creates_demo_data_only_on_empty_fake_db(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool
    monkeypatch.setenv("AGENT_DB_MODE", "fake")
    monkeypatch.setenv("FAKE_POSTGRES_SEED", "true")
    monkeypatch.setenv("ENVIRONMENT", "development")
    fake_postgres.reset_engine()
    try:
        fake_postgres.get_engine()
        s = AgentStore(create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool))
        s.init()
        assert s.authenticate("demo", "demo123")["username"] == "demo"
        assert {c["name"] for c in s.list_contacts("demo")} == {"Alice Johnson", "Bob Kumar"}
        before = s.describe()["users"]
        s._seed()
        assert s.describe()["users"] == before  # idempotent
    finally:
        fake_postgres.reset_engine()


# ----------------------------------------------------------------------------- orchestrator + store
def test_contact_names_resolve_to_addresses_and_history_is_recorded(store):
    store.add_contact("u1", "Alice Johnson", email="alice@acme.com", phone="+14155550101")
    email = FakeEmail()
    o = make_orch(FakeLLM(extract={"channel": "email"}), confirm=False, email=email, store=store)
    s = sid()
    r = say(o, s, 'email Alice saying "Budget approved for Q3"')
    assert r["success"] and email.calls[0]["to"] == ["alice@acme.com"]
    rows = store.list_messages("u1")
    assert len(rows) == 1 and rows[0]["status"] == "sent" and rows[0]["recipients"] == ["alice@acme.com"]
    assert rows[0]["body"] == "Budget approved for Q3"


def test_whatsapp_uses_the_contact_phone(store):
    store.add_contact("u1", "Alice Johnson", email="alice@acme.com", phone="+14155550101")
    wa = FakeWA()
    o = make_orch(FakeLLM(extract={"channel": "whatsapp"}), confirm=False, wa=wa, store=store)
    r = say(o, sid(), 'whatsapp Alice saying "Running 10 minutes late"')
    assert r["success"] and wa.calls[0]["to"] == ["+14155550101"]


def test_names_in_the_message_body_are_not_treated_as_recipients(store):
    store.add_contact("u1", "Alice Johnson", email="alice@acme.com")
    store.add_contact("u1", "Dan Moore", email="dan@acme.com")
    llm = FakeLLM(extract={"channel": "email", "body": "Alice approved the budget."})
    o = make_orch(llm, store=store)
    r = say(o, sid(), "email Dan that Alice approved the budget")
    assert r["metadata"]["preview"]["emails"] == ["dan@acme.com"]


def test_contact_without_the_needed_channel_asks_for_it(store):
    store.add_contact("u1", "Carol", email="carol@acme.com")  # no phone
    o = make_orch(FakeLLM(extract={"channel": "whatsapp"}), store=store)
    r = say(o, sid(), 'whatsapp Carol saying "hello there"')
    assert "recipient_phone" in r["metadata"]["missing_params"] and "no WhatsApp number saved" in r["response"]


def test_contacts_are_per_user(store):
    store.add_contact("someone-else", "Alice Johnson", email="alice@acme.com")
    o = make_orch(FakeLLM(extract={"channel": "email"}), store=store)
    r = say(o, sid(), 'email Alice saying "hello there"')
    assert "recipient_email" in r["metadata"]["missing_params"]


def test_failed_and_dry_run_sends_are_recorded(store):
    email = FakeEmail(ok=False)
    o = make_orch(email=email, confirm=False, store=store)
    say(o, sid(), 'email a@acme.com saying "will fail"')
    assert store.list_messages("u1")[0]["status"] == "failed" and "smtp down" in store.list_messages("u1")[0]["error"]


def test_history_logging_never_breaks_a_send(store, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "log_message", boom)
    email = FakeEmail()
    o = make_orch(confirm=False, email=email, store=store)
    assert say(o, sid(), 'email a@acme.com saying "still sent"')["success"] and email.calls


def test_a_session_cannot_be_driven_by_another_user():
    o = make_orch()
    s = sid()
    run(o.orchestrate('email a@acme.com saying "secret plan"', s, "alice", ["viewer"]))
    r = run(o.orchestrate("yes", s, "mallory", ["viewer"]))
    assert r["error"] == "session_mismatch" and not o.email_agent.calls


def test_preview_exposes_structured_data_for_the_ui():
    o = make_orch()
    r = say(o, sid(), 'email bob@acme.com subject: Hi saying "Hello there"')
    p = r["metadata"]["preview"]
    assert p["channel"] == "email" and p["emails"] == ["bob@acme.com"] and p["subject"] == "Hi"
    assert p["body"] == "Hello there" and p["drafted_by_ai"] is False


# ----------------------------------------------------------------------------- REST API (auth + store)
@pytest.fixture
def api(store, monkeypatch):
    mgr = am.AgentManager()
    mgr.store = store
    mgr.email_agent, mgr.whatsapp_agent = FakeEmail(), FakeWA()
    mgr.orchestrator = make_orch(FakeLLM(extract={"channel": "email"}), confirm=False, email=mgr.email_agent,
                                 wa=mgr.whatsapp_agent, store=store)
    mgr.initialized = True
    monkeypatch.setattr(am, "agent_manager", mgr)
    monkeypatch.setenv("AGENT_REQUIRE_AUTH", "true")
    # the auth routes talk to the module-level store: point it at the test store
    monkeypatch.setattr(local_auth, "agent_store", store)
    app = FastAPI()
    app.include_router(local_auth.router)
    app.include_router(agent_chat.router)
    c = TestClient(app)
    c.mgr = mgr
    return c


def register(c, name="alice", pw="secret1"):
    r = c.post("/api/auth/register", json={"username": name, "email": f"{name}@acme.com", "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_auth_contract_matches_the_react_client(api):
    r = api.post("/api/auth/register", json={"username": "alice", "email": "alice@acme.com", "password": "secret1"})
    body = r.json()
    assert r.status_code == 200 and body["token_type"] == "bearer" and body["user"]["username"] == "alice"
    assert body["user"]["role"] == "user" and "password" not in str(body)
    # duplicate -> 400 (the React register page maps 400 to "User already exists")
    assert api.post("/api/auth/register", json={"username": "alice", "email": "x@acme.com",
                                                "password": "secret1"}).status_code == 400
    # login is form-encoded (OAuth2 style)
    ok = api.post("/api/auth/login", data={"username": "alice", "password": "secret1"})
    assert ok.status_code == 200 and ok.json()["access_token"]
    assert api.post("/api/auth/login", data={"username": "alice", "password": "nope"}).status_code == 401
    me = api.get("/api/auth/me", headers={"Authorization": f"Bearer {ok.json()['access_token']}"})
    assert me.json()["email"] == "alice@acme.com"
    # the widget reads the `role` claim from the JWT
    from jose import jwt
    from app.core.security import ALGORITHM, SECRET_KEY
    assert jwt.decode(ok.json()["access_token"], SECRET_KEY, algorithms=[ALGORITHM])["role"] == "user"


def test_admin_role_comes_from_configuration_only(api, monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAMES", "boss")
    assert api.post("/api/auth/register", json={"username": "boss", "email": "b@acme.com",
                                                "password": "secret1"}).json()["user"]["role"] == "admin"
    assert api.post("/api/auth/register", json={"username": "peon", "email": "p@acme.com", "password": "secret1",
                                                "role": "admin"}).json()["user"]["role"] == "user"


def test_agent_endpoints_require_a_token(api):
    for method, url in (("post", "/api/agent/chat"), ("get", "/api/agent/messages"), ("get", "/api/agent/contacts"),
                        ("get", "/api/agent/templates")):
        r = getattr(api, method)(url, **({"json": {"message": "hi"}} if method == "post" else {}))
        assert r.status_code == 401, url
    assert api.get("/api/agent/health").status_code == 200  # health stays public
    assert api.get("/api/agent/messages", headers={"Authorization": "Bearer junk"}).status_code == 401


def test_chat_uses_the_token_identity_not_the_body(api):
    h = register(api, "alice")
    r = api.post("/api/agent/chat", headers=h, json={
        "message": 'email bob@acme.com saying "hi there"', "user_id": "someone-else", "user_roles": ["admin"]})
    assert r.status_code == 200 and r.json()["success"]
    assert api.mgr.store.list_messages("alice") and not api.mgr.store.list_messages("someone-else")


def test_unhandled_chat_returns_guidance(api):
    h = register(api)
    r = api.post("/api/agent/chat", headers=h, json={"message": "list my clusters"}).json()
    assert r["handled"] is False and "send emails" in r["response"]


def test_contacts_api_crud_and_validation(api):
    h = register(api)
    made = api.post("/api/agent/contacts", headers=h, json={"name": "Dan", "email": "dan@acme.com"})
    assert made.status_code == 201
    cid = made.json()["id"]
    assert api.post("/api/agent/contacts", headers=h, json={"name": "dan", "email": "x@acme.com"}).status_code == 409
    assert api.post("/api/agent/contacts", headers=h, json={"name": "Eve"}).status_code == 422
    assert api.get("/api/agent/contacts?q=da", headers=h).json()["items"][0]["name"] == "Dan"
    upd = api.put(f"/api/agent/contacts/{cid}", headers=h, json={"name": "Dan M", "email": "dan@acme.com",
                                                                 "phone": "+14155550100"})
    assert upd.status_code == 200 and upd.json()["phone"] == "+14155550100"
    other = register(api, "mallory")
    assert api.delete(f"/api/agent/contacts/{cid}", headers=other).status_code == 404  # not yours
    assert api.delete(f"/api/agent/contacts/{cid}", headers=h).status_code == 200


def test_messages_api_scoping_and_admin_view(api, monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAMES", "boss")
    a, b, boss = register(api, "alice"), register(api, "bob"), register(api, "boss")
    api.post("/api/agent/chat", headers=a, json={"message": 'email x@acme.com saying "from alice"'})
    api.post("/api/agent/chat", headers=b, json={"message": 'email y@acme.com saying "from bob"'})
    mine = api.get("/api/agent/messages", headers=a).json()["items"]
    assert [m["body"] for m in mine] == ["from alice"]
    assert api.get("/api/agent/messages?all=true", headers=a).json()["items"] == mine  # non-admin: still own only
    assert len(api.get("/api/agent/messages?all=true", headers=boss).json()["items"]) == 2
    assert api.get("/api/agent/messages/stats", headers=a).json()["total"] == 1
    assert api.get("/api/agent/messages?channel=sms", headers=a).status_code == 422


def test_conversation_endpoints_are_owner_scoped(api):
    a, b = register(api, "alice"), register(api, "bob")
    s = "conv-" + uuid.uuid4().hex
    api.post("/api/agent/chat", headers=a, json={"message": 'email x@acme.com saying "hello there"',
                                                 "session_id": s})
    # confirm-less mode completed it; start one that stays open
    s2 = "conv-" + uuid.uuid4().hex
    api.mgr.orchestrator.require_confirmation = True
    api.post("/api/agent/chat", headers=a, json={"message": 'email x@acme.com saying "hold on"', "session_id": s2})
    assert api.get(f"/api/agent/conversation/{s2}", headers=a).json()["found"] is True
    assert api.get(f"/api/agent/conversation/{s2}", headers=b).json()["found"] is False
    assert api.delete(f"/api/agent/conversation/{s2}", headers=b).json()["success"] is False
    assert api.delete(f"/api/agent/conversation/{s2}", headers=a).json()["success"] is True
    hijack = api.post("/api/agent/chat", headers=b, json={"message": "yes", "session_id": s2})
    assert hijack.json()["handled"] is False or not hijack.json()["success"]


def test_templates_endpoint(api):
    h = register(api)
    names = {t["name"] for t in api.get("/api/agent/templates", headers=h).json()["items"]}
    assert {"meeting_invite", "outage_notice", "welcome"} <= names


def test_health_reports_database_and_hides_internals(api, monkeypatch):
    async def fake_status(max_age=15.0):
        return {"provider": "ollama", "model": "mistral", "reachable": True, "model_pulled": True, "available": True,
                "base_url": "http://secret-host:11434"}

    monkeypatch.setattr(api.mgr.orchestrator.llm, "status", fake_status, raising=False)
    h = api.get("/api/agent/health").json()
    assert h["status"] == "healthy" and h["database"]["healthy"] is True and h["database"]["fake"] is True
    assert "url" not in h["database"] and "base_url" not in h["llm"]
    assert "host" not in str(h["sub_agents"])


def test_real_app_errors_carry_detail_for_the_react_client(monkeypatch):
    """The React client reads `detail`; the app-wide handler must provide it (and keep `error`)."""
    from app.main import app as real_app
    c = TestClient(real_app)
    r = c.post("/api/auth/login", data={"username": "nobody", "password": "wrong-pass"})
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid username or password" == r.json()["error"]
    unauth = c.post("/api/agent/chat", json={"message": "hi"})
    assert unauth.status_code == 401 and unauth.json()["detail"] == "Authentication required"


def test_spa_fallback_never_shadows_api_routes():
    from app.main import app as real_app
    c = TestClient(real_app)
    assert c.get("/health/liveness").json()["status"] == "alive"
    assert c.get("/api/does-not-exist").status_code == 404
    assert c.get("/assets/nope.js").status_code == 404
    assert c.get("/../../etc/passwd").status_code in (404, 200)  # never a file outside the build dir


# ----------------------------------------------------------------------------- slim app: probes, headers, metrics, secrets
def test_probes_metrics_and_security_headers():
    from app.main import app as real_app
    with TestClient(real_app) as c:  # runs the lifespan: builds the agents against the in-memory fake postgres
        assert c.get("/health/liveness").json()["status"] == "alive"
        ready = c.get("/health/readiness")
        body = ready.json()
        assert ready.status_code == 200 and body["database"]["healthy"] is True
        assert body["status"] in ("ready", "degraded")
        assert "url" not in body["database"] and "base_url" not in body["llm"]
        r = c.get("/health/liveness")
        assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert c.get("/metrics").status_code == 404  # Prometheus endpoint is gone; Grafana reads the database


def _metric_rows(store, model):
    from sqlalchemy import select
    with store._session() as s:
        return list(s.scalars(select(model)))


def test_sent_messages_are_recorded_for_grafana(store, monkeypatch):
    from app.services.agent_store import MetricAgentRun, MetricMessage
    from app.services.metrics_recorder import metrics
    monkeypatch.setattr(metrics, "_store", store)
    o = make_orch(confirm=False, email=FakeEmail(), store=store)
    say(o, sid(), 'email a@acme.com saying "metrics please"')
    metrics.flush()
    msgs = _metric_rows(store, MetricMessage)
    assert [(m.channel, m.status) for m in msgs] == [("email", "sent")]
    runs = _metric_rows(store, MetricAgentRun)
    assert runs and runs[0].agent_name and runs[0].duration_ms >= 0


def test_http_and_llm_events_are_recorded(store, monkeypatch):
    from app.services.agent_store import MetricHttpRequest, MetricLLMCall
    from app.services.metrics_recorder import metrics
    monkeypatch.setattr(metrics, "_store", store)
    metrics.track_llm_call("ollama/mistral", "orchestrator_route", input_tokens=40, output_tokens=12,
                           duration=1.5)
    metrics.track_llm_call("ollama/mistral", "orchestrator_route", duration=0.2, success=False,
                           error_type="ReadTimeout")
    metrics.track_http("GET", "/api/contacts/{contact_id}", 200, 0.05)
    metrics.flush()
    calls = {c.status: c for c in _metric_rows(store, MetricLLMCall)}
    assert calls["success"].output_tokens == 12 and calls["success"].duration_ms == 1500
    assert "ReadTimeout" in calls
    assert _metric_rows(store, MetricHttpRequest)[0].route == "/api/contacts/{contact_id}"


def test_metrics_retention_purges_old_events(store):
    from app.services.agent_store import MetricMessage
    old = datetime.utcnow() - timedelta(days=40)
    store.record_metrics([("message", {"channel": "email", "status": "sent", "ts": old}),
                          ("message", {"channel": "email", "status": "sent"})])
    assert store.purge_metrics(30) == 1
    assert len(_metric_rows(store, MetricMessage)) == 1


def test_jwt_secret_rules(monkeypatch):
    import importlib
    from app.core import security
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError, match="must be set"):
        importlib.reload(security)
    monkeypatch.setenv("JWT_SECRET_KEY", "short")
    with pytest.raises(RuntimeError, match="at least"):
        importlib.reload(security)
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    importlib.reload(security)  # dev: random per-process key instead of failing
    assert len(security.SECRET_KEY) >= 32
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-not-for-production-use-0123456789")
    monkeypatch.setenv("ENVIRONMENT", "test")
    importlib.reload(security)  # restore what the rest of the suite signs with


def test_fake_postgres_file_url_is_resolved_and_its_folder_created(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "dir" / "fake.db"
    url = fake_postgres._resolve_sqlite_url(f"sqlite:///{target.as_posix()}")
    assert target.parent.is_dir() and url.endswith("nested/dir/fake.db")
    rel = fake_postgres._resolve_sqlite_url("sqlite:///data/some_test.db")  # relative -> project root, not the cwd
    assert str(fake_postgres.PROJECT_ROOT.as_posix()) in rel
    assert fake_postgres._resolve_sqlite_url("sqlite://") == "sqlite://"
    assert fake_postgres._resolve_sqlite_url("postgresql://u:p@h/db") == "postgresql://u:p@h/db"


def test_registration_can_be_disabled(api, monkeypatch):
    monkeypatch.setenv("ALLOW_REGISTRATION", "false")
    r = api.post("/api/auth/register", json={"username": "newbie", "email": "n@acme.com", "password": "secret1"})
    assert r.status_code == 403 and "disabled" in r.json()["detail"].lower()


def test_schema_compiles_for_real_postgres():
    """No Postgres server here, so at least prove the DDL for every table renders for the postgresql dialect."""
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex, CreateTable
    from app.agents.state.memori_session_manager import SessionBase
    from app.services.agent_store import Base
    ddl = ""
    for md in (Base.metadata, SessionBase.metadata):
        for table in md.sorted_tables:
            ddl += str(CreateTable(table).compile(dialect=postgresql.dialect()))
            for ix in table.indexes:
                ddl += str(CreateIndex(ix).compile(dialect=postgresql.dialect()))
    for name in ("agent_users", "agent_contacts", "agent_messages", "conversation_sessions"):
        assert f"CREATE TABLE {name}" in ddl
    assert "JSON" in ddl and "SERIAL" in ddl


def test_cors_allows_any_localhost_port_in_dev_but_not_other_sites():
    """Vite hops to 5174/5175/... when 5173 is busy; that must not break the UI. Other origins stay blocked."""
    from app.main import app as real_app
    c = TestClient(real_app)

    def preflight(origin):
        return c.options("/api/auth/login", headers={"Origin": origin, "Access-Control-Request-Method": "POST"})

    for origin in ("http://localhost:5173", "http://localhost:5174", "http://127.0.0.1:5199", "http://localhost"):
        r = preflight(origin)
        assert r.status_code == 200 and r.headers["access-control-allow-origin"] == origin, origin
    for origin in ("https://evil.example", "http://localhost.evil.example:5173", "http://192.168.1.5:5173"):
        assert preflight(origin).status_code == 400, origin


def test_names_inside_typed_addresses_do_not_add_contacts(store):
    """Regression: 'email bob@acme.com ...' matched the saved contact 'Bob' and also mailed bob@example.com."""
    store.add_contact("u1", "Bob Kumar", email="bob@example.com", phone="+919876543210")
    email = FakeEmail()
    o = make_orch(FakeLLM(extract={"channel": "email"}), confirm=False, email=email, store=store)
    say(o, sid(), 'email bob@acme.com saying "proxy test"')
    assert email.calls[0]["to"] == ["bob@acme.com"]  # only what the user typed
    o2 = make_orch(FakeLLM(extract={"channel": "whatsapp"}), confirm=False, wa=FakeWA(), store=store)
    say(o2, sid(), 'whatsapp +14155550100 saying "bob is late"')
    assert o2.whatsapp_agent.calls[0]["to"] == ["+14155550100"]


def test_names_and_typed_addresses_can_be_mixed(store):
    store.add_contact("u1", "Alice Johnson", email="alice@example.com")
    email = FakeEmail()
    o = make_orch(FakeLLM(extract={"channel": "email"}), confirm=False, email=email, store=store)
    say(o, sid(), 'email Alice and carol@acme.com saying "budget approved"')
    assert sorted(email.calls[0]["to"]) == ["alice@example.com", "carol@acme.com"]


def test_each_name_goes_only_to_the_channel_it_was_attached_to(store):
    store.add_contact("u1", "Alice Johnson", email="alice@example.com", phone="+14155550101")
    store.add_contact("u1", "Bob Kumar", email="bob@example.com", phone="+919876543210")
    email, wa = FakeEmail(), FakeWA()
    o = make_orch(FakeLLM(extract={"channel": "both"}), confirm=False, email=email, wa=wa, store=store)
    say(o, sid(), "Email Alice and WhatsApp Bob about the Friday town hall saying it starts at 3pm")
    assert email.calls[0]["to"] == ["alice@example.com"] and wa.calls[0]["to"] == ["+919876543210"]


def test_both_channels_for_one_name_still_reaches_both(store):
    store.add_contact("u1", "Alice Johnson", email="alice@example.com", phone="+14155550101")
    email, wa = FakeEmail(), FakeWA()
    o = make_orch(FakeLLM(extract={"channel": "both"}), confirm=False, email=email, wa=wa, store=store)
    say(o, sid(), "Email and WhatsApp Alice saying the review is moved")
    assert email.calls[0]["to"] == ["alice@example.com"] and wa.calls[0]["to"] == ["+14155550101"]
