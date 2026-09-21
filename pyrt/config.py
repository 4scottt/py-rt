"""Runtime configuration, read once from the environment (plan §6).

Every name the oldbox card and the image agree on lives here. Nothing else
in the package reads ``os.environ`` for a setting.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final
from urllib.parse import quote

MAIL_MODES: Final = ("log", "file", "smtp")


class ConfigError(Exception):
    """A setting the app cannot serve without is missing or wrong."""


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"{name} must be an integer") from exc


@dataclass(frozen=True, slots=True)
class Settings:
    """The environment of plan §6, typed and frozen."""

    port: int = 8080
    base_url: str = ""
    site_name: str = "py-rt"

    db_host: str = "localhost"
    db_port: int = 3306
    db_name: str = "pyrt"
    db_user: str = "pyrt"
    db_password: str = ""
    database_url_override: str = ""

    root_password: str | None = None
    session_secret: str = ""

    mail_mode: str = "log"
    mail_file: str = "/var/lib/py-rt/mail.log"
    smtp_host: str = ""
    smtp_port: int = 25
    smtp_user: str = ""
    smtp_password: str = ""

    tz: str = "UTC"
    workers: int = 2

    def database_url(self) -> str:
        """The SQLAlchemy URL, ``DATABASE_URL`` winning when it is set."""
        if self.database_url_override:
            return self.database_url_override
        user = quote(self.db_user, safe="")
        password = quote(self.db_password, safe="")
        return (
            f"mysql+pymysql://{user}:{password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}?charset=utf8mb4"
        )

    def require_serving(self) -> None:
        """Raise :class:`ConfigError` when a serve-time setting is missing.

        The two messages are the card's signatures (FP O06) and are exact.
        """
        if not self.base_url:
            raise ConfigError("BASE_URL is required")
        if not self.session_secret:
            raise ConfigError("SESSION_SECRET is required")


def load_settings() -> Settings:
    """Read :class:`Settings` from the process environment."""
    mail_mode = _env("MAIL_MODE", "log").lower() or "log"
    if mail_mode not in MAIL_MODES:
        raise ConfigError("MAIL_MODE must be one of log, file, smtp")
    root_password = os.environ.get("ROOT_PASSWORD")
    return Settings(
        port=_env_int("PORT", 8080),
        base_url=_env("BASE_URL").rstrip("/"),
        site_name=_env("SITE_NAME", "py-rt") or "py-rt",
        db_host=_env("DB_HOST", "localhost") or "localhost",
        db_port=_env_int("DB_PORT", 3306),
        db_name=_env("DB_NAME", "pyrt") or "pyrt",
        db_user=_env("DB_USER", "pyrt") or "pyrt",
        db_password=os.environ.get("DB_PASSWORD", ""),
        database_url_override=_env("DATABASE_URL"),
        root_password=root_password if root_password else None,
        session_secret=os.environ.get("SESSION_SECRET", "").strip(),
        mail_mode=mail_mode,
        mail_file=_env("MAIL_FILE", "/var/lib/py-rt/mail.log") or "/var/lib/py-rt/mail.log",
        smtp_host=_env("SMTP_HOST"),
        smtp_port=_env_int("SMTP_PORT", 25),
        smtp_user=_env("SMTP_USER"),
        smtp_password=os.environ.get("SMTP_PASSWORD", ""),
        tz=_env("TZ", "UTC") or "UTC",
        workers=_env_int("WORKERS", 2),
    )
