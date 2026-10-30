"""
WhatsApp Agent - sends WhatsApp messages through the Meta WhatsApp Cloud API or Twilio.

Environment:
    WHATSAPP_PROVIDER          "meta" (default) | "twilio"
  Meta Cloud API:
    WHATSAPP_ACCESS_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_API_VERSION (v21.0)
    WHATSAPP_DEFAULT_TEMPLATE / WHATSAPP_TEMPLATE_LANG (en): optional approved template used for
        business-initiated messages (WhatsApp only allows free text inside the 24h customer window)
  Twilio:
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM (e.g. +14155238886)
  Common:
    WHATSAPP_TIMEOUT (15), WHATSAPP_MAX_ATTEMPTS (3), WHATSAPP_MAX_CONCURRENCY (10),
    WHATSAPP_MAX_RECIPIENTS (20), DEFAULT_COUNTRY_CODE (91), AGENT_DRY_RUN (false)
"""
import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional

import httpx

from app.agents.base_agent import BaseAgent
from app.agents.common import (AgentResult, IdempotencyCache, PermanentError, TransientError, env_bool, env_float,
                               env_int, mask_recipient, normalize_phone, with_retries)

logger = logging.getLogger(__name__)

MAX_BODY_CHARS = 4096  # WhatsApp text message limit


def _retry_after(resp: httpx.Response) -> Optional[float]:
    try:
        return float(resp.headers.get("Retry-After"))
    except (TypeError, ValueError):
        return None


class WhatsAppAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__("WhatsAppAgent", "Sends WhatsApp messages via the Meta Cloud API or Twilio.")
        self.provider = os.getenv("WHATSAPP_PROVIDER", "meta").strip().lower()
        # Meta
        self.token = os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()
        self.phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
        self.api_version = os.getenv("WHATSAPP_API_VERSION", "v21.0").strip()
        self.default_template = os.getenv("WHATSAPP_DEFAULT_TEMPLATE", "").strip()
        self.template_lang = os.getenv("WHATSAPP_TEMPLATE_LANG", "en").strip()
        # Twilio
        self.twilio_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
        self.twilio_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
        self.twilio_from = os.getenv("TWILIO_WHATSAPP_FROM", "").strip().replace("whatsapp:", "")
        # Provider endpoints: the real ones unless overridden (staging mocks / provider test proxies)
        self.meta_base_url = os.getenv("WHATSAPP_META_BASE_URL", "https://graph.facebook.com").strip().rstrip("/")
        self.twilio_base_url = os.getenv("TWILIO_API_BASE_URL", "https://api.twilio.com").strip().rstrip("/")
        # Common
        self.timeout = env_float("WHATSAPP_TIMEOUT", 15.0)
        self.max_attempts = env_int("WHATSAPP_MAX_ATTEMPTS", 3)
        self.max_recipients = env_int("WHATSAPP_MAX_RECIPIENTS", 20)
        self.dry_run = env_bool("AGENT_DRY_RUN", False)
        self._slots = asyncio.Semaphore(env_int("WHATSAPP_MAX_CONCURRENCY", 10))
        self._sent = IdempotencyCache(ttl_seconds=env_float("AGENT_IDEMPOTENCY_TTL", 300.0))
        self._client: Optional[httpx.AsyncClient] = None

    # ------------------------------------------------------------------ status
    @property
    def configured(self) -> bool:
        if self.provider == "twilio":
            return bool(self.twilio_sid and self.twilio_token and self.twilio_from)
        if self.provider == "meta":
            return bool(self.token and self.phone_number_id)
        return False

    def health(self) -> Dict[str, Any]:
        return {"agent": self.agent_name, "ready": self.configured or self.dry_run, "provider": self.provider,
                "configured": self.configured, "dry_run": self.dry_run}

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=min(self.timeout, 5.0)),
                limits=httpx.Limits(max_connections=50, max_keepalive_connections=20))
        return self._client

    # ------------------------------------------------------------------ public API
    async def send(
        self,
        to: List[str],
        body: str = "",
        *,
        template_name: Optional[str] = None,
        template_params: Optional[List[str]] = None,
        idempotency_key: Optional[str] = None,
    ) -> AgentResult:
        started = time.time()
        result = await self._send(to, body, template_name, template_params or [], idempotency_key)
        self._record("send_whatsapp", started, result.success, result.error)
        return result

    async def _send(self, to, body, template_name, template_params, idempotency_key) -> AgentResult:
        phones, invalid = [], []
        for raw in to or []:
            p = normalize_phone(raw)
            (phones if p else invalid).append(p or str(raw))
        phones = list(dict.fromkeys(phones))
        if invalid:
            return AgentResult(False, "whatsapp", error=f"invalid phone number(s): {', '.join(invalid)} "
                               "(use international format, e.g. +919876543210)")
        if not phones:
            return AgentResult(False, "whatsapp", error="at least one phone number is required")
        if len(phones) > self.max_recipients:
            return AgentResult(False, "whatsapp", error=f"too many recipients ({len(phones)} > {self.max_recipients})")
        body = (body or "").strip()
        template_name = template_name or None
        if not body and not template_name:
            return AgentResult(False, "whatsapp", error="message text is required")
        if len(body) > MAX_BODY_CHARS:
            return AgentResult(False, "whatsapp", error=f"message too long ({len(body)} > {MAX_BODY_CHARS} characters)")

        if self.dry_run:
            logger.info(f"🧪 [dry-run] whatsapp -> {[mask_recipient(p) for p in phones]}")
            return AgentResult(True, "whatsapp", dry_run=True, recipients=phones,
                               message=f"[dry-run] WhatsApp message validated for {len(phones)} recipient(s); not sent.")
        if not self.configured:
            return AgentResult(False, "whatsapp",
                               error=f"WhatsApp service is not configured for provider '{self.provider}'",
                               recipients=phones)

        sent, failed, ids, duplicates = [], [], [], 0
        results = await asyncio.gather(
            *[self._send_one(p, body, template_name, template_params, idempotency_key) for p in phones])
        errors: List[str] = []
        retryable = False
        for phone, (ok, ident, err, dup, can_retry) in zip(phones, results):
            if ok:
                sent.append(phone)
                if ident:
                    ids.append(ident)
                duplicates += int(dup)
            else:
                failed.append(phone)
                errors.append(f"{mask_recipient(phone)}: {err}")
                retryable = retryable or can_retry

        if failed and not sent:
            return AgentResult(False, "whatsapp", error="; ".join(errors), retryable=retryable, recipients=phones)
        msg = f"WhatsApp message sent to {len(sent)} recipient(s)"
        if duplicates == len(sent):
            msg = "Duplicate request ignored: this WhatsApp message was already sent moments ago."
        if failed:
            msg += f"; failed for {len(failed)}: " + "; ".join(errors)
        return AgentResult(True, "whatsapp", message=msg, recipients=phones, provider_ids=ids,
                           duplicate=bool(sent) and duplicates == len(sent),
                           details={"sent": [mask_recipient(p) for p in sent],
                                    "failed": [mask_recipient(p) for p in failed]})

    async def _send_one(self, phone, body, template_name, template_params, idempotency_key):
        """Returns (ok, provider_id, error, duplicate, retryable)."""
        key = IdempotencyCache.key("whatsapp", phone, body, template_name, template_params, idempotency_key)
        if self._sent.seen(key):
            return True, None, None, True, False
        try:
            async with self._slots:
                ident = await with_retries(lambda: self._deliver(phone, body, template_name, template_params),
                                           attempts=self.max_attempts, label="WhatsApp send")
        except TransientError as e:
            logger.error(f"❌ WhatsApp to {mask_recipient(phone)} failed after retries: {e}")
            return False, None, f"provider unavailable: {e}", False, True
        except PermanentError as e:
            logger.error(f"❌ WhatsApp to {mask_recipient(phone)} rejected: {e}")
            return False, None, str(e), False, False
        self._sent.remember(key)
        logger.info(f"✅ WhatsApp sent to {mask_recipient(phone)}")
        return True, ident, None, False, False

    # ------------------------------------------------------------------ providers
    async def _deliver(self, phone, body, template_name, template_params) -> Optional[str]:
        try:
            if self.provider == "twilio":
                return await self._deliver_twilio(phone, body)
            return await self._deliver_meta(phone, body, template_name, template_params)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise TransientError(f"{type(e).__name__}: {e}") from e

    def _classify(self, resp: httpx.Response, detail: str) -> None:
        """Raise the right error type for a non-2xx response."""
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientError(f"HTTP {resp.status_code} {detail}", retry_after=_retry_after(resp))
        raise PermanentError(f"HTTP {resp.status_code} {detail}")

    async def _deliver_meta(self, phone, body, template_name, template_params) -> Optional[str]:
        template_name = template_name or (self.default_template or None)
        payload: Dict[str, Any] = {"messaging_product": "whatsapp", "recipient_type": "individual",
                                   "to": phone.lstrip("+")}
        if template_name:
            template: Dict[str, Any] = {"name": template_name, "language": {"code": self.template_lang}}
            params = template_params or ([body] if body else [])
            if params:
                template["components"] = [{"type": "body", "parameters": [{"type": "text", "text": str(p)}
                                                                          for p in params]}]
            payload.update(type="template", template=template)
        else:
            payload.update(type="text", text={"preview_url": False, "body": body})
        url = f"{self.meta_base_url}/{self.api_version}/{self.phone_number_id}/messages"
        resp = await self._http().post(url, json=payload, headers={"Authorization": f"Bearer {self.token}"})
        if resp.status_code >= 300:
            try:
                err = resp.json().get("error", {})
                detail = f"{err.get('message', resp.text[:200])} (code {err.get('code')})"
            except ValueError:
                detail = resp.text[:200]
            self._classify(resp, detail)
        try:
            return resp.json()["messages"][0]["id"]
        except (ValueError, KeyError, IndexError):
            return None

    async def _deliver_twilio(self, phone, body) -> Optional[str]:
        url = f"{self.twilio_base_url}/2010-04-01/Accounts/{self.twilio_sid}/Messages.json"
        data = {"To": f"whatsapp:{phone}", "From": f"whatsapp:{self.twilio_from}", "Body": body}
        resp = await self._http().post(url, data=data, auth=(self.twilio_sid, self.twilio_token))
        if resp.status_code >= 300:
            try:
                j = resp.json()
                detail = f"{j.get('message', resp.text[:200])} (code {j.get('code')})"
            except ValueError:
                detail = resp.text[:200]
            self._classify(resp, detail)
        try:
            return resp.json().get("sid")
        except ValueError:
            return None
