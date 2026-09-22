"""The four rights pages of plan §8 (FP R03-R05).

``/admin/queues/{id}/group-rights`` and ``/admin/queues/{id}/user-rights``
grant on one queue, ``/admin/global/group-rights`` and
``/admin/global/user-rights`` grant everywhere; one template renders all
four, as plan §8's count says ("a rights page is one template for four
routes"). A queue page is gated by ``AdminQueue`` on that queue, a global
page by ``SuperUser``: a grant that reaches every queue must not be in the
hands of one queue's administrator.

The checkboxes are named after the principal, so the POST cannot declare
them as parameters: one dependency reads the form once and hands the
handler the set of names that came in (:func:`posted_names`), and
:mod:`pyrt.rights.service` decides what that set means.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from starlette.datastructures import UploadFile
from starlette.responses import RedirectResponse, Response

from pyrt.acl import Forbidden, has_right, require_right
from pyrt.db.models import PrincipalKind, Queue
from pyrt.rights import service
from pyrt.rights.service import Section
from pyrt.web.deps import ActorPrincipals, Config, Db, SignedIn
from pyrt.web.errors import SEE_OTHER
from pyrt.web.templating import absolute_url, render

router = APIRouter()

#: Plan §8's headings and button, kept verbatim: the walk expects them.
GROUP_RIGHTS_HEADING: Final = "Group Rights"
USER_RIGHTS_HEADING: Final = "User Rights"
GLOBAL_GROUP_RIGHTS_HEADING: Final = "Global Group Rights"
GLOBAL_USER_RIGHTS_HEADING: Final = "Global User Rights"
SAVE_BUTTON: Final = "Save Changes"

#: Which sub-tab of the queue's modify page is the current one.
GROUP_TAB: Final = "group-rights"
USER_TAB: Final = "user-rights"

QueueId = Annotated[int, Path(ge=1)]
Msg = Annotated[str, Query()]


async def posted_names(request: Request) -> frozenset[str]:
    """The control names the form ticked.

    An unchecked checkbox sends nothing, so the keys that arrive *are* the
    checked set. The dependency is async so the handler stays synchronous
    (its database work runs off the event loop, as every other page's does).
    """
    form = await request.form()
    return frozenset(
        key for key, value in form.multi_items() if not isinstance(value, UploadFile) and value
    )


PostedNames = Annotated[frozenset[str], Depends(posted_names)]


@router.get("/admin/queues/{queue_id}/group-rights", include_in_schema=False)
def queue_group_rights(
    request: Request,
    db: Db,
    held: ActorPrincipals,
    queue_id: QueueId,
    msg: Msg = "",
) -> Response:
    """FP R03: a checkbox per queue-scoped right for every group."""
    queue = _administered_queue(request, db, held, queue_id)
    return _page(
        request,
        heading=GROUP_RIGHTS_HEADING,
        action=f"/admin/queues/{queue.id}/group-rights",
        sections=service.group_sections(db, queue.id),
        rights=service.rights_offered(queue.id),
        queue=queue,
        tab=GROUP_TAB,
        message=service.message_for(msg),
    )


@router.post("/admin/queues/{queue_id}/group-rights", include_in_schema=False)
def save_queue_group_rights(
    request: Request,
    db: Db,
    settings: Config,
    user: SignedIn,
    held: ActorPrincipals,
    queue_id: QueueId,
    posted: PostedNames,
) -> Response:
    """FP R03: the ticked boxes become this queue's group grants."""
    queue = _administered_queue(request, db, held, queue_id)
    _save(db, service.group_sections(db, queue.id), queue.id, posted, PrincipalKind.GROUP, user.id)
    return _saved(settings, f"/admin/queues/{queue.id}/group-rights")


@router.get("/admin/queues/{queue_id}/user-rights", include_in_schema=False)
def queue_user_rights(
    request: Request,
    db: Db,
    held: ActorPrincipals,
    queue_id: QueueId,
    msg: Msg = "",
) -> Response:
    """FP R04: the same page for the privileged users."""
    queue = _administered_queue(request, db, held, queue_id)
    return _page(
        request,
        heading=USER_RIGHTS_HEADING,
        action=f"/admin/queues/{queue.id}/user-rights",
        sections=service.user_sections(db, queue.id),
        rights=service.rights_offered(queue.id),
        queue=queue,
        tab=USER_TAB,
        message=service.message_for(msg),
    )


@router.post("/admin/queues/{queue_id}/user-rights", include_in_schema=False)
def save_queue_user_rights(
    request: Request,
    db: Db,
    settings: Config,
    user: SignedIn,
    held: ActorPrincipals,
    queue_id: QueueId,
    posted: PostedNames,
) -> Response:
    """FP R04: the ticked boxes become this queue's user grants."""
    queue = _administered_queue(request, db, held, queue_id)
    _save(db, service.user_sections(db, queue.id), queue.id, posted, PrincipalKind.USER, user.id)
    return _saved(settings, f"/admin/queues/{queue.id}/user-rights")


@router.get(
    "/admin/global/group-rights",
    include_in_schema=False,
    dependencies=[Depends(require_right(service.SUPER_USER))],
)
def global_group_rights(request: Request, db: Db, msg: Msg = "") -> Response:
    """FP R05: the global page, the global-only rights included."""
    return _page(
        request,
        heading=GLOBAL_GROUP_RIGHTS_HEADING,
        action="/admin/global/group-rights",
        sections=service.group_sections(db, None),
        rights=service.rights_offered(None),
        message=service.message_for(msg),
    )


@router.post(
    "/admin/global/group-rights",
    include_in_schema=False,
    dependencies=[Depends(require_right(service.SUPER_USER))],
)
def save_global_group_rights(
    request: Request,
    db: Db,
    settings: Config,
    user: SignedIn,
    posted: PostedNames,
) -> Response:
    """FP R05: the ticked boxes become the global group grants."""
    _save(db, service.group_sections(db, None), None, posted, PrincipalKind.GROUP, user.id)
    return _saved(settings, "/admin/global/group-rights")


@router.get(
    "/admin/global/user-rights",
    include_in_schema=False,
    dependencies=[Depends(require_right(service.SUPER_USER))],
)
def global_user_rights(request: Request, db: Db, msg: Msg = "") -> Response:
    """FP R05: the same globally for the privileged users."""
    return _page(
        request,
        heading=GLOBAL_USER_RIGHTS_HEADING,
        action="/admin/global/user-rights",
        sections=service.user_sections(db, None),
        rights=service.rights_offered(None),
        message=service.message_for(msg),
    )


@router.post(
    "/admin/global/user-rights",
    include_in_schema=False,
    dependencies=[Depends(require_right(service.SUPER_USER))],
)
def save_global_user_rights(
    request: Request,
    db: Db,
    settings: Config,
    user: SignedIn,
    posted: PostedNames,
) -> Response:
    """FP R05: the ticked boxes become the global user grants."""
    _save(db, service.user_sections(db, None), None, posted, PrincipalKind.USER, user.id)
    return _saved(settings, "/admin/global/user-rights")


def _administered_queue(request: Request, db: Db, held: ActorPrincipals, queue_id: int) -> Queue:
    """The queue, or 404; ``AdminQueue`` on it is required to see it at all.

    The GET is gated like the POST: a form nobody may submit is a trap (the
    queue package's rule, kept).
    """
    queue = db.get(Queue, queue_id)
    if queue is None:
        raise HTTPException(status_code=404)
    if not has_right(db, held, service.ADMIN_QUEUE, queue.id, request):
        raise Forbidden(service.ADMIN_QUEUE, queue.id)
    return queue


def _save(
    db: Db,
    sections: list[Section],
    queue_id: int | None,
    posted: frozenset[str],
    principal_kind: PrincipalKind,
    actor_id: int,
) -> None:
    service.save_rights(
        db,
        sections,
        service.rights_offered(queue_id),
        queue_id,
        posted,
        principal_kind=principal_kind.value,
        actor_id=actor_id,
    )


def _saved(settings: Config, path: str) -> RedirectResponse:
    """Back to the page with ``?msg=saved`` (the message never rides a body)."""
    return RedirectResponse(absolute_url(settings, f"{path}?msg=saved"), status_code=SEE_OTHER)


def _page(
    request: Request,
    *,
    heading: str,
    action: str,
    sections: list[Section],
    rights: tuple[str, ...],
    message: str,
    queue: Queue | None = None,
    tab: str = "",
) -> Response:
    """The one rights template, for all four routes."""
    return render(
        request,
        "admin/rights/rights.html",
        {
            "page_title": heading,
            "action": action,
            "sections": sections,
            "rights": rights,
            "queue": queue,
            "current_tab": tab,
            "button": SAVE_BUTTON,
            "message": message,
            "message_ok": bool(message),
        },
    )
