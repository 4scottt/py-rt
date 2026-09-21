"""The ticket lifecycle (plan §10): which status may follow which.

Six statuses and a fixed map of transitions, with the two dates the moves
carry: ``started`` on the first leave from ``new``, ``resolved`` on entering
``resolved`` and cleared on leaving it.

Pure: no session, no request, no template. The update page and the basics
page offer :func:`choices` and re-check the answer with :func:`refusal`, so
a status the select never showed cannot arrive through a hand-made POST.
"""

from __future__ import annotations

import datetime as dt
from typing import Final

from pyrt.db.models import STATUSES, Ticket, User

#: The statuses of plan §8's vocabulary, named so a caller reads as the plan
#: does. ``STATUSES`` (the model's) is the same six, in the same order.
NEW: Final = "new"
OPEN: Final = "open"
STALLED: Final = "stalled"
RESOLVED: Final = "resolved"
REJECTED: Final = "rejected"
DELETED: Final = "deleted"

#: Plan §10's lifecycle: ``new → open``, ``open ↔ stalled``, any active one to
#: ``resolved``, ``rejected`` or ``deleted``, and any inactive one back to
#: ``open`` (the reopen). The current status is not in its own list; the
#: select puts it first through :func:`choices`.
TRANSITIONS: Final[dict[str, tuple[str, ...]]] = {
    NEW: (OPEN, STALLED, RESOLVED, REJECTED, DELETED),
    OPEN: (STALLED, RESOLVED, REJECTED, DELETED),
    STALLED: (OPEN, RESOLVED, REJECTED, DELETED),
    RESOLVED: (OPEN,),
    REJECTED: (OPEN,),
    DELETED: (OPEN,),
}

#: The refusal a status nobody defined comes back with.
UNKNOWN_STATUS: Final = "That is not a status a ticket can have"


class BadTransition(Exception):
    """A status change the lifecycle does not allow.

    :func:`apply_status` raises it; a caller that asked :func:`refusal`
    first never sees one.
    """

    def __init__(self, message: str, old: str = "", new: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.old = old
        self.new = new


def transitions(status: str) -> tuple[str, ...]:
    """The statuses reachable from ``status``, itself excluded."""
    return TRANSITIONS.get(status, ())


def choices(status: str) -> tuple[str, ...]:
    """The ``Status`` select: the current status first, then where it may go.

    A status the map does not know (an imported row, say) is still offered as
    itself, so the select never silently rewrites the ticket it is showing.
    """
    if not status:
        return ()
    return (status, *transitions(status))


def may_change(old: str, new: str) -> bool:
    """Whether ``old`` may become ``new`` (a move, not a no-op)."""
    return new in transitions(old)


def refusal(old: str, new: str) -> str:
    """Why that status change cannot be made, or ``""`` when it can.

    Leaving the status where it is is not a refusal: the caller decides
    whether a form that changed nothing is worth a message.
    """
    if new not in STATUSES:
        return UNKNOWN_STATUS
    if new == old or may_change(old, new):
        return ""
    return f"A ticket cannot go from {old} to {new}"


def apply_status(ticket: Ticket, new_status: str, actor: User, now: dt.datetime) -> None:
    """Move ``ticket`` to ``new_status``, with the dates plan §10 asks for.

    ``started`` is set on the first leave from ``new`` when it is still
    unset; ``resolved`` is set on entering ``resolved`` and cleared on
    leaving it. The transaction is the caller's: this touches the ticket
    row and nothing else.
    """
    old = ticket.status
    problem = refusal(old, new_status)
    if problem:
        raise BadTransition(problem, old, new_status)
    if new_status == old:
        return
    if old == NEW and ticket.started is None:
        ticket.started = now
    ticket.status = new_status
    if new_status == RESOLVED:
        ticket.resolved = now
    elif old == RESOLVED:
        ticket.resolved = None
    ticket.last_updated = now
    ticket.last_updated_by = actor.id
