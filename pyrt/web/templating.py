"""Jinja2 for the shell (plan §11) and the URL helper (plan §6, FP O05).

Every absolute URL the app builds comes from ``BASE_URL``. The request's
Host is never read: probes arrive as 127.0.0.1 and a mail link must work
from outside the container.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from fastapi.templating import Jinja2Templates
from starlette.requests import Request
from starlette.templating import _TemplateResponse

from pyrt import __version__
from pyrt.acl import has_right, principals
from pyrt.config import Settings
from pyrt.db.models import User

PACKAGE_ROOT: Final = Path(__file__).resolve().parent.parent
TEMPLATES_DIR: Final = PACKAGE_ROOT / "templates"
STATIC_DIR: Final = PACKAGE_ROOT / "static"

#: The right that shows the Admin menu (plan §8's vocabulary, FP R07).
SHOW_CONFIG_TAB: Final = "ShowConfigTab"


def absolute_url(settings: Settings, path: str) -> str:
    """``BASE_URL`` + ``path``; an absolute URL is returned unchanged."""
    if path.startswith(("http://", "https://")):
        return path
    if not path.startswith("/"):
        path = "/" + path
    return f"{settings.base_url}{path}"


def shell_context(request: Request) -> dict[str, Any]:
    """What every template gets: the user, the shell's labels, the menus.

    ``nav_queues`` is empty here; the package that owns queues fills it (the
    "New ticket in" submenu of plan §11).
    """
    settings: Settings = request.app.state.settings
    user: User | None = getattr(request.state, "user", None)
    return {
        "settings": settings,
        "site_name": settings.site_name,
        "version": __version__,
        "current_user": user,
        "show_admin": _may_see_admin(request, user),
        "nav_queues": [],
        "url": lambda path: absolute_url(settings, path),
    }


def _may_see_admin(request: Request, user: User | None) -> bool:
    """The Admin menu shows with ``ShowConfigTab`` (or ``SuperUser``)."""
    if user is None:
        return False
    db = getattr(request.state, "db", None)
    if db is None:  # an error page rendered before the context was opened
        return False
    return has_right(db, principals(db, user, request), SHOW_CONFIG_TAB, None, request)


def make_templates() -> Jinja2Templates:
    """The environment: autoescaped, with the shell's context processor."""
    templates = Jinja2Templates(directory=TEMPLATES_DIR, context_processors=[shell_context])
    templates.env.autoescape = True
    templates.env.trim_blocks = True
    templates.env.lstrip_blocks = True
    return templates


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    status_code: int = 200,
) -> _TemplateResponse:
    """Render ``name`` with the shell's context plus ``context``.

    The one way a router makes an HTML response::

        return render(request, "queues/index.html", {"queues": rows})
    """
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(request, name, context or {}, status_code=status_code)
