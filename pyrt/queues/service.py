"""The queue pages' queries and writes (plan §8's Admin paragraph, §12).

Nothing here reads a form or renders a template: the router hands in the
fields it parsed and gets back rows, a refusal or a message. The two
functions a later package calls are :func:`queues_for_create` (the "New
ticket in" menu and the create form's select) and :func:`subject_tag_for`
(the mail subject tag, FP Q06).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pyrt.acl import Forbidden, Principals, has_right, queues_with_right
from pyrt.db.models import Queue, utcnow

#: The rights of plan §8's vocabulary these pages ask about.
ADMIN_QUEUE: Final = "AdminQueue"
SEE_QUEUE: Final = "SeeQueue"
CREATE_TICKET: Final = "CreateTicket"

#: The two refusals the create and modify forms come back with.
NAME_REQUIRED: Final = "A queue needs a name"
NAME_TAKEN: Final = "A queue with that name already exists"


@dataclass(frozen=True, slots=True)
class QueueFields:
    """What the Basics form carries, already stripped.

    The field names are the form's control names; the labels are plan §8's
    (``Name``, ``Description``, ``Subject Tag``, ``Reply Address``,
    ``Comment Address``, ``Enabled``).
    """

    name: str = ""
    description: str = ""
    subject_tag: str = ""
    correspond_address: str = ""
    comment_address: str = ""
    enabled: bool = True

    @classmethod
    def from_queue(cls, queue: Queue) -> QueueFields:
        """The form as a stored queue fills it."""
        return cls(
            name=queue.name,
            description=queue.description,
            subject_tag=queue.subject_tag or "",
            correspond_address=queue.correspond_address,
            comment_address=queue.comment_address,
            enabled=not queue.disabled,
        )


def get_queue(db: Session, queue_id: int) -> Queue | None:
    """One queue by id, or None (the router's 404)."""
    return db.get(Queue, queue_id)


def all_queues(db: Session) -> list[Queue]:
    """Every queue, disabled ones included, by name."""
    return list(db.scalars(select(Queue).order_by(Queue.name)).all())


def queues_for_list(
    db: Session,
    held: Principals,
    request: Request | None = None,
    *,
    include_disabled: bool = False,
) -> list[Queue]:
    """The Select list of FP Q01, or :class:`Forbidden` when it is empty.

    A global ``AdminQueue`` or ``SeeQueue`` (so ``SuperUser`` too) shows
    every queue; otherwise the user sees the queues it holds one of the two
    rights on, and holding neither anywhere is a refusal, not a blank page.
    """
    rows = all_queues(db)
    if not _globally(db, held, request):
        ids = {queue.id for queue in rows}
        allowed = queues_with_right(db, held, SEE_QUEUE, ids, request) | queues_with_right(
            db, held, ADMIN_QUEUE, ids, request
        )
        if not allowed:
            raise Forbidden(SEE_QUEUE)
        rows = [queue for queue in rows if queue.id in allowed]
    if include_disabled:
        return rows
    return [queue for queue in rows if not queue.disabled]


def _globally(db: Session, held: Principals, request: Request | None) -> bool:
    """Whether the user may see every queue without a per-queue grant."""
    return has_right(db, held, ADMIN_QUEUE, None, request) or has_right(
        db, held, SEE_QUEUE, None, request
    )


def may_administer(
    db: Session, held: Principals, queue_id: int | None = None, request: Request | None = None
) -> bool:
    """``AdminQueue`` on that queue, or globally when ``queue_id`` is None."""
    return has_right(db, held, ADMIN_QUEUE, queue_id, request)


def queues_for_create(db: Session, held: Principals, request: Request | None = None) -> list[Queue]:
    """The enabled queues the user may ``CreateTicket`` in, by name.

    The tickets package's entry point: the "New ticket in" submenu of plan
    §11 and the create form's ``Queue`` select are this list.
    """
    rows = list(db.scalars(select(Queue).where(Queue.disabled.is_(False)).order_by(Queue.name)))
    allowed = queues_with_right(db, held, CREATE_TICKET, {queue.id for queue in rows}, request)
    return [queue for queue in rows if queue.id in allowed]


def subject_tag_for(queue: Queue, site_name: str) -> str:
    """FP Q06: the queue's own subject tag when it has one, else the site's.

    The mail code builds ``[<tag> #<id>]`` from this; a queue whose tag is
    unset or blank falls back to ``SITE_NAME``.
    """
    tag = (queue.subject_tag or "").strip()
    return tag or site_name


def name_problem(db: Session, fields: QueueFields, *, queue_id: int | None = None) -> str:
    """The message the form comes back with, or "" when the name will do.

    The name is required and unique case-insensitively (FP Q02); a modify
    passes its own id so a queue may keep its name.
    """
    if not fields.name:
        return NAME_REQUIRED
    statement = select(Queue.id).where(func.lower(Queue.name) == fields.name.lower())
    if queue_id is not None:
        statement = statement.where(Queue.id != queue_id)
    if db.scalar(statement.limit(1)) is not None:
        return NAME_TAKEN
    return ""


def create_queue(db: Session, fields: QueueFields) -> Queue:
    """Write a new queue and return it, its id in hand."""
    queue = Queue(
        name=fields.name,
        description=fields.description,
        subject_tag=fields.subject_tag or None,
        correspond_address=fields.correspond_address,
        comment_address=fields.comment_address,
        disabled=not fields.enabled,
    )
    db.add(queue)
    db.commit()
    db.refresh(queue)
    return queue


def save_queue(db: Session, queue: Queue, fields: QueueFields) -> Queue:
    """Save the basics of an existing queue (FP Q03)."""
    queue.name = fields.name
    queue.description = fields.description
    queue.subject_tag = fields.subject_tag or None
    queue.correspond_address = fields.correspond_address
    queue.comment_address = fields.comment_address
    queue.disabled = not fields.enabled
    queue.last_updated = utcnow()
    db.commit()
    db.refresh(queue)
    return queue
