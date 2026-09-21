"""The ``pyrt`` command: migrate | seed | serve | healthcheck.

``serve`` is the image's default command and does the startup of plan §6:
migrate, seed, listen.
"""

from __future__ import annotations

import argparse
import logging
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence

from pyrt import __version__
from pyrt import logging as pyrt_logging
from pyrt.config import ConfigError, Settings, load_settings
from pyrt.db import migrate as db_migrate
from pyrt.db.engine import make_engine, make_session_factory, session_scope
from pyrt.db.seed import seed as run_seed

log = logging.getLogger("pyrt.cli")

HEALTH_TIMEOUT = 5.0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pyrt", description="py-rt: Request Tracker's ticket core"
    )
    parser.add_argument("--version", action="version", version=f"py-rt {__version__}")
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="root log level for this command (default INFO)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="upgrade the database to the latest revision")
    sub.add_parser("seed", help="migrate, then seed a fresh database")
    sub.add_parser("serve", help="migrate, seed and serve")
    sub.add_parser("healthcheck", help="GET /health on the local port; exit 0 when it is 200")
    return parser


def _migrate(settings: Settings) -> None:
    db_migrate.upgrade(settings.database_url())


def _seed(settings: Settings) -> bool:
    engine = make_engine(settings.database_url())
    try:
        with session_scope(make_session_factory(engine)) as session:
            return run_seed(session, settings.root_password)
    finally:
        engine.dispose()


def _serve(settings: Settings) -> int:
    import uvicorn

    _migrate(settings)
    _seed(settings)
    uvicorn.run(
        "pyrt.app:create_app",
        factory=True,
        host="0.0.0.0",  # a container listens on every interface
        port=settings.port,
        workers=settings.workers,
        access_log=True,
        log_config=None,
    )
    return 0


def _healthcheck(settings: Settings) -> int:
    url = f"http://127.0.0.1:{settings.port}/health"
    try:
        with urllib.request.urlopen(url, timeout=HEALTH_TIMEOUT) as response:
            return 0 if response.status == 200 else 1
    except (urllib.error.URLError, OSError, ValueError):
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; the console script exits with what this returns."""
    args = _parser().parse_args(argv)
    pyrt_logging.configure(args.log_level)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.command == "healthcheck":
        return _healthcheck(settings)

    if args.command == "serve":
        try:
            settings.require_serving()
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return _serve(settings)

    if args.command == "migrate":
        _migrate(settings)
        return 0

    if args.command == "seed":
        _migrate(settings)
        seeded = _seed(settings)
        log.info("seed", extra={"seeded": seeded})
        return 0

    return 2  # pragma: no cover - argparse rejects an unknown command first


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
