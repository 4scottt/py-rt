"""The first-start seed (plan §10, "Seeding").

Runs after the migrations and only when ``users`` is empty: the system and
role groups, ``Nobody``, ``root`` with ``ROOT_PASSWORD``, the ``General``
queue and ``SuperUser`` to root globally. Nothing is granted to Everyone —
the mail goal's first subtask grants it, as RT's own initial data leaves it.
"""

from __future__ import annotations

import logging

import bcrypt
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pyrt.db.models import (
    DEFAULT_QUEUE_NAME,
    NOBODY_USER_NAME,
    ROLE_GROUPS,
    ROOT_USER_EMAIL,
    ROOT_USER_NAME,
    SYSTEM_GROUPS,
    Group,
    GroupKind,
    ObjectKind,
    PrincipalKind,
    Queue,
    Right,
    User,
    utcnow,
)

log = logging.getLogger(__name__)

BCRYPT_COST = 12


def hash_password(password: str) -> str:
    """A bcrypt hash at cost 12 (RT's default; the login page's ~200 ms)."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=BCRYPT_COST)).decode()


def verify_password(password: str, password_hash: str | None) -> bool:
    """True when ``password`` matches; a user without a hash cannot sign in."""
    if not password_hash:
        return False
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def users_exist(session: Session) -> bool:
    """Whether any user row exists (what decides the seed runs)."""
    count = session.scalar(select(func.count()).select_from(User))
    return bool(count)


def find_group(session: Session, name: str) -> Group | None:
    """The group of that name, whatever its kind."""
    return session.scalar(select(Group).where(Group.name == name))


def find_user(session: Session, name: str) -> User | None:
    """The user of that name."""
    return session.scalar(select(User).where(User.name == name))


def find_queue(session: Session, name: str) -> Queue | None:
    """The queue of that name."""
    return session.scalar(select(Queue).where(Queue.name == name))


def seed(session: Session, root_password: str | None) -> bool:
    """Seed a fresh database; return False when one is already seeded.

    ``root_password`` of ``None`` leaves root without a hash: it exists and
    holds SuperUser, but cannot sign in until a password is set.
    """
    if users_exist(session):
        return False

    now = utcnow()

    for name in SYSTEM_GROUPS:
        session.add(Group(name=name, description="", kind=GroupKind.SYSTEM, created=now))
    for name in ROLE_GROUPS:
        session.add(Group(name=name, description="", kind=GroupKind.ROLE, created=now))

    session.add(
        User(
            name=NOBODY_USER_NAME,
            password_hash=None,
            email=None,
            real_name=NOBODY_USER_NAME,
            privileged=False,
            disabled=True,
            created=now,
            last_updated=now,
        )
    )
    root = User(
        name=ROOT_USER_NAME,
        password_hash=hash_password(root_password) if root_password else None,
        email=ROOT_USER_EMAIL,
        real_name="",
        privileged=True,
        disabled=False,
        created=now,
        last_updated=now,
    )
    session.add(root)

    session.add(
        Queue(
            name=DEFAULT_QUEUE_NAME,
            description=DEFAULT_QUEUE_NAME,
            subject_tag=None,
            correspond_address="",
            comment_address="",
            disabled=False,
            created=now,
            last_updated=now,
        )
    )
    session.flush()

    session.add(
        Right(
            principal_kind=PrincipalKind.USER,
            principal_id=root.id,
            right_name="SuperUser",
            object_kind=ObjectKind.SYSTEM,
            object_id=0,
            created=now,
            creator_id=root.id,
        )
    )
    session.commit()
    log.info("seeded", extra={"queue": DEFAULT_QUEUE_NAME, "root": ROOT_USER_NAME})
    return True
