"""The public routes: ``/``, ``/login``, ``/logout`` and ``/health``.

Signed out ``/`` is the login form; signed in it is "RT at a glance" with the
two lists of plan §8 (FP L01, L02). ``/health`` is the platform's probe of
§6: no auth, no session, no row written.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Final

from fastapi import APIRouter, Form, Query, Request
from sqlalchemy import Row, Select, select, text
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse, RedirectResponse, Response

from pyrt import auth
from pyrt.acl import Principals, queues_with_right
from pyrt.db.models import ACTIVE_STATUSES, NOBODY_USER_NAME, Queue, Ticket, User
from pyrt.web.deps import ActorPrincipals, Config, CurrentUser, Db
from pyrt.web.errors import SEE_OTHER, safe_next
from pyrt.web.templating import render

log = logging.getLogger(__name__)

router = APIRouter()

#: The headings of plan §8's fixed vocabulary; the walk expects them verbatim.
GLANCE_HEADING: Final = "RT at a glance"
LOGIN_HEADING: Final = "Login"
OWNED_BOX: Final = "10 highest priority tickets I own"
UNOWNED_BOX: Final = "10 newest unowned tickets"

#: What both lists show (plan §8's table) and how many rows.
GLANCE_LIMIT: Final = 10
SHOW_TICKET: Final = "ShowTicket"

WRONG_PASSWORD: Final = "Your username or password is incorrect"

GlanceRow = Row[tuple[int, str, str, str, Any, int]]


def _glance_select() -> Select[tuple[int, str, str, str, Any, int]]:
    """Id, Subject, Status, Queue, Created — the columns both boxes show."""
    return (
        select(
            Ticket.id,
            Ticket.subject,
            Ticket.status,
            Queue.name.label("queue_name"),
            Ticket.created,
            Ticket.queue_id,
        )
        .join(Queue, Queue.id == Ticket.queue_id)
        .where(Ticket.status.in_(ACTIVE_STATUSES))
    )


def owned_tickets(db: Session, user: User) -> list[GlanceRow]:
    """The 10 highest priority active tickets ``user`` owns."""
    statement = (
        _glance_select()
        .where(Ticket.owner_id == user.id)
        .order_by(Ticket.priority.desc(), Ticket.id.desc())
        .limit(GLANCE_LIMIT)
    )
    return list(db.execute(statement).all())


def unowned_tickets(db: Session) -> list[GlanceRow]:
    """The 10 newest active tickets nobody owns."""
    nobody = db.scalar(select(User.id).where(User.name == NOBODY_USER_NAME))
    if nobody is None:  # an unseeded database has no unowned owner
        return []
    statement = (
        _glance_select()
        .where(Ticket.owner_id == nobody)
        .order_by(Ticket.created.desc(), Ticket.id.desc())
        .limit(GLANCE_LIMIT)
    )
    return list(db.execute(statement).all())


def visible(
    db: Session, held: Principals, rows: list[GlanceRow], request: Request
) -> list[GlanceRow]:
    """Drop the rows in queues the user may not ``ShowTicket`` in."""
    if not rows:
        return rows
    allowed = queues_with_right(db, held, SHOW_TICKET, {row.queue_id for row in rows}, request)
    return [row for row in rows if row.queue_id in allowed]


@router.get("/", include_in_schema=False)
def home(
    request: Request,
    db: Db,
    user: CurrentUser,
    held: ActorPrincipals,
    next_path: Annotated[str, Query(alias="next")] = "",
) -> Response:
    """Signed out: the login form. Signed in: RT at a glance (FP L01, L02)."""
    if user is None:
        return render(
            request,
            "login.html",
            {"page_title": LOGIN_HEADING, "next": safe_next(next_path), "message": ""},
        )
    return render(
        request,
        "home.html",
        {
            "page_title": GLANCE_HEADING,
            "owned_box": OWNED_BOX,
            "unowned_box": UNOWNED_BOX,
            "owned": visible(db, held, owned_tickets(db, user), request),
            "unowned": visible(db, held, unowned_tickets(db), request),
        },
    )


@router.post("/login", include_in_schema=False)
def login(
    request: Request,
    db: Db,
    settings: Config,
    name: Annotated[str, Form(alias="user")] = "",
    password: Annotated[str, Form(alias="pass")] = "",
    next_path: Annotated[str, Form(alias="next")] = "",
) -> Response:
    """Verify the password (the hash's time, always) and set the cookie."""
    user = auth.authenticate(db, name.strip(), password)
    if user is None:
        log.info("sign-in refused", extra={"user": name.strip()})
        return render(
            request,
            "login.html",
            {
                "page_title": LOGIN_HEADING,
                "next": safe_next(next_path),
                "message": WRONG_PASSWORD,
            },
        )
    response = RedirectResponse(safe_next(next_path), status_code=SEE_OTHER)
    auth.set_session_cookie(response, settings, user)
    log.info("signed in", extra={"user": user.name})
    return response


@router.get("/logout", include_in_schema=False)
def logout(settings: Config) -> Response:
    """Clear the cookie and go home."""
    response = RedirectResponse("/", status_code=SEE_OTHER)
    auth.clear_session_cookie(response, settings)
    return response


@router.get("/health", include_in_schema=False)
def health(request: Request) -> JSONResponse:
    """A cheap database ping; no auth, no session, no row written (§6)."""
    engine = request.app.state.engine
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # the probe never raises, it reports
        log.warning("health check failed", extra={"error": str(exc)})
        return JSONResponse({"status": "db unreachable"}, status_code=503)
    return JSONResponse({"status": "ok"}, status_code=200)
