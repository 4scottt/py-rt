"""Run Alembic programmatically: no ``alembic.ini`` at runtime.

``pyrt migrate`` and ``pyrt serve`` both come through :func:`upgrade`, which
is a no-op once the database is at head.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def make_config(url: str) -> Config:
    """An Alembic config pointed at this package's migrations and ``url``."""
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    # ConfigParser interpolates %; a password may contain one.
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    """Upgrade the database at ``url`` to ``revision`` (default head)."""
    command.upgrade(make_config(url), revision)


def current(url: str) -> None:  # pragma: no cover - a hand tool
    """Print the database's current revision."""
    command.current(make_config(url), verbose=True)
