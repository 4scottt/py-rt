"""The queries and writes behind the user admin pages (plan §8, FP U01-U03).

Nothing here touches the request or a template: a page hands in what the form
said and gets back rows, a message, or a user. The one rule the rest of the
app borrows is :func:`privileged_users` — the owner select of a ticket page is
the same list this package's User Rights page will show.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pyrt.auth import hash_password
from pyrt.db.models import NOBODY_USER_NAME, ROOT_USER_NAME, User, utcnow

#: An address is one ``@`` with no spaces around it. The plan asks for no
#: more: the mail goal proves an address by using it, not by matching it.
EMAIL_PATTERN: Final = re.compile(r"^[^@\s]+@[^@\s]+$")

#: The messages the pages show. The name clash is the one the plan names
#: verbatim; the others are this package's own words.
NAME_REQUIRED: Final = "A name is required"
NAME_TAKEN: Final = "A user with that name already exists"
BAD_EMAIL: Final = "That does not look like an email address"
ROOT_STAYS_ENABLED: Final = "The root user cannot be disabled"
CREATED: Final = "User created"
UPDATED: Final = "User updated"

#: ``?msg=`` on the modify page, so a redirect after POST carries no text.
MESSAGES: Final[dict[str, str]] = {"created": CREATED, "saved": UPDATED}


@dataclass(slots=True)
class UserForm:
    """What the form said, kept so a refused POST re-renders with the values."""

    name: str = ""
    email: str = ""
    real_name: str = ""
    privileged: bool = False
    enabled: bool = True

    @classmethod
    def of(cls, user: User) -> UserForm:
        """The form as a stored user fills it."""
        return cls(
            name=user.name,
            email=user.email or "",
            real_name=user.real_name,
            privileged=user.privileged,
            enabled=not user.disabled,
        )

    def cleaned(self) -> UserForm:
        """The same values with the text fields stripped."""
        return UserForm(
            name=self.name.strip(),
            email=self.email.strip(),
            real_name=self.real_name.strip(),
            privileged=self.privileged,
            enabled=self.enabled,
        )


def listed_users(db: Session, *, include_disabled: bool = False) -> list[User]:
    """The list page's rows: every user but ``Nobody``, ordered by name.

    ``Nobody`` is the unowned owner, not a person, and never lists (plan §10).
    A disabled user is hidden until the page is asked for it (FP U01).
    """
    statement = select(User).where(User.name != NOBODY_USER_NAME)
    if not include_disabled:
        statement = statement.where(User.disabled.is_(False))
    return list(db.scalars(statement.order_by(User.name)).all())


def privileged_users(db: Session) -> list[User]:
    """The enabled privileged users, ordered by name.

    Plan §8: "a privileged user appears in the owner select and on User
    Rights". Disabling a user drops it from both without deleting a row
    (FP U03), so every list of candidates comes through here.
    """
    statement = select(User).where(
        User.name != NOBODY_USER_NAME,
        User.privileged.is_(True),
        User.disabled.is_(False),
    )
    return list(db.scalars(statement.order_by(User.name)).all())


def editable_user(db: Session, user_id: int) -> User | None:
    """The user that page may modify, or None for an unknown id or ``Nobody``."""
    user = db.get(User, user_id)
    if user is None or user.name == NOBODY_USER_NAME:
        return None
    return user


def looks_like_email(email: str) -> bool:
    """Whether ``email`` could be an address: one ``@``, no spaces."""
    return bool(EMAIL_PATTERN.match(email))


def name_is_free(db: Session, name: str, *, exclude_id: int | None = None) -> bool:
    """Whether ``name`` is unused, compared without case (FP U02)."""
    statement = select(User.id).where(func.lower(User.name) == name.lower())
    if exclude_id is not None:
        statement = statement.where(User.id != exclude_id)
    return db.scalar(statement.limit(1)) is None


def refusal(db: Session, form: UserForm, *, user: User | None = None) -> str:
    """Why this form cannot be saved, or ``""`` when it can.

    The name is required and unique; the email is optional but must look
    like an address when given; ``root`` cannot be disabled (plan §10: a
    user is disabled, never deleted, and the one that holds SuperUser from
    the seed stays able to sign in).
    """
    if not form.name:
        return NAME_REQUIRED
    if not name_is_free(db, form.name, exclude_id=user.id if user else None):
        return NAME_TAKEN
    if form.email and not looks_like_email(form.email):
        return BAD_EMAIL
    if user is not None and user.name == ROOT_USER_NAME and not form.enabled:
        return ROOT_STAYS_ENABLED
    return ""


def create_user(db: Session, form: UserForm, password: str = "") -> User:
    """Write the new user. An empty password leaves it unable to sign in."""
    now = utcnow()
    user = User(
        name=form.name,
        password_hash=hash_password(password) if password else None,
        email=form.email or None,
        real_name=form.real_name,
        privileged=form.privileged,
        disabled=not form.enabled,
        created=now,
        last_updated=now,
    )
    db.add(user)
    db.commit()
    return user


def save_user(db: Session, user: User, form: UserForm, password: str = "") -> User:
    """Apply the form to ``user``; the password changes only when given."""
    user.name = form.name
    user.email = form.email or None
    user.real_name = form.real_name
    user.privileged = form.privileged
    user.disabled = not form.enabled
    if password:
        user.password_hash = hash_password(password)
    user.last_updated = utcnow()
    db.commit()
    return user
