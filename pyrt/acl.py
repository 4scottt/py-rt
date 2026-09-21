"""The ACL resolver (plan §10, the rewrite's one piece of real design).

Per request, once: the effective principal set of the current user. Per
right, once: one indexed query on ``rights``. A page that checks six rights
costs the set query plus six memoised lookups, against RT's per-widget
reloads.

Nothing here reads the request body or a template; the web layer wraps it in
:func:`require_right`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Annotated, Any, Final

from fastapi import Depends, Request
from sqlalchemy import ColumnElement, Select, or_, select, tuple_
from sqlalchemy.orm import Session

from pyrt.db.models import (
    SYSTEM_GROUP_EVERYONE,
    SYSTEM_GROUP_PRIVILEGED,
    SYSTEM_GROUP_UNPRIVILEGED,
    SYSTEM_GROUPS,
    Group,
    GroupKind,
    GroupMember,
    ObjectKind,
    PrincipalKind,
    Right,
    Ticket,
    TicketWatcher,
    User,
    WatcherRole,
)

#: One principal: ``("user", id)`` or ``("group", id)``. A system group and a
#: role are groups, so a grant needs no third kind.
Principal = tuple[str, int]

#: The effective principal set of a user, hashable so it keys the memo.
Principals = frozenset[Principal]

#: Nobody is nobody: an anonymous or disabled user holds no principal at all,
#: not even Everyone, so no grant can reach them.
NO_PRINCIPALS: Final[Principals] = frozenset()

SUPER_USER: Final = "SuperUser"

#: The role groups a ticket in hand can add.
ROLE_OWNER: Final = "Owner"
ROLE_REQUESTOR: Final = "Requestor"


def _principals_query(user_id: int) -> Select[tuple[int, str, str]]:
    """The one query behind the set: the system groups and the user's own.

    A left join keeps it single: every system group comes back whatever the
    membership rows say, and a user-defined group comes back only when the
    user is a member of it and it is enabled.
    """
    return (
        select(Group.id, Group.kind, Group.name)
        .outerjoin(
            GroupMember,
            (GroupMember.group_id == Group.id) & (GroupMember.user_id == user_id),
        )
        .where(
            or_(
                (Group.kind == GroupKind.SYSTEM) & Group.name.in_(SYSTEM_GROUPS),
                (Group.kind == GroupKind.USER_DEFINED)
                & (Group.disabled.is_(False))
                & (GroupMember.user_id.is_not(None)),
            )
        )
    )


def principals(db: Session, user: User | None, request: Request | None = None) -> Principals:
    """The effective principal set of ``user``, memoised on the request.

    The user itself, the enabled user-defined groups it is a member of,
    ``Everyone``, and ``Privileged`` or ``Unprivileged`` by its flag. A
    disabled user, and an anonymous one, hold nothing.
    """
    if user is None or user.disabled:
        return NO_PRINCIPALS

    cached = _request_cache(request, "principals")
    if cached is not None and user.id in cached:
        found: Principals = cached[user.id]
        return found

    found = _compute_principals(db, user)
    if cached is not None:
        cached[user.id] = found
    return found


def _compute_principals(db: Session, user: User) -> Principals:
    wanted_system = SYSTEM_GROUP_PRIVILEGED if user.privileged else SYSTEM_GROUP_UNPRIVILEGED
    items: set[Principal] = {(PrincipalKind.USER.value, user.id)}
    for group_id, kind, name in db.execute(_principals_query(user.id)).all():
        if kind == GroupKind.SYSTEM and name not in (SYSTEM_GROUP_EVERYONE, wanted_system):
            continue
        items.add((PrincipalKind.GROUP.value, group_id))
    return frozenset(items)


def ticket_principals(
    db: Session, user: User | None, ticket: Ticket, request: Request | None = None
) -> Principals:
    """:func:`principals` plus the roles ``user`` holds on ``ticket``.

    ``Owner`` when it owns the ticket, ``Requestor`` when it is a requestor
    watcher. Cc and AdminCc are grantable but no journey assigns them.
    """
    base = principals(db, user, request)
    if not base or user is None:
        return base

    wanted: list[str] = []
    if ticket.owner_id == user.id:
        wanted.append(ROLE_OWNER)
    is_requestor = db.scalar(
        select(TicketWatcher.user_id).where(
            TicketWatcher.ticket_id == ticket.id,
            TicketWatcher.user_id == user.id,
            TicketWatcher.role == WatcherRole.REQUESTOR,
        )
    )
    if is_requestor is not None:
        wanted.append(ROLE_REQUESTOR)
    if not wanted:
        return base

    role_ids = db.scalars(
        select(Group.id).where(Group.kind == GroupKind.ROLE, Group.name.in_(wanted))
    ).all()
    return base | {(PrincipalKind.GROUP.value, role_id) for role_id in role_ids}


def has_right(
    db: Session,
    principals: Principals,
    right: str,
    queue_id: int | None = None,
    request: Request | None = None,
) -> bool:
    """Whether the set holds ``right`` on that queue (or globally).

    One indexed query: the principal is in the set, the right is the one
    asked for or ``SuperUser``, and the grant is global or on this queue. The
    answer is memoised on the request per (set, right, queue).
    """
    if not principals:
        return False

    key = (principals, right, queue_id)
    cached = _request_cache(request, "rights")
    if cached is not None and key in cached:
        answer: bool = cached[key]
        return answer

    answer = _query_right(db, principals, right, queue_id)
    if cached is not None:
        cached[key] = answer
    return answer


def _query_right(db: Session, principals: Principals, right: str, queue_id: int | None) -> bool:
    names = (right,) if right == SUPER_USER else (right, SUPER_USER)
    scope: ColumnElement[bool] = Right.object_kind == ObjectKind.SYSTEM
    if queue_id is not None:
        scope = or_(scope, (Right.object_kind == ObjectKind.QUEUE) & (Right.object_id == queue_id))
    found = db.scalar(
        select(Right.id)
        .where(
            tuple_(Right.principal_kind, Right.principal_id).in_(sorted(principals)),
            Right.right_name.in_(names),
            scope,
        )
        .limit(1)
    )
    return found is not None


def queues_with_right(
    db: Session,
    principals: Principals,
    right: str,
    queue_ids: Iterable[int],
    request: Request | None = None,
) -> set[int]:
    """The subset of ``queue_ids`` the set holds ``right`` on.

    A convenience over :func:`has_right` for a list page; each queue costs one
    memoised lookup, and a global grant answers them all from the same row.
    """
    return {
        queue_id
        for queue_id in set(queue_ids)
        if has_right(db, principals, right, queue_id, request)
    }


class Forbidden(Exception):
    """A refused action. The web layer renders it as the 403 page (FP R08)."""

    def __init__(self, right: str, queue_id: int | None = None) -> None:
        super().__init__(f"{right} is required")
        self.right = right
        self.queue_id = queue_id


def _request_cache(request: Request | None, name: str) -> dict[Any, Any] | None:
    """The per-request memo of that name, or None when there is no request."""
    if request is None:
        return None
    store: dict[str, dict[Any, Any]] | None = getattr(request.state, "acl_cache", None)
    if store is None:
        store = {}
        request.state.acl_cache = store
    bucket = store.get(name)
    if bucket is None:
        bucket = {}
        store[name] = bucket
    return bucket


def request_principals(request: Request) -> Principals:
    """The current request's principal set (the middleware put the user there).

    A FastAPI dependency: ``principals: Annotated[Principals,
    Depends(request_principals)]``.
    """
    user: User | None = getattr(request.state, "user", None)
    db: Session = request.state.db
    return principals(db, user, request)


def require_right(right: str, queue_id: int | None = None) -> Callable[..., None]:
    """A dependency that raises :class:`Forbidden` unless the right is held.

    ``@router.get("/admin/users", dependencies=[Depends(require_right("AdminUsers"))])``
    for a fixed object; a route that learns the queue from the path calls
    :func:`has_right` itself with the queue in hand.
    """

    def dependency(
        request: Request,
        held: Annotated[Principals, Depends(request_principals)],
    ) -> None:
        if not has_right(request.state.db, held, right, queue_id, request):
            raise Forbidden(right, queue_id)

    return dependency
