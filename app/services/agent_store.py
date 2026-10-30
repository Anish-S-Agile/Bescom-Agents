"""
Agent data store: users, address book (contacts) and the send history (audit log).

Plain SQLAlchemy, so it runs unchanged on PostgreSQL or on the fake postgres (see fake_postgres.py).
All methods are synchronous and return plain dicts; async callers use `await store.run(store.method, ...)`.
"""
import asyncio
import logging
import os
import re
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from passlib.context import CryptContext
from sqlalchemy import (JSON, Boolean, Column, DateTime, Float, Integer, String, Text, UniqueConstraint, delete, func,
                        or_, select, text)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import declarative_base, sessionmaker

from app.services import fake_postgres

logger = logging.getLogger(__name__)
Base = declarative_base()
_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 5
MAX_PASSWORD_BYTES = 72  # bcrypt limit


class DuplicateError(ValueError):
    """Unique constraint violated (username/email/contact name)."""


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


# ----------------------------------------------------------------------------- models
class AgentUser(Base):
    __tablename__ = "agent_users"
    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(64), nullable=False)
    username_lc = Column(String(64), nullable=False, unique=True, index=True)
    email = Column(String(255), nullable=False)
    email_lc = Column(String(255), nullable=False, unique=True, index=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(16), nullable=False, default="user")
    is_active = Column(Boolean, nullable=False, default=True)
    failed_logins = Column(Integer, nullable=False, default=0)
    locked_until = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_login = Column(DateTime, nullable=True)


class AgentContact(Base):
    __tablename__ = "agent_contacts"
    __table_args__ = (UniqueConstraint("owner", "name_lc", name="uq_agent_contact_owner_name"),)
    id = Column(Integer, primary_key=True, autoincrement=True)
    owner = Column(String(255), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    name_lc = Column(String(120), nullable=False)
    email = Column(String(255), nullable=True)
    phone = Column(String(32), nullable=True)
    organization = Column(String(160), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class AgentMessage(Base):
    __tablename__ = "agent_messages"
    id = Column(Integer, primary_key=True, autoincrement=True)
    owner = Column(String(255), nullable=False, index=True)
    session_id = Column(String(128), nullable=True, index=True)
    channel = Column(String(16), nullable=False, index=True)
    status = Column(String(16), nullable=False, index=True)  # sent | failed | dry_run | duplicate
    recipients = Column(JSON, nullable=False, default=list)
    subject = Column(String(300), nullable=True)
    body = Column(Text, nullable=True)
    provider_ids = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    drafted_by_ai = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)


# Monitoring events (written by app/services/metrics_recorder.py, read by Grafana).
# Timestamps are naive UTC, which Grafana's PostgreSQL data source treats as UTC.
class MetricLLMCall(Base):
    __tablename__ = "metrics_llm_calls"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    model = Column(String(120), nullable=False)
    operation = Column(String(64), nullable=False)
    status = Column(String(64), nullable=False)  # success | <exception class name>
    duration_ms = Column(Float, nullable=False, default=0.0)
    input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)


class MetricAgentRun(Base):
    __tablename__ = "metrics_agent_runs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    agent_name = Column(String(64), nullable=False)
    operation = Column(String(64), nullable=False)
    success = Column(Boolean, nullable=False)
    duration_ms = Column(Float, nullable=False, default=0.0)


class MetricMessage(Base):
    __tablename__ = "metrics_messages"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    channel = Column(String(16), nullable=False)
    status = Column(String(16), nullable=False)  # sent | failed | dry_run | duplicate


class MetricHttpRequest(Base):
    __tablename__ = "metrics_http_requests"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    method = Column(String(8), nullable=False)
    route = Column(String(200), nullable=False)  # route template, so /api/contacts/7 -> /api/contacts/{id}
    status_code = Column(Integer, nullable=False)
    duration_ms = Column(Float, nullable=False, default=0.0)


METRIC_MODELS = {"llm_call": MetricLLMCall, "agent_run": MetricAgentRun, "message": MetricMessage,
                 "http_request": MetricHttpRequest}
GRAFANA_ROLE = "grafana_ro"  # created by grafana/postgres-init; may only read the metrics_* tables


def _user_dict(u: AgentUser) -> Dict[str, Any]:
    return {"id": u.id, "username": u.username, "email": u.email, "role": u.role, "is_active": u.is_active,
            "is_verified": True, "created_at": u.created_at.isoformat() if u.created_at else None,
            "last_login": u.last_login.isoformat() if u.last_login else None}


def _contact_dict(c: AgentContact) -> Dict[str, Any]:
    return {"id": c.id, "name": c.name, "email": c.email, "phone": c.phone, "organization": c.organization,
            "notes": c.notes, "created_at": c.created_at.isoformat() if c.created_at else None}


def _message_dict(m: AgentMessage) -> Dict[str, Any]:
    return {"id": m.id, "owner": m.owner, "session_id": m.session_id, "channel": m.channel, "status": m.status,
            "recipients": m.recipients or [], "subject": m.subject, "body": m.body,
            "provider_ids": m.provider_ids or [], "error": m.error, "drafted_by_ai": m.drafted_by_ai,
            "created_at": m.created_at.isoformat() if m.created_at else None}


# ----------------------------------------------------------------------------- store
_SCHEMA_LOCK_KEY = 727274  # arbitrary constant shared by every worker (pg advisory lock)


class AgentStore:
    def __init__(self, engine=None):
        self._engine = engine
        self._session_factory = None
        self._ready = False
        self._init_lock = threading.Lock()

    # ------------------------------------------------------------------ lifecycle
    @property
    def engine(self):
        if self._engine is None:
            self._engine = fake_postgres.get_engine()
        return self._engine

    def init(self) -> Dict[str, Any]:
        """Create tables (idempotent) and seed demo data when running on the fake postgres."""
        with self._init_lock:
            if not self._ready:
                self._create_tables()
                self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
                self._ready = True
                self._grant_grafana_read()
                if fake_postgres.should_seed():
                    self._seed()
        return self.describe()

    def _create_tables(self) -> None:
        if self.engine.dialect.name != "postgresql":
            Base.metadata.create_all(self.engine)
            return
        # Several uvicorn workers start at once on a fresh database; CREATE TABLE racing itself fails with a
        # UniqueViolation. A transaction-scoped advisory lock lets one worker create, the rest then find the tables.
        with self.engine.begin() as conn:
            conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _SCHEMA_LOCK_KEY})
            Base.metadata.create_all(conn)

    @property
    def ready(self) -> bool:
        """Tables exist and the store has been initialised (monitoring writes are skipped until then)."""
        return self._ready

    def _grant_grafana_read(self) -> None:
        """On PostgreSQL, let the Grafana role read the metrics tables (and nothing else) when that role exists."""
        if self.engine.dialect.name != "postgresql":
            return
        tables = ", ".join(m.__tablename__ for m in METRIC_MODELS.values())
        try:
            with self.engine.begin() as conn:
                conn.execute(text(
                    f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{GRAFANA_ROLE}') THEN "
                    f"GRANT SELECT ON {tables} TO {GRAFANA_ROLE}; END IF; END $$"))
        except Exception as e:  # monitoring access must never stop the app from starting
            logger.warning("Could not grant %s read access to the metrics tables: %s", GRAFANA_ROLE, str(e)[:160])

    def _session(self):
        if not self._ready:
            self.init()
        return self._session_factory()

    def describe(self) -> Dict[str, Any]:
        info = fake_postgres.describe()
        try:
            with self._session() as s:
                info.update(users=s.scalar(select(func.count(AgentUser.id))),
                            contacts=s.scalar(select(func.count(AgentContact.id))),
                            messages=s.scalar(select(func.count(AgentMessage.id))), healthy=True)
        except Exception as e:
            info.update(healthy=False, error=str(e)[:160])
        return info

    async def run(self, fn, *args, **kwargs):
        """Run a blocking store call off the event loop."""
        return await asyncio.to_thread(fn, *args, **kwargs)

    def _seed(self) -> None:
        with self._session() as s:
            if s.scalar(select(func.count(AgentUser.id))):
                return
        admin_pw = os.getenv("FAKE_POSTGRES_ADMIN_PASSWORD", "admin123")
        self.create_user("admin", "admin@example.com", admin_pw, role="admin")
        self.create_user("demo", "demo@example.com", os.getenv("FAKE_POSTGRES_DEMO_PASSWORD", "demo123"))
        for owner in ("demo", "admin"):
            self.add_contact(owner, "Alice Johnson", email="alice@example.com", phone="+14155550101",
                             organization="Acme Corp")
            self.add_contact(owner, "Bob Kumar", email="bob@example.com", phone="+919876543210",
                             organization="Globex")
        logger.warning("🧪 Seeded demo users (admin / demo) in the fake postgres - change or disable with "
                       "FAKE_POSTGRES_SEED=false")

    # ------------------------------------------------------------------ users
    @staticmethod
    def _check_password(password: str) -> None:
        if not password or len(password) < 6:
            raise ValueError("Password must be at least 6 characters")
        if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
            raise ValueError(f"Password must be at most {MAX_PASSWORD_BYTES} bytes")

    def create_user(self, username: str, email: str, password: str, role: str = "user") -> Dict[str, Any]:
        username, email = (username or "").strip(), (email or "").strip()
        if len(username) < 3 or len(username) > 64 or not re.fullmatch(r"[A-Za-z0-9_.\-@]+", username):
            raise ValueError("Username must be 3-64 characters (letters, digits, . _ - @)")
        self._check_password(password)
        user = AgentUser(username=username, username_lc=username.lower(), email=email, email_lc=email.lower(),
                         password_hash=_pwd.hash(password), role="admin" if role == "admin" else "user")
        with self._session() as s:
            s.add(user)
            try:
                s.commit()
            except IntegrityError as e:
                s.rollback()
                raise DuplicateError("User already exists") from e
            return _user_dict(user)

    def authenticate(self, login: str, password: str) -> Dict[str, Any]:
        """Verify credentials (username or email). Raises AuthError; locks the account after repeated failures."""
        key = (login or "").strip().lower()
        with self._session() as s:
            u = s.scalar(select(AgentUser).where(or_(AgentUser.username_lc == key, AgentUser.email_lc == key)))
            if u is None:
                _pwd.hash(password or "x")  # keep timing similar to a real check
                raise AuthError("Invalid username or password")
            now = datetime.utcnow()
            if u.locked_until and u.locked_until > now:
                mins = int((u.locked_until - now).total_seconds() // 60) + 1
                raise AuthError(f"Too many failed attempts. Try again in {mins} minute(s).", status=429)
            if not u.is_active:
                raise AuthError("Account disabled", status=403)
            if not _pwd.verify((password or "")[:200], u.password_hash):
                u.failed_logins = (u.failed_logins or 0) + 1
                if u.failed_logins >= MAX_FAILED_LOGINS:
                    u.locked_until, u.failed_logins = now + timedelta(minutes=LOCKOUT_MINUTES), 0
                s.commit()
                raise AuthError("Invalid username or password")
            u.failed_logins, u.locked_until, u.last_login = 0, None, now
            s.commit()
            return _user_dict(u)

    def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        with self._session() as s:
            u = s.scalar(select(AgentUser).where(AgentUser.username_lc == (username or "").strip().lower()))
            return _user_dict(u) if u else None

    # ------------------------------------------------------------------ contacts
    @staticmethod
    def _clean_contact(name, email, phone):
        from app.agents.common import normalize_email, normalize_phone
        name = (name or "").strip()
        if not name or len(name) > 120:
            raise ValueError("Contact name is required (max 120 characters)")
        email = (email or "").strip() or None
        phone = (phone or "").strip() or None
        if email:
            email = normalize_email(email)
            if not email:
                raise ValueError("Invalid email address")
        if phone:
            phone = normalize_phone(phone)
            if not phone:
                raise ValueError("Invalid phone number (use international format, e.g. +919876543210)")
        if not email and not phone:
            raise ValueError("A contact needs an email address or a phone number")
        return name, email, phone

    def add_contact(self, owner: str, name: str, email: str = None, phone: str = None,
                    organization: str = None, notes: str = None) -> Dict[str, Any]:
        name, email, phone = self._clean_contact(name, email, phone)
        c = AgentContact(owner=owner, name=name, name_lc=name.lower(), email=email, phone=phone,
                         organization=(organization or "").strip()[:160] or None,
                         notes=(notes or "").strip()[:2000] or None)
        with self._session() as s:
            s.add(c)
            try:
                s.commit()
            except IntegrityError as e:
                s.rollback()
                raise DuplicateError(f"A contact named '{name}' already exists") from e
            return _contact_dict(c)

    def update_contact(self, owner: str, contact_id: int, **fields) -> Optional[Dict[str, Any]]:
        with self._session() as s:
            c = s.scalar(select(AgentContact).where(AgentContact.id == contact_id, AgentContact.owner == owner))
            if c is None:
                return None
            name, email, phone = self._clean_contact(fields.get("name", c.name), fields.get("email", c.email),
                                                     fields.get("phone", c.phone))
            c.name, c.name_lc, c.email, c.phone = name, name.lower(), email, phone
            if "organization" in fields:
                c.organization = (fields["organization"] or "").strip()[:160] or None
            if "notes" in fields:
                c.notes = (fields["notes"] or "").strip()[:2000] or None
            try:
                s.commit()
            except IntegrityError as e:
                s.rollback()
                raise DuplicateError(f"A contact named '{name}' already exists") from e
            return _contact_dict(c)

    def delete_contact(self, owner: str, contact_id: int) -> bool:
        with self._session() as s:
            c = s.scalar(select(AgentContact).where(AgentContact.id == contact_id, AgentContact.owner == owner))
            if c is None:
                return False
            s.delete(c)
            s.commit()
            return True

    def list_contacts(self, owner: str, q: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        stmt = select(AgentContact).where(AgentContact.owner == owner)
        if q:
            like = f"%{q.strip().lower()}%"
            stmt = stmt.where(or_(AgentContact.name_lc.like(like), func.lower(AgentContact.email).like(like),
                                  func.lower(AgentContact.organization).like(like)))
        with self._session() as s:
            return [_contact_dict(c) for c in s.scalars(stmt.order_by(AgentContact.name_lc).limit(limit))]

    def find_in_text(self, owner: str, text: str) -> List[Dict[str, Any]]:
        """Contacts the user mentioned by name: full name, or first name when it is unambiguous."""
        low = (text or "").lower()
        if not low.strip():
            return []
        contacts = self.list_contacts(owner, limit=1000)
        firsts: Dict[str, int] = {}
        for c in contacts:
            f = c["name"].split()[0].lower()
            firsts[f] = firsts.get(f, 0) + 1
        found: List[Dict[str, Any]] = []
        for c in contacts:
            full = c["name"].lower()
            first = full.split()[0]
            hit = re.search(rf"(?<!\w){re.escape(full)}(?!\w)", low) or (
                len(first) >= 3 and firsts[first] == 1 and re.search(rf"(?<!\w){re.escape(first)}(?!\w)", low))
            if hit:
                found.append(c)
        return found

    # ------------------------------------------------------------------ message history
    def log_message(self, owner: str, channel: str, status: str, recipients: List[str], subject: str = None,
                    body: str = None, session_id: str = None, provider_ids: List[str] = None, error: str = None,
                    drafted_by_ai: bool = False) -> int:
        m = AgentMessage(owner=owner or "anonymous", session_id=session_id, channel=channel, status=status,
                         recipients=list(recipients or []), subject=(subject or "")[:300] or None,
                         body=body, provider_ids=provider_ids or [], error=(error or None),
                         drafted_by_ai=bool(drafted_by_ai))
        with self._session() as s:
            s.add(m)
            s.commit()
            return m.id

    def list_messages(self, owner: Optional[str], limit: int = 50, offset: int = 0,
                      channel: Optional[str] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """owner=None returns everyone's messages (admin)."""
        stmt = select(AgentMessage)
        if owner is not None:
            stmt = stmt.where(AgentMessage.owner == owner)
        if channel:
            stmt = stmt.where(AgentMessage.channel == channel)
        if status:
            stmt = stmt.where(AgentMessage.status == status)
        stmt = stmt.order_by(AgentMessage.created_at.desc(), AgentMessage.id.desc()).offset(max(offset, 0)).limit(
            min(max(limit, 1), 500))
        with self._session() as s:
            return [_message_dict(m) for m in s.scalars(stmt)]

    def message_stats(self, owner: Optional[str]) -> Dict[str, Any]:
        stmt = select(AgentMessage.channel, AgentMessage.status, func.count(AgentMessage.id)).group_by(
            AgentMessage.channel, AgentMessage.status)
        if owner is not None:
            stmt = stmt.where(AgentMessage.owner == owner)
        out: Dict[str, Any] = {"total": 0, "by_channel": {}, "by_status": {}}
        with self._session() as s:
            for channel, status, n in s.execute(stmt):
                out["total"] += n
                out["by_channel"][channel] = out["by_channel"].get(channel, 0) + n
                out["by_status"][status] = out["by_status"].get(status, 0) + n
        return out

    # ------------------------------------------------------------------ monitoring events
    def record_metrics(self, events: List[Tuple[str, Dict[str, Any]]]) -> None:
        """Insert a batch of (kind, fields) events; kind is a key of METRIC_MODELS."""
        with self._session() as s:
            s.add_all(METRIC_MODELS[kind](**fields) for kind, fields in events)
            s.commit()

    def purge_metrics(self, older_than_days: int) -> int:
        cutoff = datetime.utcnow() - timedelta(days=older_than_days)
        removed = 0
        with self._session() as s:
            for model in METRIC_MODELS.values():
                removed += s.execute(delete(model).where(model.ts < cutoff)).rowcount or 0
            s.commit()
        return removed


agent_store = AgentStore()
