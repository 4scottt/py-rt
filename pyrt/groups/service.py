"""The queries and writes behind the group admin pages (plan §8, FP U05-U08).

A *user-defined group* is the only kind a person may make or edit: the three
system groups and the four roles are rows of their own kind so ``rights`` can
name them, and their membership is a rule, never a row (plan §12). So every
query here narrows to ``GroupKind.USER_DEFINED`` — except the name check,
which looks at every group, because two rows may not share a name.

Nothing here touches the request or a template. :func:`user_defined_groups`
is the one function the rest of the app borrows: the Group Rights pages list
the same groups this package's Select list shows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pyrt.db.models import NOBODY_USER_NAME, Group, GroupKind, GroupMember, User, utcnow

#: The messages the pages show. "user-defined group" is plan §8's word, so
#: the refusals say "group" and mean this kind.
NAME_REQUIRED: Final = "A group needs a name"
NAME_TAKEN: Final = "A group with that name already exists"
CREATED: Final = "Group created"
UPDATED: Final = "Group updated"
MEMBER_ADDED: Final = "Member added"
MEMBER_REMOVED: Final = "Member removed"
ALREADY_MEMBER: Final = "Already a member"

#: ``?msg=`` after a redirect, so a POST hands its outcome on as a code.
MESSAGES: Final[dict[str, str]] = {
    "created": CREATED,
    "saved": UPDATED,
    "added": MEMBER_ADDED,
    "removed": MEMBER_REMOVED,
    "already": ALREADY_MEMBER,
}

#: The messages that read as a success; "Already a member" is a no-op, not one.
OK_MESSAGES: Final[frozenset[str]] = frozenset({CREATED, UPDATED, MEMBER_ADDED, MEMBER_REMOVED})


@dataclass(slots=True)
class GroupForm:
    """What the form said, kept so a refused POST re-renders with the values."""

    name: str = ""
    description: str = ""
    enabled: bool = True

    @classmethod
    def of(cls, group: Group) -> GroupForm:
        """The form as a stored group fills it."""
        return cls(
            name=group.name,
            description=group.description,
            enabled=not group.disabled,
        )

    def cleaned(self) -> GroupForm:
        """The same values with the text fields stripped."""
        return GroupForm(
            name=self.name.strip(),
            description=self.description.strip(),
            enabled=self.enabled,
        )


def user_defined_groups(db: Session, *, include_disabled: bool = False) -> list[Group]:
    """The user-defined groups, ordered by name (FP U05).

    Never the system groups or the roles: they are not a person's to list,
    create or edit. A disabled group is hidden until the page asks for it.
    """
    statement = select(Group).where(Group.kind == GroupKind.USER_DEFINED)
    if not include_disabled:
        statement = statement.where(Group.disabled.is_(False))
    return list(db.scalars(statement.order_by(Group.name)).all())


def editable_group(db: Session, group_id: int) -> Group | None:
    """The group that page may modify, or None for an unknown id or a kind
    a person does not own (a system group, a role)."""
    group = db.get(Group, group_id)
    if group is None or group.kind != GroupKind.USER_DEFINED:
        return None
    return group


def name_is_free(db: Session, name: str, *, exclude_id: int | None = None) -> bool:
    """Whether ``name`` is unused by *any* group, compared without case.

    Every kind counts: ``groups.name`` is unique, and a user-defined group
    called Everyone would shadow the system one in every list that names it.
    """
    statement = select(Group.id).where(func.lower(Group.name) == name.lower())
    if exclude_id is not None:
        statement = statement.where(Group.id != exclude_id)
    return db.scalar(statement.limit(1)) is None


def refusal(db: Session, form: GroupForm, *, group: Group | None = None) -> str:
    """Why this form cannot be saved, or ``""`` when it can."""
    if not form.name:
        return NAME_REQUIRED
    if not name_is_free(db, form.name, exclude_id=group.id if group else None):
        return NAME_TAKEN
    return ""


def create_group(db: Session, form: GroupForm) -> Group:
    """Write the new group. Its kind is user-defined; no other kind is made."""
    group = Group(
        name=form.name,
        description=form.description,
        kind=GroupKind.USER_DEFINED,
        disabled=not form.enabled,
        created=utcnow(),
    )
    db.add(group)
    db.commit()
    return group


def save_group(db: Session, group: Group, form: GroupForm) -> Group:
    """Apply the form to ``group``.

    Unchecking Enabled sets ``disabled``: the grants to the group stay, and
    the resolver stops counting them (FP U08, :mod:`pyrt.acl`).
    """
    group.name = form.name
    group.description = form.description
    group.disabled = not form.enabled
    db.commit()
    return group


def members(db: Session, group: Group) -> list[User]:
    """The users in ``group``, ordered by name (FP U06)."""
    statement = (
        select(User)
        .join(GroupMember, GroupMember.user_id == User.id)
        .where(GroupMember.group_id == group.id)
        .order_by(User.name)
    )
    return list(db.scalars(statement).all())


def candidates(db: Session, group: Group) -> list[User]:
    """The users the Member select offers: enabled, not ``Nobody``, not in it.

    RT's autocomplete becomes a select (plan §7), so the options are the
    whole candidate list, ordered by name.
    """
    already = select(GroupMember.user_id).where(GroupMember.group_id == group.id)
    statement = (
        select(User)
        .where(
            User.name != NOBODY_USER_NAME,
            User.disabled.is_(False),
            User.id.not_in(already),
        )
        .order_by(User.name)
    )
    return list(db.scalars(statement).all())


def addable_user(db: Session, user_id: int) -> User | None:
    """The user the Member select could have offered, or None.

    ``Nobody`` is the unowned owner, not a person, and a disabled user is out
    of every list (plan §10), so neither may be added by a stale form.
    """
    user = db.get(User, user_id)
    if user is None or user.disabled or user.name == NOBODY_USER_NAME:
        return None
    return user


def is_member(db: Session, group: Group, user: User) -> bool:
    """Whether ``user`` is in ``group`` (the pair is the primary key)."""
    return db.get(GroupMember, (group.id, user.id)) is not None


def add_member(db: Session, group: Group, user: User) -> bool:
    """Put ``user`` in ``group``; False when it was already in it.

    A user is in a group at most once: the pair is the key, so a second add
    writes nothing (FP U06).
    """
    if is_member(db, group, user):
        return False
    db.add(GroupMember(group_id=group.id, user_id=user.id))
    db.commit()
    return True


def remove_member(db: Session, group: Group, user: User) -> bool:
    """Take ``user`` out of ``group``; False when it was not in it."""
    row = db.get(GroupMember, (group.id, user.id))
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True
