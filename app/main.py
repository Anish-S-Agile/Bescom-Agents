"""
Bescom API: an Ollama (mistral) orchestrator that sends email and WhatsApp messages through two
sub-agents. In production it also serves the built React frontend (react-frontend/dist).

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import List

from dotenv import load_dotenv

load_dotenv()  # must run before the app modules below read their settings

import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402

from app.api.routes import agent_chat, local_auth  # noqa: E402

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("agents_api")

VERSION = "3.0.0"
IS_PRODUCTION = os.getenv("ENVIRONMENT", os.getenv("ENV", "development")).strip().lower() in ("production", "prod")


# ===================== Lifespan =====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.agents import get_agent_manager
    logger.info("Starting Bescom API v%s (%s)", VERSION, "production" if IS_PRODUCTION else "development")
    try:
        manager = get_agent_manager()  # database tables, orchestrator, sub-agents, mistral warm-up
        app.state.agents_ready = True
        db = await manager.store.run(manager.store.describe)
        logger.info("✅ Agents ready | database: %s%s", db.get("mode"), " (FAKE - not for production)" if db.get("fake") else "")
    except Exception:
        app.state.agents_ready = False
        logger.exception("❌ Agent initialization failed")
        if IS_PRODUCTION:
            raise  # fail fast: a production instance without its database must not look healthy
    yield
    try:
        await get_agent_manager().shutdown()
    except Exception as e:
        logger.warning("⚠️ Error shutting down agents: %s", e)
    from app.services.metrics_recorder import metrics
    metrics.shutdown()  # write queued monitoring events
    logger.info("Shutdown complete")


app = FastAPI(
    title="Bescom API",
    description="Bescom orchestrator agent (Ollama mistral) with Email and WhatsApp sub-agents",
    version=VERSION,
    lifespan=lifespan,
    docs_url=None if IS_PRODUCTION else "/docs",
    redoc_url=None,
    openapi_url=None if IS_PRODUCTION else "/openapi.json",
)

# ===================== CORS =====================
allowed_origins: List[str] = [o.strip() for o in os.getenv(
    "ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173,http://localhost:3000"
).split(",") if o.strip()]
if "*" in allowed_origins:
    logger.warning("⚠️ ALLOWED_ORIGINS contains '*' - any website can call this API from a browser")
# Development only: Vite moves to the next free port (5174, 5175, ...) when 5173 is busy, so accept any
# localhost port. Production uses the explicit ALLOWED_ORIGINS list alone.
_DEV_ORIGIN_REGEX = None if IS_PRODUCTION else r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"
app.add_middleware(CORSMiddleware, allow_origins=allowed_origins, allow_origin_regex=_DEV_ORIGIN_REGEX,
                   allow_credentials=False,
                   allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"], allow_headers=["Authorization", "Content-Type"])


# ===================== Security headers =====================
_CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if not request.url.path.startswith(("/docs", "/openapi.json")):  # Swagger UI loads assets from a CDN
            response.headers["Content-Security-Policy"] = _CSP
        return response


app.add_middleware(SecurityHeadersMiddleware)


# ===================== Request timing (Grafana "application" dashboard) =====================
_UNTIMED_PREFIXES = ("/health", "/assets/")  # probes and static files would only add noise


class RequestMetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path.startswith(_UNTIMED_PREFIXES):
            return await call_next(request)
        from app.services.metrics_recorder import metrics
        started, status = time.perf_counter(), 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = request.scope.get("route")  # the template (/api/contacts/{id}), never the raw path
            metrics.track_http(request.method, getattr(route, "path", None) or "(no route)", status,
                               time.perf_counter() - started)


app.add_middleware(RequestMetricsMiddleware)

# ===================== API routes =====================
app.include_router(local_auth.router)
app.include_router(agent_chat.router)


# ===================== Probes =====================
@app.get("/health/liveness")
async def liveness():
    """Process is up (container liveness probe)."""
    return {"status": "alive", "service": "bescom", "version": VERSION}


@app.get("/health/readiness")
async def readiness():
    """Ready when the database answers. LLM and sender state are reported but do not fail readiness:
    without Ollama the orchestrator degrades to rule-based extraction instead of going down."""
    from app.agents import get_agent_manager
    try:
        health = await get_agent_manager().health()
    except Exception as e:
        logger.exception("readiness check failed")
        return JSONResponse({"status": "unavailable", "error": str(e)[:200]}, status_code=503)
    db_ok = bool(health["database"].get("healthy"))
    llm = health["llm"]
    degraded = not (llm.get("reachable") and llm.get("model_pulled")) or not all(
        (s or {}).get("ready") for s in health["sub_agents"].values())
    body = {"status": "unavailable" if not db_ok else "degraded" if degraded else "ready",
            "database": {k: v for k, v in health["database"].items() if k != "url"},
            "llm": {k: v for k, v in llm.items() if k != "base_url"},
            "sub_agents": health["sub_agents"], "timestamp": datetime.utcnow().isoformat()}
    return JSONResponse(body, status_code=200 if db_ok else 503)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "bescom"}


# ===================== Error handlers =====================
@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    # `detail` is what the React client reads; `error` is kept for older clients.
    return JSONResponse(status_code=exc.status_code,
                        content={"error": exc.detail, "detail": exc.detail, "status_code": exc.status_code},
                        headers=getattr(exc, "headers", None))


@app.exception_handler(Exception)
async def general_exception_handler(request, exc):
    logger.exception("Unhandled exception: %s", exc)
    return JSONResponse(status_code=500, content={"error": "Internal server error", "detail": "Internal server error",
                                                  "status_code": 500})


# ===================== React SPA (registered LAST so it can never shadow an API route) =====================
BASE_DIR = Path(__file__).resolve().parent.parent
frontend_path = Path(os.getenv("FRONTEND_DIST", str(BASE_DIR / "react-frontend" / "dist")))
_RESERVED_PREFIXES = ("api/", "health", "metrics", "docs", "openapi.json")

if (frontend_path / "index.html").exists():
    logger.info("✅ Serving React frontend from %s", frontend_path)
    if (frontend_path / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=str(frontend_path / "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        """Serve a real built file if one matches, otherwise index.html so client-side routes work."""
        if full_path.startswith(_RESERVED_PREFIXES):
            raise HTTPException(status_code=404, detail="Route not found")
        root = frontend_path.resolve()
        candidate = (root / full_path).resolve()
        if full_path and candidate.is_file() and root in candidate.parents:  # no path traversal
            return FileResponse(candidate)
        return FileResponse(root / "index.html")
else:
    logger.warning("⚠️ React build not found at %s - API only (npm run build in react-frontend, or use the dev server)",
                   frontend_path)

    @app.get("/", include_in_schema=False)
    async def api_root():
        return {"service": "bescom", "version": VERSION, "docs": None if IS_PRODUCTION else "/docs"}


if __name__ == "__main__":
    uvicorn.run("app.main:app", host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8000")),
                workers=int(os.getenv("UVICORN_WORKERS", "1")), log_level=os.getenv("UVICORN_LOG_LEVEL", "info"))
