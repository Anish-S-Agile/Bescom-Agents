# syntax=docker/dockerfile:1.7
# Bescom API + React frontend in one image.
#   docker build -t bescom .
# Ollama (mistral) runs outside this image - set OLLAMA_BASE_URL (see docker-compose.yml).

# ---------- Stage 1: build the React frontend ----------
FROM node:20-slim AS frontend
WORKDIR /react-frontend
COPY react-frontend/package.json react-frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY react-frontend/ ./
# Same origin in this image (the API serves the UI), so the UI calls relative /api URLs.
ENV VITE_API_URL=""
RUN npm run build

# ---------- Stage 2: Python dependencies ----------
FROM python:3.11-slim AS deps
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.txt ./
RUN python -m venv /opt/venv && /opt/venv/bin/pip install -r requirements.txt

# ---------- Stage 3: runtime ----------
FROM python:3.11-slim AS runtime
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FRONTEND_DIST=/app/frontend \
    FAKE_POSTGRES_URL=sqlite:////app/data/fake_postgres.db
WORKDIR /app

COPY --from=deps /opt/venv /opt/venv
COPY app ./app
COPY --from=frontend /react-frontend/dist ./frontend

# Non-root; /app/data holds the fake postgres file when AGENT_DB_MODE=fake (mount a volume there)
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
# No curl in slim images: probe with the interpreter itself
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/health/liveness' % os.getenv('PORT','8000'), timeout=3)"

# exec so uvicorn is PID 1 and receives SIGTERM (graceful shutdown closes the HTTP clients)
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${UVICORN_WORKERS:-1}"]
