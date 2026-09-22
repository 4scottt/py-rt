"""Creating, showing and replaying a ticket (FP T01-T04, T11, T12, T14).

Plan §8's create form and ticket page, §10's transaction record, §11's two
columns. The gates are ``CreateTicket`` on the queue to create and
``ShowTicket`` on the ticket's queue to read it, the second resolved with
the ticket's own roles so a grant to Requestor reaches the person who
wrote in.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from pyrt.config import Settings
from pyrt.db.models import (
    DEFAULT_QUEUE_NAME,
    NO_SUBJECT,
    NOBODY_USER_NAME,
    Message,
    Queue,
    Ticket,
    TicketWatcher,
    Transaction,
    TransactionType,
    User,
    WatcherRole,
    utcnow,
)
from pyrt.tickets import hooks, service, transactions
from tests.test_acl import grant, group_id, make_queue
from tests.test_auth import make_user, sign_in
from tests.test_queues import fresh, queue_by_name, user_by_name

FORBIDDEN = "You are not allowed"
CREATE_HEADING = "Create a ticket"


def general(db: Session) -> Queue:
    return queue_by_name(db, DEFAULT_QUEUE_NAME)


def root(db: Session) -> User:
    return user_by_name(db, "root")


def nobody(db: Session) -> User:
    return user_by_name(db, NOBODY_USER_NAME)


def only_ticket(db: Session) -> Ticket:
    """The one ticket the test wrote, read after the app's commit."""
    fresh(db)
    tickets = list(db.scalars(select(Ticket).order_by(Ticket.id)).all())
    assert len(tickets) == 1, tickets
    return tickets[0]


def ticket_count(db: Session) -> int:
    fresh(db)
    return len(list(db.scalars(select(Ticket.id)).all()))


def make_ticket(
    db: Session,
    queue: Queue,
    actor: User,
    *,
    subject: str = "A ticket",
    status: str = "new",
    priority: int = 0,
    requestor: User | None = None,
) -> Ticket:
    """A ticket written straight to the database, the way the app writes one."""
    now = utcnow()
    ticket = Ticket(
        queue_id=queue.id,
        owner_id=nobody(db).id,
        subject=subject,
        status=status,
        priority=priority,
        created=now,
        last_updated=now,
        creator_id=actor.id,
        last_updated_by=actor.id,
    )
    db.add(ticket)
    db.flush()
    if requestor is not None:
        db.add(TicketWatcher(ticket_id=ticket.id, user_id=requestor.id, role=WatcherRole.REQUESTOR))
    transactions.record(db, ticket, TransactionType.CREATE, actor, body="The first word")
    db.commit()
    return ticket


def create_form(client: TestClient, db: Session, **fields: str) -> Response:
    """POST the create form with the defaults every field needs."""
    data = {
        "queue": str(general(db).id),
        "status": "new",
        "owner": str(nobody(db).id),
        "requestors": "",
        "subject": "Printer is on fire",
        "content": "It started smoking this morning.",
    }
    data.update(fields)
    return client.post("/ticket/new", data=data, follow_redirects=False)


# --- T01: create -----------------------------------------------------------


def test_fp_t01_the_create_form_has_every_control(client: TestClient, db: Session) -> None:
    """Plan §8: the selects, the fields, the textarea and the Create button."""
    queue = general(db)
    sign_in(client)
    page = client.get(f"/ticket/new?queue={queue.id}")

    assert page.status_code == 200
    assert CREATE_HEADING in page.text
    assert DEFAULT_QUEUE_NAME in page.text
    for control in (
        '<select id="queue" name="queue">',
        '<select id="status" name="status">',
        '<select id="owner" name="owner">',
        'name="requestors"',
        'name="subject"',
        'name="content"',
    ):
        assert control in page.text, control
    for label in ("Queue", "Status", "Owner", "Requestors", "Subject", "Content"):
        assert f">{label}</label>" in page.text, label
    assert f'<option value="{queue.id}" selected>' in page.text
    assert ">Create</button>" in page.text
    # The statuses a new ticket may have, and no more (plan §10's lifecycle).
    assert '<option value="stalled"' in page.text
    assert '<option value="resolved"' not in page.text
    # No control name may contain "::" and no label may end in a colon.
    assert "::" not in page.text
    assert ":</label>" not in page.text


def test_fp_t01_create_a_ticket_with_every_field(client: TestClient, db: Session) -> None:
    """The row, its owner, its queue and the requestor created for it."""
    queue = general(db)
    owner = root(db)
    sign_in(client)

    made = create_form(
        client,
        db,
        status="open",
        owner=str(owner.id),
        requestors="ADA@example.invalid",
        subject="Printer is on fire",
    )
    assert made.status_code == 303
    ticket = only_ticket(db)
    assert made.headers["location"] == f"http://testserver/ticket/{ticket.id}"

    assert ticket.subject == "Printer is on fire"
    assert ticket.status == "open"
    assert ticket.queue_id == queue.id
    assert ticket.owner_id == owner.id
    assert ticket.priority == 0
    assert ticket.creator_id == owner.id
    assert ticket.last_updated_by == owner.id
    assert ticket.started is None
    assert ticket.resolved is None

    # The requestor nobody knew is now an unprivileged user with no password.
    requestor = db.scalar(select(User).where(User.email == "ADA@example.invalid"))
    assert requestor is not None
    assert requestor.name == "ADA@example.invalid"
    assert requestor.real_name == ""
    assert requestor.privileged is False
    assert requestor.password_hash is None


def test_fp_t01_a_known_address_is_not_a_second_user(client: TestClient, db: Session) -> None:
    """The requestor lookup is case-insensitive (FP T01's "if unknown")."""
    ada = make_user(db, "ada", "ada-password")
    before = len(list(db.scalars(select(User.id)).all()))

    sign_in(client)
    assert create_form(client, db, requestors="ADA@EXAMPLE.INVALID").status_code == 303

    fresh(db)
    assert len(list(db.scalars(select(User.id)).all())) == before
    ticket = only_ticket(db)
    watcher = db.scalar(select(TicketWatcher).where(TicketWatcher.ticket_id == ticket.id))
    assert watcher is not None
    assert watcher.user_id == ada.id


def test_fp_t01_an_empty_subject_and_an_empty_requestor(client: TestClient, db: Session) -> None:
    """No subject is "[no subject]"; no address makes the actor the requestor."""
    sign_in(client)
    assert create_form(client, db, subject="  ", requestors="").status_code == 303

    ticket = only_ticket(db)
    assert ticket.subject == NO_SUBJECT
    watcher = db.scalar(select(TicketWatcher).where(TicketWatcher.ticket_id == ticket.id))
    assert watcher is not None
    assert watcher.user_id == root(db).id
    assert watcher.role == WatcherRole.REQUESTOR


def test_fp_t01_create_needs_create_ticket_on_that_queue(client: TestClient, db: Session) -> None:
    """No right is the denial page, on the form and on the submit."""
    support = make_queue(db, "Support")
    agent = make_user(db, "agent", "agent-password", privileged=True)

    sign_in(client, "agent", "agent-password")
    refused = client.get(f"/ticket/new?queue={general(db).id}")
    assert refused.status_code == 403
    assert FORBIDDEN in refused.text

    # A right on another queue is not a right on this one.
    grant(db, ("user", agent.id), "CreateTicket", queue=support)
    grant(db, ("user", agent.id), "SeeQueue", queue=support)
    posted = create_form(client, db)
    assert posted.status_code == 403
    assert ticket_count(db) == 0

    # And with the rights on this queue it goes through (SeeQueue is the
    # companion of FP R06: a queue is offered only to someone who sees it;
    # tests/test_gates.py is where that half is proved).
    grant(db, ("user", agent.id), "CreateTicket", queue=general(db))
    grant(db, ("user", agent.id), "SeeQueue", queue=general(db))
    assert create_form(client, db).status_code == 303
    assert ticket_count(db) == 1


def test_fp_t01_a_disabled_queue_is_refused(client: TestClient, db: Session) -> None:
    """A closed queue takes no ticket, and says so on the form."""
    closed = make_queue(db, "Retired")
    closed.disabled = True
    db.commit()

    sign_in(client)
    posted = create_form(client, db, queue=str(closed.id))
    assert posted.status_code == 200
    assert service.QUEUE_DISABLED in posted.text
    assert ticket_count(db) == 0
    assert "Retired" not in posted.text  # nor is it offered by the select

    # The service says the same on its own, whoever asks it.
    assert service.refusal(db, service.TicketForm(), closed) == service.QUEUE_DISABLED


def test_fp_t01_an_owner_must_be_nobody_or_a_privileged_user(
    client: TestClient, db: Session
) -> None:
    """The select's values are the rule, and the POST re-checks them."""
    plain = make_user(db, "plain")
    sign_in(client)

    posted = create_form(client, db, owner=str(plain.id))
    assert posted.status_code == 200
    assert service.BAD_OWNER in posted.text
    assert ticket_count(db) == 0

    # The select offers Nobody first, then the privileged users.
    page = client.get("/ticket/new")
    assert f'<option value="{nobody(db).id}"' in page.text
    assert f'<option value="{root(db).id}"' in page.text
    assert f'<option value="{plain.id}"' not in page.text


def test_fp_t01_a_requestor_must_look_like_an_address(client: TestClient, db: Session) -> None:
    """Text that is not an address is a message, not a user named after it."""
    sign_in(client)
    posted = create_form(client, db, requestors="ada at example")
    assert posted.status_code == 200
    assert service.BAD_REQUESTOR in posted.text
    assert ticket_count(db) == 0


# --- T02: the Create transaction -------------------------------------------


def test_fp_t02_create_records_a_transaction_with_its_message(
    client: TestClient, db: Session
) -> None:
    """One Create row, the content as its message, the timestamps together."""
    sign_in(client)
    assert create_form(client, db, content="It started smoking.").status_code == 303

    ticket = only_ticket(db)
    rows = list(db.scalars(select(Transaction).where(Transaction.ticket_id == ticket.id)).all())
    assert len(rows) == 1
    assert rows[0].type == TransactionType.CREATE
    assert rows[0].creator_id == root(db).id
    assert rows[0].field is None

    message = db.get(Message, rows[0].id)
    assert message is not None
    assert message.body == "It started smoking."
    assert message.content_type == "text/plain"

    # FP T02: Created and LastUpdated are the same moment on a new ticket.
    assert ticket.created == ticket.last_updated
    assert ticket.last_updated_by == root(db).id


def test_fp_t02_an_empty_content_writes_no_message(client: TestClient, db: Session) -> None:
    """A transaction with nothing to say has no messages row at all."""
    sign_in(client)
    assert create_form(client, db, content="").status_code == 303

    ticket = only_ticket(db)
    transaction = db.scalar(select(Transaction).where(Transaction.ticket_id == ticket.id))
    assert transaction is not None
    assert db.get(Message, transaction.id) is None


# --- T03: the ticket page --------------------------------------------------


def test_fp_t03_the_ticket_page_shows_its_boxes(client: TestClient, db: Session) -> None:
    """The title bar, the five titleboxes, their fields and the action links."""
    ada = make_user(db, "ada", "ada-password")
    sign_in(client)
    assert create_form(client, db, requestors="ada@example.invalid").status_code == 303
    ticket = only_ticket(db)

    page = client.get(f"/ticket/{ticket.id}")
    assert page.status_code == 200
    assert f"<h1>#{ticket.id}: Printer is on fire</h1>" in page.text

    for box in ("Basics", "People", "Dates", "Custom Fields", "History"):
        assert f'<h2 class="titlebox-title">{box}</h2>' in page.text, box
    for field in ("Id", "Status", "Queue", "Owner", "Priority", "Requestors"):
        assert f"<th>{field}</th>" in page.text, field
    for field in ("Created", "Last Updated", "Resolved"):
        assert f"<th>{field}</th>" in page.text, field

    assert f"<td>{ticket.id}</td>" in page.text
    assert "<td>new</td>" in page.text
    assert f"<td>{DEFAULT_QUEUE_NAME}</td>" in page.text
    assert NOBODY_USER_NAME in page.text
    assert ada.name in page.text
    assert service.NOT_SET in page.text  # Resolved, on a ticket that is open

    for label in ("Reply", "Comment", "Resolve", "Basics", "History"):
        assert f">{label}</a>" in page.text, label
    assert f"http://testserver/ticket/{ticket.id}/update?action=comment" in page.text
    assert f"http://testserver/ticket/{ticket.id}/basics" in page.text


def test_fp_t03_show_ticket_reaches_the_requestor_through_its_role(
    client: TestClient, db: Session
) -> None:
    """A grant to the Requestor role opens the page to the person who wrote in."""
    make_user(db, "ada", "ada-password")
    sign_in(client)
    assert create_form(client, db, requestors="ada@example.invalid").status_code == 303
    ticket = only_ticket(db)

    sign_in(client, "ada", "ada-password")
    assert client.get(f"/ticket/{ticket.id}").status_code == 403

    grant(db, ("group", group_id(db, "Requestor")), "ShowTicket", queue=general(db))
    allowed = client.get(f"/ticket/{ticket.id}")
    assert allowed.status_code == 200
    assert "Printer is on fire" in allowed.text

    # The same grant does not open another queue's tickets to her.
    other = make_ticket(db, make_queue(db, "Support"), root(db))
    assert client.get(f"/ticket/{other.id}").status_code == 403


# --- T04: the history ------------------------------------------------------


def test_fp_t04_the_history_is_oldest_first_and_rendered(client: TestClient, db: Session) -> None:
    """Sentence, creator, time, and the body escaped, split and linkified."""
    body = "Read <b>this</b> first\nline two\n\nThen https://example.com/fix"
    ticket = make_ticket(db, general(db), root(db))
    transactions.record(db, ticket, TransactionType.CORRESPOND, root(db), body=body)
    transactions.record(
        db,
        ticket,
        TransactionType.STATUS,
        root(db),
        field="Status",
        old_value="new",
        new_value="open",
    )
    db.commit()

    sign_in(client)
    page = client.get(f"/ticket/{ticket.id}/history")
    assert page.status_code == 200

    assert "Ticket created" in page.text
    assert "Correspondence added" in page.text
    assert "Status changed from new to open" in page.text
    assert page.text.index("Ticket created") < page.text.index("Correspondence added")
    assert page.text.index("Correspondence added") < page.text.index("Status changed")
    assert "root" in page.text

    assert "&lt;b&gt;this&lt;/b&gt;" in page.text
    assert "<b>this</b>" not in page.text
    assert "Read &lt;b&gt;this&lt;/b&gt; first<br>line two" in page.text
    assert '<a href="https://example.com/fix">https://example.com/fix</a>' in page.text

    # The same block is on the ticket page itself (FP T03's History box).
    assert "Correspondence added" in client.get(f"/ticket/{ticket.id}").text


def test_fp_t04_a_comment_is_hidden_without_comment_on_ticket(
    client: TestClient, db: Session
) -> None:
    """The staff's aside: the row stays, the body needs CommentOnTicket."""
    ada = make_user(db, "ada", "ada-password", privileged=True)
    ticket = make_ticket(db, general(db), root(db))
    transactions.record(
        db, ticket, TransactionType.COMMENT, root(db), body="She is a known trouble caller"
    )
    db.commit()
    grant(db, ("user", ada.id), "ShowTicket", queue=general(db))

    sign_in(client, "ada", "ada-password")
    page = client.get(f"/ticket/{ticket.id}/history")
    assert page.status_code == 200
    assert "Comment added" in page.text
    assert "known trouble caller" not in page.text
    assert "This comment is not shown to you" in page.text

    grant(db, ("user", ada.id), "CommentOnTicket", queue=general(db))
    shown = client.get(f"/ticket/{ticket.id}/history")
    assert "known trouble caller" in shown.text
    assert "This comment is not shown to you" not in shown.text


def test_fp_t04_the_history_needs_show_ticket(client: TestClient, db: Session) -> None:
    """The history alone is behind the ticket page's gate."""
    make_user(db, "ada", "ada-password")
    ticket = make_ticket(db, general(db), root(db))

    sign_in(client, "ada", "ada-password")
    refused = client.get(f"/ticket/{ticket.id}/history")
    assert refused.status_code == 403
    assert FORBIDDEN in refused.text


# --- the notification seam -------------------------------------------------


def test_create_fires_the_created_hook_after_the_commit(client: TestClient, db: Session) -> None:
    """The seam the mail package hangs its notifications on (plan §10)."""
    seen: list[tuple[int, str, str]] = []

    def listener(
        session: Session,
        settings: Settings,
        ticket: Ticket,
        transaction: Transaction,
        actor: User,
    ) -> None:
        seen.append((ticket.id, transaction.type, actor.name))

    def raiser(*args: object) -> None:
        raise RuntimeError("no mail server here")

    registered = list(hooks.created)
    hooks.created.extend([raiser, listener])
    try:
        sign_in(client)
        assert create_form(client, db).status_code == 303
    finally:
        hooks.created[:] = registered

    ticket = only_ticket(db)
    # A listener that fails is logged, not raised: the ticket is written.
    assert seen == [(ticket.id, TransactionType.CREATE, "root")]


# --- T11, T12, T14 ---------------------------------------------------------


def test_fp_t11_an_unknown_id_is_404_and_a_deleted_ticket_still_renders(
    client: TestClient, db: Session
) -> None:
    """Ids are global integers; deleting is a status, not a hole in the world."""
    ticket = make_ticket(db, general(db), root(db), subject="Gone but not lost")
    sign_in(client)

    missing = client.get(f"/ticket/{ticket.id + 500}")
    assert missing.status_code == 404
    assert "There is no page at this address" in missing.text

    ticket.status = "deleted"
    db.commit()
    still = client.get(f"/ticket/{ticket.id}")
    assert still.status_code == 200
    assert "Gone but not lost" in still.text
    assert "<td>deleted</td>" in still.text

    # And it is gone from RT at a glance, which lists the active statuses.
    assert "Gone but not lost" not in client.get("/").text


def test_fp_t12_priority_is_shown_in_basics(client: TestClient, db: Session) -> None:
    """An integer 0-99, in the Basics box."""
    ticket = make_ticket(db, general(db), root(db), priority=17)
    sign_in(client)

    page = client.get(f"/ticket/{ticket.id}")
    assert "<th>Priority</th>" in page.text
    assert "<td>17</td>" in page.text

    # A ticket created through the form starts at 0 and shows it.
    assert create_form(client, db).status_code == 303
    fresh(db)
    made = db.scalars(select(Ticket).order_by(Ticket.id.desc())).first()
    assert made is not None
    assert made.priority == 0
    assert "<td>0</td>" in client.get(f"/ticket/{made.id}").text


def test_fp_t14_record_moves_last_updated_and_last_updated_by(db: Session) -> None:
    """Every transaction touches the ticket it belongs to (plan §10)."""
    ada = make_user(db, "ada")
    ticket = make_ticket(db, general(db), root(db))
    before = utcnow().replace(year=2000)
    ticket.last_updated = before
    ticket.last_updated_by = root(db).id
    db.commit()

    transaction = transactions.record(db, ticket, TransactionType.COMMENT, ada, body="Noted")
    db.commit()

    assert ticket.last_updated > before
    assert ticket.last_updated_by == ada.id
    assert transaction.ticket_id == ticket.id
    message = db.get(Message, transaction.id)
    assert message is not None
    assert message.body == "Noted"

    # And a transaction without a body writes no message.
    plain = transactions.record(db, ticket, TransactionType.STATUS, ada, old_value="new")
    db.commit()
    assert db.get(Message, plain.id) is None
