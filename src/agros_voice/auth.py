from __future__ import annotations

import secrets
import time
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from .config import get_settings
from .db import unscoped
from .services import Actor

SESSION_COOKIE = "agros_session"
CSRF_COOKIE = "agros_csrf"
CSRF_HEADER = "X-CSRF-Token"
SESSION_TTL = 8 * 3600
READ_ROLES = {"admin", "manager", "cta", "viewer"}
CALL_ROLES = {"admin", "manager", "cta"}
WRITE_ROLES = {"admin", "manager"}

_ph = PasswordHasher()
_DUMMY_HASH = _ph.hash("timing-equalizer-not-a-password")


def hash_password(password: str) -> str:
    return _ph.hash(password)


def authenticate(email: str, password: str) -> Actor | None:
    with unscoped() as conn:
        row = conn.execute(
            "select * from auth_lookup_user(%s)", (email.strip().lower(),)
        ).fetchone()
    try:
        _ph.verify(row["password_hash"] if row else _DUMMY_HASH, password)
    except VerifyMismatchError:
        return None
    if not row or row["disabled"]:
        return None
    return Actor(str(row["tenant_id"]), str(row["id"]), row["role"])


def issue_token(actor: Actor) -> str:
    now = int(time.time())
    claims = {
        "sub": actor.user_id,
        "tenant_id": actor.tenant_id,
        "role": actor.role,
        "iat": now,
        "exp": now + SESSION_TTL,
    }
    return jwt.encode(claims, get_settings().session_secret, algorithm="HS256")


def decode_token(token: str) -> Actor | None:
    try:
        c: dict[str, Any] = jwt.decode(
            token,
            get_settings().session_secret,
            algorithms=["HS256"],
            options={"require": ["exp", "sub", "tenant_id", "role"]},
        )
    except jwt.PyJWTError:
        return None
    if c["role"] not in READ_ROLES:
        return None
    return Actor(str(c["tenant_id"]), str(c["sub"]), str(c["role"]))


def new_csrf() -> str:
    return secrets.token_urlsafe(24)
