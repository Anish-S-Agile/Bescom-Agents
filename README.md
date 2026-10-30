# Bescom

An **orchestrator agent** (local Ollama `mistral`) that sends **email** and **WhatsApp** messages through two
sub-agents, with a React UI (chat, contacts, send history) and a FastAPI backend.

```
React UI --> FastAPI --> OrchestratorAgent (mistral) --+--> EmailAgent    (SMTP)
                 |                                      `--> WhatsAppAgent (Meta Cloud API / Twilio)
                 `--> database: PostgreSQL, or the "fake postgres" (SQLite) for testing
```
Details of the agents: [`app/agents/README.md`](app/agents/README.md). UI: [`react-frontend/README.md`](react-frontend/README.md).

## Layout
| Path | What |
|---|---|
| `app/` | API (`main.py`), agents, auth, database services |
| `react-frontend/` | React UI (built into the Docker image and served by the API) |
| `app/tests/` | `python -m pytest app/tests -q` (needs no network; `test_ollama_live.py` uses real mistral if it is running) |
| `.env` / `.env.example` | local testing profile / production template |
| `Dockerfile`, `docker-compose.yml`, `docker-compose.postgres.yml` | build and run |
| `backup/` | everything removed from the project (RAG, Tata, OpenWebUI, old Docker files), restorable |

## Run with Docker (fake postgres, senders in dry-run)
```bash
ollama pull mistral                     # once, on your machine
docker compose up --build               # http://localhost:8000   login: demo / demo123  (admin / admin123)
```
No Ollama on the host? `OLLAMA_URL_IN_DOCKER=http://ollama:11434 docker compose --profile ollama up --build`
(the `ollama-pull` service downloads mistral into a volume).

`AGENT_DRY_RUN=true` in `.env` means nothing is really sent: previews and history work, delivery is only validated.

## Then: real PostgreSQL
```bash
# .env: POSTGRES_PASSWORD=<letters and digits>
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up --build
```
This starts `postgres:16`, sets `AGENT_DB_MODE=postgres` (the app refuses to start without it) and turns the demo seed off.
It also starts **Grafana** on http://localhost:3001 (set `GRAFANA_RO_PASSWORD` and `GRAFANA_ADMIN_PASSWORD` in `.env`).

## Local or VM: one configuration
The same code and `.env` keys run on a laptop, in Docker and on a VM; only the values change.

| Where | Database (`.env`) | Ollama |
|---|---|---|
| Laptop, quick testing | `AGENT_DB_MODE=fake` (or `auto`: real Postgres if reachable, else SQLite) | `OLLAMA_BASE_URL=http://localhost:11434` |
| Laptop / VM with Postgres on the same machine | `AGENT_DB_MODE=postgres`, `AGENT_DB_HOST=localhost`, `AGENT_DB_NAME/USER/PASSWORD` | same |
| VM using a separate Postgres server | `AGENT_DB_MODE=postgres`, `AGENT_DB_HOST=<db ip/hostname>`, `AGENT_DB_SSLMODE=require` | `OLLAMA_BASE_URL=http://<ollama host>:11434` |
| Postgres managed by compose | `docker compose -f docker-compose.yml -f docker-compose.postgres.yml up` | as above |

- Give either `AGENT_DATABASE_URL=postgresql://user:pass@host:5432/db` **or** the separate `AGENT_DB_*` parts. The parts form needs no URL-encoding of the password.
- Inside Docker, a `localhost` Postgres or Ollama is mapped to the Docker host automatically (`host.docker.internal`); set `REWRITE_LOCALHOST_IN_DOCKER=false` to turn that off.
- With `AGENT_DB_MODE=postgres` the app waits for the database (`AGENT_DB_CONNECT_RETRIES` x `AGENT_DB_CONNECT_RETRY_DELAY`s), so the app and database can start together on a VM.
- `ENVIRONMENT=production` never falls back to SQLite in `auto` mode; it refuses to start instead.
- The VM's Postgres must accept the app's address (`listen_addresses`, `pg_hba.conf`) and the firewall must allow port 5432 from the app only.

## Monitoring (Grafana)
There is no Prometheus. The app records LLM calls (latency, tokens, errors), agent runs, sent messages and HTTP
requests into `metrics_*` tables in its own database; Grafana reads them through a read-only role.
Open Grafana -> Dashboards -> *Bescom* -> *model and application performance*.
Events older than `METRICS_RETENTION_DAYS` (default 30) are deleted; `METRICS_ENABLED=false` switches recording off.

If your Postgres volume already existed before this change, create the Grafana role once, then restart the app:
```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml exec postgres sh /docker-entrypoint-initdb.d/01-grafana-ro.sh
docker compose -f docker-compose.yml -f docker-compose.postgres.yml restart app
```

## Run without Docker
```bash
python -m venv .venv && .venv/Scripts/activate        # Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
python -m uvicorn app.main:app --port 8000            # API (+ UI if react-frontend/dist is built)
cd react-frontend && npm install && npm start         # UI dev server on :5173
```

## Production checklist
1. Start from `.env.example`: `ENVIRONMENT=production`, `AGENT_DB_MODE=postgres`, `AGENT_DRY_RUN=false`.
2. Generate `JWT_SECRET_KEY` and `AGENT_API_KEY` (`python -c "import secrets; print(secrets.token_urlsafe(48))"`). Production refuses to start without a JWT key.
3. `ALLOW_REGISTRATION=false` once your users exist; keep `ADMIN_USERNAMES` empty and create the admin deliberately.
4. Fill in `SMTP_*` and `WHATSAPP_*` (or `TWILIO_*`). WhatsApp needs an approved template for business-initiated messages.
5. Put TLS in front (reverse proxy / load balancer) and set `ALLOWED_ORIGINS` to your domain if the UI is served elsewhere.
6. Watch the Grafana dashboard (set strong `GRAFANA_ADMIN_PASSWORD` / `GRAFANA_RO_PASSWORD`, keep port 3001 behind your proxy/VPN); probe `GET /health/liveness` and `GET /health/readiness`.
7. Rate limits and the duplicate-send cache are per process: with N workers the effective limit is N x the setting.
8. Keep secrets in your platform's secret store rather than a `.env` file on disk.
