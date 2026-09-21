"""The notification seam (plan §10's scrips, FP M07).

The tickets package knows *when* something happened; it must not know that
mail exists. So it announces: a write fires the list of that name, and the
mail package appends its senders to the list at start-up.

A hook runs after the commit, on rows already written, and its failure is
logged and swallowed: a notification that cannot be sent must not undo a
ticket that is already in the database.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Final

from sqlalchemy.orm import Session

from pyrt.config import Settings
from pyrt.db.models import Ticket, Transaction, User

log = logging.getLogger(__name__)

#: What a listener is handed: the session, the settings, the rows in hand.
Hook = Callable[[Session, Settings, Ticket, Transaction, User], None]

#: The four moments. A package appends to the list it cares about::
#:
#:     from pyrt.tickets import hooks
#:     hooks.created.append(notify_requestor_of_create)
created: list[Hook] = []
corresponded: list[Hook] = []
commented: list[Hook] = []
status_changed: list[Hook] = []

CREATED: Final = "created"
CORRESPONDED: Final = "corresponded"
COMMENTED: Final = "commented"
STATUS_CHANGED: Final = "status_changed"

#: The lists by name, the same objects a package appends to.
_LISTS: Final[dict[str, list[Hook]]] = {
    CREATED: created,
    CORRESPONDED: corresponded,
    COMMENTED: commented,
    STATUS_CHANGED: status_changed,
}


def listeners(name: str) -> list[Hook]:
    """The listeners of that moment, in the order they were registered."""
    try:
        return _LISTS[name]
    except KeyError:  # pragma: no cover - a typo in a caller, not a state
        raise ValueError(f"no such hook: {name}") from None


def fire(
    name: str,
    db: Session,
    settings: Settings,
    ticket: Ticket,
    transaction: Transaction,
    actor: User,
) -> None:
    """Run every listener of ``name``; a failure is logged, never raised."""
    for hook in listeners(name):
        try:
            hook(db, settings, ticket, transaction, actor)
        except Exception as exc:  # a notification never fails the write
            log.warning(
                "notification failed",
                extra={"hook": name, "ticket": ticket.id, "error": str(exc)},
            )
