"""The transaction record (plan §10): every write to a ticket is one row.

``record`` is the only way a row reaches ``transactions``; it writes the
``messages`` row when there is a body and moves ``last_updated`` and
``last_updated_by`` on the ticket (FP T14). It flushes but does not commit:
the caller owns the unit of work, so a refused write leaves nothing behind.

``describe`` turns a row back into the sentence the history shows. The
history renders from these rows alone (plan §10), so the sentence lives
here beside the writer.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy.orm import Session

from pyrt.db.models import Message, Ticket, Transaction, TransactionType, User, utcnow

#: What a value reads as when there was none (a first Set, a cleared date).
NO_VALUE: Final = "(none)"

#: The sentences of FP T04, one per transaction type.
CREATED_SENTENCE: Final = "Ticket created"
CORRESPOND_SENTENCE: Final = "Correspondence added"
COMMENT_SENTENCE: Final = "Comment added"


def record(
    db: Session,
    ticket: Ticket,
    type: str,
    actor: User,
    *,
    field: str | None = None,
    old_value: str | None = None,
    new_value: str | None = None,
    body: str | None = None,
    content_type: str = "text/plain",
    message_id: str | None = None,
    headers: str | None = None,
) -> Transaction:
    """Write one transaction (and its message) and touch the ticket.

    ``body`` of ``None`` is a transaction without a message; an empty string
    is a message that says nothing, which is not what a form with no content
    means, so the callers pass ``content or None``.

    The row is flushed, so its id is in hand and the message can point at
    it, but nothing is committed here.
    """
    now = utcnow()
    transaction = Transaction(
        ticket_id=ticket.id,
        type=type,
        field=field,
        old_value=old_value,
        new_value=new_value,
        creator_id=actor.id,
        created=now,
    )
    db.add(transaction)
    db.flush()
    if body is not None:
        db.add(
            Message(
                transaction_id=transaction.id,
                content_type=content_type,
                body=body,
                headers=headers,
                message_id=message_id,
            )
        )
    # FP T14: LastUpdated and LastUpdatedBy move on every transaction.
    ticket.last_updated = now
    ticket.last_updated_by = actor.id
    db.flush()
    return transaction


def describe(
    type: str,
    field: str | None = None,
    old_value: str | None = None,
    new_value: str | None = None,
) -> str:
    """The history's sentence for one transaction (FP T04).

    ``Create``, ``Correspond`` and ``Comment`` say what happened; a
    ``Status``, a ``Set`` and a ``CustomField`` say what moved and to what.
    """
    if type == TransactionType.CREATE:
        return CREATED_SENTENCE
    if type == TransactionType.CORRESPOND:
        return CORRESPOND_SENTENCE
    if type == TransactionType.COMMENT:
        return COMMENT_SENTENCE
    if type == TransactionType.STATUS:
        return _moved("Status", old_value, new_value)
    if type == TransactionType.SET:
        return _moved(field or "Field", old_value, new_value)
    if type == TransactionType.CUSTOM_FIELD:
        return _moved(f"Custom field {field or ''}".strip(), old_value, new_value)
    return type


def _moved(what: str, old_value: str | None, new_value: str | None) -> str:
    """ "X changed from A to B", with the ends it has."""
    old = old_value if old_value not in (None, "") else NO_VALUE
    new = new_value if new_value not in (None, "") else NO_VALUE
    return f"{what} changed from {old} to {new}"
