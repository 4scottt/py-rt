"""The foundation: migrations, the seed and the health probe."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, inspect, select
from sqlalchemy.orm import Session

from pyrt import cli
from pyrt.db import migrate
from pyrt.db.models import (
    DEFAULT_QUEUE_NAME,
    ROLE_GROUPS,
    ROOT_USER_NAME,
    SYSTEM_GROUP_EVERYONE,
    SYSTEM_GROUPS,
    TABLE_NAMES,
    Group,
    GroupKind,
    ObjectKind,
    PrincipalKind,
    Queue,
    Right,
    User,
)
from pyrt.db.seed import seed, verify_password
from tests.conftest import ROOT_TEST_PASSWORD, truncate_all


def test_fp_o01_migrations_idempotent_and_seed_once(
    engine: Engine, db: Session, dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upgrading twice is a no-op; the seed runs only when ``users`` is empty."""
    migrate.upgrade(dsn)
    migrate.upgrade(dsn)

    # The fixture seeded already, so a second seed declines.
    assert seed(db, ROOT_TEST_PASSWORD) is False
    db.rollback()

    truncate_all(engine)
    assert seed(db, ROOT_TEST_PASSWORD) is True
    assert seed(db, ROOT_TEST_PASSWORD) is False
    db.rollback()

    # `pyrt migrate` on its own works and leaves the schema alone.
    monkeypatch.setenv("DATABASE_URL", dsn)
    assert cli.main(["migrate"]) == 0
    assert set(inspect(engine).get_table_names()) >= set(TABLE_NAMES)


def test_fp_q05_general_queue_seeded_and_everyone_has_nothing(db: Session) -> None:
    """General exists after the seed; Everyone is granted nothing."""
    general = db.scalar(select(Queue).where(Queue.name == DEFAULT_QUEUE_NAME))
    assert general is not None
    assert general.disabled is False

    everyone = db.scalar(select(Group).where(Group.name == SYSTEM_GROUP_EVERYONE))
    assert everyone is not None
    assert everyone.kind == GroupKind.SYSTEM

    grants = db.scalars(
        select(Right).where(
            Right.principal_kind == PrincipalKind.GROUP,
            Right.principal_id == everyone.id,
        )
    ).all()
    assert grants == []


def test_fp_u04_root_seeded_privileged_with_superuser_and_idempotent(db: Session) -> None:
    """root is privileged, its password verifies, its SuperUser grant is global."""
    root = db.scalar(select(User).where(User.name == ROOT_USER_NAME))
    assert root is not None
    assert root.privileged is True
    assert root.disabled is False
    assert verify_password(ROOT_TEST_PASSWORD, root.password_hash)
    assert not verify_password("wrong", root.password_hash)

    grants = db.scalars(
        select(Right).where(
            Right.principal_kind == PrincipalKind.USER,
            Right.principal_id == root.id,
        )
    ).all()
    assert len(grants) == 1
    assert grants[0].right_name == "SuperUser"
    assert grants[0].object_kind == ObjectKind.SYSTEM
    assert grants[0].object_id == 0

    # Re-seeding changes nothing.
    hash_before = root.password_hash
    assert seed(db, "another") is False
    db.rollback()
    db.expire_all()
    root_again = db.scalar(select(User).where(User.name == ROOT_USER_NAME))
    assert root_again is not None
    assert root_again.password_hash == hash_before


def test_seed_creates_the_system_and_role_groups_and_nobody(db: Session) -> None:
    groups = {g.name: g for g in db.scalars(select(Group)).all()}
    assert set(groups) == set(SYSTEM_GROUPS) | set(ROLE_GROUPS)
    for name in SYSTEM_GROUPS:
        assert groups[name].kind == GroupKind.SYSTEM
    for name in ROLE_GROUPS:
        assert groups[name].kind == GroupKind.ROLE

    nobody = db.scalar(select(User).where(User.name == "Nobody"))
    assert nobody is not None
    assert nobody.disabled is True
    assert nobody.privileged is False
    assert nobody.password_hash is None
    assert nobody.email is None


def test_seed_without_a_root_password_leaves_root_unable_to_sign_in(
    engine: Engine, db: Session
) -> None:
    truncate_all(engine)
    assert seed(db, None) is True
    root = db.scalar(select(User).where(User.name == ROOT_USER_NAME))
    assert root is not None
    assert root.password_hash is None
    assert not verify_password("", root.password_hash)


def test_migrations_create_every_table_of_the_data_model(engine: Engine) -> None:
    """Plan §12: the twelve tables plus Alembic's own."""
    tables = set(inspect(engine).get_table_names())
    assert tables == set(TABLE_NAMES) | {"alembic_version"}


def test_health_answers_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
