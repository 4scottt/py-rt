"""Test fixtures: one MariaDB per run, truncated and re-seeded per test.

``TEST_DSN`` names the database (``scripts/test.sh`` brings it up and sets
it); without it the suite skips with a message rather than inventing a
SQLite that would not be the product's database.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from pyrt.app import create_app
from pyrt.db import migrate
from pyrt.db.engine import make_engine, make_session_factory
from pyrt.db.models import TABLE_NAMES
from pyrt.db.seed import seed

TEST_DSN = os.environ.get("TEST_DSN", "")
SKIP_REASON = (
    "TEST_DSN is not set: run scripts/test.sh (it brings MariaDB up on 3308 and sets it), "
    "or export TEST_DSN=mysql+pymysql://pyrt:pyrt@127.0.0.1:3308/pyrt_test?charset=utf8mb4"
)

#: A named MySQL lock so two runs against the same database serialize
#: instead of truncating each other's rows.
TEST_LOCK = "pyrt_test_suite"
TEST_LOCK_TIMEOUT = 120

ROOT_TEST_PASSWORD = "password"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip the whole suite, with the reason, when there is no database."""
    if TEST_DSN:
        return
    skip = pytest.mark.skip(reason=SKIP_REASON)
    for item in items:
        item.add_marker(skip)


def truncate_all(engine: Engine) -> None:
    """Empty every table of plan §12, foreign keys out of the way."""
    with engine.begin() as connection:
        connection.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        for table in TABLE_NAMES:
            connection.execute(text(f"TRUNCATE TABLE `{table}`"))
        connection.execute(text("SET FOREIGN_KEY_CHECKS=1"))


@pytest.fixture(scope="session")
def dsn() -> str:
    """The test database's URL."""
    if not TEST_DSN:
        pytest.skip(SKIP_REASON)
    return TEST_DSN


@pytest.fixture(scope="session")
def engine(dsn: str) -> Iterator[Engine]:
    """One engine per run, its schema migrated once."""
    migrate.upgrade(dsn)
    eng = make_engine(dsn)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    """A session over a freshly truncated and seeded database."""
    with engine.connect() as lock:
        got = lock.execute(
            text("SELECT GET_LOCK(:name, :timeout)"),
            {"name": TEST_LOCK, "timeout": TEST_LOCK_TIMEOUT},
        ).scalar()
        if got != 1:
            pytest.fail(f"another run holds {TEST_LOCK}")
        try:
            truncate_all(engine)
            session = make_session_factory(engine)()
            seed(session, ROOT_TEST_PASSWORD)
            try:
                yield session
            finally:
                session.close()
        finally:
            lock.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": TEST_LOCK})


@pytest.fixture
def client(db: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """An HTTP client over the app, pointed at the test database."""
    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("SESSION_SECRET", "test")
    monkeypatch.setenv("SITE_NAME", "test")
    app = create_app(telemetry=None)  # the suite never builds real telemetry
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.state.engine.dispose()


@pytest.fixture(autouse=True)
def _no_ambient_otel(monkeypatch: pytest.MonkeyPatch) -> None:
    """An exported OTEL_EXPORTER_OTLP_ENDPOINT on the machine must not turn
    the suite's apps into exporters (O03 tests set their own)."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
