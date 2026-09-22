"""The admin index and the queue pages (plan §8, §11; FP Q01-Q04).

Three screens over two templates: the Select list with its Create tab, the
create form, and the modify page with the four sub-tabs of FP Q04 as links
to pages. The gates are plan §8's: ``ShowConfigTab`` for the index,
``AdminQueue`` globally to create, ``AdminQueue`` on the queue to modify,
and ``SeeQueue`` or ``AdminQueue`` to be shown the list at all.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from starlette.responses import RedirectResponse, Response

from pyrt.acl import Forbidden, require_right
from pyrt.db.models import Queue
from pyrt.queues import service
from pyrt.queues.service import QueueFields
from pyrt.web.deps import ActorPrincipals, Config, Db
from pyrt.web.errors import SEE_OTHER
from pyrt.web.templating import SHOW_CONFIG_TAB, absolute_url, render

router = APIRouter()

#: Plan §8's headings and buttons, kept verbatim: the walk expects them.
ADMIN_HEADING: Final = "Admin"
QUEUES_HEADING: Final = "Queues"
CREATE_HEADING: Final = "Create a queue"
MODIFY_HEADING: Final = "Modify a queue"
CREATE_BUTTON: Final = "Create"
SAVE_BUTTON: Final = "Save Changes"

#: The sections the index links (plan §8's Admin menu).
ADMIN_SECTIONS: Final[tuple[tuple[str, str], ...]] = (
    ("Queues", "/admin/queues"),
    ("Users", "/admin/users"),
    ("Groups", "/admin/groups"),
    ("Custom Fields", "/admin/custom-fields"),
    ("Global Group Rights", "/admin/global/group-rights"),
    ("Global User Rights", "/admin/global/user-rights"),
)

#: What ``?msg=`` may say after a redirect; anything else is ignored.
MESSAGES: Final[dict[str, str]] = {
    "created": "Queue created",
    "saved": "Queue updated",
}


def _message(msg: str) -> str:
    return MESSAGES.get(msg, "")


def _posted(
    name: str,
    description: str,
    subject_tag: str,
    correspond_address: str,
    comment_address: str,
    enabled: str,
) -> QueueFields:
    """The Basics form as it came in, stripped; an absent checkbox is off."""
    return QueueFields(
        name=name.strip(),
        description=description.strip(),
        subject_tag=subject_tag.strip(),
        correspond_address=correspond_address.strip(),
        comment_address=comment_address.strip(),
        enabled=bool(enabled.strip()),
    )


def _queue_or_404(db: Db, queue_id: int) -> Queue:
    queue = service.get_queue(db, queue_id)
    if queue is None:
        raise HTTPException(status_code=404)
    return queue


@router.get(
    "/admin/",
    include_in_schema=False,
    dependencies=[Depends(require_right(SHOW_CONFIG_TAB))],
)
def admin_index(request: Request) -> Response:
    """The index of plan §8: the four admin sections as links."""
    return render(
        request,
        "admin/index.html",
        {"page_title": ADMIN_HEADING, "sections": ADMIN_SECTIONS},
    )


@router.get("/admin/queues", include_in_schema=False)
def queues_index(
    request: Request,
    db: Db,
    held: ActorPrincipals,
    disabled: Annotated[str, Query()] = "",
) -> Response:
    """FP Q01: the Select list; disabled queues only with ``?disabled=1``."""
    include_disabled = disabled.strip() not in ("", "0")
    queues = service.queues_for_list(db, held, request, include_disabled=include_disabled)
    return render(
        request,
        "admin/queues/index.html",
        {
            "page_title": QUEUES_HEADING,
            "queues": queues,
            "include_disabled": include_disabled,
        },
    )


@router.get(
    "/admin/queues/new",
    include_in_schema=False,
    dependencies=[Depends(require_right(service.ADMIN_QUEUE))],
)
def queue_new(request: Request) -> Response:
    """The create form, Enabled checked (FP Q02)."""
    return _create_form(request, QueueFields(), "")


@router.post(
    "/admin/queues/new",
    include_in_schema=False,
    dependencies=[Depends(require_right(service.ADMIN_QUEUE))],
)
def queue_create(
    request: Request,
    db: Db,
    settings: Config,
    name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    subject_tag: Annotated[str, Form()] = "",
    correspond_address: Annotated[str, Form()] = "",
    comment_address: Annotated[str, Form()] = "",
    enabled: Annotated[str, Form()] = "",
) -> Response:
    """FP Q02: create, or come back with the message and the values kept."""
    fields = _posted(name, description, subject_tag, correspond_address, comment_address, enabled)
    problem = service.name_problem(db, fields)
    if problem:
        return _create_form(request, fields, problem)
    queue = service.create_queue(db, fields)
    return RedirectResponse(
        absolute_url(settings, f"/admin/queues/{queue.id}?msg=created"),
        status_code=SEE_OTHER,
    )


@router.get("/admin/queues/{queue_id}", include_in_schema=False)
def queue_modify(
    request: Request,
    db: Db,
    held: ActorPrincipals,
    queue_id: int,
    msg: Annotated[str, Query()] = "",
) -> Response:
    """FP Q03, Q04: the Basics form and the four sub-tabs of that queue."""
    queue = _queue_or_404(db, queue_id)
    if not service.may_administer(db, held, queue.id, request):
        raise Forbidden(service.ADMIN_QUEUE, queue.id)
    return _modify_form(request, queue, QueueFields.from_queue(queue), _message(msg))


@router.post("/admin/queues/{queue_id}", include_in_schema=False)
def queue_save(
    request: Request,
    db: Db,
    held: ActorPrincipals,
    settings: Config,
    queue_id: int,
    name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    subject_tag: Annotated[str, Form()] = "",
    correspond_address: Annotated[str, Form()] = "",
    comment_address: Annotated[str, Form()] = "",
    enabled: Annotated[str, Form()] = "",
) -> Response:
    """FP Q03: save the basics; disabling is a save, never a delete."""
    queue = _queue_or_404(db, queue_id)
    if not service.may_administer(db, held, queue.id, request):
        raise Forbidden(service.ADMIN_QUEUE, queue.id)
    fields = _posted(name, description, subject_tag, correspond_address, comment_address, enabled)
    problem = service.name_problem(db, fields, queue_id=queue.id)
    if problem:
        return _modify_form(request, queue, fields, problem)
    service.save_queue(db, queue, fields)
    return RedirectResponse(
        absolute_url(settings, f"/admin/queues/{queue.id}?msg=saved"),
        status_code=SEE_OTHER,
    )


def _create_form(request: Request, fields: QueueFields, message: str) -> Response:
    return render(
        request,
        "admin/queues/new.html",
        {
            "page_title": CREATE_HEADING,
            "fields": fields,
            "message": message,
            "message_ok": message in MESSAGES.values(),
            "button": CREATE_BUTTON,
        },
    )


def _modify_form(request: Request, queue: Queue, fields: QueueFields, message: str) -> Response:
    return render(
        request,
        "admin/queues/modify.html",
        {
            "page_title": MODIFY_HEADING,
            "queue": queue,
            "fields": fields,
            "message": message,
            "message_ok": message in MESSAGES.values(),
            "button": SAVE_BUTTON,
        },
    )
