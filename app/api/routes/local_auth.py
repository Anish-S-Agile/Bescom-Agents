"""
Local authentication for the React frontend, backed by the agent store (PostgreSQL or the fake postgres).

  POST /api/auth/register   JSON {username, email, password}   -> {access_token, token_type, user}
  POST /api/auth/login      form  username (or email), password -> {access_token, token_type, user}
  GET  /api/auth/me         Bearer token                        -> user

Tokens are HS256 JWTs signed with JWT_SECRET_KEY and carry `sub` (username), `role` and `roles`.
Set ADMIN_USERNAMES (comma list) to give specific usernames the admin role at registration.
"""
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Form, Header, HTTPException
from jose import JWTError, jwt
from pydantic import BaseModel, Field

from app.core.security import ALGORITHM, SECRET_KEY
from app.services.agent_store import AuthError, DuplicateError, agent_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])

TOKEN_TTL_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "480"))


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    email: str = Field(..., min_length=3, max_length=255, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(..., min_length=6, max_length=128)


class CurrentUser(BaseModel):
    username: str
    role: str = "viewer"
    roles: List[str] = []
    authenticated: bool = False


def _admin_usernames() -> List[str]:
    return [u.strip().lower() for u in os.getenv("ADMIN_USERNAMES", "").split(",") if u.strip()]


def _roles_for(role: str) -> List[str]:
    return ["admin", "developer", "viewer"] if role == "admin" else ["viewer"]


def create_token(user: Dict[str, Any]) -> str:
    now = datetime.utcnow()
    return jwt.encode({"sub": user["username"], "email": user["email"], "role": user["role"],
                       "roles": _roles_for(user["role"]), "iat": now,
                       "exp": now + timedelta(minutes=TOKEN_TTL_MINUTES)}, SECRET_KEY, algorithm=ALGORITHM)


def _auth_response(user: Dict[str, Any]) -> Dict[str, Any]:
    return {"access_token": create_token(user), "token_type": "bearer", "user": user}


def decode_token(authorization: Optional[str]) -> Optional[CurrentUser]:
    """Return the user for a valid Bearer token, None when absent/invalid."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    try:
        p = jwt.decode(authorization.split(" ", 1)[1].strip(), SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None
    sub = p.get("sub") or p.get("email")
    if not sub:
        return None
    role = p.get("role") or ("admin" if "admin" in (p.get("roles") or []) else "viewer")
    return CurrentUser(username=sub, role=role, roles=p.get("roles") or _roles_for(role), authenticated=True)


def require_user(authorization: Optional[str] = Header(None)) -> CurrentUser:
    user = decode_token(authorization)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


@router.post("/register")
async def register(req: RegisterRequest):
    if os.getenv("ALLOW_REGISTRATION", "true").strip().lower() not in ("1", "true", "yes", "on"):
        raise HTTPException(status_code=403, detail="Registration is disabled. Ask an administrator for an account.")
    role = "admin" if req.username.strip().lower() in _admin_usernames() else "user"
    try:
        user = await agent_store.run(agent_store.create_user, req.username, req.email, req.password, role)
    except DuplicateError:
        raise HTTPException(status_code=400, detail="User already exists")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    logger.info("✅ Registered user %s (%s)", user["username"], user["role"])
    return _auth_response(user)


@router.post("/login")
async def login(username: str = Form(...), password: str = Form(...)):
    try:
        user = await agent_store.run(agent_store.authenticate, username, password)
    except AuthError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    return _auth_response(user)


@router.get("/me")
async def me(current: CurrentUser = Depends(require_user)):
    user = await agent_store.run(agent_store.get_user, current.username)
    if not user:
        raise HTTPException(status_code=401, detail="Unknown user")
    return user

