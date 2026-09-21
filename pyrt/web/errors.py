"""The error pages and the sign-in redirect.

A refused action is the 403 page with the heading "You are not allowed"
(plan §9, FP R08), never a 500. An anonymous request for a page that needs a
session is a redirect to ``/`` carrying where it wanted to go (FP L04).
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import FastAPI
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from pyrt.acl import Forbidden
from pyrt.web.templating import render

log = logging.getLogger(__name__)

#: A redirect that a browser turns into a GET of the login page.
SEE_OTHER = 303

FORBIDDEN_HEADING = "You are not allowed"


class RedirectToLogin(Exception):
    """Raised for an anonymous request to a page that needs a session."""

    def __init__(self, next_path: str = "") -> None:
        super().__init__("a session is required")
        self.next_path = next_path


def wanted_path(request: Request) -> str:
    """The path (with its query) the request asked for, for ``next``."""
    path = request.url.path
    query = request.url.query
    return f"{path}?{query}" if query else path


def login_redirect(next_path: str = "") -> RedirectResponse:
    """A redirect to ``/``; a relative path is safe to hand back as ``next``."""
    target = "/"
    if next_path.startswith("/") and not next_path.startswith("//"):
        target = f"/?next={quote(next_path, safe='/')}"
    return RedirectResponse(target, status_code=SEE_OTHER)


def safe_next(candidate: str | None) -> str:
    """``candidate`` when it is a path on this site, else ``/``."""
    if candidate and candidate.startswith("/") and not candidate.startswith("//"):
        return candidate
    return "/"


async def forbidden_handler(request: Request, exc: Exception) -> Response:
    """FP R08: the denial page, 403, with the right that was missing."""
    right = getattr(exc, "right", "")
    log.info("forbidden", extra={"path": request.url.path, "right": right})
    return render(request, "errors/403.html", {"page_title": FORBIDDEN_HEADING}, status_code=403)


async def redirect_handler(request: Request, exc: Exception) -> Response:
    """FP L04: back to the login page, remembering where to return to."""
    return login_redirect(getattr(exc, "next_path", ""))


async def http_exception_handler(request: Request, exc: Exception) -> Response:
    """404 and 403 as pages; anything else keeps Starlette's plain answer."""
    assert isinstance(exc, StarletteHTTPException)
    if exc.status_code == 404:
        return render(request, "errors/404.html", {"page_title": "Page not found"}, status_code=404)
    if exc.status_code == 403:
        return render(
            request, "errors/403.html", {"page_title": FORBIDDEN_HEADING}, status_code=403
        )
    return Response(exc.detail, status_code=exc.status_code, headers=exc.headers)


def register(app: FastAPI) -> None:
    """Install the handlers. A later package adds nothing here."""
    app.add_exception_handler(Forbidden, forbidden_handler)
    app.add_exception_handler(RedirectToLogin, redirect_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
