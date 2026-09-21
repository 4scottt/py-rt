"""The SQLAlchemy engine and session factory (PyMySQL, utf8mb4)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

SessionFactory = sessionmaker[Session]


def make_engine(url: str, *, echo: bool = False) -> Engine:
    """An engine for ``url``; pre-ping so a recycled sidecar connection heals."""
    return create_engine(url, pool_pre_ping=True, future=True, echo=echo)


def make_session_factory(engine: Engine) -> SessionFactory:
    """A session factory bound to ``engine``."""
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: SessionFactory) -> Iterator[Session]:
    """A transactional scope: commit on success, roll back on an exception."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
