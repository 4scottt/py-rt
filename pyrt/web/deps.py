"""The request context: one database session, the user, the session gate.

One middleware does three things once per request: it opens the session the
request's queries run in, resolves the signed-in user from the cookie, and
turns an anonymous request for a page that needs a session into a redirect
(FP L04). Everything after it reads ``request.state``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Final

from fastapi import Depends
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from pyrt import auth
from pyrt.acl import Principals, request_principals
from pyrt.config import Settings
from pyrt.db.models import User
from pyrt.web.errors import RedirectToLogin, login_redirect, wanted_path

#: The paths a request reaches without a session (FP L04's whitelist).
PUBLIC_PATHS: Final[frozenset[str]] = frozenset({"/", "/login", "/logout", "/health"})
PUBLIC_PREFIXES: Final[tuple[str, ...]] = ("/static/",)


def is_public(path: str) -> bool:
    """Whether ``path`` is reachable signed out."""
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Open the session, resolve the user, gate the private paths.

    The session is closed when the handler returns, so a response must have
    its body in hand by then: a template response renders eagerly, and no
    route may stream rows out of the database lazily.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        settings: Settings = request.app.state.settings
        session: Session = request.app.state.session_factory()
        request.state.db = session
        request.state.settings = settings
        request.state.acl_cache = {}
        try:
            request.state.user = auth.current_user(request, session, settings)
            if request.state.user is None and not is_public(request.url.path):
                return login_redirect(wanted_path(request))
            return await call_next(request)
        finally:
            session.close()


def get_db(request: Request) -> Session:
    """The request's session. It is closed when the response is done."""
    db: Session = request.state.db
    return db


def get_settings(request: Request) -> Settings:
    """The process's settings (plan §6), read once at start."""
    settings: Settings = request.app.state.settings
    return settings


def optional_user(request: Request) -> User | None:
    """The signed-in user, or None on the public paths."""
    user: User | None = getattr(request.state, "user", None)
    return user


def signed_in(request: Request) -> User:
    """The signed-in user; an anonymous request is redirected to the login."""
    user: User | None = getattr(request.state, "user", None)
    if user is None:
        raise RedirectToLogin(wanted_path(request))
    return user


#: The annotations a router declares its dependencies with::
#:
#:     def page(db: Db, user: SignedIn, held: ActorPrincipals) -> Response
Db = Annotated[Session, Depends(get_db)]
Config = Annotated[Settings, Depends(get_settings)]
CurrentUser = Annotated[User | None, Depends(optional_user)]
SignedIn = Annotated[User, Depends(signed_in)]
ActorPrincipals = Annotated[Principals, Depends(request_principals)]
