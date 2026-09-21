"""The update page and the basics page (plan §8, §10; FP T05-T10, T13).

Two writes over one record: the update page adds a ``Correspond`` or a
``Comment`` and may move the status; the basics page saves the four fields
of FP T09, each change its own ``Set`` transaction with its old and new
value. Both go through :mod:`pyrt.tickets.lifecycle` for the status, so the
dates of plan §10 are set in one place.

Nothing here reads a form or renders a template: the router parses, checks
the rights and hands in the fields; what comes back is a refusal message or
``""``. The hooks fire after the commit, on rows already written, exactly as
``create_ticket`` does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from fastapi import Request
from sqlalchemy.orm import Session

from pyrt.acl import Principals, has_right, principals
from pyrt.config import Settings
from pyrt.db.models import (
    NO_SUBJECT,
    Queue,
    Ticket,
    TransactionType,
    User,
    utcnow,
)
from pyrt.queues.service import get_queue, queues_for_create
from pyrt.tickets import hooks, lifecycle, service, transactions
from pyrt.users.service import privileged_users

#: The rights of plan §8's vocabulary these two pages ask about (FP R06:
#: ``ReplyToTicket``/``CommentOnTicket`` gate the update page's radio,
#: ``ModifyTicket`` gates basics and every status change, ``OwnTicket`` gates
#: the owner select).
REPLY_TO_TICKET: Final = "ReplyToTicket"
COMMENT_ON_TICKET: Final = service.COMMENT_ON_TICKET
MODIFY_TICKET: Final = "ModifyTicket"
OWN_TICKET: Final = "OwnTicket"

#: The two values of the ``UpdateType`` radio (plan §8's URL map).
RESPOND: Final = "respond"
COMMENT: Final = "comment"
UPDATE_TYPES: Final[tuple[str, ...]] = (RESPOND, COMMENT)

#: The ``field`` of the transactions these pages write (FP T07, T09).
STATUS_FIELD: Final = "Status"
SUBJECT_FIELD: Final = "Subject"
QUEUE_FIELD: Final = "Queue"
OWNER_FIELD: Final = "Owner"
PRIORITY_FIELD: Final = "Priority"

#: What a form that cannot be saved comes back with, its values kept.
NOTHING_TO_UPDATE: Final = "Nothing to update"
NOTHING_CHANGED: Final = "Nothing changed"
BAD_OWNER: Final = "That user cannot own this ticket"
BAD_QUEUE: Final = "That is not a queue you can move this ticket to"
BAD_PRIORITY: Final = "Priority is a whole number from 0 to 99"

#: FP T12: priority is an integer 0-99.
PRIORITY_MIN: Final = 0
PRIORITY_MAX: Final = 99


@dataclass(slots=True)
class UpdateForm:
    """What the update form carries (plan §8: ``UpdateType``, ``Status``, ``Content``)."""

    update_type: str = RESPOND
    status: str = ""
    content: str = ""

    def cleaned(self) -> UpdateForm:
        """The same values, the radio narrowed to one of the two it may be."""
        return UpdateForm(
            update_type=self.update_type if self.update_type in UPDATE_TYPES else RESPOND,
            status=self.status.strip(),
            content=self.content,
        )


@dataclass(slots=True)
class BasicsForm:
    """What the basics form carries (plan §8: the five fields and the CFs).

    ``priority`` stays the string the form sent so a value that is not a
    number comes back on the form the person typed it into.
    """

    subject: str = ""
    queue: int = 0
    status: str = ""
    owner: int = 0
    priority: str = "0"

    @classmethod
    def of(cls, ticket: Ticket) -> BasicsForm:
        """The form as a stored ticket fills it."""
        return cls(
            subject=ticket.subject,
            queue=ticket.queue_id,
            status=ticket.status,
            owner=ticket.owner_id,
            priority=str(ticket.priority),
        )


# --- the rights the two pages ask about ------------------------------------


def may_reply(db: Session, held: Principals, queue_id: int, request: Request | None = None) -> bool:
    """``ReplyToTicket`` on the ticket's queue (the Reply half of the radio)."""
    return has_right(db, held, REPLY_TO_TICKET, queue_id, request)


def may_comment(
    db: Session, held: Principals, queue_id: int, request: Request | None = None
) -> bool:
    """``CommentOnTicket`` on the ticket's queue (the Comment half)."""
    return has_right(db, held, COMMENT_ON_TICKET, queue_id, request)


def may_modify(
    db: Session, held: Principals, queue_id: int, request: Request | None = None
) -> bool:
    """``ModifyTicket`` on the ticket's queue: basics, and any status change."""
    return has_right(db, held, MODIFY_TICKET, queue_id, request)


# --- the selects -----------------------------------------------------------


def owner_choices(
    db: Session,
    queue_id: int,
    current_owner_id: int = 0,
    request: Request | None = None,
) -> list[User]:
    """FP T10: Nobody, the privileged users with ``OwnTicket`` on the queue.

    The ticket's current owner is kept on the list even when the right has
    since gone, so the form can be saved without silently reassigning it.
    """
    chosen: list[User] = []
    unowned = service.nobody(db)
    if unowned is not None:
        chosen.append(unowned)
    for user in privileged_users(db):
        if has_right(db, principals(db, user, request), OWN_TICKET, queue_id, request):
            chosen.append(user)
    if current_owner_id and not any(user.id == current_owner_id for user in chosen):
        current = db.get(User, current_owner_id)
        if current is not None:
            chosen.append(current)
    return chosen


def queue_choices(
    db: Session,
    held: Principals,
    current_queue_id: int = 0,
    request: Request | None = None,
) -> list[Queue]:
    """The ``Queue`` select: the queues the actor may create in, plus this one.

    Moving a ticket is putting it where a new one could go, so the list is
    :func:`queues_for_create`; the queue it is in now stays on it even when
    that queue is disabled or closed to the actor.
    """
    allowed = queues_for_create(db, held, request)
    if current_queue_id and not any(queue.id == current_queue_id for queue in allowed):
        current = get_queue(db, current_queue_id)
        if current is not None:
            allowed = sorted([*allowed, current], key=lambda queue: queue.name)
    return allowed


# --- the update page's write -----------------------------------------------


def update_ticket(
    db: Session,
    settings: Settings,
    ticket: Ticket,
    actor: User,
    form: UpdateForm,
) -> str:
    """FP T05-T08: the reply or comment, and the status the form chose.

    One unit of work: the ``Correspond`` or ``Comment`` with its message,
    then the ``Status`` transaction when the status moved, then the commit.
    The first reply to a ``new`` ticket opens it (FP T05), which is a status
    change like any other and is recorded as one.

    Returns the message the form comes back with, or ``""`` when it went
    through. The rights are the router's: a right the user does not hold is
    the denial page, not a message on a form.
    """
    clean = form.cleaned()
    content = clean.content.strip()
    wanted = clean.status or ticket.status
    if not content and wanted == ticket.status:
        return NOTHING_TO_UPDATE

    # FP T05: a reply to a ticket nobody has touched yet opens it, unless the
    # form asked for somewhere else to go.
    if (
        content
        and clean.update_type == RESPOND
        and ticket.status == lifecycle.NEW
        and wanted == lifecycle.NEW
    ):
        wanted = lifecycle.OPEN

    problem = lifecycle.refusal(ticket.status, wanted)
    if problem:
        return problem

    now = utcnow()
    written = None
    if content:
        kind = (
            TransactionType.CORRESPOND if clean.update_type == RESPOND else TransactionType.COMMENT
        )
        written = transactions.record(db, ticket, kind, actor, body=content)
    moved = None
    if wanted != ticket.status:
        was = ticket.status
        lifecycle.apply_status(ticket, wanted, actor, now)
        moved = transactions.record(
            db,
            ticket,
            TransactionType.STATUS,
            actor,
            field=STATUS_FIELD,
            old_value=was,
            new_value=wanted,
        )
    db.commit()

    if written is not None:
        moment = hooks.CORRESPONDED if clean.update_type == RESPOND else hooks.COMMENTED
        hooks.fire(moment, db, settings, ticket, written, actor)
    if moved is not None:
        hooks.fire(hooks.STATUS_CHANGED, db, settings, ticket, moved, actor)
    return ""


# --- the basics page's write -----------------------------------------------


def basics_refusal(
    ticket: Ticket,
    form: BasicsForm,
    queues: list[Queue],
    owners: list[User],
) -> str:
    """Why the basics form cannot be saved, or ``""`` when it can.

    The selects' own values are the rule, and the POST re-checks them
    against the lists the page offered: a queue the actor may not create in,
    a user who may not own this ticket and a status the lifecycle does not
    reach are all refusals with the values kept.
    """
    if not any(queue.id == form.queue for queue in queues):
        return BAD_QUEUE
    if not any(owner.id == form.owner for owner in owners):
        return BAD_OWNER
    if _priority(form.priority) is None:
        return BAD_PRIORITY
    return lifecycle.refusal(ticket.status, form.status or ticket.status)


def save_basics(
    db: Session,
    settings: Settings,
    ticket: Ticket,
    actor: User,
    form: BasicsForm,
) -> str:
    """FP T09, T10: save the basics, one ``Set`` transaction per change.

    Called after :func:`basics_refusal` said yes. Nothing is written until a
    field has actually moved, so a form saved unchanged leaves no row behind
    and comes back saying so.
    """
    now = utcnow()
    wrote = False

    subject = form.subject.strip() or NO_SUBJECT
    if subject != ticket.subject:
        was, ticket.subject = ticket.subject, subject
        transactions.record(
            db,
            ticket,
            TransactionType.SET,
            actor,
            field=SUBJECT_FIELD,
            old_value=was,
            new_value=subject,
        )
        wrote = True

    if form.queue and form.queue != ticket.queue_id:
        was_queue = get_queue(db, ticket.queue_id)
        new_queue = get_queue(db, form.queue)
        if new_queue is not None:
            ticket.queue_id = new_queue.id
            transactions.record(
                db,
                ticket,
                TransactionType.SET,
                actor,
                field=QUEUE_FIELD,
                old_value=was_queue.name if was_queue else None,
                new_value=new_queue.name,
            )
            wrote = True

    if form.owner and form.owner != ticket.owner_id:
        was_owner = db.get(User, ticket.owner_id)
        new_owner = db.get(User, form.owner)
        if new_owner is not None:
            ticket.owner_id = new_owner.id
            transactions.record(
                db,
                ticket,
                TransactionType.SET,
                actor,
                field=OWNER_FIELD,
                old_value=was_owner.name if was_owner else None,
                new_value=new_owner.name,
            )
            wrote = True

    priority = _priority(form.priority)
    if priority is not None and priority != ticket.priority:
        was_priority, ticket.priority = ticket.priority, priority
        transactions.record(
            db,
            ticket,
            TransactionType.SET,
            actor,
            field=PRIORITY_FIELD,
            old_value=str(was_priority),
            new_value=str(priority),
        )
        wrote = True

    moved = None
    wanted = form.status or ticket.status
    if wanted != ticket.status:
        was_status = ticket.status
        lifecycle.apply_status(ticket, wanted, actor, now)
        moved = transactions.record(
            db,
            ticket,
            TransactionType.STATUS,
            actor,
            field=STATUS_FIELD,
            old_value=was_status,
            new_value=wanted,
        )
        wrote = True

    if not wrote:
        # Nothing was written, so there is nothing to commit and nothing to
        # undo: the form simply came back the way it went out.
        return NOTHING_CHANGED
    db.commit()
    if moved is not None:
        hooks.fire(hooks.STATUS_CHANGED, db, settings, ticket, moved, actor)
    return ""


def _priority(raw: str) -> int | None:
    """The priority the form sent, or None when it is not one (FP T12)."""
    text = raw.strip()
    if not text.isdigit():
        return None
    value = int(text)
    if value < PRIORITY_MIN or value > PRIORITY_MAX:
        return None
    return value
