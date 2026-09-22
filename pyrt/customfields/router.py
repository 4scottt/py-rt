"""The custom field screens of plan §8 (FP F01, F02).

Five routes over four templates: the Select list with its Create tab, the
create form, the modify page with its two sub-tabs (Basics and Applies to),
the field's own "Applies to" page, and the same pairing seen from the other
end — a queue's ``Custom Fields`` sub-tab. The goal says "Create a ticket
custom field called Printer model … and apply it to the Support queue", and
either page does the applying: both write ``queue_custom_fields``.

The gates are plan §8's: ``AdminCustomField`` (global-only) for the field's
own pages, ``AdminQueue`` on the queue for the queue's page — a queue's
administrator says which of the fields their tickets carry, and only a
global administrator makes or unmakes a field.

The "Applies to" checkboxes are named after the row they stand for, so the
POST cannot declare them as parameters: :data:`pyrt.rights.router.PostedNames`
reads the form once and hands the handler the names that came in, and the
service decides what that set means.
"""

from __future__ import annotations

import logging
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Form, HTTPException, Path, Query, Request
from starlette.datastructures import UploadFile
from starlette.responses import RedirectResponse, Response

from pyrt.acl import Forbidden, has_right, require_right
from pyrt.customfields import service
from pyrt.customfields.service import FieldForm
from pyrt.db.models import CustomField, Queue
from pyrt.queues.service import ADMIN_QUEUE, get_queue
from pyrt.rights.router import PostedNames
from pyrt.web.deps import ActorPrincipals, Config, Db
from pyrt.web.errors import SEE_OTHER
from pyrt.web.templating import absolute_url, render

log = logging.getLogger(__name__)

router = APIRouter()

#: Plan §8's headings and buttons, kept verbatim: the goal text and the walk
#: both name them.
LIST_HEADING: Final = "Custom Fields"
CREATE_HEADING: Final = "Create a custom field"
MODIFY_HEADING: Final = "Modify a custom field"
APPLIES_TO_HEADING: Final = "Applies to"
QUEUE_HEADING: Final = "Custom Fields"
CREATE_BUTTON: Final = "Create"
SAVE_BUTTON: Final = "Save Changes"

LIST_PATH: Final = "/admin/custom-fields"
CREATE_PATH: Final = "/admin/custom-fields/new"

#: What an "Applies to" page says when it has nothing to offer.
NO_QUEUES: Final = "No queues"
NO_FIELDS: Final = "No custom fields"

#: The prefix of a value input on the ticket forms, ``cf-{field id}``.
VALUE_PREFIX: Final = "cf-"

FieldId = Annotated[int, Path(ge=1)]
QueueId = Annotated[int, Path(ge=1)]
Msg = Annotated[str, Query()]

_ADMIN_CUSTOM_FIELD: Final = [Depends(require_right(service.ADMIN_CUSTOM_FIELD))]


async def posted_values(request: Request) -> dict[int, str]:
    """The ``cf-{id}`` inputs of a ticket form, by field id.

    The ticket router's dependency: the create and basics forms carry a text
    input per custom field, named after the field, so they cannot be declared
    as parameters. What may be *written* is not decided here — only the fields
    the ticket's queue carries are ever written
    (:func:`pyrt.customfields.service.apply_values`).
    """
    form = await request.form()
    found: dict[int, str] = {}
    for key, value in form.multi_items():
        if isinstance(value, UploadFile) or not key.startswith(VALUE_PREFIX):
            continue
        ident = key[len(VALUE_PREFIX) :]
        if ident.isdigit():
            found[int(ident)] = value
    return found


#: The annotation the tickets router declares the dependency with.
PostedValues = Annotated[dict[int, str], Depends(posted_values)]


# --- the field's own pages --------------------------------------------------


@router.get(LIST_PATH, include_in_schema=False, dependencies=_ADMIN_CUSTOM_FIELD)
def field_list(
    request: Request,
    db: Db,
    include_disabled: Annotated[bool, Query(alias="disabled")] = False,
) -> Response:
    """FP F01: the Select list; disabled fields only with ``?disabled=1``."""
    return render(
        request,
        "admin/customfields/index.html",
        {
            "page_title": LIST_HEADING,
            "fields": service.all_fields(db, include_disabled=include_disabled),
            "include_disabled": include_disabled,
            "type_label": service.TYPE_LABEL,
            "applies_to_label": service.APPLIES_TO_LABEL,
        },
    )


@router.get(CREATE_PATH, include_in_schema=False, dependencies=_ADMIN_CUSTOM_FIELD)
def new_field_form(request: Request) -> Response:
    """The Create tab's empty form; Enabled is checked to start."""
    return _form_page(
        request,
        FieldForm(),
        heading=CREATE_HEADING,
        action=CREATE_PATH,
        button=CREATE_BUTTON,
    )


@router.post(CREATE_PATH, include_in_schema=False, dependencies=_ADMIN_CUSTOM_FIELD)
def create_field(
    request: Request,
    db: Db,
    settings: Config,
    name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    type: Annotated[str, Form()] = service.TYPE_VALUE,
    applies_to: Annotated[str, Form()] = service.APPLIES_TO_VALUE,
    enabled: Annotated[bool, Form()] = False,
) -> Response:
    """FP F01: a name unique across the fields, and the two one-option selects."""
    form = FieldForm(name, description, type, applies_to, enabled).cleaned()
    refused = service.refusal(db, form)
    if refused:
        return _form_page(
            request,
            form,
            heading=CREATE_HEADING,
            action=CREATE_PATH,
            button=CREATE_BUTTON,
            message=refused,
        )
    field = service.create_field(db, form)
    log.info("custom field created", extra={"field": field.name})
    return RedirectResponse(
        absolute_url(settings, f"{LIST_PATH}/{field.id}?msg=created"), status_code=SEE_OTHER
    )


@router.get(LIST_PATH + "/{field_id}", include_in_schema=False, dependencies=_ADMIN_CUSTOM_FIELD)
def modify_field_form(request: Request, db: Db, field_id: FieldId, msg: Msg = "") -> Response:
    """The Basics form of one field, with its two sub-tabs."""
    field = _field_or_404(db, field_id)
    return _form_page(
        request,
        FieldForm.of(field),
        heading=MODIFY_HEADING,
        action=f"{LIST_PATH}/{field.id}",
        button=SAVE_BUTTON,
        field=field,
        message=service.message_for(msg),
    )


@router.post(LIST_PATH + "/{field_id}", include_in_schema=False, dependencies=_ADMIN_CUSTOM_FIELD)
def modify_field(
    request: Request,
    db: Db,
    settings: Config,
    field_id: FieldId,
    name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    type: Annotated[str, Form()] = service.TYPE_VALUE,
    applies_to: Annotated[str, Form()] = service.APPLIES_TO_VALUE,
    enabled: Annotated[bool, Form()] = False,
) -> Response:
    """FP F06: unchecking Enabled hides the field everywhere, values kept."""
    field = _field_or_404(db, field_id)
    form = FieldForm(name, description, type, applies_to, enabled).cleaned()
    refused = service.refusal(db, form, field=field)
    if refused:
        return _form_page(
            request,
            form,
            heading=MODIFY_HEADING,
            action=f"{LIST_PATH}/{field.id}",
            button=SAVE_BUTTON,
            field=field,
            message=refused,
        )
    service.save_field(db, field, form)
    log.info("custom field updated", extra={"field": field.name, "disabled": field.disabled})
    return RedirectResponse(
        absolute_url(settings, f"{LIST_PATH}/{field.id}?msg=saved"), status_code=SEE_OTHER
    )


@router.get(
    LIST_PATH + "/{field_id}/applies-to",
    include_in_schema=False,
    dependencies=_ADMIN_CUSTOM_FIELD,
)
def applies_to_page(request: Request, db: Db, field_id: FieldId, msg: Msg = "") -> Response:
    """FP F02: a checkbox per enabled queue, ticked where the field applies."""
    field = _field_or_404(db, field_id)
    message = service.message_for(msg)
    return render(
        request,
        "admin/customfields/applies_to.html",
        {
            "page_title": APPLIES_TO_HEADING,
            "field": field,
            "boxes": service.queue_boxes(db, field.id),
            "prefix": "queue",
            "empty_message": NO_QUEUES,
            "action": f"{LIST_PATH}/{field.id}/applies-to",
            "button": SAVE_BUTTON,
            "message": message,
            "message_ok": message in service.OK_MESSAGES,
        },
    )


@router.post(
    LIST_PATH + "/{field_id}/applies-to",
    include_in_schema=False,
    dependencies=_ADMIN_CUSTOM_FIELD,
)
def save_applies_to(db: Db, settings: Config, field_id: FieldId, posted: PostedNames) -> Response:
    """FP F02: the ticked queues become the field's queues, difference only."""
    field = _field_or_404(db, field_id)
    added, removed = service.save_applies_to(db, field, service.queue_boxes(db, field.id), posted)
    log.info(
        "custom field applies-to saved",
        extra={"field": field.name, "added": added, "removed": removed},
    )
    return RedirectResponse(
        absolute_url(settings, f"{LIST_PATH}/{field.id}/applies-to?msg=applied"),
        status_code=SEE_OTHER,
    )


# --- the same pairing from the queue's side ---------------------------------


@router.get("/admin/queues/{queue_id}/custom-fields", include_in_schema=False)
def queue_custom_fields(
    request: Request,
    db: Db,
    held: ActorPrincipals,
    queue_id: QueueId,
    msg: Msg = "",
) -> Response:
    """FP F02, FP Q04: the queue's ``Custom Fields`` sub-tab.

    ``AdminQueue`` on the queue is the gate, on the GET as on the POST: a
    form nobody may submit is a trap (the queue package's rule, kept).
    """
    queue = _administered_queue(request, db, held, queue_id)
    message = service.message_for(msg)
    return render(
        request,
        "admin/customfields/queue.html",
        {
            "page_title": QUEUE_HEADING,
            "queue": queue,
            "boxes": service.field_boxes(db, queue.id),
            "prefix": "field",
            "empty_message": NO_FIELDS,
            "action": f"/admin/queues/{queue.id}/custom-fields",
            "button": SAVE_BUTTON,
            "message": message,
            "message_ok": message in service.OK_MESSAGES,
        },
    )


@router.post("/admin/queues/{queue_id}/custom-fields", include_in_schema=False)
def save_queue_custom_fields(
    request: Request,
    db: Db,
    settings: Config,
    held: ActorPrincipals,
    queue_id: QueueId,
    posted: PostedNames,
) -> Response:
    """FP F02: the ticked fields become the queue's fields, difference only."""
    queue = _administered_queue(request, db, held, queue_id)
    added, removed = service.save_queue_fields(db, queue, service.field_boxes(db, queue.id), posted)
    log.info(
        "queue custom fields saved",
        extra={"queue": queue.name, "added": added, "removed": removed},
    )
    return RedirectResponse(
        absolute_url(settings, f"/admin/queues/{queue.id}/custom-fields?msg=queue-saved"),
        status_code=SEE_OTHER,
    )


# --- helpers ----------------------------------------------------------------


def _field_or_404(db: Db, field_id: int) -> CustomField:
    field = service.get_field(db, field_id)
    if field is None:
        raise HTTPException(status_code=404)
    return field


def _administered_queue(request: Request, db: Db, held: ActorPrincipals, queue_id: int) -> Queue:
    queue = get_queue(db, queue_id)
    if queue is None:
        raise HTTPException(status_code=404)
    if not has_right(db, held, ADMIN_QUEUE, queue.id, request):
        raise Forbidden(ADMIN_QUEUE, queue.id)
    return queue


def _form_page(
    request: Request,
    form: FieldForm,
    *,
    heading: str,
    action: str,
    button: str,
    field: CustomField | None = None,
    message: str = "",
) -> Response:
    """The one Basics template, for Create and for Modify alike."""
    return render(
        request,
        "admin/customfields/modify.html",
        {
            "page_title": heading,
            "form": form,
            "action": action,
            "button": button,
            "field": field,
            "type_label": service.TYPE_LABEL,
            "type_value": service.TYPE_VALUE,
            "applies_to_label": service.APPLIES_TO_LABEL,
            "applies_to_value": service.APPLIES_TO_VALUE,
            "message": message,
            "message_ok": message in service.OK_MESSAGES,
        },
    )
