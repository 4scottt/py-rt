"""The custom field pages' queries and writes (plan §8, §10, §12; FP F01-F07).

One kind of field exists, plan §8's ``Enter one value``: a freeform single
value that applies to ``Tickets``. Three tables carry it (plan §12):
``custom_fields`` is the field itself, ``queue_custom_fields`` is which
queues' tickets carry it ("Applies to"), and ``ticket_custom_field_values``
is one value per field per ticket.

Nothing here reads a form or renders a template: the router hands in the
fields it parsed and gets back rows, a refusal or a message. The ticket
pages' entry points are :func:`ticket_values` (plan §12's fourth query for
the ticket page: the queue's custom fields left-joined to the ticket's
values), :func:`stray_values` (FP F07) and :func:`apply_values` (FP F04,
which records the ``CustomField`` transaction of plan §10).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pyrt.db.models import (
    CustomField,
    CustomFieldKind,
    Queue,
    QueueCustomField,
    Ticket,
    TicketCustomFieldValue,
    TransactionType,
    User,
    utcnow,
)
from pyrt.tickets import transactions

#: The rights of plan §8's vocabulary these pages ask about. ``SeeCustomField``
#: and ``ModifyCustomField`` are queue-scoped (FP F05); ``AdminCustomField`` is
#: global-only, and ``SuperUser`` covers all three through the resolver.
ADMIN_CUSTOM_FIELD: Final = "AdminCustomField"
SEE_CUSTOM_FIELD: Final = "SeeCustomField"
MODIFY_CUSTOM_FIELD: Final = "ModifyCustomField"

#: Plan §8's fixed vocabulary, kept verbatim: the goal text and the walk both
#: name these words. The value beside each label is the form's, not the
#: person's; ``applies_to`` has no column because a field applies to tickets
#: and to nothing else in this rewrite (plan §7's scope).
TYPE_LABEL: Final = "Enter one value"
TYPE_VALUE: Final = CustomFieldKind.FREEFORM_SINGLE.value
APPLIES_TO_LABEL: Final = "Tickets"
APPLIES_TO_VALUE: Final = "tickets"

#: The longest a value may be (plan §12's ``value varchar 255``).
VALUE_MAX: Final = 255

#: The messages these pages show. The plan fixes none of them but the
#: headings, so these are this package's words, in the shape the queue, user
#: and group pages already use.
NAME_REQUIRED: Final = "A custom field needs a name"
NAME_TAKEN: Final = "A custom field with that name already exists"
CREATED: Final = "Custom field created"
UPDATED: Final = "Custom field updated"
APPLIES_TO_SAVED: Final = "Applies to updated"
QUEUE_FIELDS_SAVED: Final = "Custom fields updated"

#: ``?msg=`` after a redirect, so a POST hands its outcome on as a code.
MESSAGES: Final[dict[str, str]] = {
    "created": CREATED,
    "saved": UPDATED,
    "applied": APPLIES_TO_SAVED,
    "queue-saved": QUEUE_FIELDS_SAVED,
}

#: Every message here reads as a success.
OK_MESSAGES: Final[frozenset[str]] = frozenset(MESSAGES.values())

#: What the ticket page says where there is no value, where the value belongs
#: to a field this queue does not carry (FP F07), and where the viewer holds
#: no ``SeeCustomField`` on the queue (FP F05).
NOT_SET: Final = "(not set)"
NOT_APPLIED: Final = "(not applied to this queue)"
NOT_SHOWN: Final = "Custom fields are not shown to you"


def message_for(msg: str) -> str:
    """The text ``?msg=`` stands for, or "" when it says nothing known."""
    return MESSAGES.get(msg, "")


@dataclass(slots=True)
class FieldForm:
    """What the Basics form said, kept so a refused POST keeps its values.

    ``kind`` and ``applies_to`` are the two selects of plan §8. Each offers
    one option, and neither is stored as the form sends it: the row's kind is
    always ``freeform_single`` and a field always applies to tickets. They are
    on the form because the goal text names them ("type ``Enter one value``,
    applies to ``Tickets``") and a person must be able to pick them.
    """

    name: str = ""
    description: str = ""
    kind: str = TYPE_VALUE
    applies_to: str = APPLIES_TO_VALUE
    enabled: bool = True

    @classmethod
    def of(cls, field: CustomField) -> FieldForm:
        """The form as a stored field fills it."""
        return cls(
            name=field.name,
            description=field.description,
            kind=field.kind,
            applies_to=APPLIES_TO_VALUE,
            enabled=not field.disabled,
        )

    def cleaned(self) -> FieldForm:
        """The same values with the text fields stripped."""
        return FieldForm(
            name=self.name.strip(),
            description=self.description.strip(),
            kind=TYPE_VALUE,
            applies_to=APPLIES_TO_VALUE,
            enabled=self.enabled,
        )


@dataclass(frozen=True, slots=True)
class FieldValue:
    """One custom field as a ticket form or the ticket page shows it.

    ``applied`` is False for the stray of FP F07: a value a ticket still
    holds for a field its queue does not carry.
    """

    id: int
    name: str
    description: str
    value: str
    applied: bool = True

    @property
    def control(self) -> str:
        """The input's ``name`` on the create and basics forms, ``cf-{id}``."""
        return f"cf-{self.id}"

    @property
    def shown(self) -> str:
        """What the ticket page prints for this field."""
        return self.value or NOT_SET


@dataclass(frozen=True, slots=True)
class Applied:
    """One checkbox on an "Applies to" page: a queue, or a field."""

    id: int
    name: str
    checked: bool

    def control(self, prefix: str) -> str:
        """The checkbox's ``name``: ``queue-{id}`` or ``field-{id}``."""
        return f"{prefix}-{self.id}"


# --- the admin pages' reads and writes --------------------------------------


def all_fields(db: Session, *, include_disabled: bool = False) -> list[CustomField]:
    """The Select list of FP F01, by sort order then name."""
    statement = select(CustomField)
    if not include_disabled:
        statement = statement.where(CustomField.disabled.is_(False))
    return list(db.scalars(statement.order_by(CustomField.sort_order, CustomField.name)).all())


def get_field(db: Session, field_id: int) -> CustomField | None:
    """One custom field by id, or None (the router's 404)."""
    return db.get(CustomField, field_id)


def refusal(db: Session, form: FieldForm, *, field: CustomField | None = None) -> str:
    """Why this form cannot be saved, or ``""`` when it can (FP F01).

    The name is required and unique case-insensitively; a modify passes its
    own field so it may keep its name.
    """
    if not form.name:
        return NAME_REQUIRED
    statement = select(CustomField.id).where(func.lower(CustomField.name) == form.name.lower())
    if field is not None:
        statement = statement.where(CustomField.id != field.id)
    if db.scalar(statement.limit(1)) is not None:
        return NAME_TAKEN
    return ""


def create_field(db: Session, form: FieldForm) -> CustomField:
    """Write the new field and return it, its id in hand (FP F01)."""
    field = CustomField(
        name=form.name,
        description=form.description,
        kind=CustomFieldKind.FREEFORM_SINGLE,
        sort_order=0,
        disabled=not form.enabled,
        created=utcnow(),
    )
    db.add(field)
    db.commit()
    db.refresh(field)
    return field


def save_field(db: Session, field: CustomField, form: FieldForm) -> CustomField:
    """Apply the form to ``field``.

    Unchecking Enabled sets ``disabled``: the field falls out of every form
    and off the ticket page, and its values stay in the table (FP F06).
    """
    field.name = form.name
    field.description = form.description
    field.disabled = not form.enabled
    db.commit()
    db.refresh(field)
    return field


def enabled_queues(db: Session) -> list[Queue]:
    """The queues an "Applies to" page offers, by name (FP F02)."""
    return list(
        db.scalars(select(Queue).where(Queue.disabled.is_(False)).order_by(Queue.name)).all()
    )


def applied_queue_ids(db: Session, field_id: int) -> set[int]:
    """The queues that carry ``field_id`` today."""
    return set(
        db.scalars(
            select(QueueCustomField.queue_id).where(QueueCustomField.custom_field_id == field_id)
        ).all()
    )


def applied_field_ids(db: Session, queue_id: int) -> set[int]:
    """The custom fields ``queue_id`` carries today."""
    return set(
        db.scalars(
            select(QueueCustomField.custom_field_id).where(QueueCustomField.queue_id == queue_id)
        ).all()
    )


def queue_boxes(db: Session, field_id: int) -> list[Applied]:
    """The field's "Applies to" page: a checkbox per enabled queue (FP F02)."""
    checked = applied_queue_ids(db, field_id)
    return [Applied(queue.id, queue.name, queue.id in checked) for queue in enabled_queues(db)]


def field_boxes(db: Session, queue_id: int) -> list[Applied]:
    """The queue's Custom Fields page: a checkbox per enabled field (FP F02)."""
    checked = applied_field_ids(db, queue_id)
    return [Applied(field.id, field.name, field.id in checked) for field in all_fields(db)]


def save_applies_to(
    db: Session, field: CustomField, offered: list[Applied], posted: frozenset[str]
) -> tuple[int, int]:
    """FP F02, from the field's side: the ticked queues become its queues.

    Only the queues this page offered are touched, so a grant on a disabled
    queue survives a save that never showed it. The names are rendered again
    and looked up in what came in, never parsed (the rights pages' rule).
    """
    wanted = {box.id for box in offered if box.control("queue") in posted}
    had = {box.id for box in offered if box.checked}
    order = field.sort_order or 0
    return _write_pairs(
        db,
        add=[(queue_id, field.id, order) for queue_id in sorted(wanted - had)],
        remove=[(queue_id, field.id) for queue_id in sorted(had - wanted)],
    )


def save_queue_fields(
    db: Session, queue: Queue, offered: list[Applied], posted: frozenset[str]
) -> tuple[int, int]:
    """FP F02, from the queue's side: the ticked fields become its fields.

    The goal's "apply it to the Support queue" may be done from either page;
    both write the same ``queue_custom_fields`` rows.
    """
    wanted = {box.id for box in offered if box.control("field") in posted}
    had = {box.id for box in offered if box.checked}
    orders = {field.id: field.sort_order or 0 for field in all_fields(db, include_disabled=True)}
    return _write_pairs(
        db,
        add=[(queue.id, field_id, orders.get(field_id, 0)) for field_id in sorted(wanted - had)],
        remove=[(queue.id, field_id) for field_id in sorted(had - wanted)],
    )


def _write_pairs(
    db: Session,
    *,
    add: list[tuple[int, int, int]],
    remove: list[tuple[int, int]],
) -> tuple[int, int]:
    """Insert and delete ``(queue_id, custom_field_id)`` pairs; (added, removed)."""
    for queue_id, field_id in remove:
        row = db.get(QueueCustomField, (queue_id, field_id))
        if row is not None:
            db.delete(row)
    for queue_id, field_id, order in add:
        db.add(QueueCustomField(queue_id=queue_id, custom_field_id=field_id, sort_order=order))
    if add or remove:
        db.commit()
    return len(add), len(remove)


# --- the ticket pages' reads ------------------------------------------------


def queue_fields(db: Session, queue_id: int) -> list[CustomField]:
    """The enabled custom fields ``queue_id`` carries, in sort order (FP F03).

    A disabled field is not here, so it is off the create form, off basics
    and off the ticket page while its values stay (FP F06).
    """
    return list(
        db.scalars(
            select(CustomField)
            .join(QueueCustomField, QueueCustomField.custom_field_id == CustomField.id)
            .where(
                QueueCustomField.queue_id == queue_id,
                CustomField.disabled.is_(False),
            )
            .order_by(QueueCustomField.sort_order, CustomField.sort_order, CustomField.name)
        ).all()
    )


def blank_values(db: Session, queue_id: int) -> list[FieldValue]:
    """The queue's fields with no values: the create form's rows (FP F03)."""
    return [
        FieldValue(field.id, field.name, field.description, "")
        for field in queue_fields(db, queue_id)
    ]


def ticket_values(db: Session, ticket_id: int, queue_id: int) -> list[FieldValue]:
    """Plan §12's fourth query: the queue's fields left-joined to the values.

    One query for the whole Custom Fields box and for the basics form's rows;
    a field the ticket has no value for comes back with ``""``.
    """
    rows = (
        db.execute(
            select(CustomField, TicketCustomFieldValue.value)
            .join(QueueCustomField, QueueCustomField.custom_field_id == CustomField.id)
            .outerjoin(
                TicketCustomFieldValue,
                (TicketCustomFieldValue.custom_field_id == CustomField.id)
                & (TicketCustomFieldValue.ticket_id == ticket_id),
            )
            .where(
                QueueCustomField.queue_id == queue_id,
                CustomField.disabled.is_(False),
            )
            .order_by(QueueCustomField.sort_order, CustomField.sort_order, CustomField.name)
        )
        .tuples()
        .all()
    )
    return [
        FieldValue(field.id, field.name, field.description, value or "") for field, value in rows
    ]


def stray_values(db: Session, ticket_id: int, queue_id: int) -> list[FieldValue]:
    """FP F07: the values this ticket holds for fields its queue does not carry.

    A ticket moved to another queue keeps what it was told; the page shows it
    read-only, marked, after the queue's own fields. A *disabled* field is not
    here either (FP F06): its value stays in the table and off every page.
    """
    carried = select(QueueCustomField.custom_field_id).where(QueueCustomField.queue_id == queue_id)
    rows = (
        db.execute(
            select(CustomField, TicketCustomFieldValue.value)
            .join(
                TicketCustomFieldValue,
                TicketCustomFieldValue.custom_field_id == CustomField.id,
            )
            .where(
                TicketCustomFieldValue.ticket_id == ticket_id,
                CustomField.disabled.is_(False),
                CustomField.id.not_in(carried),
            )
            .order_by(CustomField.sort_order, CustomField.name)
        )
        .tuples()
        .all()
    )
    return [
        FieldValue(field.id, field.name, field.description, value or "", applied=False)
        for field, value in rows
    ]


# --- the ticket pages' write ------------------------------------------------


def apply_values(
    db: Session,
    ticket: Ticket,
    actor: User,
    values: dict[int, str],
) -> int:
    """FP F04: write the values the form sent; how many actually moved.

    Only the fields the ticket's queue carries are written, whatever a stale
    or hand-made form posted, and only a value that changed is touched: a new
    one inserts a row, a different one updates it, an empty one deletes it.
    Each change is one ``CustomField`` transaction whose ``field`` is the
    field's name (plan §10), with the old and the new value.

    Like :func:`pyrt.tickets.transactions.record`, this flushes and does not
    commit: the caller owns the unit of work, so a create or a basics save
    writes its values in the same transaction as the rest of the form.
    """
    wrote = 0
    for field in queue_fields(db, ticket.queue_id):
        if field.id not in values:
            continue
        new_value = values[field.id].strip()[:VALUE_MAX]
        row = db.get(TicketCustomFieldValue, (ticket.id, field.id))
        old_value = row.value if row is not None else ""
        if new_value == old_value:
            continue
        if not new_value:
            if row is not None:
                db.delete(row)
        elif row is None:
            db.add(
                TicketCustomFieldValue(
                    ticket_id=ticket.id, custom_field_id=field.id, value=new_value
                )
            )
        else:
            row.value = new_value
        db.flush()
        transactions.record(
            db,
            ticket,
            TransactionType.CUSTOM_FIELD,
            actor,
            field=field.name,
            old_value=old_value or None,
            new_value=new_value or None,
        )
        wrote += 1
    return wrote


__all__ = [
    "ADMIN_CUSTOM_FIELD",
    "APPLIES_TO_LABEL",
    "APPLIES_TO_SAVED",
    "APPLIES_TO_VALUE",
    "CREATED",
    "MESSAGES",
    "MODIFY_CUSTOM_FIELD",
    "NAME_REQUIRED",
    "NAME_TAKEN",
    "NOT_APPLIED",
    "NOT_SET",
    "NOT_SHOWN",
    "OK_MESSAGES",
    "QUEUE_FIELDS_SAVED",
    "SEE_CUSTOM_FIELD",
    "TYPE_LABEL",
    "TYPE_VALUE",
    "UPDATED",
    "Applied",
    "FieldForm",
    "FieldValue",
    "all_fields",
    "applied_field_ids",
    "applied_queue_ids",
    "apply_values",
    "blank_values",
    "create_field",
    "enabled_queues",
    "field_boxes",
    "get_field",
    "message_for",
    "queue_boxes",
    "queue_fields",
    "refusal",
    "save_applies_to",
    "save_field",
    "save_queue_fields",
    "stray_values",
    "ticket_values",
]
