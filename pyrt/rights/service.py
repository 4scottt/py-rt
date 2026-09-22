"""The rights pages' queries and writes (plan §8's rights paragraph, §12).

One shape serves the four pages of plan §8: a section per kind of principal,
a row per principal, a checkbox per right, one ``Save Changes``. The two
axes are the principal kind (``group`` or ``user``) and the object the
grants hang on (one queue, or ``system`` for the global pages).

Nothing here reads a request or renders a template: the router hands in the
control names the form ticked and gets back the rows that changed.

**The control name** is plan §8's verbatim: ``{principal}-{right}``, e.g.
``Everyone-CreateTicket`` or ``Technicians-OwnTicket``. A group named
``Level-2`` makes that name ambiguous to a reader
(``Level-2-OwnTicket``), so nothing here parses one: the page's own
principals and rights are re-derived from the database on the POST and
their names are *rendered* again (:meth:`PrincipalRow.control`) and looked
up in what was posted. Splitting on the last ``-`` would answer the same
(no right name contains one), but building the name leaves no guesswork and
no chance of a posted name reaching a principal that page never offered.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sqlalchemy import delete, select, tuple_
from sqlalchemy.orm import Session

from pyrt.db.models import (
    GLOBAL_ONLY_RIGHTS,
    RIGHTS,
    ROLE_GROUPS,
    SYSTEM_GROUPS,
    Group,
    GroupKind,
    ObjectKind,
    PrincipalKind,
    Right,
    utcnow,
)
from pyrt.users.service import privileged_users

#: The rights these pages ask about (plan §8's vocabulary table).
ADMIN_QUEUE: Final = "AdminQueue"
SUPER_USER: Final = "SuperUser"

#: A queue page offers the queue-scoped rights: every name in ``RIGHTS``
#: minus the global-only ones. The global pages offer them all.
QUEUE_RIGHTS: Final[tuple[str, ...]] = tuple(
    name for name in RIGHTS if name not in GLOBAL_ONLY_RIGHTS
)
ALL_RIGHTS: Final[tuple[str, ...]] = RIGHTS

#: The section titles, in the order plan §8 lists the principals.
SYSTEM_SECTION: Final = "System groups"
ROLES_SECTION: Final = "Roles"
USER_DEFINED_SECTION: Final = "User-defined groups"
PRIVILEGED_SECTION: Final = "Privileged users"

#: What an empty section says (this package's words; the plan fixes neither).
NO_USER_DEFINED: Final = "No user-defined groups yet"
NO_PRIVILEGED: Final = "No privileged users"

#: The one message these pages show, carried as ``?msg=saved``.
SAVED: Final = "Rights updated"
MESSAGES: Final[dict[str, str]] = {"saved": SAVED}


def message_for(msg: str) -> str:
    """The text ``?msg=`` stands for, or "" when it says nothing known."""
    return MESSAGES.get(msg, "")


@dataclass(frozen=True, slots=True)
class PrincipalRow:
    """One principal on a rights page and the rights it holds on that object."""

    kind: str
    id: int
    name: str
    checked: frozenset[str]

    def control(self, right: str) -> str:
        """The checkbox's ``name``, plan §8's ``{principal}-{right}``."""
        return f"{self.name}-{right}"

    def element_id(self, right: str) -> str:
        """The checkbox's ``id``: the name may hold spaces, an id may not."""
        return f"{self.kind}-{self.id}-{right}"


@dataclass(frozen=True, slots=True)
class Section:
    """One titlebox of a rights page: its heading and its principals."""

    title: str
    rows: tuple[PrincipalRow, ...]
    empty_message: str = ""


def object_of(queue_id: int | None) -> tuple[str, int]:
    """The ``(object_kind, object_id)`` a page's grants hang on.

    A queue page scopes to that queue; the global pages are ``system`` with
    object id 0, as plan §12's ``rights`` table spells it.
    """
    if queue_id is None:
        return ObjectKind.SYSTEM.value, 0
    return ObjectKind.QUEUE.value, queue_id


def rights_offered(queue_id: int | None) -> tuple[str, ...]:
    """The checkboxes a page carries: queue-scoped, or every right globally."""
    return ALL_RIGHTS if queue_id is None else QUEUE_RIGHTS


def group_sections(db: Session, queue_id: int | None) -> list[Section]:
    """The Group Rights sections: system groups, roles, user-defined groups.

    Two queries: the groups, and the grants on this object (plan §12's three
    for the page, with the principal set the shell already resolved).
    """
    object_kind, object_id = object_of(queue_id)
    held = _granted(db, PrincipalKind.GROUP.value, object_kind, object_id)
    by_kind = _groups(db)
    return [
        Section(SYSTEM_SECTION, _rows(_named(by_kind[GroupKind.SYSTEM], SYSTEM_GROUPS), held)),
        Section(ROLES_SECTION, _rows(_named(by_kind[GroupKind.ROLE], ROLE_GROUPS), held)),
        Section(
            USER_DEFINED_SECTION,
            _rows(by_kind[GroupKind.USER_DEFINED], held),
            NO_USER_DEFINED,
        ),
    ]


def user_sections(db: Session, queue_id: int | None) -> list[Section]:
    """The User Rights section: the enabled privileged users (plan §8).

    A user is privileged to be granted rights, so the list is
    :func:`pyrt.users.service.privileged_users`; disabling a user takes it
    off the page without touching a grant.
    """
    object_kind, object_id = object_of(queue_id)
    held = _granted(db, PrincipalKind.USER.value, object_kind, object_id)
    rows = tuple(
        PrincipalRow(PrincipalKind.USER.value, user.id, user.name, held.get(user.id, frozenset()))
        for user in privileged_users(db)
    )
    return [Section(PRIVILEGED_SECTION, rows, NO_PRIVILEGED)]


def save_rights(
    db: Session,
    sections: list[Section],
    rights: tuple[str, ...],
    queue_id: int | None,
    posted: frozenset[str],
    *,
    principal_kind: str,
    actor_id: int | None,
) -> tuple[int, int]:
    """Make the ticked boxes the truth for this object; return (added, removed).

    The checked set *is* the desired set: a pair this page offers and the
    form did not tick is deleted, a ticked one that has no row is inserted.
    Only the pairs this page offered are touched, so a global grant survives
    a queue page's save and a right the page does not show survives both.
    """
    object_kind, object_id = object_of(queue_id)
    add: list[tuple[int, str]] = []
    remove: list[tuple[int, str]] = []
    for section in sections:
        for row in section.rows:
            for right in rights:
                wanted = row.control(right) in posted
                had = right in row.checked
                if wanted and not had:
                    add.append((row.id, right))
                elif had and not wanted:
                    remove.append((row.id, right))

    if remove:
        db.execute(
            delete(Right).where(
                Right.principal_kind == principal_kind,
                Right.object_kind == object_kind,
                Right.object_id == object_id,
                tuple_(Right.principal_id, Right.right_name).in_(sorted(remove)),
            )
        )
    now = utcnow()
    for principal_id, right_name in add:
        db.add(
            Right(
                principal_kind=principal_kind,
                principal_id=principal_id,
                right_name=right_name,
                object_kind=object_kind,
                object_id=object_id,
                created=now,
                creator_id=actor_id,
            )
        )
    if add or remove:
        db.commit()
    return len(add), len(remove)


def _granted(
    db: Session, principal_kind: str, object_kind: str, object_id: int
) -> dict[int, frozenset[str]]:
    """The rights each principal of that kind holds on that object, by id."""
    found: dict[int, set[str]] = {}
    rows = db.execute(
        select(Right.principal_id, Right.right_name).where(
            Right.principal_kind == principal_kind,
            Right.object_kind == object_kind,
            Right.object_id == object_id,
        )
    ).all()
    for principal_id, right_name in rows:
        found.setdefault(principal_id, set()).add(right_name)
    return {principal_id: frozenset(names) for principal_id, names in found.items()}


def _groups(db: Session) -> dict[str, list[Group]]:
    """Every group a rights page may show, by kind, user-defined ones by name.

    A disabled user-defined group is off the page: its grants stay in
    ``rights`` but reach nobody (FP U08), so offering its boxes would
    promise something the resolver does not keep.
    """
    by_kind: dict[str, list[Group]] = {
        GroupKind.SYSTEM.value: [],
        GroupKind.ROLE.value: [],
        GroupKind.USER_DEFINED.value: [],
    }
    rows = db.scalars(
        select(Group)
        .where(
            (Group.kind != GroupKind.USER_DEFINED) | (Group.disabled.is_(False)),
        )
        .order_by(Group.name)
    ).all()
    for group in rows:
        if group.kind in by_kind:
            by_kind[group.kind].append(group)
    return by_kind


def _named(groups: list[Group], order: tuple[str, ...]) -> list[Group]:
    """``groups`` in the plan's order, and only the names the plan names."""
    by_name = {group.name: group for group in groups}
    return [by_name[name] for name in order if name in by_name]


def _rows(groups: list[Group], held: dict[int, frozenset[str]]) -> tuple[PrincipalRow, ...]:
    return tuple(
        PrincipalRow(
            PrincipalKind.GROUP.value, group.id, group.name, held.get(group.id, frozenset())
        )
        for group in groups
    )


__all__ = [
    "ADMIN_QUEUE",
    "ALL_RIGHTS",
    "MESSAGES",
    "QUEUE_RIGHTS",
    "SAVED",
    "SUPER_USER",
    "PrincipalRow",
    "Section",
    "group_sections",
    "message_for",
    "object_of",
    "rights_offered",
    "save_rights",
    "user_sections",
]
