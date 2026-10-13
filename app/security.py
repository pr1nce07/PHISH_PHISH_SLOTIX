"""Authentication, sessions, CSRF, rate limiting, role checks and audit logging.

The login mechanism is deliberately isolated here so that Google sign-in (OIDC) and authenticator
(TOTP) MFA can be added later without touching the rest of the application: every other module only
depends on `current_user` / `require_roles`.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import timedelta

from fastapi import Depends, HTTPException, Request
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import models as m
from .config import settings
from .db import SessionLocal, get_db

COOKIE = "tt_session"
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


# ---- passwords --------------------------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    n, r, p = settings.scrypt_n, 8, 1
    dk = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=256 * 1024 * 1024)
    return f"scrypt${n}${r}${p}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str | None) -> bool:
    try:
        _, n, r, p, salt, dk = (stored or "").split("$")
        calc = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32,
                              maxmem=256 * 1024 * 1024)
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except Exception:
        return False


_DUMMY = None


def dummy_verify(password: str) -> None:
    """Burn the same CPU for unknown users so response time does not reveal account existence."""
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_password(secrets.token_urlsafe(16))
    verify_password(password, _DUMMY)


def password_problem(pw: str) -> str | None:
    if len(pw) < 12:
        return "Password must be at least 12 characters long."
    if len(pw) > 200:
        return "Password is too long."
    if pw.lower() == pw or pw.upper() == pw or not any(c.isdigit() for c in pw):
        return "Password needs upper- and lower-case letters and a digit."
    return None


# ---- rate limiting ----------------------------------------------------------------------------
class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window: int) -> bool:
        """Record a hit; False when the limit is exceeded."""
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True

    def blocked(self, key: str, limit: int, window: int) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - window:
                q.popleft()
            return len(q) >= limit

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


def rate_limit(name: str, limit: int, window: int):
    def dep(request: Request):
        user = current_user_optional(request)
        key = f"{name}:{user.id if user else client_ip(request)}"
        if not limiter.hit(key, limit, window):
            raise HTTPException(429, "Too many requests. Please slow down and try again later.")
    return dep


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# ---- audit ------------------------------------------------------------------------------------
_SECRET_KEYS = ("password", "token", "secret", "csrf", "cookie")


def _scrub(value):
    if isinstance(value, dict):
        return {k: ("[redacted]" if any(s in k.lower() for s in _SECRET_KEYS) else _scrub(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value[:50]]
    if isinstance(value, str) and len(value) > 300:
        return value[:300] + "…"
    return value


def audit(db: Session, user, action: str, entity: str | None = None, entity_id=None, detail: dict | None = None,
          request: Request | None = None, outcome: str = "ok") -> None:
    db.add(m.AuditEvent(actor_user_id=getattr(user, "id", None), actor_name=getattr(user, "username", None), action=action,
                        entity=entity, entity_id=None if entity_id is None else str(entity_id), outcome=outcome,
                        detail=_scrub(detail or {}), ip=client_ip(request) if request else None))


def audit_now(user, action: str, request: Request | None = None, outcome: str = "denied", detail: dict | None = None) -> None:
    """Write an audit event in its own transaction (used for denied/failed attempts)."""
    with SessionLocal() as db:
        audit(db, user, action, detail=detail, request=request, outcome=outcome)
        db.commit()


# ---- sessions ---------------------------------------------------------------------------------
@dataclass
class CurrentUser:
    id: int
    username: str
    full_name: str
    roles: frozenset[str]
    faculty_id: int | None
    student_group_id: int | None
    csrf_token: str
    token_hash: str

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def user_roles(db: Session, user_id: int) -> frozenset[str]:
    return frozenset(db.scalars(select(m.AppRole.code).join(m.AppUserRole, m.AppUserRole.role_id == m.AppRole.id)
                                .where(m.AppUserRole.user_id == user_id)))


def create_session(db: Session, user: m.AppUser) -> tuple[str, str]:
    """New random session id on every login (prevents session fixation). Only its hash is stored."""
    db.execute(delete(m.AuthSession).where(m.AuthSession.expires_at < m.utcnow()))
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    db.add(m.AuthSession(token_hash=_hash_token(token), user_id=user.id, csrf_token=csrf,
                         expires_at=m.utcnow() + timedelta(minutes=settings.session_minutes)))
    return token, csrf


def revoke_sessions(db: Session, user_id: int, keep: str | None = None) -> None:
    q = delete(m.AuthSession).where(m.AuthSession.user_id == user_id)
    if keep:
        q = q.where(m.AuthSession.token_hash != keep)
    db.execute(q)


def _load_user(request: Request) -> CurrentUser | None:
    token = request.cookies.get(COOKIE)
    if not token or len(token) > 200:
        return None
    with SessionLocal() as db:
        th = _hash_token(token)
        sess = db.get(m.AuthSession, th)
        now = m.utcnow()
        if sess is None:
            return None
        if sess.expires_at < now or sess.last_seen < now - timedelta(minutes=settings.session_idle_minutes):
            db.delete(sess)
            db.commit()
            return None
        user = db.get(m.AppUser, sess.user_id)
        if user is None or not user.active:
            return None
        if (now - sess.last_seen).total_seconds() > 60:
            sess.last_seen = now
            db.commit()
        return CurrentUser(user.id, user.username, user.full_name, user_roles(db, user.id), user.faculty_id,
                           user.student_group_id, sess.csrf_token, th)


def current_user_optional(request: Request) -> CurrentUser | None:
    if not hasattr(request.state, "user"):
        request.state.user = _load_user(request)
    return request.state.user


def current_user(request: Request) -> CurrentUser:
    user = current_user_optional(request)
    if user is None:
        raise HTTPException(401, "Authentication required.")
    if request.method in UNSAFE:
        sent = request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(sent, user.csrf_token):
            audit_now(user, "security.csrf_rejected", request, detail={"path": request.url.path})
            raise HTTPException(403, "Invalid or missing CSRF token.")
    return user


def require_roles(*roles: str):
    """Default-deny role gate. Denials are audited."""
    def dep(request: Request, user: CurrentUser = Depends(current_user)) -> CurrentUser:
        if not user.roles & set(roles):
            audit_now(user, "security.forbidden", request, detail={"path": request.url.path, "method": request.method,
                                                                    "required": sorted(roles)})
            raise HTTPException(403, "You do not have permission to perform this action.")
        return user
    return dep


require_admin = require_roles("admin")
require_faculty = require_roles("faculty")
any_user = current_user
DB = Depends(get_db)
