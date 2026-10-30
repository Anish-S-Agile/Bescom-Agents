# Agents

One coordinating agent and two sub-agents. The LLM is local **Ollama `mistral`** - no API keys.

```
OrchestratorAgent  (routing, field extraction, drafting, multi-turn, confirmation)
 |- EmailAgent       SMTP email: announcements, invites, reminders, outage notices ...
 `- WhatsAppAgent    WhatsApp Cloud API (Meta) or Twilio
```

| File | Role |
|---|---|
| `orchestrator_agent.py` | Understands the request, asks for missing details, previews, dispatches |
| `email_agent.py`, `email_templates.py` | SMTP sender + 7 templates (`meeting_invite`, `announcement`, `reminder`, `follow_up`, `thank_you`, `outage_notice`, `welcome`) |
| `whatsapp_agent.py` | Meta Cloud API / Twilio sender (text and approved templates) |
| `agent_manager.py` | Builds the hierarchy, lifecycle, stats, shutdown |
| `common.py` | Validation, rate limiter, retry/backoff, idempotency cache |
| `state/` | Conversation state (multi-turn, persisted in the agent database) |
| `../services/agent_store.py` | Users, **contacts** (address book), **send history** - plain SQLAlchemy |
| `../services/fake_postgres.py` | Database selection: real PostgreSQL, or the **fake postgres** (SQLite) for tests/dev |
| `../api/routes/local_auth.py` | `/api/auth/register|login|me` (JWT) used by the React app |

## How a request flows
1. `POST /api/agent/chat` -> orchestrator. A cheap rule-based pre-filter (no LLM) decides whether the message is a send request; anything else gets a short "here is what I can do" reply.
2. The orchestrator extracts recipients with regex, and channel/subject/body/template with one mistral JSON call.
3. Missing pieces are asked for one at a time ("Who should I email?"). If there is no body, mistral drafts one.
4. A preview is shown; the user replies **yes** / **cancel** / an edit. Then the sub-agent(s) send.
5. If the provider is temporarily down, the conversation stays open so **yes** retries.

## Data and the fake Postgres
`AGENT_DB_MODE=auto` (default) uses PostgreSQL when reachable and otherwise the fake postgres
(`data/fake_postgres.db`, seeded with `admin`/`demo` users and sample contacts - dev only, never in production).
`fake` forces it (tests use a pure in-memory instance); `postgres` refuses to start without a real database.
The same models run on both. Tables: `agent_users`, `agent_contacts`, `agent_messages`, `conversation_sessions`.

- **Contacts:** "email Alice about the review" resolves *Alice* from the user's own address book (full name, or a
  first name when unambiguous). Only the recipient part of the sentence is searched, never the message text.
- **History:** every send (sent / failed / dry_run / duplicate) is recorded per user and shown in the React History tab.
- **Auth:** agent endpoints need a Bearer token; the identity and roles come from the token, not the request body.
  Conversations and contacts are private to their owner.

## Ollama / mistral
`GET /api/agent/health` reports whether Ollama is reachable **and** the model is pulled (with a fix hint).
mistral does routing/extraction/drafting through one JSON-mode call each (`temperature 0`, `keep_alive 30m`);
failures trip a 60s cooldown and the orchestrator falls back to rule-based extraction.
`python -m pytest app/tests/test_ollama_live.py` exercises the real model (skipped if Ollama is not running).

## Safety properties
- Recipients come **only** from the user's own text, never from the LLM.
- Any text written or rephrased by the LLM is **always** previewed, even with `AGENT_REQUIRE_CONFIRMATION=false`.
- Header-injection stripping, recipient caps, optional domain allow-list, per-user rate limit, optional role gate.
- Retries with exponential backoff + jitter on timeouts / 429 / 5xx / SMTP 4xx; auth and 4xx errors are not retried.
- Idempotency: an identical send within 5 minutes is suppressed (no double emails on client retries).
- PII is masked in logs and API responses. If Ollama is down, extraction falls back to rules and the bot asks for the body.

## API
- `POST /api/agent/chat` - natural language. `GET /api/agent/health` (public), `/stats`, `/conversation/{id}`.
- `GET|POST /api/agent/contacts`, `PUT|DELETE /api/agent/contacts/{id}`, `GET /api/agent/messages[/stats]`,
  `GET /api/agent/templates` - all per-user, Bearer token required.
- `POST /api/agent/email/send`, `POST /api/agent/whatsapp/send` - structured, require `X-API-Key: $AGENT_API_KEY`
  (503 until the key is configured).

## Configuration (`.env`)
See the "Orchestrator + Email / WhatsApp sub-agents" block in `.env`. Set `AGENT_DRY_RUN=true` to validate and
render everything without sending (use for staging).

## Production notes
- Run `ollama pull mistral`; the model is warmed at startup (`keep_alive` 30m).
- Rate limit and idempotency caches are per process: with N uvicorn workers the effective limit is N x configured.
  Put a shared limiter (Redis) in front if you need a strict global limit.
- WhatsApp only allows free-form text within 24h of the customer's last message. For business-initiated messages
  create an approved template and set `WHATSAPP_DEFAULT_TEMPLATE` (the message text is passed as `{{1}}`).
- Keep SMTP credentials and the WhatsApp token in a secret manager, not in `.env` in the repo.

## Tests
`python -m pytest app/tests -q` (no network required).

Everything else (cluster/resource agents, the RAG knowledge base, Tata auth, OpenWebUI endpoints) was moved to
`backup/`.
