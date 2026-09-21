"""The shell: the glance page, the session gate, the header, the platform bits.

FP L02, L04, L05, O02, O05, O06, plus the two error pages.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any, cast

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from starlette.responses import PlainTextResponse, Response

from pyrt import cli
from pyrt.acl import require_right
from pyrt.app import create_app
from pyrt.config import ConfigError, Settings
from pyrt.db.models import (
    DEFAULT_QUEUE_NAME,
    NOBODY_USER_NAME,
    TABLE_NAMES,
    Queue,
    Ticket,
    User,
    utcnow,
)
from tests.conftest import ROOT_TEST_PASSWORD, TEST_DSN
from tests.test_acl import grant, make_queue
from tests.test_auth import make_user, sign_in

GLANCE = "RT at a glance"
OWNED_BOX = "10 highest priority tickets I own"
UNOWNED_BOX = "10 newest unowned tickets"


def make_ticket(
    db: Session,
    queue: Queue,
    owner: User,
    subject: str,
    *,
    priority: int = 0,
    status: str = "new",
    created: dt.datetime | None = None,
) -> Ticket:
    when = created or utcnow()
    ticket = Ticket(
        queue_id=queue.id,
        owner_id=owner.id,
        subject=subject,
        status=status,
        priority=priority,
        created=when,
        last_updated=when,
        creator_id=owner.id,
        last_updated_by=owner.id,
    )
    db.add(ticket)
    db.commit()
    return ticket


def queue_by_name(db: Session, name: str) -> Queue:
    queue = db.scalar(select(Queue).where(Queue.name == name))
    assert queue is not None
    return queue


def user_by_name(db: Session, name: str) -> User:
    user = db.scalar(select(User).where(User.name == name))
    assert user is not None
    return user


def order_of(haystack: str, *needles: str) -> list[int]:
    """Where each needle appears, so a test can assert the row order."""
    return [haystack.index(needle) for needle in needles]


def test_fp_l02_glance_lists_owned_and_unowned_active_tickets(
    client: TestClient, db: Session
) -> None:
    """The two boxes, their order, and no resolved ticket in either."""
    general = queue_by_name(db, DEFAULT_QUEUE_NAME)
    root = user_by_name(db, "root")
    nobody = user_by_name(db, NOBODY_USER_NAME)

    make_ticket(db, general, root, "mine-low", priority=10)
    make_ticket(db, general, root, "mine-high", priority=90)
    make_ticket(db, general, root, "mine-middle", priority=50)
    make_ticket(db, general, root, "mine-resolved", priority=99, status="resolved")
    for number in range(1, 13):  # twelve, so the limit of ten shows
        make_ticket(db, general, root, f"mine-bulk-{number:02d}", priority=number)

    base = utcnow()
    make_ticket(db, general, nobody, "free-oldest", created=base - dt.timedelta(hours=3))
    make_ticket(db, general, nobody, "free-middle", created=base - dt.timedelta(hours=2))
    make_ticket(db, general, nobody, "free-newest", created=base - dt.timedelta(hours=1))
    make_ticket(db, general, nobody, "free-rejected", status="rejected")

    sign_in(client)
    page = client.get("/")
    assert page.status_code == 200
    assert "<h1>RT at a glance</h1>" in page.text
    assert OWNED_BOX in page.text
    assert UNOWNED_BOX in page.text

    # Highest priority first, and only the ten highest.
    assert order_of(page.text, "mine-high", "mine-middle", "mine-low") == sorted(
        order_of(page.text, "mine-high", "mine-middle", "mine-low")
    )
    assert "mine-bulk-12" in page.text
    assert "mine-bulk-01" not in page.text
    assert "mine-bulk-02" not in page.text

    # Newest first, and nothing inactive in either box.
    assert order_of(page.text, "free-newest", "free-middle", "free-oldest") == sorted(
        order_of(page.text, "free-newest", "free-middle", "free-oldest")
    )
    assert "mine-resolved" not in page.text
    assert "free-rejected" not in page.text


def test_fp_l02_glance_hides_tickets_the_user_may_not_show(client: TestClient, db: Session) -> None:
    """The lists are filtered by ``ShowTicket``, queue by queue."""
    general = queue_by_name(db, DEFAULT_QUEUE_NAME)
    secret = make_queue(db, "Secret")
    agent = make_user(db, "agent", "agent-password", privileged=True)
    grant(db, ("user", agent.id), "ShowTicket", queue=general)

    make_ticket(db, general, agent, "allowed-ticket", priority=1)
    make_ticket(db, secret, agent, "hidden-ticket", priority=99)
    nobody = user_by_name(db, NOBODY_USER_NAME)
    make_ticket(db, secret, nobody, "hidden-unowned")

    sign_in(client, "agent", "agent-password")
    page = client.get("/")
    assert "allowed-ticket" in page.text
    assert "hidden-ticket" not in page.text
    assert "hidden-unowned" not in page.text


def test_fp_l04_a_private_path_needs_a_session_and_comes_back_to_it(
    client: TestClient,
) -> None:
    """Anonymous private paths redirect; the whitelist does not."""
    gated = client.get("/admin/queues", follow_redirects=False)
    assert gated.status_code == 303
    assert gated.headers["location"] == "/?next=/admin/queues"

    with_query = client.get("/ticket/12?tab=history", follow_redirects=False)
    assert with_query.status_code == 303
    assert with_query.headers["location"] == "/?next=/ticket/12%3Ftab%3Dhistory"
    # ... and the form hands the decoded path back.
    assert (
        'name="next" value="/ticket/12?tab=history"'
        in client.get(with_query.headers["location"]).text
    )

    # The whitelist of FP L04.
    assert client.get("/", follow_redirects=False).status_code == 200
    assert client.get("/health", follow_redirects=False).status_code == 200
    assert client.get("/static/style.css", follow_redirects=False).status_code == 200
    assert client.get("/logout", follow_redirects=False).headers["location"] == "/"

    # The login page carries `next`, and signing in returns to it.
    form = client.get("/?next=/admin/queues")
    assert 'name="next" value="/admin/queues"' in form.text
    back = client.post(
        "/login",
        data={"user": "root", "pass": ROOT_TEST_PASSWORD, "next": "/admin/queues"},
        follow_redirects=False,
    )
    assert back.status_code == 303
    assert back.headers["location"] == "/admin/queues"

    # A signed-in request is no longer redirected: root reaches the page.
    assert client.get("/admin/queues", follow_redirects=False).status_code == 200


def test_fp_l04_an_offsite_next_is_not_followed(client: TestClient) -> None:
    """``next`` only ever names a path on this site."""
    away = client.post(
        "/login",
        data={"user": "root", "pass": ROOT_TEST_PASSWORD, "next": "https://evil.example/"},
        follow_redirects=False,
    )
    assert away.headers["location"] == "/"


def test_fp_l05_the_quick_search_box_is_in_every_signed_in_header(
    client: TestClient,
) -> None:
    """FP L05: the box named ``q`` and its ``Search`` button (plan §11)."""
    sign_in(client)
    for path in ("/", "/?next=/"):
        page = client.get(path)
        assert page.status_code == 200
        assert 'id="quick-search"' in page.text
        assert 'name="q"' in page.text
        assert ">Search</button>" in page.text
        assert "/search" in page.text
        assert "Logged in as root" in page.text
        assert "RT for test" in page.text  # the logo, SITE_NAME from the settings
        assert "a rewrite of Request Tracker's ticket core" in page.text


def test_fp_o02_health_needs_nothing_writes_nothing_and_reports_the_database(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """200 without a session, no row written, 503 when the database is away."""
    counts_before = {
        table: db.scalar(select(func.count()).select_from(text(f"`{table}`")))
        for table in TABLE_NAMES
    }

    client.cookies.clear()
    ok = client.get("/health", headers={"Authorization": "Bearer nonsense"})
    assert ok.status_code == 200
    assert ok.json() == {"status": "ok"}
    assert "set-cookie" not in ok.headers

    db.expire_all()
    counts_after = {
        table: db.scalar(select(func.count()).select_from(text(f"`{table}`")))
        for table in TABLE_NAMES
    }
    assert counts_after == counts_before, "a probe must not write a row"

    # A database that cannot be reached is 503, not an exception.
    unreachable = create_app(
        Settings(
            base_url="http://testserver",
            session_secret="test",
            database_url_override="mysql+pymysql://pyrt:pyrt@127.0.0.1:1/none?charset=utf8mb4",
        )
    )
    try:
        with TestClient(unreachable) as broken:
            down = broken.get("/health")
            assert down.status_code == 503
            assert down.json() == {"status": "db unreachable"}
    finally:
        unreachable.state.engine.dispose()

    # `pyrt healthcheck` exits 0 on a 200 and 1 on anything else.
    class FakeResponse:
        def __init__(self, status: int) -> None:
            self.status = status

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

    def answer(status: int) -> Any:
        def urlopen(url: str, timeout: float | None = None) -> FakeResponse:
            assert url == "http://127.0.0.1:8080/health"
            return FakeResponse(status)

        return urlopen

    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.setattr(cli.urllib.request, "urlopen", answer(200))
    assert cli.main(["healthcheck"]) == 0
    monkeypatch.setattr(cli.urllib.request, "urlopen", answer(503))
    assert cli.main(["healthcheck"]) == 1

    def refuse(url: str, timeout: float | None = None) -> FakeResponse:
        raise OSError("connection refused")

    monkeypatch.setattr(cli.urllib.request, "urlopen", refuse)
    assert cli.main(["healthcheck"]) == 1


def test_fp_o05_links_are_built_from_base_url_never_from_the_host_header(
    client: TestClient,
) -> None:
    """A forged Host reaches no link on the page (plan §6, §10)."""
    foreign = {"Host": "evil.example"}

    signed_out = client.get("/", headers=foreign)
    assert signed_out.status_code == 200
    assert "evil.example" not in signed_out.text
    assert "http://testserver/static/style.css" in signed_out.text

    landing = client.post(
        "/login",
        data={"user": "root", "pass": ROOT_TEST_PASSWORD},
        headers=foreign,
        follow_redirects=False,
    )
    assert "evil.example" not in landing.headers["location"]

    signed_in = client.get("/", headers=foreign)
    assert "evil.example" not in signed_in.text
    assert "http://testserver/logout" in signed_in.text


def test_fp_o06_refuses_to_serve_without_base_url_or_session_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The card's two signatures, one line each (plan §6)."""
    monkeypatch.setenv("SESSION_SECRET", "test")
    monkeypatch.delenv("BASE_URL", raising=False)
    with pytest.raises(ConfigError, match=r"^BASE_URL is required$"):
        create_app()

    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    with pytest.raises(ConfigError, match=r"^SESSION_SECRET is required$"):
        create_app()


def _probe_app(client: TestClient) -> FastAPI:
    """Two routes a later package will own, for the 403 and 404 pages."""
    app = cast(FastAPI, client.app)

    @app.get(
        "/probe/needs-admin-users",
        include_in_schema=False,
        dependencies=[Depends(require_right("AdminUsers"))],
    )
    def needs_admin_users() -> Response:
        return PlainTextResponse("allowed")

    @app.get("/probe/needs-a-queue-right", include_in_schema=False)
    def needs_a_queue_right(
        _: Annotated[None, Depends(require_right("AdminQueue", 1))],
    ) -> Response:
        return PlainTextResponse("allowed")

    return app


def test_a_refused_action_is_the_denial_page_not_an_error(client: TestClient, db: Session) -> None:
    """The 403 page of plan §11, with the heading the walk expects."""
    _probe_app(client)
    make_user(db, "agent", "agent-password", privileged=True)

    sign_in(client, "agent", "agent-password")
    refused = client.get("/probe/needs-admin-users")
    assert refused.status_code == 403
    assert "You are not allowed" in refused.text
    assert "RT for test" in refused.text  # the shell is still around it
    assert client.get("/probe/needs-a-queue-right").status_code == 403

    # SuperUser is allowed everything.
    client.cookies.clear()
    sign_in(client)
    assert client.get("/probe/needs-admin-users").text == "allowed"
    assert client.get("/probe/needs-a-queue-right").text == "allowed"


def test_an_unknown_path_is_the_not_found_page(client: TestClient) -> None:
    sign_in(client)
    missing = client.get("/no-such-page")
    assert missing.status_code == 404
    assert "Page not found" in missing.text
    assert "There is no page at this address." in missing.text


def test_the_test_database_is_the_one_the_app_uses(client: TestClient) -> None:
    """A guard on the fixtures: the app and the tests share one database."""
    assert client.app.state.settings.database_url() == TEST_DSN
