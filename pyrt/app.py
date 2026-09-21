"""The ASGI application.

M1-foundation keeps this small: configuration, the engine and ``/health``.
A later package adds the routers, templates, middleware and telemetry.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from pyrt import __version__
from pyrt.config import Settings, load_settings
from pyrt.db.engine import make_engine, make_session_factory

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. ``uvicorn`` calls this as a factory."""
    settings = settings or load_settings()
    settings.require_serving()

    app = FastAPI(title="py-rt", version=__version__, docs_url=None, redoc_url=None)
    engine = make_engine(settings.database_url())
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)

    @app.get("/health", include_in_schema=False)
    def health() -> JSONResponse:
        """A cheap database ping; no auth, no session, no row written."""
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception as exc:  # the probe never raises, it reports
            log.warning("health check failed", extra={"error": str(exc)})
            return JSONResponse({"status": "db unreachable"}, status_code=503)
        return JSONResponse({"status": "ok"}, status_code=200)

    return app
