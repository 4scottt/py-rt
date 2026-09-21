"""Passwords and the signed session cookie (plan §10, "Sessions", "Passwords").

bcrypt at cost 12 is RT's default and is what makes the login page's ~200 ms
on every side. The session is an ``itsdangerous`` token over
``{"uid": …, "iat": …}`` in a cookie; the ``Authorization`` header is never
read (plan §6: Caddy strips it).
"""

from __future__ import annotations

import logging
import time
from functools import lru_cache
from typing import Final

import bcrypt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import Response

from pyrt.config import Settings
from pyrt.db.models import User

log = logging.getLogger(__name__)

#: The cookie's name, its lifetime and the salt the signer is namespaced with.
SESSION_COOKIE: Final = "pyrt_session"
SESSION_MAX_AGE: Final = 12 * 60 * 60  # 12 hours (plan §10)
SESSION_SALT: Final = "pyrt.session"

BCRYPT_COST: Final = 12

#: The password a sign-in verifies against when the user does not exist, so an
#: unknown name costs the same as a known one.
_DUMMY_PASSWORD: Final = "pyrt-no-such-user"


def hash_password(password: str) -> str:
    """A bcrypt hash at cost 12 (RT's default; the login page's ~200 ms)."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=BCRYPT_COST)).decode()


def verify_password(password: str, password_hash: str | None) -> bool:
    """True when ``password`` matches; a user without a hash cannot sign in."""
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:  # a hash the library cannot parse is simply no match
        return False


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """One cost-12 hash, computed once, to spend on names that do not exist."""
    return hash_password(_DUMMY_PASSWORD)


def authenticate(db: Session, name: str, password: str) -> User | None:
    """The user of that name when the password verifies and it may sign in.

    Every call spends one bcrypt verification, whether or not the name
    exists, so a wrong name and a wrong password take the same time.
    """
    user = db.scalar(select(User).where(User.name == name)) if name else None
    candidate = user.password_hash if user is not None and user.password_hash else _dummy_hash()
    matched = verify_password(password, candidate)
    if user is None or not matched:
        return None
    if user.disabled or not user.password_hash:
        return None
    return user


def serializer(settings: Settings) -> URLSafeTimedSerializer:
    """The signer for the session cookie, keyed by ``SESSION_SECRET``."""
    return URLSafeTimedSerializer(settings.session_secret, salt=SESSION_SALT)


def issue_session(settings: Settings, user: User, issued_at: int | None = None) -> str:
    """The cookie value for ``user``."""
    payload = {"uid": user.id, "iat": issued_at if issued_at is not None else int(time.time())}
    return serializer(settings).dumps(payload)


def read_session(settings: Settings, token: str) -> int | None:
    """The user id in ``token``, or None when it is missing, stale or forged."""
    if not token:
        return None
    try:
        payload = serializer(settings).loads(token, max_age=SESSION_MAX_AGE)
    except SignatureExpired:
        return None
    except BadSignature:
        log.info("session cookie rejected")
        return None
    if not isinstance(payload, dict):
        return None
    uid = payload.get("uid")
    return uid if isinstance(uid, int) else None


def cookie_is_secure(settings: Settings) -> bool:
    """Secure is set when ``BASE_URL`` is https (plan §10)."""
    return settings.base_url.lower().startswith("https://")


def set_session_cookie(response: Response, settings: Settings, user: User) -> None:
    """Sign ``user`` into ``response``: HttpOnly, SameSite=Lax, 12 hours."""
    response.set_cookie(
        SESSION_COOKIE,
        issue_session(settings, user),
        max_age=SESSION_MAX_AGE,
        path="/",
        httponly=True,
        samesite="lax",
        secure=cookie_is_secure(settings),
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    """Sign out: the cookie is deleted with the flags it was set with."""
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        httponly=True,
        samesite="lax",
        secure=cookie_is_secure(settings),
    )


def current_user(request: Request, db: Session, settings: Settings) -> User | None:
    """The signed-in user, or None.

    None for a missing, expired, tampered or stale cookie, and for a user that
    has since been disabled. The request's ``Authorization`` header is never
    consulted.
    """
    token = request.cookies.get(SESSION_COOKIE, "")
    uid = read_session(settings, token)
    if uid is None:
        return None
    user = db.get(User, uid)
    if user is None or user.disabled:
        return None
    return user
