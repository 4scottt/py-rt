"""The simple search: the grammar and the one query behind it (plan §10, §12).

Two halves, both pure of the web. :func:`parse` turns the box's string into a
:class:`Query` — words, one status option and the lone-integer case — with no
database in hand. :func:`results` turns a :class:`Query` and a principal set
into the rows the table shows, with the ``ShowTicket`` filter in SQL rather
than per row: one ``IN`` list for the queues the user may see and an ``OR``
for the two ticket roles it may hold.

Case matters nowhere: every column is ``utf8mb4_unicode_ci``, so ``LIKE`` is
already case-insensitive and neither side needs ``LOWER()`` (which would cost
the index).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import ColumnElement, Row, Select, false, or_, select, tuple_
from sqlalchemy.orm import Session

from pyrt.acl import ROLE_OWNER, ROLE_REQUESTOR, SUPER_USER, Principals
from pyrt.db.models import (
    INACTIVE_STATUSES,
    STATUSES,
    Group,
    GroupKind,
    Message,
    ObjectKind,
    PrincipalKind,
    Queue,
    Right,
    Ticket,
    TicketWatcher,
    Transaction,
    User,
    WatcherRole,
)

#: The right the results are filtered by (plan §8's vocabulary, FP R06).
SHOW_TICKET: Final = "ShowTicket"

#: The one option word of plan §8: ``status:any`` lifts the default filter.
#: ``status:<name>`` for a real status narrows to it instead — a small bonus
#: the grammar gets for free, not something a goal's text depends on.
STATUS_KEY: Final = "status"
STATUS_ANY: Final = "any"

#: The default filter: a search hides what is already done with.
HIDDEN_STATUSES: Final[tuple[str, ...]] = INACTIVE_STATUSES

#: How many rows one results page shows. There is no paging in this
#: milestone: the fixture dataset is small and the journeys search for one
#: ticket, so a cap is the whole answer to a query that matches everything.
RESULT_LIMIT: Final = 50

#: One results row: Id, Subject, Status, Queue, Owner, Created (FP S04).
ResultRow = Row[tuple[int, str, str, str, str, Any]]


@dataclass(frozen=True, slots=True)
class Query:
    """A parsed search box (FP S01, S03).

    ``words`` are the terms that must *all* match; ``status`` is ``""`` for
    the default filter, ``"any"`` for none, or one of :data:`STATUSES`;
    ``ticket_id`` is set when the box held a bare ticket number, which the
    router turns into a redirect instead of a search.
    """

    words: tuple[str, ...] = ()
    status: str = ""
    ticket_id: int | None = None

    @property
    def is_empty(self) -> bool:
        """Whether there is nothing to search for (an empty box, or options alone)."""
        return not self.words and self.ticket_id is None


def parse(raw: str) -> Query:
    """Split the box on whitespace into words, options and maybe a ticket id.

    A token shaped ``key:value`` is an option: ``status:`` is the one key
    that is known, and any other key is ignored rather than refused (FP S03)
    — a search that says something the grammar has not learnt still searches
    for the words beside it. The remaining tokens are words, and a single
    word that is all digits is a ticket number (FP S01).
    """
    words: list[str] = []
    status = ""
    for token in raw.split():
        key, sep, value = token.partition(":")
        if sep and key and value:
            if key.casefold() == STATUS_KEY:
                status = _status_option(value) or status
            continue  # an unknown option is not an error, just not a word
        words.append(token)

    if len(words) == 1 and words[0].isdigit():
        number = int(words[0])
        if number > 0:
            return Query(status=status, ticket_id=number)
    return Query(words=tuple(words), status=status)


def _status_option(value: str) -> str:
    """``any``, a real status, or ``""`` when the value means nothing here."""
    wanted = value.casefold()
    if wanted == STATUS_ANY:
        return STATUS_ANY
    return wanted if wanted in STATUSES else ""


# --- what the user may see -------------------------------------------------


@dataclass(frozen=True, slots=True)
class Visibility:
    """Where ``ShowTicket`` reaches, read once (plan §10's "in SQL, not per row").

    Three principals hold it in three different scopes: the user's own set
    (its groups and the system groups), the ``Owner`` role, and the
    ``Requestor`` role. Each is either global (``everywhere``) or a set of
    queue ids, and the filter is the ``OR`` of the three.
    """

    everywhere: bool = False
    queues: frozenset[int] = frozenset()
    owner_everywhere: bool = False
    owner_queues: frozenset[int] = frozenset()
    requestor_everywhere: bool = False
    requestor_queues: frozenset[int] = frozenset()


def _grants_query(principals: Principals) -> Select[tuple[str, int, str, int, str]]:
    """Every ``ShowTicket`` (or ``SuperUser``) grant that could reach this user.

    One query for all three scopes: the grants to a principal in the set, and
    the grants to the ``Owner`` and ``Requestor`` role groups, which the set
    never holds (a role is added per ticket, and there is no ticket here).
    The left join names the role a grant was made to, or NULL when the grant
    went to a user or a user-defined group (the column is declared NOT NULL,
    so the typed ``Select`` says ``str`` where the outer join can hand back
    ``None``; :func:`visibility` treats anything that is not a role name as
    the base scope, which covers both).
    """
    is_role = (
        (Group.id == Right.principal_id)
        & (Right.principal_kind == PrincipalKind.GROUP.value)
        & (Group.kind == GroupKind.ROLE.value)
    )
    reaches: list[ColumnElement[bool]] = [Group.name.in_((ROLE_OWNER, ROLE_REQUESTOR))]
    if principals:
        reaches.append(tuple_(Right.principal_kind, Right.principal_id).in_(sorted(principals)))
    return (
        select(
            Right.principal_kind,
            Right.principal_id,
            Right.object_kind,
            Right.object_id,
            Group.name,
        )
        .outerjoin(Group, is_role)
        .where(Right.right_name.in_((SHOW_TICKET, SUPER_USER)), or_(*reaches))
    )


def visibility(db: Session, principals: Principals) -> Visibility:
    """Read the grants and sort them into the three scopes of :class:`Visibility`."""
    everywhere = {ROLE_OWNER: False, ROLE_REQUESTOR: False, "": False}
    queues: dict[str, set[int]] = {ROLE_OWNER: set(), ROLE_REQUESTOR: set(), "": set()}
    for _kind, _ident, object_kind, object_id, role in db.execute(_grants_query(principals)).all():
        scope = role if role in (ROLE_OWNER, ROLE_REQUESTOR) else ""
        if object_kind == ObjectKind.QUEUE:
            queues[scope].add(object_id)
        else:
            everywhere[scope] = True
    return Visibility(
        everywhere=everywhere[""],
        queues=frozenset(queues[""]),
        owner_everywhere=everywhere[ROLE_OWNER],
        owner_queues=frozenset(queues[ROLE_OWNER]),
        requestor_everywhere=everywhere[ROLE_REQUESTOR],
        requestor_queues=frozenset(queues[ROLE_REQUESTOR]),
    )


def _visible_clause(seen: Visibility, user_id: int) -> ColumnElement[bool] | None:
    """The ``ShowTicket`` filter as SQL, or None when nothing is filtered.

    None is a ``SuperUser`` or a global ``ShowTicket``: every ticket passes,
    and the query is the query. Otherwise the queues the user may see, plus
    its own tickets where the role that reaches them holds the right.
    """
    if seen.everywhere:
        return None

    clauses: list[ColumnElement[bool]] = []
    if seen.queues:
        clauses.append(Ticket.queue_id.in_(seen.queues))
    owned = _role_clause(seen.owner_everywhere, seen.owner_queues, Ticket.owner_id == user_id)
    if owned is not None:
        clauses.append(owned)
    asked = _role_clause(
        seen.requestor_everywhere,
        seen.requestor_queues,
        select(TicketWatcher.user_id)
        .where(
            TicketWatcher.ticket_id == Ticket.id,
            TicketWatcher.user_id == user_id,
            TicketWatcher.role == WatcherRole.REQUESTOR.value,
        )
        .exists(),
    )
    if asked is not None:
        clauses.append(asked)
    if not clauses:
        return false()
    return or_(*clauses)


def _role_clause(
    anywhere: bool, queues: frozenset[int], holds_role: ColumnElement[bool]
) -> ColumnElement[bool] | None:
    """``holds_role`` where that role was granted the right, or None."""
    if anywhere:
        return holds_role
    if queues:
        return Ticket.queue_id.in_(queues) & holds_role
    return None


# --- the results -----------------------------------------------------------


def _word_clause(word: str) -> ColumnElement[bool]:
    """One word: in the subject, or in any message body of the ticket (FP S01).

    ``contains(autoescape=True)`` keeps a ``%`` or a ``_`` the person typed a
    character rather than a wildcard.
    """
    in_body = (
        select(Message.transaction_id)
        .join(Transaction, Transaction.id == Message.transaction_id)
        .where(
            Transaction.ticket_id == Ticket.id,
            Message.body.contains(word, autoescape=True),
        )
        .exists()
    )
    return or_(Ticket.subject.contains(word, autoescape=True), in_body)


def _results_query(
    query: Query, seen: Visibility, user_id: int, limit: int
) -> Select[tuple[int, str, str, str, str, Any]]:
    """The results page in one statement: the columns, the filters, the order."""
    statement = (
        select(
            Ticket.id,
            Ticket.subject,
            Ticket.status,
            Queue.name.label("queue_name"),
            User.name.label("owner_name"),
            Ticket.created,
        )
        .join(Queue, Queue.id == Ticket.queue_id)
        .join(User, User.id == Ticket.owner_id)
    )
    for word in query.words:
        statement = statement.where(_word_clause(word))
    if query.status == "":
        statement = statement.where(Ticket.status.not_in(HIDDEN_STATUSES))
    elif query.status != STATUS_ANY:
        statement = statement.where(Ticket.status == query.status)
    visible = _visible_clause(seen, user_id)
    if visible is not None:
        statement = statement.where(visible)
    return statement.order_by(Ticket.created.desc(), Ticket.id.desc()).limit(limit)


def results(
    db: Session,
    principals: Principals,
    query: Query,
    user_id: int,
    limit: int = RESULT_LIMIT,
) -> list[ResultRow]:
    """The rows the table shows, newest first (FP S02, S04).

    Two queries: where ``ShowTicket`` reaches, and the page itself with its
    queue and owner joined. An empty query is neither (FP S03): a box with
    nothing in it lists nothing rather than everything.
    """
    if query.is_empty:
        return []
    seen = visibility(db, principals)
    return list(db.execute(_results_query(query, seen, user_id, limit)).all())
