"""The user admin pages: the list, Create, and modify (plan §8, FP U01-U03).

Three screens behind one right, ``AdminUsers``, which is global-only: the
list with its Select and Create tabs, the create form, and the modify form
they both end at. A user is disabled, never deleted (plan §10), so the
modify page is where a person leaves the system.
"""

from __future__ import annotations

import logging
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Form, HTTPException, Path, Query, Request
from starlette.responses import RedirectResponse, Response

from pyrt.acl import require_right
from pyrt.db.models import User
from pyrt.users import service
from pyrt.users.service import UserForm
from pyrt.web.deps import Config, Db
from pyrt.web.errors import SEE_OTHER
from pyrt.web.templating import absolute_url, render

log = logging.getLogger(__name__)

#: The right that gates every page here (plan §8: global-only; SuperUser
#: covers it through the resolver).
ADMIN_USERS: Final = "AdminUsers"

#: The headings and the button labels of plan §8's fixed vocabulary.
LIST_HEADING: Final = "Users"
CREATE_HEADING: Final = "Create a user"
MODIFY_HEADING: Final = "Modify a user"
CREATE_BUTTON: Final = "Create"
SAVE_BUTTON: Final = "Save Changes"

LIST_PATH: Final = "/admin/users"
CREATE_PATH: Final = "/admin/users/new"

router = APIRouter(prefix=LIST_PATH, dependencies=[Depends(require_right(ADMIN_USERS))])

UserId = Annotated[int, Path(ge=1)]


def _form_page(
    request: Request,
    form: UserForm,
    *,
    heading: str,
    action: str,
    button: str,
    user: User | None = None,
    message: str = "",
    status_code: int = 200,
) -> Response:
    """The one form template, for Create and for Modify alike."""
    return render(
        request,
        "admin/users/modify.html",
        {
            "page_title": heading,
            "form": form,
            "action": action,
            "button": button,
            "user": user,
            "message": message,
            "message_ok": message in service.MESSAGES.values(),
        },
        status_code=status_code,
    )


@router.get("", include_in_schema=False)
def user_list(
    request: Request,
    db: Db,
    include_disabled: Annotated[bool, Query(alias="disabled")] = False,
) -> Response:
    """FP U01: the Select list, with Nobody out and disabled users hidden."""
    return render(
        request,
        "admin/users/index.html",
        {
            "page_title": LIST_HEADING,
            "users": service.listed_users(db, include_disabled=include_disabled),
            "include_disabled": include_disabled,
        },
    )


@router.get("/new", include_in_schema=False)
def new_user_form(request: Request) -> Response:
    """The Create tab's empty form; Enabled is checked to start."""
    return _form_page(
        request,
        UserForm(),
        heading=CREATE_HEADING,
        action=CREATE_PATH,
        button=CREATE_BUTTON,
    )


@router.post("/new", include_in_schema=False)
def create_user(
    request: Request,
    db: Db,
    settings: Config,
    name: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    real_name: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    privileged: Annotated[bool, Form()] = False,
    enabled: Annotated[bool, Form()] = False,
) -> Response:
    """FP U02: a unique name, an optional password, the privileged flag."""
    form = UserForm(name, email, real_name, privileged, enabled).cleaned()
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
    user = service.create_user(db, form, password)
    log.info("user created", extra={"user": user.name, "privileged": user.privileged})
    return RedirectResponse(
        absolute_url(settings, f"{LIST_PATH}/{user.id}?msg=created"), status_code=SEE_OTHER
    )


@router.get("/{user_id}", include_in_schema=False)
def modify_user_form(
    request: Request,
    db: Db,
    user_id: UserId,
    msg: Annotated[str, Query()] = "",
) -> Response:
    """The modify form, with the message a redirect after POST asked for."""
    user = service.editable_user(db, user_id)
    if user is None:
        raise HTTPException(status_code=404)
    return _form_page(
        request,
        UserForm.of(user),
        heading=MODIFY_HEADING,
        action=f"{LIST_PATH}/{user.id}",
        button=SAVE_BUTTON,
        user=user,
        message=service.MESSAGES.get(msg, ""),
    )


@router.post("/{user_id}", include_in_schema=False)
def modify_user(
    request: Request,
    db: Db,
    settings: Config,
    user_id: UserId,
    name: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    real_name: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    privileged: Annotated[bool, Form()] = False,
    enabled: Annotated[bool, Form()] = False,
) -> Response:
    """FP U03: the password changes only when filled; Enabled off disables."""
    user = service.editable_user(db, user_id)
    if user is None:
        raise HTTPException(status_code=404)
    form = UserForm(name, email, real_name, privileged, enabled).cleaned()
    refused = service.refusal(db, form, user=user)
    if refused:
        return _form_page(
            request,
            form,
            heading=MODIFY_HEADING,
            action=f"{LIST_PATH}/{user.id}",
            button=SAVE_BUTTON,
            user=user,
            message=refused,
        )
    service.save_user(db, user, form, password)
    log.info("user updated", extra={"user": user.name, "disabled": user.disabled})
    return RedirectResponse(
        absolute_url(settings, f"{LIST_PATH}/{user.id}?msg=saved"), status_code=SEE_OTHER
    )
