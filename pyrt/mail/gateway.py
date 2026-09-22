"""Mail in: one message becomes a ticket or a reply (FP M01-M04, R09).

The counterpart of :mod:`pyrt.mail.notify`. A message arrives on the
gateway's standard input, `pyrt mailgate` reads it and hands it here with
the queue the platform named; what comes back is a :class:`Result` the
command turns into the protocol's ``ok`` / ``not ok`` lines.

The shape of the decision (plan §10):

1. the **action** must be one this gateway speaks (``correspond``, and
   ``comment`` for the staff address of a queue);
2. the **queue** is the one named on the command line -- never the ``To``
   header, which in the platform's own workload is a single fixed address
   for every queue there is;
3. the **sender** is the ``From`` address's user, created unprivileged
   when it is new, so the person who wrote in is a principal the ACL can
   reach (they hold ``Everyone`` and ``Unprivileged``, and nothing else);
4. the **subject tag** decides what happens: ``[<name> #<id>]`` whose name
   is ours and whose id names a ticket is a reply onto that ticket;
   anything else opens a new one, our tag stripped from the subject it is
   stored under;
5. the **right** is asked of the sender, not of an administrator:
   ``CreateTicket`` on the queue to open a ticket, ``ReplyToTicket`` on the
   ticket's queue to answer one -- which is how the mail goal's first
   subtask (R09) reads as a grant to ``Everyone``, and how a stranger's
   mail is refused where no such grant exists.

Nothing here prints, and nothing here reads the environment: the command
owns the protocol and the session, this module owns the decision. A
refusal rolls the session back, so a message the gateway would not accept
leaves nothing behind -- not even the user its ``From`` would have made.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pyrt.acl import has_right, principals, ticket_principals
from pyrt.config import Settings
from pyrt.db.models import Queue, Ticket, TransactionType, User, utcnow
from pyrt.mail.notify import TAG_RE
from pyrt.mail.parse import Parsed, ParseError, parse_message
from pyrt.queues.service import subject_tag_for
from pyrt.tickets import hooks, lifecycle, service, transactions
from pyrt.tickets.update import STATUS_FIELD

log = logging.getLogger(__name__)

#: The two actions the gateway speaks. ``correspond`` is what the platform's
#: exercise body sends and what a queue's reply address would receive;
#: ``comment`` is the staff aside, recorded as a ``Comment`` and mailed to
#: nobody (T06).
CORRESPOND: Final = "correspond"
COMMENT: Final = "comment"
ACTIONS: Final[tuple[str, ...]] = (CORRESPOND, COMMENT)

#: The rights of plan §8's vocabulary this gateway asks about.
CREATE_TICKET: Final = "CreateTicket"
REPLY_TO_TICKET: Final = "ReplyToTicket"
COMMENT_ON_TICKET: Final = "CommentOnTicket"

#: A ticket is born ``new`` from a message, and the first correspondence on
#: a ``new`` ticket opens it -- the same rule the update page follows (T05).
CONTENT_TYPE: Final = "text/plain"

#: ``tickets.subject`` is a ``VARCHAR(200)``: a longer one is stored short
#: rather than refused.
SUBJECT_LIMIT: Final = 200

#: ``users.name`` and ``users.email`` are ``VARCHAR(200)`` and ``(120)``.
NAME_LIMIT: Final = 200
EMAIL_LIMIT: Final = 120
REAL_NAME_LIMIT: Final = 120

#: A tag id larger than the ``tickets.id`` column could hold names no ticket
#: and is read as no tag at all, not as a database error.
MAX_TICKET_ID: Final = 2**31 - 1

#: The refusal a database nobody has seeded comes back with.
NO_NOBODY: Final = "the database has no Nobody user: seed it first"


def unknown_action(action: str) -> str:
    """``not ok`` for an action this gateway does not speak."""
    return f"unknown action: {action}"


def no_such_queue(name: str) -> str:
    """``not ok`` for a queue that is not there.

    A **disabled** queue answers with the same words: a stranger writing in
    learns that mail to that address is not filed, and nothing about how
    this side is configured.
    """
    return f"no such queue {name}"


def permission_denied(right: str) -> str:
    """``not ok`` for a right the sender does not hold (M04)."""
    return f"permission denied: {right}"


@dataclass(frozen=True, slots=True)
class Result:
    """What the gateway did: the command prints it and exits on it.

    ``ok`` with a ``ticket_id`` is the protocol's two lines; ``ok`` false
    carries the reason that follows ``not ok:``.
    """

    ok: bool
    ticket_id: int | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class _Threading:
    """What the subject tag decided: the ticket to reply to, and the subject.

    ``ticket`` is None when the message opens a new one; ``subject`` is the
    subject as it will be stored, our own tag taken out of it.
    """

    ticket: Ticket | None
    subject: str


def deliver(
    db: Session,
    settings: Settings,
    *,
    queue_name: str,
    action: str,
    raw: bytes,
) -> Result:
    """File one message. Never raises: every failure is a :class:`Result`.

    The unexpected ones are logged with their traceback and come back as
    ``<type>: <message>``, because the caller's standard output is a
    protocol another program reads, not a place for a stack trace.
    """
    try:
        return _deliver(db, settings, queue_name=queue_name, action=action, raw=raw)
    except ParseError as exc:
        return _refuse(db, str(exc))
    except Exception as exc:
        log.exception("gateway failed", extra={"queue": queue_name, "action": action})
        return _refuse(db, f"{type(exc).__name__}: {exc}")


def _deliver(
    db: Session,
    settings: Settings,
    *,
    queue_name: str,
    action: str,
    raw: bytes,
) -> Result:
    wanted = (action or "").strip().lower()
    if wanted not in ACTIONS:
        return _refuse(db, unknown_action(action))

    queue = queue_by_name(db, queue_name)
    if queue is None or queue.disabled:
        return _refuse(db, no_such_queue(queue_name))

    parsed = parse_message(raw)
    sender = sender_user(db, parsed)
    threading = _threading(db, settings, queue, parsed.subject)

    if threading.ticket is not None:
        return _reply(db, settings, threading.ticket, sender, parsed, wanted)
    return _create(db, settings, queue, sender, parsed, threading.subject)


# --- the sender ------------------------------------------------------------


def queue_by_name(db: Session, name: str) -> Queue | None:
    """One queue by name, compared without case (a mail header has no ids)."""
    if not name.strip():
        return None
    return db.scalars(
        select(Queue).where(func.lower(Queue.name) == name.strip().lower()).limit(1)
    ).first()


def sender_user(db: Session, parsed: Parsed) -> User:
    """The message's author as a user row, created unprivileged when new.

    Not :func:`pyrt.tickets.service.requestor_user`: that one is the create
    *form*'s and takes the signed-in actor to fall back on, which a message
    on standard input does not have -- the sender is the actor here -- and
    it has no display name to record. The row it writes is otherwise the
    same one: unprivileged, the address as the name, no password, so the
    account cannot be signed into and holds ``Everyone`` and
    ``Unprivileged`` and nothing more.
    """
    found = service.user_by_email(db, parsed.from_address)
    if found is not None:
        return found
    user = User(
        name=parsed.from_address[:NAME_LIMIT],
        password_hash=None,
        email=parsed.from_address[:EMAIL_LIMIT],
        real_name=parsed.from_name[:REAL_NAME_LIMIT],
        privileged=False,
        disabled=False,
    )
    db.add(user)
    db.flush()
    log.info("gateway user created", extra={"address": user.email})
    return user


# --- the subject tag -------------------------------------------------------


def _threading(db: Session, settings: Settings, queue: Queue, subject: str) -> _Threading:
    """Read the subject tag: a reply onto a ticket, or a new ticket.

    A tag is *ours* when its name is ``SITE_NAME`` or a queue's own subject
    tag (Q06). It threads only when it is ours **and** the id names a
    ticket, and then the name is measured against that ticket's own queue:
    ``[Support #12] …`` does not reach ticket 12 when 12 lives somewhere
    that is not tagged ``Support``. A tag that is not ours at all -- another
    tracker's -- is left in the subject, because it is part of what the
    person wrote.
    """
    match = TAG_RE.search(subject)
    if match is None:
        return _Threading(None, subject)

    name = match.group("name").strip()
    ident = int(match.group("id"))
    stripped = _without(subject, match.start(), match.end())

    ticket = db.get(Ticket, ident) if 0 < ident <= MAX_TICKET_ID else None
    if ticket is not None:
        their_queue = db.get(Queue, ticket.queue_id)
        if their_queue is not None and _is_our_tag(name, settings, their_queue):
            return _Threading(ticket, stripped)
    if _is_our_tag(name, settings, queue):
        return _Threading(None, stripped)
    return _Threading(None, subject)


def _is_our_tag(name: str, settings: Settings, queue: Queue) -> bool:
    """Whether that tag name is this side's, for that queue (FP Q06)."""
    return name == settings.site_name or name == subject_tag_for(queue, settings.site_name)


def _without(subject: str, start: int, end: int) -> str:
    """The subject with our tag cut out of it and the seam tidied."""
    return f"{subject[:start].strip()} {subject[end:].strip()}".strip()


# --- the two writes --------------------------------------------------------


def _reply(
    db: Session,
    settings: Settings,
    ticket: Ticket,
    sender: User,
    parsed: Parsed,
    action: str,
) -> Result:
    """FP M03: the message as a ``Correspond`` (or ``Comment``) on a ticket.

    The right is asked with the ticket in hand, so the sender's own roles
    count: a requestor answering their own ticket is reached by a grant to
    ``Requestor`` as well as by one to ``Everyone`` (R09).

    A ``new`` ticket opens on the correspondence, exactly as the update
    page's first reply does (T05), and the move is its own ``Status``
    transaction.
    """
    replying = action == CORRESPOND
    right = REPLY_TO_TICKET if replying else COMMENT_ON_TICKET
    held = ticket_principals(db, sender, ticket)
    if not has_right(db, held, right, ticket.queue_id):
        return _refuse(db, permission_denied(right))

    written = transactions.record(
        db,
        ticket,
        TransactionType.CORRESPOND if replying else TransactionType.COMMENT,
        sender,
        body=parsed.text,
        content_type=CONTENT_TYPE,
        message_id=parsed.message_id,
        headers=parsed.headers,
    )
    moved = None
    if replying and ticket.status == lifecycle.NEW:
        lifecycle.apply_status(ticket, lifecycle.OPEN, sender, utcnow())
        moved = transactions.record(
            db,
            ticket,
            TransactionType.STATUS,
            sender,
            field=STATUS_FIELD,
            old_value=lifecycle.NEW,
            new_value=lifecycle.OPEN,
        )
    db.commit()

    hooks.fire(
        hooks.CORRESPONDED if replying else hooks.COMMENTED,
        db,
        settings,
        ticket,
        written,
        sender,
    )
    if moved is not None:
        hooks.fire(hooks.STATUS_CHANGED, db, settings, ticket, moved, sender)
    log.info("gateway reply", extra={"ticket": ticket.id, "action": action})
    return Result(True, ticket.id)


def _create(
    db: Session,
    settings: Settings,
    queue: Queue,
    sender: User,
    parsed: Parsed,
    subject: str,
) -> Result:
    """FP M02: a new ticket in the named queue, the sender its requestor.

    ``CreateTicket`` alone is the gate here, not the create form's
    ``CreateTicket`` *and* ``SeeQueue``: nobody is being shown a menu of
    queues, the platform named one, and R09's grant is the two rights the
    mail goal's subtask gives ``Everyone``.
    """
    held = principals(db, sender)
    if not has_right(db, held, CREATE_TICKET, queue.id):
        return _refuse(db, permission_denied(CREATE_TICKET))

    unowned = service.nobody(db)
    if unowned is None:  # pragma: no cover - a database nobody seeded
        return _refuse(db, NO_NOBODY)

    form = service.TicketForm(
        queue=queue.id,
        status=lifecycle.NEW,
        owner=unowned.id,
        requestors=sender.email or "",
        subject=subject[:SUBJECT_LIMIT],
        content=parsed.text,
    )
    ticket = service.create_ticket(
        db,
        settings,
        sender,
        queue,
        form,
        message_id=parsed.message_id,
        headers=parsed.headers,
    )
    log.info("gateway ticket created", extra={"ticket": ticket.id, "queue": queue.name})
    return Result(True, ticket.id)


def _refuse(db: Session, reason: str) -> Result:
    """A refusal, the session rolled back so nothing half-written survives."""
    db.rollback()
    log.info("gateway refused", extra={"reason": reason})
    return Result(False, None, reason)
