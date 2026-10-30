"""
Fake Postgres - a drop-in database for tests and local development.

The agents (users, contacts, message history, conversation sessions) talk to the database only through
SQLAlchemy, so the very same models run on real PostgreSQL in production and on a SQLite-backed stand-in
here. Nothing else in the code base needs to know which one it is talking to.

    AGENT_DB_MODE   auto (default)  use PostgreSQL when it is reachable, otherwise fall back to the fake
                    postgres      PostgreSQL only - fail loudly if it is unreachable (production)
                    fake          always use the fake
    AGENT_DATABASE_URL   PostgreSQL URL for the agent data (falls back to DATABASE_URL), or instead the parts
                         AGENT_DB_HOST / _PORT / _NAME / _USER / _PASSWORD (+ AGENT_DB_SSLMODE)
    The same settings work on a laptop, in Docker and on a VM: a localhost host is mapped to the Docker host
    inside a container, and AGENT_DB_CONNECT_RETRIES / _POOL_SIZE / _CONNECT_TIMEOUT tune a remote database.
    FAKE_POSTGRES_URL    default sqlite:///data/fake_postgres.db ; "sqlite://" = pure in-memory (tests)
    FAKE_POSTGRES_SEED   seed a demo admin + contacts in fake mode (default true; never in production)
"""
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Union

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.pool import StaticPool

from app.services import runtime_env

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_lock = threading.Lock()
_engine: Optional[Engine] = None
_info: Dict[str, Any] = {}


def _env_true(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def is_production() -> bool:
    return os.getenv("ENVIRONMENT", os.getenv("ENV", "development")).strip().lower() in ("production", "prod")


def mask_url(url: Union[str, URL]) -> str:
    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:
        return "<unparseable url>"


def _resolve_sqlite_url(url: str) -> str:
    """Make a sqlite file URL independent of the working directory and make sure its folder exists."""
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite" or not parsed.database or parsed.database == ":memory:":
        return url
    path = Path(parsed.database)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path.as_posix()}"


def _fake_url() -> str:
    url = os.getenv("FAKE_POSTGRES_URL", "").strip()
    if url:
        return _resolve_sqlite_url(url)
    path = PROJECT_ROOT / "data" / "fake_postgres.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path.as_posix()}"


def _build_fake_engine(url: str) -> Engine:
    if url in ("sqlite://", "sqlite:///:memory:") or ":memory:" in url:
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    else:
        engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _):  # integrity + concurrency settings closer to what Postgres gives us
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        try:
            cur.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        cur.close()

    return engine


def postgres_url() -> Optional[URL]:
    """
    The PostgreSQL target, from either form (the URL wins):
      AGENT_DATABASE_URL (or DATABASE_URL)   postgresql://user:pass@host:5432/db  (postgres:// is accepted too)
      AGENT_DB_HOST / _PORT / _NAME / _USER / _PASSWORD   separate parts; the password needs no URL-encoding
    AGENT_DB_SSLMODE (disable | require | verify-full ...) is added when the URL has no sslmode of its own.
    Returns None when nothing is configured.
    """
    raw = (os.getenv("AGENT_DATABASE_URL") or os.getenv("DATABASE_URL") or "").strip()
    if raw:
        if raw.startswith("postgres://"):  # the Heroku-style scheme; SQLAlchemy 2 only knows postgresql://
            raw = "postgresql://" + raw[len("postgres://"):]
        if not raw.startswith("postgresql"):
            return None
        url = make_url(raw)
    elif os.getenv("AGENT_DB_HOST", "").strip():
        url = URL.create("postgresql+psycopg2", host=os.environ["AGENT_DB_HOST"].strip(),
                         port=int(os.getenv("AGENT_DB_PORT", "").strip() or 5432),
                         database=os.getenv("AGENT_DB_NAME", "").strip() or "agents",
                         username=os.getenv("AGENT_DB_USER", "").strip() or "agents",
                         password=os.getenv("AGENT_DB_PASSWORD", "") or None)
    else:
        return None
    host = runtime_env.container_host(url.host)
    if host != url.host:
        logger.info("PostgreSQL host %s -> %s (app runs in a container)", url.host, host)
        url = url.set(host=host)
    sslmode = os.getenv("AGENT_DB_SSLMODE", "").strip()
    if sslmode and "sslmode" not in url.query:
        url = url.update_query_dict({"sslmode": sslmode})
    return url


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _try_postgres(url: URL, attempts: int = 1) -> Optional[Engine]:
    """Connect, retrying so the app can start while a VM / container database is still coming up."""
    delay = _env_int("AGENT_DB_CONNECT_RETRY_DELAY", 3)
    engine = create_engine(
        url, pool_pre_ping=True, pool_size=_env_int("AGENT_DB_POOL_SIZE", 5),
        max_overflow=_env_int("AGENT_DB_MAX_OVERFLOW", 10),
        pool_recycle=_env_int("AGENT_DB_POOL_RECYCLE", 1800),  # firewalls / NAT on VMs drop idle connections
        connect_args={"connect_timeout": _env_int("AGENT_DB_CONNECT_TIMEOUT", 5), "keepalives": 1,
                      "keepalives_idle": 30, "keepalives_interval": 10, "keepalives_count": 5})
    for attempt in range(1, attempts + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return engine
        except Exception as e:
            logger.warning("⚠️ PostgreSQL unreachable (%s) [attempt %d/%d]: %s", mask_url(url), attempt, attempts,
                           str(e).splitlines()[0][:160])
            if attempt < attempts:
                time.sleep(delay)
    engine.dispose()
    return None


def get_engine() -> Engine:
    """Process-wide engine for agent data: real Postgres when available, fake postgres otherwise."""
    global _engine, _info
    with _lock:
        if _engine is not None:
            return _engine
        mode = os.getenv("AGENT_DB_MODE", "auto").strip().lower()
        pg_url = postgres_url()
        engine: Optional[Engine] = None
        if mode in ("auto", "postgres") and pg_url is not None:
            # postgres mode waits for the database (it is mandatory); auto tries once and falls back
            engine = _try_postgres(pg_url, attempts=max(_env_int("AGENT_DB_CONNECT_RETRIES", 5 if mode == "postgres" else 1), 1))
            if engine is None and mode == "postgres":
                raise RuntimeError(f"AGENT_DB_MODE=postgres but PostgreSQL is unreachable: {mask_url(pg_url)}")
        elif mode == "postgres":
            raise RuntimeError("AGENT_DB_MODE=postgres but no PostgreSQL is configured "
                               "(set AGENT_DATABASE_URL, or AGENT_DB_HOST/PORT/NAME/USER/PASSWORD)")
        if engine is None and mode == "auto" and is_production():
            # never silently keep production data in a local SQLite file
            raise RuntimeError("ENVIRONMENT=production but PostgreSQL is not reachable/configured "
                               f"({mask_url(pg_url) if pg_url else 'no URL'}); refusing to fall back to the fake postgres")
        if engine is not None:
            _info = {"mode": "postgres", "dialect": "postgresql", "url": mask_url(pg_url), "fake": False}
        else:
            url = _fake_url()
            engine = _build_fake_engine(url)
            _info = {"mode": "fake", "dialect": "sqlite", "url": mask_url(url), "fake": True}
            logger.warning("🧪 Using FAKE POSTGRES (%s) for agent data - fine for tests/dev, not for production",
                           _info["url"])
            if is_production() and mode == "auto":
                logger.error("❌ ENVIRONMENT=production but agent data is on the fake postgres; "
                             "set AGENT_DB_MODE=postgres to refuse this")
        _engine = engine
        return _engine


def describe() -> Dict[str, Any]:
    get_engine()
    return dict(_info)


def should_seed() -> bool:
    return bool(_info.get("fake")) and _env_true("FAKE_POSTGRES_SEED", True) and not is_production()


def reset_engine(dispose: bool = True) -> None:
    """Forget the engine (tests). Does not delete data."""
    global _engine, _info
    with _lock:
        if _engine is not None and dispose:
            _engine.dispose()
        _engine, _info = None, {}
