"""
Email Service Agent - sends common organisation emails over SMTP.

Environment:
    SMTP_HOST, SMTP_PORT (587), SMTP_USERNAME, SMTP_PASSWORD
    SMTP_FROM (defaults to SMTP_USERNAME), SMTP_FROM_NAME
    SMTP_USE_STARTTLS (true)  / SMTP_USE_SSL (false, implicit TLS e.g. port 465)
    SMTP_TIMEOUT (20), EMAIL_MAX_ATTEMPTS (3), EMAIL_MAX_CONCURRENCY (5)
    EMAIL_MAX_RECIPIENTS (50), EMAIL_ALLOWED_DOMAINS (comma list; empty = any)
    AGENT_DRY_RUN (false): validate and render but do not send
"""
import asyncio
import html
import logging
import os
import smtplib
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any, Dict, List, Optional

import aiosmtplib

from app.agents.base_agent import BaseAgent
from app.agents.common import (AgentResult, IdempotencyCache, PermanentError, TransientError, clean_header, env_bool,
                               env_float, env_int, env_list, mask_recipient, normalize_email, with_retries)
from app.agents.email_templates import render_template

logger = logging.getLogger(__name__)

_TRANSIENT_EXC = (aiosmtplib.SMTPConnectError, aiosmtplib.SMTPServerDisconnected, aiosmtplib.SMTPTimeoutError,
                  aiosmtplib.SMTPConnectTimeoutError, aiosmtplib.SMTPReadTimeoutError, asyncio.TimeoutError,
                  ConnectionError, OSError)


class EmailAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__(
            "EmailServiceAgent",
            "Sends common organisation emails (announcements, invites, reminders, notices) via SMTP.")
        self.host = os.getenv("SMTP_HOST", "").strip()
        self.port = env_int("SMTP_PORT", 587)
        self.username = os.getenv("SMTP_USERNAME", "").strip()
        self.password = os.getenv("SMTP_PASSWORD", "")
        self.sender = os.getenv("SMTP_FROM", "").strip() or self.username
        self.sender_name = os.getenv("SMTP_FROM_NAME", "").strip()
        self.use_ssl = env_bool("SMTP_USE_SSL", False)
        self.use_starttls = env_bool("SMTP_USE_STARTTLS", True) and not self.use_ssl
        self.timeout = env_float("SMTP_TIMEOUT", 20.0)
        self.max_attempts = env_int("EMAIL_MAX_ATTEMPTS", 3)
        self.max_recipients = env_int("EMAIL_MAX_RECIPIENTS", 50)
        self.allowed_domains = env_list("EMAIL_ALLOWED_DOMAINS")
        self.dry_run = env_bool("AGENT_DRY_RUN", False)
        self._slots = asyncio.Semaphore(env_int("EMAIL_MAX_CONCURRENCY", 5))
        self._sent = IdempotencyCache(ttl_seconds=env_float("AGENT_IDEMPOTENCY_TTL", 300.0))

    # ------------------------------------------------------------------ status
    @property
    def configured(self) -> bool:
        return bool(self.host and self.sender)

    def health(self) -> Dict[str, Any]:
        return {"agent": self.agent_name, "ready": self.configured or self.dry_run,
                "configured": self.configured, "dry_run": self.dry_run}

    # ------------------------------------------------------------------ public API
    async def send(
        self,
        to: List[str],
        subject: str = "",
        body: str = "",
        *,
        cc: Optional[List[str]] = None,
        bcc: Optional[List[str]] = None,
        reply_to: Optional[str] = None,
        template: Optional[str] = None,
        variables: Optional[Dict[str, Any]] = None,
        html_body: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> AgentResult:
        started = time.time()
        result = await self._send(to, subject, body, cc or [], bcc or [], reply_to, template, variables or {},
                                  html_body, idempotency_key)
        self._record("send_email", started, result.success, result.error)
        return result

    async def _send(self, to, subject, body, cc, bcc, reply_to, template, variables, html_body,
                    idempotency_key) -> AgentResult:
        # --- render template
        if template:
            try:
                rendered = render_template(template, variables, default_sender=self.sender_name)
            except ValueError as e:
                return AgentResult(False, "email", error=str(e))
            if rendered is None:
                return AgentResult(False, "email", error=f"unknown email template '{template}'")
            subject = subject or rendered["subject"]
            body = body or rendered["body"]

        # --- validate recipients
        to_ok, bad = self._validate(to)
        cc_ok, bad_cc = self._validate(cc)
        bcc_ok, bad_bcc = self._validate(bcc)
        invalid = bad + bad_cc + bad_bcc
        if invalid:
            return AgentResult(False, "email", error=f"invalid recipient address(es): {', '.join(invalid)}")
        if not to_ok:
            return AgentResult(False, "email", error="at least one recipient is required")
        everyone = list(dict.fromkeys(to_ok + cc_ok + bcc_ok))
        if len(everyone) > self.max_recipients:
            return AgentResult(False, "email", error=f"too many recipients ({len(everyone)} > {self.max_recipients})")
        if self.allowed_domains:
            blocked = [a for a in everyone if a.rsplit("@", 1)[-1].lower() not in self.allowed_domains]
            if blocked:
                return AgentResult(False, "email", error="recipient domain not allowed: " +
                                   ", ".join(mask_recipient(a) for a in blocked))
        subject = clean_header(subject)
        if not subject:
            return AgentResult(False, "email", error="email subject is required")
        if not (body or "").strip() and not html_body:
            return AgentResult(False, "email", error="email body is required")
        if reply_to:
            reply_to = normalize_email(reply_to)
            if not reply_to:
                return AgentResult(False, "email", error="invalid reply-to address")

        # --- idempotency (a retried request must not email twice)
        key = idempotency_key or IdempotencyCache.key("email", sorted(everyone), subject, body)
        if self._sent.seen(key):
            logger.info("♻️ Duplicate email suppressed")
            return AgentResult(True, "email", message="Duplicate request ignored: this email was already sent "
                               "moments ago.", duplicate=True, recipients=everyone)

        if self.dry_run:
            logger.info(f"🧪 [dry-run] email '{subject}' -> {[mask_recipient(a) for a in everyone]}")
            return AgentResult(True, "email", dry_run=True, recipients=everyone,
                               message=f"[dry-run] Email '{subject}' validated for {len(everyone)} recipient(s); not sent.")
        if not self.configured:
            return AgentResult(False, "email", error="email service is not configured (set SMTP_HOST and SMTP_FROM)",
                               recipients=everyone)

        msg = self._build_message(to_ok, cc_ok, reply_to, subject, body, html_body)
        try:
            async with self._slots:
                message_id = await with_retries(lambda: self._deliver(msg, everyone),
                                                attempts=self.max_attempts, label="SMTP send")
        except TransientError as e:
            logger.error(f"❌ Email failed after retries: {e}")
            return AgentResult(False, "email", error=f"email provider unavailable: {e}", retryable=True,
                               recipients=everyone)
        except PermanentError as e:
            logger.error(f"❌ Email rejected: {e}")
            return AgentResult(False, "email", error=str(e), recipients=everyone)

        self._sent.remember(key)
        logger.info(f"✅ Email sent '{subject}' -> {[mask_recipient(a) for a in everyone]}")
        return AgentResult(True, "email", recipients=everyone, provider_ids=[message_id],
                           message=f"Email '{subject}' sent to {len(to_ok)} recipient(s)"
                                   + (f" (+{len(everyone) - len(to_ok)} cc/bcc)" if len(everyone) > len(to_ok) else ""))

    # ------------------------------------------------------------------ internals
    def _validate(self, addresses: Optional[List[str]]):
        ok, bad = [], []
        for a in addresses or []:
            n = normalize_email(a)
            (ok if n else bad).append(n or str(a))
        return list(dict.fromkeys(ok)), bad

    def _build_message(self, to, cc, reply_to, subject, body, html_body) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = f'"{clean_header(self.sender_name, 80)}" <{self.sender}>' if self.sender_name else self.sender
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        if reply_to:
            msg["Reply-To"] = reply_to
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        domain = self.sender.rsplit("@", 1)[-1] if "@" in self.sender else None
        msg["Message-ID"] = make_msgid(domain=domain)
        msg.set_content(body or "")
        html_content = html_body or "<div style=\"font-family:Arial,sans-serif;font-size:14px;line-height:1.5\">" + \
            html.escape(body or "").replace("\n", "<br>") + "</div>"
        msg.add_alternative(html_content, subtype="html")
        return msg

    async def _deliver(self, msg: EmailMessage, recipients: List[str]) -> str:
        try:
            await aiosmtplib.send(
                msg, sender=self.sender, recipients=recipients,
                hostname=self.host, port=self.port,
                username=self.username or None, password=self.password or None,
                use_tls=self.use_ssl, start_tls=self.use_starttls, timeout=self.timeout)
        except aiosmtplib.SMTPAuthenticationError as e:
            raise PermanentError(f"SMTP authentication failed: {e}") from e
        except (aiosmtplib.SMTPRecipientsRefused, aiosmtplib.SMTPSenderRefused) as e:
            raise PermanentError(f"mail server refused the message: {e}") from e
        except aiosmtplib.SMTPResponseException as e:
            if 400 <= e.code < 500:  # 4xx = temporary condition per RFC 5321
                raise TransientError(f"SMTP {e.code} {e.message}") from e
            raise PermanentError(f"SMTP {e.code} {e.message}") from e
        except _TRANSIENT_EXC as e:
            raise TransientError(f"{type(e).__name__}: {e}") from e
        except smtplib.SMTPException as e:
            raise PermanentError(f"SMTP error: {e}") from e
        return str(msg["Message-ID"])

