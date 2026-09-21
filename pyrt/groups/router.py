"""The group admin pages: the list, Create, modify and Members (plan §8).

Four screens behind two global-only rights: ``AdminGroup`` for the list, the
Create form and Basics (FP U05), and ``AdminGroupMembership`` for the Members
page and both of its POSTs (FP U06). Only user-defined groups are here: the
system groups and the roles are rows the resolver names, not pages a person
edits, and a group is disabled, never deleted (FP U08).
"""

from __future__ import annotations

import logging
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Form, HTTPException, Path, Query, Request
from starlette.responses import RedirectResponse, Response

from pyrt.acl import require_right
from pyrt.db.models import Group
from pyrt.groups import service
from pyrt.groups.service import GroupForm
from pyrt.web.deps import Config, Db
from pyrt.web.errors import SEE_OTHER
from pyrt.web.templating import absolute_url, render

log = logging.getLogger(__name__)

#: The two gates of plan §8, both global-only; SuperUser covers either.
ADMIN_GROUP: Final = "AdminGroup"
ADMIN_GROUP_MEMBERSHIP: Final = "AdminGroupMembership"

#: The headings and button labels of plan §8's fixed vocabulary.
LIST_HEADING: Final = "Groups"
CREATE_HEADING: Final = "Create a group"
MODIFY_HEADING: Final = "Modify a group"
MEMBERS_HEADING: Final = "Members of"
CREATE_BUTTON: Final = "Create"
SAVE_BUTTON: Final = "Save Changes"
ADD_BUTTON: Final = "Add"
REMOVE_BUTTON: Final = "Remove"

LIST_PATH: Final = "/admin/groups"
CREATE_PATH: Final = "/admin/groups/new"

#: What the Members form's ``submit`` may say; one form per action (plan §8).
ADD_ACTION: Final = "add"
REMOVE_ACTION: Final = "remove"

router = APIRouter(prefix=LIST_PATH)

GroupId = Annotated[int, Path(ge=1)]

_ADMIN_GROUP: Final = [Depends(require_right(ADMIN_GROUP))]
_ADMIN_MEMBERSHIP: Final = [Depends(require_right(ADMIN_GROUP_MEMBERSHIP))]


def _group_or_404(db: Db, group_id: int) -> Group:
    """The user-defined group of that id; anything else is a 404."""
    group = service.editable_group(db, group_id)
    if group is None:
        raise HTTPException(status_code=404)
    return group


def _form_page(
    request: Request,
    form: GroupForm,
    *,
    heading: str,
    action: str,
    button: str,
    group: Group | None = None,
    message: str = "",
) -> Response:
    """The one form template, for Create and for Modify alike."""
    return render(
        request,
        "admin/groups/modify.html",
        {
            "page_title": heading,
            "form": form,
            "action": action,
            "button": button,
            "group": group,
            "message": message,
            "message_ok": message in service.OK_MESSAGES,
        },
    )


@router.get("", include_in_schema=False, dependencies=_ADMIN_GROUP)
def group_list(
    request: Request,
    db: Db,
    include_disabled: Annotated[bool, Query(alias="disabled")] = False,
) -> Response:
    """FP U05: the Select list of the user-defined groups, disabled hidden."""
    return render(
        request,
        "admin/groups/index.html",
        {
            "page_title": LIST_HEADING,
            "groups": service.user_defined_groups(db, include_disabled=include_disabled),
            "include_disabled": include_disabled,
        },
    )


@router.get("/new", include_in_schema=False, dependencies=_ADMIN_GROUP)
def new_group_form(request: Request) -> Response:
    """The Create tab's empty form; Enabled is checked to start."""
    return _form_page(
        request,
        GroupForm(),
        heading=CREATE_HEADING,
        action=CREATE_PATH,
        button=CREATE_BUTTON,
    )


@router.post("/new", include_in_schema=False, dependencies=_ADMIN_GROUP)
def create_group(
    request: Request,
    db: Db,
    settings: Config,
    name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    enabled: Annotated[bool, Form()] = False,
) -> Response:
    """FP U05: a name unique across every group, and a description."""
    form = GroupForm(name, description, enabled).cleaned()
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
    group = service.create_group(db, form)
    log.info("group created", extra={"group": group.name})
    return RedirectResponse(
        absolute_url(settings, f"{LIST_PATH}/{group.id}?msg=created"), status_code=SEE_OTHER
    )


@router.get("/{group_id}", include_in_schema=False, dependencies=_ADMIN_GROUP)
def modify_group_form(
    request: Request,
    db: Db,
    group_id: GroupId,
    msg: Annotated[str, Query()] = "",
) -> Response:
    """The modify form, with the message a redirect after POST asked for."""
    group = _group_or_404(db, group_id)
    return _form_page(
        request,
        GroupForm.of(group),
        heading=MODIFY_HEADING,
        action=f"{LIST_PATH}/{group.id}",
        button=SAVE_BUTTON,
        group=group,
        message=service.MESSAGES.get(msg, ""),
    )


@router.post("/{group_id}", include_in_schema=False, dependencies=_ADMIN_GROUP)
def modify_group(
    request: Request,
    db: Db,
    settings: Config,
    group_id: GroupId,
    name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    enabled: Annotated[bool, Form()] = False,
) -> Response:
    """FP U08: unchecking Enabled disables the group and keeps its grants."""
    group = _group_or_404(db, group_id)
    form = GroupForm(name, description, enabled).cleaned()
    refused = service.refusal(db, form, group=group)
    if refused:
        return _form_page(
            request,
            form,
            heading=MODIFY_HEADING,
            action=f"{LIST_PATH}/{group.id}",
            button=SAVE_BUTTON,
            group=group,
            message=refused,
        )
    service.save_group(db, group, form)
    log.info("group updated", extra={"group": group.name, "disabled": group.disabled})
    return RedirectResponse(
        absolute_url(settings, f"{LIST_PATH}/{group.id}?msg=saved"), status_code=SEE_OTHER
    )


@router.get("/{group_id}/members", include_in_schema=False, dependencies=_ADMIN_MEMBERSHIP)
def members_page(
    request: Request,
    db: Db,
    group_id: GroupId,
    msg: Annotated[str, Query()] = "",
) -> Response:
    """FP U06: the members, a Remove form per row, and the Member select."""
    group = _group_or_404(db, group_id)
    message = service.MESSAGES.get(msg, "")
    return render(
        request,
        "admin/groups/members.html",
        {
            "page_title": f"{MEMBERS_HEADING} {group.name}",
            "group": group,
            "members": service.members(db, group),
            "candidates": service.candidates(db, group),
            "message": message,
            "message_ok": message in service.OK_MESSAGES,
        },
    )


@router.post("/{group_id}/members", include_in_schema=False, dependencies=_ADMIN_MEMBERSHIP)
def change_members(
    request: Request,
    db: Db,
    settings: Config,
    group_id: GroupId,
    submit: Annotated[str, Form()] = ADD_ACTION,
    user: Annotated[int, Form()] = 0,
) -> Response:
    """FP U06: Add from the select, Remove from a row; both end here.

    A user is in a group at most once, so a second Add writes nothing and
    says so.
    """
    group = _group_or_404(db, group_id)
    member = service.addable_user(db, user)
    if member is None:
        raise HTTPException(status_code=404)

    if submit == REMOVE_ACTION:
        service.remove_member(db, group, member)
        log.info("member removed", extra={"group": group.name, "user": member.name})
        outcome = "removed"
    else:
        added = service.add_member(db, group, member)
        log.info("member added", extra={"group": group.name, "user": member.name, "added": added})
        outcome = "added" if added else "already"

    return RedirectResponse(
        absolute_url(settings, f"{LIST_PATH}/{group.id}/members?msg={outcome}"),
        status_code=SEE_OTHER,
    )
