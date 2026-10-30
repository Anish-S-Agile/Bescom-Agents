"""
JWT signing settings, shared by the auth routes.

JWT_SECRET_KEY must be set. In production (ENVIRONMENT=production) the app refuses to start without it;
elsewhere a random per-process key is used so tokens are valid only until the next restart.
"""
import logging
import os
import secrets

logger = logging.getLogger(__name__)

ALGORITHM = "HS256"
MIN_SECRET_LENGTH = 32


def _load_secret() -> str:
    secret = os.getenv("JWT_SECRET_KEY", "").strip()
    env = os.getenv("ENVIRONMENT", "development").strip().lower()
    if secret:
        if len(secret) < MIN_SECRET_LENGTH:
            if env in ("production", "prod"):
                raise RuntimeError(f"JWT_SECRET_KEY must be at least {MIN_SECRET_LENGTH} characters")
            logger.warning("⚠️ JWT_SECRET_KEY is shorter than %d characters", MIN_SECRET_LENGTH)
        return secret
    if env in ("production", "prod"):
        raise RuntimeError("JWT_SECRET_KEY must be set when ENVIRONMENT=production")
    logger.warning("⚠️ JWT_SECRET_KEY not set - using a random key; logins will not survive a restart")
    return secrets.token_urlsafe(48)


SECRET_KEY = _load_secret()
