"""The ticket screens: create, display, history (plan §8, §11).

Three routes and one POST. The gates are plan §8's: ``CreateTicket`` on the
chosen queue to create, ``ShowTicket`` on the ticket's queue to read it —
checked against the ticket's own principal set, so a grant to the Owner or
the Requestor role reaches the person it was meant for.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Form, HTTPException, Query, Request
from starlette.responses import RedirectResponse, Response

from pyrt.acl import Forbidden, has_right, ticket_principals
from pyrt.db.models import Queue
from pyrt.queues.service import get_queue, queues_for_create
from pyrt.tickets import service
from pyrt.tickets.service import TicketForm
from pyrt.web.deps import ActorPrincipals, Config, Db, SignedIn
from pyrt.web.errors import SEE_OTHER
from pyrt.web.templating import absolute_url, render

router = APIRouter()

#: Plan §8's headings and buttons, kept verbatim: the walk expects them.
CREATE_HEADING: Final = "Create a ticket"
HISTORY_HEADING: Final = "History"
CREATE_BUTTON: Final = "Create"

#: The action links under the title bar (plan §8's Display row). The update
#: and basics pages are the next package's; the links are theirs already.
ACTIONS: Final[tuple[tuple[str, str], ...]] = (
    ("Reply", "/ticket/{id}/update?action=respond"),
    ("Comment", "/ticket/{id}/update?action=comment"),
    ("Resolve", "/ticket/{id}/update?action=respond&status=resolved"),
    ("Basics", "/ticket/{id}/basics"),
    ("History", "/ticket/{id}/history"),
)


def _int(raw: str, default: int = 0) -> int:
    """A form or query field that should be an integer, never a 422."""
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        return default


@router.get("/ticket/new", include_in_schema=False)
def ticket_new(
    request: Request,
    db: Db,
    held: ActorPrincipals,
    queue: Annotated[str, Query()] = "",
) -> Response:
    """FP T01: the create form, its queue preselected from the link."""
    chosen, allowed, closed = _chosen_queue(request, db, held, _int(queue))
    form = TicketForm(queue=chosen.id, owner=_nobody_id(db))
    return _create_form(request, db, allowed, chosen, form, closed)


@router.post("/ticket/new", include_in_schema=False)
def ticket_create(
    request: Request,
    db: Db,
    held: ActorPrincipals,
    settings: Config,
    actor: SignedIn,
    queue: Annotated[str, Form()] = "",
    status: Annotated[str, Form()] = "new",
    owner: Annotated[str, Form()] = "",
    requestors: Annotated[str, Form()] = "",
    subject: Annotated[str, Form()] = "",
    content: Annotated[str, Form()] = "",
) -> Response:
    """FP T01, T02: create the ticket, or come back with the message."""
    chosen, allowed, closed = _chosen_queue(request, db, held, _int(queue))
    form = TicketForm(
        queue=chosen.id,
        status=status.strip(),
        owner=_int(owner, _nobody_id(db)),
        requestors=requestors.strip(),
        subject=subject.strip(),
        content=content,
    )
    problem = closed or service.refusal(db, form, chosen)
    if problem:
        return _create_form(request, db, allowed, chosen, form, problem)
    ticket = service.create_ticket(db, settings, actor, chosen, form)
    return RedirectResponse(absolute_url(settings, f"/ticket/{ticket.id}"), status_code=SEE_OTHER)


@router.get("/ticket/{ticket_id}", include_in_schema=False)
def ticket_display(request: Request, db: Db, actor: SignedIn, ticket_id: int) -> Response:
    """FP T03, T11, T12: the ticket page, its five boxes and its history."""
    view, entries = _ticket_page(request, db, actor, ticket_id)
    return render(
        request,
        "ticket/display.html",
        {
            "page_title": f"#{view.ticket.id}: {view.ticket.subject}",
            "ticket": view.ticket,
            "queue": view.queue,
            "owner": view.owner,
            "requestors": service.requestors(db, view.ticket.id),
            "entries": entries,
            "actions": ACTIONS,
            "not_set": service.NOT_SET,
        },
    )


@router.get("/ticket/{ticket_id}/history", include_in_schema=False)
def ticket_history(request: Request, db: Db, actor: SignedIn, ticket_id: int) -> Response:
    """FP T04: the history alone, behind the same gate as the page."""
    view, entries = _ticket_page(request, db, actor, ticket_id)
    return render(
        request,
        "ticket/history.html",
        {
            "page_title": f"#{view.ticket.id}: {view.ticket.subject}",
            "ticket": view.ticket,
            "entries": entries,
            "actions": ACTIONS,
        },
    )


def _ticket_page(
    request: Request, db: Db, actor: SignedIn, ticket_id: int
) -> tuple[service.TicketView, list[service.HistoryEntry]]:
    """Load the ticket behind ``ShowTicket``, with the history it may show."""
    view = service.load_ticket(db, ticket_id)
    if view is None:  # FP T11: an unknown id is a page that is not there
        raise HTTPException(status_code=404)
    held = ticket_principals(db, actor, view.ticket, request)
    queue_id = view.ticket.queue_id
    if not has_right(db, held, service.SHOW_TICKET, queue_id, request):
        raise Forbidden(service.SHOW_TICKET, queue_id)
    may_comment = has_right(db, held, service.COMMENT_ON_TICKET, queue_id, request)
    return view, service.history(db, view.ticket.id, show_comments=may_comment)


def _chosen_queue(
    request: Request, db: Db, held: ActorPrincipals, queue_id: int
) -> tuple[Queue, list[Queue], str]:
    """The queue the form is for, the queues its select offers, a refusal.

    The one asked for, or the first the user may create in. A queue the
    user may not create in is the denial page, not a form whose submit
    would be refused (the rule M1 set on the queue modify page); a queue
    that is only *disabled* is a message on the form instead, because the
    person did nothing wrong and another queue is there to pick.
    """
    allowed = queues_for_create(db, held, request)
    if queue_id:
        for candidate in allowed:
            if candidate.id == queue_id:
                return candidate, allowed, ""
        wanted = get_queue(db, queue_id)
        if wanted is None:  # FP T11's rule for a queue: no row, no page
            raise HTTPException(status_code=404)
        if not allowed or not has_right(db, held, service.CREATE_TICKET, wanted.id, request):
            raise Forbidden(service.CREATE_TICKET, queue_id)
        return allowed[0], allowed, service.QUEUE_DISABLED
    if not allowed:
        raise Forbidden(service.CREATE_TICKET)
    return allowed[0], allowed, ""


def _nobody_id(db: Db) -> int:
    """Nobody's id, the owner select's default (plan §10's unowned owner)."""
    unowned = service.nobody(db)
    return 0 if unowned is None else unowned.id


def _create_form(
    request: Request,
    db: Db,
    allowed: list[Queue],
    chosen: Queue,
    form: TicketForm,
    message: str,
) -> Response:
    return render(
        request,
        "ticket/new.html",
        {
            "page_title": f"{CREATE_HEADING} in {chosen.name}",
            "queues": allowed,
            "statuses": service.NEW_TICKET_STATUSES,
            "owners": service.owner_choices(db),
            "queue": chosen,
            "form": form,
            "message": message,
            "button": CREATE_BUTTON,
        },
    )
