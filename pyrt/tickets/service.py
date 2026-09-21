"""The ticket pages' queries and writes (plan §8, §11, §12; FP T01-T04).

Nothing here reads a form or renders a template: the router hands in the
fields it parsed and gets back rows, a refusal or a ticket. The page is
built to the query budget of plan §12: the principal set, the ticket with
its queue and owner joined, the watchers with their users, the custom
fields (M4's) and the history with its messages.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Final

from markupsafe import Markup
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pyrt.config import Settings
from pyrt.db.models import (
    NO_SUBJECT,
    NOBODY_USER_NAME,
    Message,
    Queue,
    Ticket,
    TicketWatcher,
    Transaction,
    TransactionType,
    User,
    WatcherRole,
    utcnow,
)
from pyrt.tickets import hooks, transactions
from pyrt.tickets.render import render_body
from pyrt.users.service import looks_like_email, privileged_users

#: The rights of plan §8's vocabulary these pages ask about.
CREATE_TICKET: Final = "CreateTicket"
SHOW_TICKET: Final = "ShowTicket"
COMMENT_ON_TICKET: Final = "CommentOnTicket"

#: The statuses a ticket may be created in (plan §10's lifecycle: a ticket
#: is born active, and resolving one is a transition, never a creation).
NEW_TICKET_STATUSES: Final[tuple[str, ...]] = ("new", "open", "stalled")

#: The refusals the create form comes back with, its values kept.
QUEUE_REQUIRED: Final = "Pick a queue to create the ticket in"
QUEUE_DISABLED: Final = "That queue is disabled"
BAD_STATUS: Final = "That is not a status a new ticket can have"
BAD_OWNER: Final = "That user cannot own a ticket"
BAD_REQUESTOR: Final = "That does not look like an email address"

#: What a date reads as when it has not happened yet.
NOT_SET: Final = "Not set"


@dataclass(slots=True)
class TicketForm:
    """What the create form carries, already stripped.

    The attribute names are the form's control names (plan §8: ``Queue``,
    ``Status``, ``Owner``, ``Requestors``, ``Subject``, ``Content``).
    """

    queue: int = 0
    status: str = "new"
    owner: int = 0
    requestors: str = ""
    subject: str = ""
    content: str = ""


@dataclass(frozen=True, slots=True)
class TicketView:
    """One ticket with the two rows its page names, from one query."""

    ticket: Ticket
    queue: Queue
    owner: User


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """One transaction as the history shows it (FP T04)."""

    id: int
    type: str
    description: str
    creator: str
    created: dt.datetime
    html: Markup
    hidden: bool = False


# --- reads -----------------------------------------------------------------


def nobody(db: Session) -> User | None:
    """The unowned owner. Every ticket has one, so it is never NULL."""
    return db.scalar(select(User).where(User.name == NOBODY_USER_NAME))


def owner_choices(db: Session) -> list[User]:
    """The ``Owner`` select: Nobody first, then the privileged users.

    Plan §8: "a privileged user appears in the owner select". The narrower
    rule of FP T10 (``OwnTicket`` on the queue) is the update package's.
    """
    unowned = nobody(db)
    choices = [] if unowned is None else [unowned]
    return choices + [user for user in privileged_users(db) if user.name != NOBODY_USER_NAME]


def load_ticket(db: Session, ticket_id: int) -> TicketView | None:
    """The ticket with its queue and its owner, in one query, or None.

    None is the router's 404 (FP T11). A ticket in status ``deleted`` loads
    like any other: it is dropped from the lists, not from the world.
    """
    row = (
        db.execute(
            select(Ticket, Queue, User)
            .join(Queue, Queue.id == Ticket.queue_id)
            .join(User, User.id == Ticket.owner_id)
            .where(Ticket.id == ticket_id)
        )
        .tuples()
        .first()
    )
    if row is None:
        return None
    ticket, queue, owner = row
    return TicketView(ticket=ticket, queue=queue, owner=owner)


def requestors(db: Session, ticket_id: int) -> list[User]:
    """The requestor watchers with their users, in one query."""
    return list(
        db.scalars(
            select(User)
            .join(TicketWatcher, TicketWatcher.user_id == User.id)
            .where(
                TicketWatcher.ticket_id == ticket_id,
                TicketWatcher.role == WatcherRole.REQUESTOR,
            )
            .order_by(User.name)
        ).all()
    )


def history(db: Session, ticket_id: int, *, show_comments: bool = True) -> list[HistoryEntry]:
    """The transactions oldest first, with their messages, in one query.

    A ``Comment``'s body is the staff's aside: it is rendered only for a
    viewer who holds ``CommentOnTicket`` on the queue (the rule FP T06
    tests from the other side). The row itself stays, so the history is
    still an account of what happened.
    """
    rows = (
        db.execute(
            select(Transaction, User.name, Message.body)
            .join(User, User.id == Transaction.creator_id)
            .outerjoin(Message, Message.transaction_id == Transaction.id)
            .where(Transaction.ticket_id == ticket_id)
            .order_by(Transaction.id)
        )
        .tuples()
        .all()
    )
    entries = []
    for transaction, creator, body in rows:
        hidden = transaction.type == TransactionType.COMMENT and not show_comments
        entries.append(
            HistoryEntry(
                id=transaction.id,
                type=transaction.type,
                description=transactions.describe(
                    transaction.type,
                    transaction.field,
                    transaction.old_value,
                    transaction.new_value,
                ),
                creator=creator,
                created=transaction.created,
                html=Markup("") if hidden or body is None else render_body(body),
                hidden=hidden and body is not None,
            )
        )
    return entries


def user_by_email(db: Session, address: str) -> User | None:
    """The user with that address, compared without case (FP T01)."""
    return db.scalars(
        select(User).where(func.lower(User.email) == address.lower()).order_by(User.id).limit(1)
    ).first()


# --- writes ----------------------------------------------------------------


def refusal(db: Session, form: TicketForm, queue: Queue | None) -> str:
    """Why this create form cannot be submitted, or ``""`` when it can.

    The gate (``CreateTicket`` on the queue) is the router's: a right the
    user does not hold is the 403 page, not a message on a form.
    """
    if queue is None:
        return QUEUE_REQUIRED
    if queue.disabled:
        return QUEUE_DISABLED
    if form.status not in NEW_TICKET_STATUSES:
        return BAD_STATUS
    if form.requestors and not looks_like_email(form.requestors):
        return BAD_REQUESTOR
    if not _may_own(db, form.owner):
        return BAD_OWNER
    return ""


def _may_own(db: Session, owner_id: int) -> bool:
    """Whether that id is Nobody or an enabled privileged user."""
    return any(candidate.id == owner_id for candidate in owner_choices(db))


def requestor_user(db: Session, address: str, actor: User) -> User:
    """The requestor of a new ticket: the address's user, or the actor.

    An address nobody holds becomes an unprivileged user with the address
    as its name (FP T01, and plan §10's rule for the mail gateway), so the
    person who wrote in is a principal the ACL can reach.
    """
    address = address.strip()
    if not address:
        return actor
    found = user_by_email(db, address)
    if found is not None:
        return found
    user = User(
        name=address,
        password_hash=None,
        email=address,
        real_name="",
        privileged=False,
        disabled=False,
    )
    db.add(user)
    db.flush()
    return user


def create_ticket(
    db: Session,
    settings: Settings,
    actor: User,
    queue: Queue,
    form: TicketForm,
) -> Ticket:
    """FP T01, T02: the ticket, its requestor and its ``Create`` transaction.

    One unit of work: the requestor (looked up or created), the ticket, the
    watcher row and the transaction with the content as its message. The
    ``created`` hook fires after the commit, on rows already written.
    """
    now = utcnow()
    requestor = requestor_user(db, form.requestors, actor)
    ticket = Ticket(
        queue_id=queue.id,
        owner_id=form.owner,
        subject=form.subject or NO_SUBJECT,
        status=form.status,
        priority=0,
        created=now,
        started=None,
        resolved=None,
        last_updated=now,
        creator_id=actor.id,
        last_updated_by=actor.id,
    )
    db.add(ticket)
    db.flush()
    db.add(TicketWatcher(ticket_id=ticket.id, user_id=requestor.id, role=WatcherRole.REQUESTOR))
    transaction = transactions.record(
        db, ticket, TransactionType.CREATE, actor, body=form.content or None
    )
    # FP T02: Created and LastUpdated are the same moment on a new ticket,
    # and so is the transaction that made it.
    transaction.created = now
    ticket.last_updated = now
    db.commit()
    hooks.fire(hooks.CREATED, db, settings, ticket, transaction, actor)
    return ticket
