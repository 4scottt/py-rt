"""The ASGI application: configuration, the routers, the shell, the errors.

This module is the wiring and nothing else. A later package adds one line to
:func:`routers` and owns its own router module, templates and tests.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, FastAPI
from starlette.staticfiles import StaticFiles

from pyrt import __version__
from pyrt.config import Settings, load_settings
from pyrt.db.engine import make_engine, make_session_factory
from pyrt.web import errors
from pyrt.web.deps import RequestContextMiddleware
from pyrt.web.templating import STATIC_DIR, make_templates

log = logging.getLogger(__name__)


def routers() -> list[APIRouter]:
    """Every router, in the order they are registered.

    The extension point of M1: a package adds its import and its router
    here, and nothing else in this module changes.
    """
    from pyrt.queues import router as queues
    from pyrt.users import router as users
    from pyrt.web import home

    return [home.router, queues.router, users.router]


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. ``uvicorn`` calls this as a factory."""
    settings = settings or load_settings()
    settings.require_serving()

    app = FastAPI(title="py-rt", version=__version__, docs_url=None, redoc_url=None)
    engine = make_engine(settings.database_url())
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.templates = make_templates()

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    for router in routers():
        app.include_router(router)

    errors.register(app)
    app.add_middleware(RequestContextMiddleware)

    log.info("app built", extra={"base_url": settings.base_url, "site": settings.site_name})
    return app
