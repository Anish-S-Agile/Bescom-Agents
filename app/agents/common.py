"""
Shared building blocks for the orchestrator and its sub-agents:
result type, env helpers, recipient validation, rate limiting, retry and idempotency.
"""
import asyncio
import hashlib
import logging
import os
import random
import re
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------- env helpers
def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def env_list(name: str) -> List[str]:
    return [p.strip().lower() for p in os.getenv(name, "").split(",") if p.strip()]


# ----------------------------------------------------------------------------- result type
@dataclass
class AgentResult:
    """Uniform result returned by every sub-agent."""
    success: bool
    channel: str
    message: str = ""
    error: Optional[str] = None
    retryable: bool = False
    dry_run: bool = False
    duplicate: bool = False
    recipients: List[str] = field(default_factory=list)
    provider_ids: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "channel": self.channel,
            "message": self.message,
            "error": self.error,
            "retryable": self.retryable,
            "dry_run": self.dry_run,
            "duplicate": self.duplicate,
            "recipients": [mask_recipient(r) for r in self.recipients],
            "provider_ids": self.provider_ids,
            "details": self.details,
        }


class TransientError(Exception):
    """A failure worth retrying (timeout, 429, 5xx, dropped connection)."""

    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


class PermanentError(Exception):
    """A failure that retrying cannot fix (bad credentials, rejected recipient, 4xx)."""


# ----------------------------------------------------------------------------- validation
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
_PHONE_CANDIDATE_RE = re.compile(r"(?<![\w@])(\+?\d[\d\s().\-]{7,17}\d)(?![\w@])")
_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


def find_emails(text: str) -> List[str]:
    seen: List[str] = []
    for m in _EMAIL_RE.findall(text or ""):
        if m.lower() not in (s.lower() for s in seen):
            seen.append(m)
    return seen


def normalize_email(address: str) -> Optional[str]:
    """Return a normalised address, or None when it is not a valid email."""
    from email_validator import EmailNotValidError, validate_email
    try:
        return validate_email((address or "").strip(), check_deliverability=False).normalized
    except EmailNotValidError:
        return None


def normalize_phone(raw: str, default_country_code: Optional[str] = None) -> Optional[str]:
    """Normalise a phone number to E.164 (+<country><number>) or return None."""
    if raw is None:
        return None
    digits = re.sub(r"[^\d+]", "", str(raw).strip())
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if not digits.startswith("+"):
        cc = (default_country_code if default_country_code is not None
              else os.getenv("DEFAULT_COUNTRY_CODE", "91")).lstrip("+")
        digits = digits.lstrip("0")
        if cc and len(digits) <= 10:  # national number without country code
            digits = f"+{cc}{digits}"
        else:
            digits = f"+{digits}"
    return digits if _E164_RE.match(digits) else None


def find_phones(text: str) -> List[str]:
    """Extract phone numbers (normalised to E.164) from free text, ignoring digits inside emails."""
    scrubbed = _EMAIL_RE.sub(" ", text or "")
    out: List[str] = []
    for cand in _PHONE_CANDIDATE_RE.findall(scrubbed):
        phone = normalize_phone(cand)
        if phone and phone not in out:
            out.append(phone)
    return out


def strip_addresses(text: str) -> str:
    """Remove email addresses and phone numbers, so names inside them ('bob' in bob@acme.com) cannot match."""
    return _PHONE_CANDIDATE_RE.sub(" ", _EMAIL_RE.sub(" ", text or ""))


def mask_recipient(value: str) -> str:
    """Mask a recipient for logs and API responses (PII)."""
    if not value:
        return value
    if "@" in value:
        local, _, domain = value.partition("@")
        return f"{local[:1]}***@{domain}"
    return f"{value[:3]}{'*' * max(len(value) - 6, 2)}{value[-3:]}"


def clean_header(value: str, limit: int = 200) -> str:
    """Strip CR/LF so user text can never inject extra mail headers."""
    return re.sub(r"[\r\n]+", " ", value or "").strip()[:limit]


# ----------------------------------------------------------------------------- rate limiting
class RateLimiter:
    """Sliding-window limiter, per key. In-process: with N workers the effective limit is N x limit."""

    def __init__(self, max_calls: int, window_seconds: float = 60.0):
        self.max_calls = max_calls
        self.window = window_seconds
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> Tuple[bool, float]:
        """Return (allowed, seconds_until_retry)."""
        if self.max_calls <= 0:
            return True, 0.0
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.max_calls:
                return False, max(self.window - (now - hits[0]), 1.0)
            hits.append(now)
            if len(self._hits) > 10_000:  # bound memory
                for k in [k for k, v in self._hits.items() if not v]:
                    del self._hits[k]
            return True, 0.0


# ----------------------------------------------------------------------------- idempotency
class IdempotencyCache:
    """Remembers recently *successful* sends so a retried request cannot double-send."""

    def __init__(self, ttl_seconds: float = 300.0, max_entries: int = 5000):
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._seen: Dict[str, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def key(*parts: Any) -> str:
        return hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()

    def seen(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            ts = self._seen.get(key)
            if ts is None:
                return False
            if now - ts > self.ttl:
                del self._seen[key]
                return False
            return True

    def remember(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            if len(self._seen) >= self.max_entries:
                cutoff = now - self.ttl
                self._seen = {k: t for k, t in self._seen.items() if t > cutoff}
                if len(self._seen) >= self.max_entries:
                    self._seen.pop(next(iter(self._seen)))
            self._seen[key] = now


# ----------------------------------------------------------------------------- retry
async def with_retries(
    op: Callable[[], Awaitable[Any]],
    *,
    attempts: int,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    label: str = "operation",
) -> Any:
    """Run `op`, retrying TransientError with exponential backoff + jitter (honours Retry-After)."""
    attempts = max(attempts, 1)
    for attempt in range(1, attempts + 1):
        try:
            return await op()
        except TransientError as e:
            if attempt >= attempts:
                raise
            delay = e.retry_after if e.retry_after is not None else base_delay * (2 ** (attempt - 1))
            delay = min(delay, max_delay) + random.uniform(0, 0.25)
            logger.warning(f"⚠️ {label} attempt {attempt}/{attempts} failed ({e}); retrying in {delay:.1f}s")
            await asyncio.sleep(delay)
