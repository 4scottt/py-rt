"""The ticket screens: create, display, history, update, basics (plan §8, §11).

The gates are plan §8's and FP R06's: ``CreateTicket`` on the chosen queue to
create, ``ShowTicket`` on the ticket's queue to read it,
``ReplyToTicket``/``CommentOnTicket`` for the update page's radio, and
``ModifyTicket`` for basics and for any status change — each checked against
the ticket's own principal set, so a grant to the Owner or the Requestor role
reaches the person it was meant for.

The queue's custom fields ride these same three screens (FP F03-F07): the
``cf-{id}`` inputs come in through :data:`pyrt.customfields.router.PostedValues`
and the values are written inside the create's and the basics save's own unit
of work. ``ModifyCustomField`` on the queue renders and saves them,
``SeeCustomField`` shows them; without either the rows are not sent to the
template at all, so a form nobody may submit is never drawn.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Annotated, Final

from fastapi import APIRouter, Form, HTTPException, Query, Request
from starlette.responses import RedirectResponse, Response

from pyrt.acl import Forbidden, Principals, has_right, ticket_principals
from pyrt.customfields import service as customfields
from pyrt.customfields.router import PostedValues
from pyrt.customfields.service import FieldValue
from pyrt.db.models import Queue
from pyrt.queues.service import get_queue, may_create_in, queues_for_create
from pyrt.tickets import lifecycle, service, update
from pyrt.tickets.service import TicketForm
from pyrt.tickets.update import BasicsForm, UpdateForm
from pyrt.web.deps import ActorPrincipals, Config, Db, SignedIn
from pyrt.web.errors import SEE_OTHER
from pyrt.web.templating import absolute_url, render

router = APIRouter()

#: Plan §8's headings and buttons, kept verbatim: the walk expects them.
CREATE_HEADING: Final = "Create a ticket"
HISTORY_HEADING: Final = "History"
UPDATE_HEADING: Final = "Update"
BASICS_HEADING: Final = "Basics"
CREATE_BUTTON: Final = "Create"
UPDATE_BUTTON: Final = "Update Ticket"
SAVE_BUTTON: Final = "Save Changes"

#: What the ticket page's Custom Fields box says when the queue carries none.
NO_CUSTOM_FIELDS: Final = "No custom fields"

#: The action links under the title bar (plan §8's Display row).
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
    return _create_form(request, db, held, allowed, chosen, form, closed)


@router.post("/ticket/new", include_in_schema=False)
def ticket_create(
    request: Request,
    db: Db,
    held: ActorPrincipals,
    settings: Config,
    actor: SignedIn,
    posted_values: PostedValues,
    queue: Annotated[str, Form()] = "",
    status: Annotated[str, Form()] = "new",
    owner: Annotated[str, Form()] = "",
    requestors: Annotated[str, Form()] = "",
    subject: Annotated[str, Form()] = "",
    content: Annotated[str, Form()] = "",
) -> Response:
    """FP T01, T02, F04: create the ticket, or come back with the message."""
    chosen, allowed, closed = _chosen_queue(request, db, held, _int(queue))
    form = TicketForm(
        queue=chosen.id,
        status=status.strip(),
        owner=_int(owner, _nobody_id(db)),
        requestors=requestors.strip(),
        subject=subject.strip(),
        content=content,
    )
    # FP F05: the values are written only by someone the form was drawn for;
    # anything else a POST carries is dropped without a word, as an unasked-for
    # field on any other form would be.
    values = posted_values if _may_set_custom_fields(request, db, held, chosen.id) else {}
    problem = closed or service.refusal(db, form, chosen, request)
    if problem:
        return _create_form(request, db, held, allowed, chosen, form, problem, values)
    ticket = service.create_ticket(db, settings, actor, chosen, form, values)
    return RedirectResponse(absolute_url(settings, f"/ticket/{ticket.id}"), status_code=SEE_OTHER)


@router.get("/ticket/{ticket_id}", include_in_schema=False)
def ticket_display(request: Request, db: Db, actor: SignedIn, ticket_id: int) -> Response:
    """FP T03, T11, T12: the ticket page, its five boxes and its history."""
    view, held, entries = _ticket_page(request, db, actor, ticket_id)
    ticket = view.ticket
    may_see = has_right(db, held, customfields.SEE_CUSTOM_FIELD, ticket.queue_id, request)
    return render(
        request,
        "ticket/display.html",
        {
            "page_title": f"#{ticket.id}: {ticket.subject}",
            "ticket": ticket,
            "queue": view.queue,
            "owner": view.owner,
            "requestors": service.requestors(db, ticket.id),
            "entries": entries,
            "actions": ACTIONS,
            "not_set": service.NOT_SET,
            # FP F05, F07: the box's rows, then the values of fields this
            # queue no longer carries; both behind SeeCustomField.
            "may_see_custom_fields": may_see,
            "custom_fields": (
                customfields.ticket_values(db, ticket.id, ticket.queue_id) if may_see else []
            ),
            "custom_field_strays": (
                customfields.stray_values(db, ticket.id, ticket.queue_id) if may_see else []
            ),
            "custom_fields_not_shown": customfields.NOT_SHOWN,
            "custom_fields_not_applied": customfields.NOT_APPLIED,
            "custom_fields_empty": NO_CUSTOM_FIELDS,
        },
    )


@router.get("/ticket/{ticket_id}/history", include_in_schema=False)
def ticket_history(request: Request, db: Db, actor: SignedIn, ticket_id: int) -> Response:
    """FP T04: the history alone, behind the same gate as the page."""
    view, _, entries = _ticket_page(request, db, actor, ticket_id)
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


@router.get("/ticket/{ticket_id}/update", include_in_schema=False)
def ticket_update_form(
    request: Request,
    db: Db,
    actor: SignedIn,
    ticket_id: int,
    action: Annotated[str, Query()] = update.RESPOND,
    status: Annotated[str, Query()] = "",
) -> Response:
    """FP T13: the update form, its radio and its status from the link."""
    view, held = _show_ticket(request, db, actor, ticket_id)
    rights = _update_rights(request, db, held, view.ticket.queue_id)
    reply, comment, modify = rights
    if not (reply or comment or modify):
        raise Forbidden(update.REPLY_TO_TICKET, view.ticket.queue_id)
    form = UpdateForm(
        update_type=_offered_type(action, reply, comment),
        status=_offered_status(view.ticket.status, status, modify),
    )
    return _update_form(request, view, form, rights, "")


@router.post("/ticket/{ticket_id}/update", include_in_schema=False)
def ticket_update(
    request: Request,
    db: Db,
    actor: SignedIn,
    settings: Config,
    ticket_id: int,
    update_type: Annotated[str, Form(alias="UpdateType")] = update.RESPOND,
    status: Annotated[str, Form()] = "",
    content: Annotated[str, Form()] = "",
) -> Response:
    """FP T05-T08: the reply or the comment, and the status the form chose."""
    view, held = _show_ticket(request, db, actor, ticket_id)
    ticket = view.ticket
    queue_id = ticket.queue_id
    rights = _update_rights(request, db, held, queue_id)
    reply, comment, modify = rights
    if not (reply or comment or modify):
        raise Forbidden(update.REPLY_TO_TICKET, queue_id)

    form = UpdateForm(update_type=update_type, status=status, content=content).cleaned()
    if form.content.strip():
        if form.update_type == update.COMMENT and not comment:
            raise Forbidden(update.COMMENT_ON_TICKET, queue_id)
        if form.update_type == update.RESPOND and not reply:
            raise Forbidden(update.REPLY_TO_TICKET, queue_id)
    if form.status and form.status != ticket.status and not modify:
        raise Forbidden(update.MODIFY_TICKET, queue_id)

    problem = update.update_ticket(db, settings, ticket, actor, form)
    if problem:
        form.status = _offered_status(ticket.status, form.status, modify)
        return _update_form(request, view, form, rights, problem)
    return RedirectResponse(absolute_url(settings, f"/ticket/{ticket.id}"), status_code=SEE_OTHER)


@router.get("/ticket/{ticket_id}/basics", include_in_schema=False)
def ticket_basics_form(
    request: Request,
    db: Db,
    actor: SignedIn,
    held_actor: ActorPrincipals,
    ticket_id: int,
) -> Response:
    """FP T09, T10: the basics form, behind ``ModifyTicket`` on the queue."""
    view, held = _modify_ticket(request, db, actor, ticket_id)
    return _basics_form(request, db, held, held_actor, view, BasicsForm.of(view.ticket), "")


@router.post("/ticket/{ticket_id}/basics", include_in_schema=False)
def ticket_basics_save(
    request: Request,
    db: Db,
    actor: SignedIn,
    held_actor: ActorPrincipals,
    settings: Config,
    posted_values: PostedValues,
    ticket_id: int,
    subject: Annotated[str, Form()] = "",
    queue: Annotated[str, Form()] = "",
    status: Annotated[str, Form()] = "",
    owner: Annotated[str, Form()] = "",
    priority: Annotated[str, Form()] = "",
) -> Response:
    """FP T09, T10, F04: each changed field its own transaction."""
    view, held = _modify_ticket(request, db, actor, ticket_id)
    ticket = view.ticket
    form = BasicsForm(
        subject=subject.strip(),
        queue=_int(queue, ticket.queue_id),
        status=status.strip() or ticket.status,
        owner=_int(owner, ticket.owner_id),
        priority=priority.strip(),
    )
    # FP F05: ModifyCustomField on the queue is what makes the inputs real;
    # without it the page showed text and a posted value is dropped.
    values = posted_values if _may_set_custom_fields(request, db, held, ticket.queue_id) else {}
    queues = update.queue_choices(db, held_actor, ticket.queue_id, request)
    owners = update.owner_choices(db, form.queue or ticket.queue_id, ticket.owner_id, request)
    problem = update.basics_refusal(ticket, form, queues, owners)
    if not problem:
        problem = update.save_basics(db, settings, ticket, actor, form, values)
    if problem:
        return _basics_form(
            request, db, held, held_actor, view, form, problem, queues=queues, values=values
        )
    return RedirectResponse(absolute_url(settings, f"/ticket/{ticket.id}"), status_code=SEE_OTHER)


def _ticket_page(
    request: Request, db: Db, actor: SignedIn, ticket_id: int
) -> tuple[service.TicketView, Principals, list[service.HistoryEntry]]:
    """Load the ticket behind ``ShowTicket``, with the history it may show.

    The principal set comes back with it: the ticket page asks two more
    rights of it (``CommentOnTicket`` for the comment bodies,
    ``SeeCustomField`` for the Custom Fields box), and asking them of the
    same set is what keeps a grant to the Owner or Requestor role working.
    """
    view, held = _show_ticket(request, db, actor, ticket_id)
    may_comment = has_right(db, held, service.COMMENT_ON_TICKET, view.ticket.queue_id, request)
    return view, held, service.history(db, view.ticket.id, show_comments=may_comment)


def _show_ticket(
    request: Request, db: Db, actor: SignedIn, ticket_id: int
) -> tuple[service.TicketView, Principals]:
    """The ticket behind ``ShowTicket``, with the principal set it was read with.

    The set carries the ticket's roles (Owner, Requestor), so every right the
    pages ask about afterwards is asked of the same set.
    """
    view = service.load_ticket(db, ticket_id)
    if view is None:  # FP T11: an unknown id is a page that is not there
        raise HTTPException(status_code=404)
    held = ticket_principals(db, actor, view.ticket, request)
    queue_id = view.ticket.queue_id
    if not has_right(db, held, service.SHOW_TICKET, queue_id, request):
        raise Forbidden(service.SHOW_TICKET, queue_id)
    return view, held


def _modify_ticket(
    request: Request, db: Db, actor: SignedIn, ticket_id: int
) -> tuple[service.TicketView, Principals]:
    """The same, and ``ModifyTicket`` as well: basics is a form to be saved.

    The GET is gated like the POST (M1's rule on the queue modify page): a
    form nobody may submit would be a trap.
    """
    view, held = _show_ticket(request, db, actor, ticket_id)
    if not update.may_modify(db, held, view.ticket.queue_id, request):
        raise Forbidden(update.MODIFY_TICKET, view.ticket.queue_id)
    return view, held


def _update_rights(
    request: Request, db: Db, held: Principals, queue_id: int
) -> tuple[bool, bool, bool]:
    """FP R06: may this viewer reply, comment, and change the status."""
    return (
        update.may_reply(db, held, queue_id, request),
        update.may_comment(db, held, queue_id, request),
        update.may_modify(db, held, queue_id, request),
    )


def _offered_type(action: str, reply: bool, comment: bool) -> str:
    """The radio's selection: the link's, narrowed to what the viewer may do."""
    wanted = action.strip() if action.strip() in update.UPDATE_TYPES else update.RESPOND
    if wanted == update.RESPOND and not reply:
        return update.COMMENT if comment else ""
    if wanted == update.COMMENT and not comment:
        return update.RESPOND if reply else ""
    return wanted


def _offered_status(current: str, wanted: str, modify: bool) -> str:
    """The status select's selection: ``?status=`` when it is one it offers."""
    if not modify:
        return ""
    return wanted.strip() if wanted.strip() in lifecycle.choices(current) else current


def _update_form(
    request: Request,
    view: service.TicketView,
    form: UpdateForm,
    rights: tuple[bool, bool, bool],
    message: str,
) -> Response:
    reply, comment, modify = rights
    return render(
        request,
        "ticket/update.html",
        {
            "page_title": f"Update ticket #{view.ticket.id}: {view.ticket.subject}",
            "titlebox": UPDATE_HEADING,
            "ticket": view.ticket,
            "queue": view.queue,
            "actions": ACTIONS,
            "form": form,
            "may_reply": reply,
            "may_comment": comment,
            "statuses": lifecycle.choices(view.ticket.status) if modify else (),
            "message": message,
            "button": UPDATE_BUTTON,
        },
    )


def _basics_form(
    request: Request,
    db: Db,
    held: Principals,
    held_actor: Principals,
    view: service.TicketView,
    form: BasicsForm,
    message: str,
    queues: list[Queue] | None = None,
    values: dict[int, str] | None = None,
) -> Response:
    ticket = view.ticket
    if queues is None:
        queues = update.queue_choices(db, held_actor, ticket.queue_id, request)
    may_set = _may_set_custom_fields(request, db, held, ticket.queue_id)
    may_see = may_set or has_right(
        db, held, customfields.SEE_CUSTOM_FIELD, ticket.queue_id, request
    )
    rows = customfields.ticket_values(db, ticket.id, ticket.queue_id) if may_see else []
    return render(
        request,
        "ticket/basics.html",
        {
            "page_title": f"Modify ticket #{ticket.id}: {ticket.subject}",
            "titlebox": BASICS_HEADING,
            "ticket": ticket,
            "queue": view.queue,
            "actions": ACTIONS,
            "form": form,
            "queues": queues,
            "owners": update.owner_choices(db, ticket.queue_id, ticket.owner_id, request),
            "statuses": lifecycle.choices(ticket.status),
            "message": message,
            "button": SAVE_BUTTON,
            # FP F04, F05: inputs with ModifyCustomField, text with
            # SeeCustomField alone, nothing without either.
            "custom_fields": _with_posted(rows, values),
            "may_modify_custom_fields": may_set,
        },
    )


def _chosen_queue(
    request: Request, db: Db, held: ActorPrincipals, queue_id: int
) -> tuple[Queue, list[Queue], str]:
    """The queue the form is for, the queues its select offers, a refusal.

    The one asked for, or the first the user may create in ("may create in"
    being ``CreateTicket`` and ``SeeQueue``, FP R06). A queue the user may
    not create in is the denial page, not a form whose submit would be
    refused (the rule M1 set on the queue modify page); a queue that is only
    *disabled* is a message on the form instead, because the person did
    nothing wrong and another queue is there to pick.
    """
    allowed = queues_for_create(db, held, request)
    if queue_id:
        for candidate in allowed:
            if candidate.id == queue_id:
                return candidate, allowed, ""
        wanted = get_queue(db, queue_id)
        if wanted is None:  # FP T11's rule for a queue: no row, no page
            raise HTTPException(status_code=404)
        if not allowed or not may_create_in(db, held, wanted.id, request):
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
    held: Principals,
    allowed: list[Queue],
    chosen: Queue,
    form: TicketForm,
    message: str,
    values: dict[int, str] | None = None,
) -> Response:
    rows: list[FieldValue] = []
    if _may_set_custom_fields(request, db, held, chosen.id):
        rows = _with_posted(customfields.blank_values(db, chosen.id), values)
    return render(
        request,
        "ticket/new.html",
        {
            "page_title": f"{CREATE_HEADING} in {chosen.name}",
            "queues": allowed,
            "statuses": service.NEW_TICKET_STATUSES,
            "owners": service.owner_choices(db, chosen.id, request=request),
            "queue": chosen,
            "form": form,
            "message": message,
            "button": CREATE_BUTTON,
            # FP F03: the chosen queue's fields, in sort order, as this form's
            # last rows. A refused POST comes back with what was typed in them.
            "custom_fields": rows,
        },
    )


def _may_set_custom_fields(request: Request, db: Db, held: Principals, queue_id: int) -> bool:
    """FP F05: ``ModifyCustomField`` on the queue sets a value; ``SuperUser`` too."""
    return has_right(db, held, customfields.MODIFY_CUSTOM_FIELD, queue_id, request)


def _with_posted(rows: list[FieldValue], values: dict[int, str] | None) -> list[FieldValue]:
    """The rows as the form sent them back, so a refusal keeps what was typed."""
    if not values:
        return rows
    return [replace(row, value=values.get(row.id, row.value)) for row in rows]
