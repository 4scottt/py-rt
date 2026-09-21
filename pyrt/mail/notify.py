"""The three notifications of FP M07, as ticket hooks.

Plan §9 M07: on create the autoreply to the requestors, on correspond a copy
to the requestors (never back to the actor), on resolve a note that the
ticket is closed. **Comments never mail the requestor** (T06), so
:func:`on_commented` exists and does nothing: the registration stays uniform
and the rule is written down where a reader looks for it.

Each function has the seam's signature ``(db, settings, ticket, transaction,
actor) -> None`` and is registered onto the tickets package's hook lists by
:func:`register`; this module never imports ``pyrt.tickets``. Every message
goes out through :func:`pyrt.mail.send.current`, so what the app configured
at start decides whether it is a log line, a file or a relay (FP M06).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from types import ModuleType
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from pyrt.config import Settings
from pyrt.db.models import Message, Queue, Ticket, TicketWatcher, Transaction, User, WatcherRole
from pyrt.mail import send, templates
from pyrt.queues.service import subject_tag_for

log = logging.getLogger(__name__)

#: The subject tag's shape (plan §10). M5's parser matches the same regex to
#: thread a reply, so it lives beside the code that writes it (FP M08).
TAG_RE: Final = re.compile(r"\[(?P<name>[^\]#]+) #(?P<id>\d+)\]")

#: RFC 3834: these are machine-generated, and a well-behaved autoresponder on
#: the other end will not answer them.
AUTO_HEADERS: Final[dict[str, str]] = {"Auto-Submitted": "auto-generated"}

#: The status whose arrival mails the requestors (FP M07); every other status
#: change is silent.
RESOLVED: Final = "resolved"

#: What a hook is: the seam the tickets package calls after it has written a
#: transaction.
Hook = Callable[[Session, Settings, Ticket, Transaction, User | None], None]


# --- the pieces every notification shares ----------------------------------


def requestors(db: Session, ticket: Ticket) -> list[User]:
    """The ticket's requestors that can be mailed, oldest user first.

    A requestor without an address is skipped rather than refused: the ticket
    was still created, there is simply nowhere to write.
    """
    rows = db.scalars(
        select(User)
        .join(TicketWatcher, TicketWatcher.user_id == User.id)
        .where(
            TicketWatcher.ticket_id == ticket.id,
            TicketWatcher.role == WatcherRole.REQUESTOR,
        )
        .order_by(User.id)
    ).all()
    return [user for user in rows if (user.email or "").strip()]


def subject_for(db: Session, settings: Settings, ticket: Ticket) -> str:
    """``[<tag> #<id>] <subject>`` (FP M08), the queue's tag winning (Q06)."""
    queue = db.get(Queue, ticket.queue_id)
    tag = subject_tag_for(queue, settings.site_name) if queue else settings.site_name
    return f"[{tag} #{ticket.id}] {ticket.subject}"


def ticket_link(settings: Settings, ticket: Ticket) -> str:
    """The ticket's absolute URL, from ``BASE_URL`` and nothing else (O05)."""
    return f"{settings.base_url.rstrip('/')}/ticket/{ticket.id}"


def transaction_text(db: Session, transaction: Transaction) -> str:
    """The transaction's message body, or "" when it carries none."""
    message = db.get(Message, transaction.id)
    return message.body if message else ""


def _deliver(
    db: Session,
    settings: Settings,
    ticket: Ticket,
    recipients: list[User],
    body: str,
    *,
    what: str,
) -> None:
    """One message per recipient, through the configured sender.

    A back end that fails is logged and swallowed: mail is a notification,
    never a reason to lose a ticket write that has already been committed.
    """
    if not recipients:
        return
    addresses = [str(user.email) for user in recipients]
    mail = send.Mail.for_ticket(
        settings,
        ticket_id=ticket.id,
        to=addresses,
        subject=subject_for(db, settings, ticket),
        body=body,
        headers=AUTO_HEADERS,
    )
    try:
        send.current(settings).send(mail)
    except Exception:
        log.exception("mail failed", extra={"ticket": ticket.id, "notification": what})


# --- the notifications -----------------------------------------------------


def on_created(
    db: Session,
    settings: Settings,
    ticket: Ticket,
    transaction: Transaction,
    actor: User | None,
) -> None:
    """The autoreply to the requestors, the create message quoted under it."""
    body = templates.created_body(
        ticket.id, ticket.subject, ticket_link(settings, ticket), transaction_text(db, transaction)
    )
    _deliver(db, settings, ticket, requestors(db, ticket), body, what="create")


def on_corresponded(
    db: Session,
    settings: Settings,
    ticket: Ticket,
    transaction: Transaction,
    actor: User | None,
) -> None:
    """The reply to the requestors, minus the one who wrote it."""
    recipients = [user for user in requestors(db, ticket) if actor is None or user.id != actor.id]
    body = templates.correspond_body(
        ticket.id, ticket.subject, ticket_link(settings, ticket), transaction_text(db, transaction)
    )
    _deliver(db, settings, ticket, recipients, body, what="correspond")


def on_status_changed(
    db: Session,
    settings: Settings,
    ticket: Ticket,
    transaction: Transaction,
    actor: User | None,
) -> None:
    """Resolve mails the requestors; every other status change is silent."""
    if (transaction.new_value or "") != RESOLVED:
        return
    body = templates.resolved_body(ticket.id, ticket.subject, ticket_link(settings, ticket))
    _deliver(db, settings, ticket, requestors(db, ticket), body, what="resolve")


def on_commented(
    db: Session,
    settings: Settings,
    ticket: Ticket,
    transaction: Transaction,
    actor: User | None,
) -> None:
    """Nothing: a comment is internal and is never mailed to the requestor."""
    return


def register(hooks: ModuleType) -> None:
    """Append the four notifications to the tickets package's hook lists.

    The module is handed in (``pyrt.tickets.hooks``) so this package never
    imports the tickets package; the app calls this once at start. It is
    idempotent: the hook lists are module state and a test suite builds many
    apps in one process, which must not mean many copies of one mail.
    """
    for name, hook in (
        ("created", on_created),
        ("corresponded", on_corresponded),
        ("commented", on_commented),
        ("status_changed", on_status_changed),
    ):
        listeners = getattr(hooks, name)
        if hook not in listeners:
            listeners.append(hook)
