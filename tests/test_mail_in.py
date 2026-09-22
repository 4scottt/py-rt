"""Mail in: the parser, the gateway and the command (FP M01-M05, R09).

Plan §10's gateway protocol, from three sides: :func:`pyrt.mail.parse.parse_message`
on its own (M05), :func:`pyrt.mail.gateway.deliver` against the database
(M02-M04) and ``pyrt mailgate`` end to end with the argv the platform's
exercise body sends (M01, R09).

The rule these tests hold to the code is that **the sender is the actor**:
a message from a stranger is filed as that stranger, and what it may do is
what the stranger's rights allow, resolved through ``Everyone`` and through
the ticket's own ``Requestor`` role. Nothing here signs in.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from typing import Final

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from pyrt import cli
from pyrt.config import Settings
from pyrt.db.models import (
    DEFAULT_QUEUE_NAME,
    NO_SUBJECT,
    Message,
    Queue,
    Ticket,
    TicketWatcher,
    Transaction,
    TransactionType,
    User,
    WatcherRole,
)
from pyrt.mail import gateway
from pyrt.mail.parse import BAD_FROM, NO_FROM, ParseError, parse_message
from tests.conftest import TEST_DSN
from tests.test_acl import grant, group_id, make_queue
from tests.test_queues import fresh, queue_by_name

#: ``SITE_NAME`` in these tests, and so the subject tag of every queue that
#: has none of its own.
SITE: Final = "test"

#: The stranger who writes in: no account, no password, no rights but the
#: ones ``Everyone`` carries.
STRANGER: Final = "stranger@example.invalid"

#: The platform's exercise body sends exactly this (oldbox's
#: ``backend/hosting/exercise-app.sh``); R09 is that message, verbatim.
WORKLOAD_FROM: Final = "workload@oldbox.invalid"
WORKLOAD_TO: Final = "support@oldbox.invalid"
WORKLOAD_RUN: Final = "abc123"
WORKLOAD_BODY: Final = "The oldbox workload sent this message to exercise the mail interface."

EVERYONE: Final = "Everyone"
REQUESTOR_ROLE: Final = "Requestor"


# --- builders --------------------------------------------------------------


@pytest.fixture(autouse=True)
def _handlers_off() -> Iterator[None]:
    """Leave no log handler pointing at a captured stream behind.

    ``cli.main`` installs one on the root logger; a test that captured
    standard output would otherwise leave later tests logging into a
    stream pytest has closed.
    """
    yield
    logging.getLogger().handlers.clear()


def message(
    *,
    sender: str = STRANGER,
    to: str = WORKLOAD_TO,
    subject: str = "The printer is on fire",
    body: str = "It is, though.",
    message_id: str | None = None,
) -> bytes:
    """One plain-text message as bytes, the shape a gateway is handed."""
    lines = [f"From: {sender}", f"To: {to}", f"Subject: {subject}"]
    if message_id is not None:
        lines.append(f"Message-ID: {message_id}")
    lines.append("Content-Type: text/plain; charset=utf-8")
    lines.append("")
    lines.append(body)
    return ("\n".join(lines) + "\n").encode()


def workload_message(ticket: int | None = None, *, tag: str = SITE) -> bytes:
    """The exercise body's fixed message: a create, or a tagged reply."""
    subject = f"oldbox workload {WORKLOAD_RUN}"
    if ticket is not None:
        subject = f"[{tag} #{ticket}] {subject}"
    return (
        f"From: {WORKLOAD_FROM}\n"
        f"To: {WORKLOAD_TO}\n"
        f"Subject: {subject}\n"
        "Content-Type: text/plain; charset=utf-8\n"
        "\n"
        f"{WORKLOAD_BODY}\n"
    ).encode()


def settings(site_name: str = SITE) -> Settings:
    """The §6 environment the gateway needs: a site name, nothing else."""
    return Settings(site_name=site_name, base_url="http://testserver")


def deliver(
    db: Session,
    raw: bytes,
    *,
    queue: str = DEFAULT_QUEUE_NAME,
    action: str = "correspond",
    site_name: str = SITE,
) -> gateway.Result:
    """:func:`gateway.deliver` with this suite's defaults."""
    return gateway.deliver(db, settings(site_name), queue_name=queue, action=action, raw=raw)


def mailgate(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    raw: bytes,
    *argv: str,
    site_name: str = SITE,
) -> tuple[int, list[str]]:
    """Run ``pyrt mailgate`` over ``raw`` and give back its exit code and stdout.

    The environment is the card's minus the two serving settings: the
    gateway must run on ``DB_*``/``DATABASE_URL`` and ``SITE_NAME`` alone.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    monkeypatch.setenv("SITE_NAME", site_name)
    monkeypatch.delenv("BASE_URL", raising=False)
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    code = cli.main(["mailgate", *argv], stdin=io.BytesIO(raw))
    return code, capsys.readouterr().out.splitlines()


# --- reads -----------------------------------------------------------------


def general(db: Session) -> Queue:
    return queue_by_name(db, DEFAULT_QUEUE_NAME)


def everyone_may(db: Session, *rights: str, queue: Queue) -> None:
    """The mail goal's first subtask: grant ``Everyone`` on that queue (R09)."""
    for right in rights:
        grant(db, ("group", group_id(db, EVERYONE)), right, queue=queue)


def only_ticket(db: Session) -> Ticket:
    fresh(db)
    tickets = list(db.scalars(select(Ticket).order_by(Ticket.id)).all())
    assert len(tickets) == 1, tickets
    return tickets[0]


def tickets_of(db: Session) -> list[Ticket]:
    fresh(db)
    return list(db.scalars(select(Ticket).order_by(Ticket.id)).all())


def history(db: Session, ticket_id: int) -> list[Transaction]:
    fresh(db)
    return list(
        db.scalars(
            select(Transaction).where(Transaction.ticket_id == ticket_id).order_by(Transaction.id)
        ).all()
    )


def body_of(db: Session, transaction: Transaction) -> Message | None:
    return db.get(Message, transaction.id)


def user_with(db: Session, address: str) -> User:
    fresh(db)
    found = list(db.scalars(select(User).where(User.email == address)).all())
    assert len(found) == 1, found
    return found[0]


def requestors_of(db: Session, ticket_id: int) -> list[User]:
    fresh(db)
    return list(
        db.scalars(
            select(User)
            .join(TicketWatcher, TicketWatcher.user_id == User.id)
            .where(
                TicketWatcher.ticket_id == ticket_id,
                TicketWatcher.role == WatcherRole.REQUESTOR,
            )
        ).all()
    )


# --- M05: the parser -------------------------------------------------------


def test_fp_m05_the_parser_reads_the_sender_the_subject_and_the_text() -> None:
    """A plain message: the address lower-cased, the name, the id, the body."""
    raw = (
        b"From: Ada Lovelace <Ada@Example.Invalid>\n"
        b"To: support@oldbox.invalid\n"
        b"Subject: =?utf-8?q?caf=C3=A9?= trouble\n"
        b"Message-ID: <one@example.invalid>\n"
        b"Content-Type: text/plain; charset=utf-8\n"
        b"\n"
        b"The machine is cold.\n"
    )
    parsed = parse_message(raw)

    assert parsed.from_address == "ada@example.invalid"
    assert parsed.from_name == "Ada Lovelace"
    assert parsed.subject == "café trouble"
    assert parsed.message_id == "<one@example.invalid>"
    assert parsed.text.strip() == "The machine is cold."
    assert "Subject:" in parsed.headers
    assert "The machine is cold." not in parsed.headers


def test_the_parser_walks_a_multipart_to_the_plain_part() -> None:
    """``multipart/alternative`` with the HTML first: the plain part wins."""
    raw = (
        b"From: ada@example.invalid\n"
        b"Subject: Both\n"
        b"MIME-Version: 1.0\n"
        b'Content-Type: multipart/alternative; boundary="b"\n'
        b"\n"
        b"--b\n"
        b"Content-Type: text/html; charset=utf-8\n"
        b"\n"
        b"<p>The HTML one</p>\n"
        b"--b\n"
        b"Content-Type: text/plain; charset=utf-8\n"
        b"\n"
        b"The plain one\n"
        b"--b--\n"
    )
    assert parse_message(raw).text.strip() == "The plain one"


def test_the_parser_strips_an_html_only_message_to_text() -> None:
    """No plain part: the tags go, the blocks leave their line breaks."""
    raw = (
        b"From: ada@example.invalid\n"
        b"Subject: HTML\n"
        b"Content-Type: text/html; charset=utf-8\n"
        b"\n"
        b"<html><head><title>t</title><style>p { color: red }</style></head>"
        b"<body><p>Hello <b>world</b></p><p>Bye &amp; thanks</p>"
        b"<script>alert(1)</script></body></html>\n"
    )
    assert parse_message(raw).text == "Hello world\n\nBye & thanks"


def test_the_parser_honours_the_parts_charset() -> None:
    """``iso-8859-1`` bytes are the characters they stand for, not mojibake."""
    raw = (
        b"From: ada@example.invalid\n"
        b"Subject: Latin\n"
        b"Content-Type: text/plain; charset=iso-8859-1\n"
        b"\n"
        b"caf\xe9 ferm\xe9\n"
    )
    assert parse_message(raw).text.strip() == "café fermé"


def test_the_parser_accepts_a_message_with_no_subject() -> None:
    """A subject nobody wrote is empty, not a refusal."""
    raw = b"From: ada@example.invalid\nContent-Type: text/plain\n\nNothing to say.\n"
    parsed = parse_message(raw)
    assert parsed.subject == ""
    assert parsed.message_id is None


def test_the_parser_refuses_a_message_with_no_usable_from() -> None:
    """No ``From`` at all, and a ``From`` that is not an address."""
    with pytest.raises(ParseError) as missing:
        parse_message(b"Subject: Anonymous\n\nWho am I?\n")
    assert str(missing.value) == NO_FROM

    with pytest.raises(ParseError) as broken:
        parse_message(b"From: not an address\nSubject: Anonymous\n\nWho am I?\n")
    assert str(broken.value) == BAD_FROM


# --- M01: the command and its protocol -------------------------------------


def test_fp_m01_the_command_prints_ok_and_the_ticket_or_not_ok(
    db: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The two lines on success, ``not ok: <reason>`` and exit 1 otherwise."""
    everyone_may(db, "CreateTicket", queue=general(db))

    code, out = mailgate(
        monkeypatch,
        capsys,
        message(subject="A ticket by mail"),
        "--queue",
        DEFAULT_QUEUE_NAME,
        "--action",
        "correspond",
        "--url",
        "http://127.0.0.1:8084",
        "--debug",
    )

    ticket = only_ticket(db)
    assert code == 0
    assert out == ["ok", f"Ticket: {ticket.id}"]

    # A refusal says why, on one line, and leaves the exit code behind.
    code, out = mailgate(
        monkeypatch, capsys, message(), "--queue", "Nowhere", "--action", "correspond"
    )
    assert code == 1
    assert out == ["not ok: no such queue Nowhere"]
    assert len(tickets_of(db)) == 1


def test_the_command_refuses_an_action_it_does_not_speak(
    db: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An action outside the two is the protocol's refusal, not a stack trace."""
    everyone_may(db, "CreateTicket", queue=general(db))
    code, out = mailgate(
        monkeypatch, capsys, message(), "--queue", DEFAULT_QUEUE_NAME, "--action", "shout"
    )
    assert code == 1
    assert out == ["not ok: unknown action: shout"]


# --- M02: a message with no tag opens a ticket ------------------------------


def test_fp_m02_an_untagged_message_opens_a_ticket_with_the_sender_as_requestor(
    db: Session,
) -> None:
    """The queue is the one named, the requestor the sender, the text the body."""
    queue = general(db)
    everyone_may(db, "CreateTicket", queue=queue)

    result = deliver(
        db,
        message(subject="The printer is on fire", message_id="<first@example.invalid>"),
    )
    assert result.ok and result.reason == ""

    ticket = only_ticket(db)
    assert result.ticket_id == ticket.id
    assert ticket.queue_id == queue.id
    assert ticket.subject == "The printer is on fire"
    assert ticket.status == "new"

    sender = user_with(db, STRANGER)
    assert sender.name == STRANGER
    assert sender.privileged is False
    assert sender.disabled is False
    assert sender.password_hash is None
    assert [user.id for user in requestors_of(db, ticket.id)] == [sender.id]

    rows = history(db, ticket.id)
    assert [row.type for row in rows] == [TransactionType.CREATE]
    assert rows[0].creator_id == sender.id
    stored = body_of(db, rows[0])
    assert stored is not None
    assert stored.body.strip() == "It is, though."
    assert stored.content_type == "text/plain"
    assert stored.message_id == "<first@example.invalid>"
    assert stored.headers is not None and "To: support@oldbox.invalid" in stored.headers


def test_a_second_message_from_the_same_address_reuses_the_user(db: Session) -> None:
    """One address, one user, however many tickets it opens."""
    everyone_may(db, "CreateTicket", queue=general(db))

    first = deliver(db, message(subject="One"))
    second = deliver(db, message(sender=STRANGER.upper(), subject="Two"))

    assert first.ok and second.ok
    assert len(tickets_of(db)) == 2
    user_with(db, STRANGER)  # asserts there is exactly one


def test_a_message_with_no_subject_is_filed_as_no_subject(db: Session) -> None:
    """The ticket still has a name, the one the rest of the product uses."""
    everyone_may(db, "CreateTicket", queue=general(db))
    raw = b"From: stranger@example.invalid\nContent-Type: text/plain\n\nJust this.\n"

    assert deliver(db, raw).ok
    assert only_ticket(db).subject == NO_SUBJECT


def test_the_queue_is_the_argument_never_the_to_header(db: Session) -> None:
    """``To: support@oldbox.invalid`` is one address for every queue there is."""
    support = make_queue(db, "Support")
    everyone_may(db, "CreateTicket", queue=support)

    result = deliver(db, message(to="general@oldbox.invalid"), queue="Support")

    assert result.ok
    assert only_ticket(db).queue_id == support.id


def test_a_disabled_queue_takes_no_mail(db: Session) -> None:
    """A queue that is off says what a queue that is not there says."""
    queue = general(db)
    everyone_may(db, "CreateTicket", queue=queue)
    queue.disabled = True
    db.commit()

    result = deliver(db, message())

    assert result.ok is False
    assert result.reason == f"no such queue {DEFAULT_QUEUE_NAME}"
    assert tickets_of(db) == []


# --- M03: the subject tag ---------------------------------------------------


def test_fp_m03_a_tagged_subject_threads_onto_that_ticket(db: Session) -> None:
    """``[<SITE_NAME> #<id>]`` is a ``Correspond``, and it opens a new ticket."""
    queue = general(db)
    everyone_may(db, "CreateTicket", "ReplyToTicket", queue=queue)

    opened = deliver(db, message(subject="The printer is on fire"))
    assert opened.ok and opened.ticket_id is not None

    replied = deliver(
        db,
        message(
            subject=f"[{SITE} #{opened.ticket_id}] The printer is on fire",
            body="Still burning.",
        ),
    )

    assert replied.ok
    assert replied.ticket_id == opened.ticket_id
    assert len(tickets_of(db)) == 1

    rows = history(db, opened.ticket_id)
    assert [row.type for row in rows] == [
        TransactionType.CREATE,
        TransactionType.CORRESPOND,
        TransactionType.STATUS,
    ]
    correspond = body_of(db, rows[1])
    assert correspond is not None and correspond.body.strip() == "Still burning."
    assert (rows[2].old_value, rows[2].new_value) == ("new", "open")

    fresh(db)
    ticket = db.get(Ticket, opened.ticket_id)
    assert ticket is not None and ticket.status == "open"


def test_a_queue_with_its_own_subject_tag_threads_with_that_name(db: Session) -> None:
    """FP Q06's tag is the one a reply may carry, beside ``SITE_NAME``."""
    support = make_queue(db, "Support")
    support.subject_tag = "SUP"
    db.commit()
    everyone_may(db, "CreateTicket", "ReplyToTicket", queue=support)

    opened = deliver(db, message(subject="Tagged"), queue="Support")
    assert opened.ok and opened.ticket_id is not None

    replied = deliver(
        db,
        message(subject=f"[SUP #{opened.ticket_id}] Tagged", body="Again."),
        queue="Support",
    )

    assert replied.ticket_id == opened.ticket_id
    assert [row.type for row in history(db, opened.ticket_id)] == [
        TransactionType.CREATE,
        TransactionType.CORRESPOND,
        TransactionType.STATUS,
    ]


def test_a_tag_with_an_unknown_id_opens_a_new_ticket(db: Session) -> None:
    """Our tag, nobody's ticket: a new one, and the tag is not in its subject."""
    everyone_may(db, "CreateTicket", queue=general(db))

    result = deliver(db, message(subject=f"[{SITE} #9999] Lost reply"))

    assert result.ok
    assert only_ticket(db).subject == "Lost reply"


def test_a_tag_that_is_not_ours_stays_in_the_subject(db: Session) -> None:
    """Another tracker's tag is part of what the person wrote."""
    everyone_may(db, "CreateTicket", queue=general(db))
    opened = deliver(db, message(subject="First"))
    assert opened.ok and opened.ticket_id is not None

    result = deliver(db, message(subject=f"[OtherRT #{opened.ticket_id}] Forwarded"))

    assert result.ok and result.ticket_id != opened.ticket_id
    tickets = tickets_of(db)
    assert len(tickets) == 2
    assert tickets[1].subject == f"[OtherRT #{opened.ticket_id}] Forwarded"


def test_a_comment_action_threads_a_comment_not_a_correspondence(db: Session) -> None:
    """``--action comment`` is the staff aside: no status move, no mail."""
    queue = general(db)
    everyone_may(db, "CreateTicket", "CommentOnTicket", queue=queue)
    opened = deliver(db, message(subject="Quiet"))
    assert opened.ok and opened.ticket_id is not None

    replied = deliver(
        db,
        message(subject=f"[{SITE} #{opened.ticket_id}] Quiet", body="Between us."),
        action="comment",
    )

    assert replied.ok
    rows = history(db, opened.ticket_id)
    assert [row.type for row in rows] == [TransactionType.CREATE, TransactionType.COMMENT]
    fresh(db)
    ticket = db.get(Ticket, opened.ticket_id)
    assert ticket is not None and ticket.status == "new"


# --- M04: the rights --------------------------------------------------------


def test_fp_m04_the_sender_needs_the_right_to_create_and_to_reply(db: Session) -> None:
    """An unknown sender is refused until ``Everyone`` is granted the right."""
    queue = general(db)

    refused = deliver(db, message())
    assert refused.ok is False
    assert refused.reason == "permission denied: CreateTicket"
    assert tickets_of(db) == []
    # The refusal was rolled back: not even the sender's user row survived.
    assert db.scalars(select(User).where(User.email == STRANGER)).first() is None

    everyone_may(db, "CreateTicket", queue=queue)
    opened = deliver(db, message())
    assert opened.ok and opened.ticket_id is not None

    # Replying is a second right, and it is asked for separately.
    denied = deliver(
        db,
        message(
            sender="other@example.invalid",
            subject=f"[{SITE} #{opened.ticket_id}] The printer is on fire",
        ),
    )
    assert denied.ok is False
    assert denied.reason == "permission denied: ReplyToTicket"
    assert [row.type for row in history(db, opened.ticket_id)] == [TransactionType.CREATE]

    everyone_may(db, "ReplyToTicket", queue=queue)
    allowed = deliver(
        db,
        message(
            sender="other@example.invalid",
            subject=f"[{SITE} #{opened.ticket_id}] The printer is on fire",
        ),
    )
    assert allowed.ok and allowed.ticket_id == opened.ticket_id


def test_a_requestor_may_reply_through_the_requestor_role(db: Session) -> None:
    """The right is resolved with the ticket in hand, so a role reaches it."""
    queue = general(db)
    everyone_may(db, "CreateTicket", queue=queue)
    grant(db, ("group", group_id(db, REQUESTOR_ROLE)), "ReplyToTicket", queue=queue)

    opened = deliver(db, message())
    assert opened.ok and opened.ticket_id is not None

    mine = deliver(db, message(subject=f"[{SITE} #{opened.ticket_id}] The printer is on fire"))
    theirs = deliver(
        db,
        message(
            sender="other@example.invalid",
            subject=f"[{SITE} #{opened.ticket_id}] The printer is on fire",
        ),
    )

    assert mine.ok and mine.ticket_id == opened.ticket_id
    assert theirs.ok is False
    assert theirs.reason == "permission denied: ReplyToTicket"


# --- R09: the platform's own workload ---------------------------------------


def test_fp_r09_everyone_granted_lets_the_workload_open_and_answer_a_ticket(
    db: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The mail goal's first subtask, end to end, with the body's own message.

    ``CreateTicket`` and ``ReplyToTicket`` to ``Everyone`` on General is the
    whole configuration; the argv, the headers and the two lines read back
    are the exercise body's (``oldbox/backend/hosting/exercise-app.sh``).
    """
    everyone_may(db, "CreateTicket", "ReplyToTicket", queue=general(db))

    code, out = mailgate(
        monkeypatch,
        capsys,
        workload_message(),
        "--queue",
        DEFAULT_QUEUE_NAME,
        "--action",
        "correspond",
        "--url",
        "http://127.0.0.1:8084",
        "--debug",
        site_name="localhost",
    )
    ticket = only_ticket(db)
    assert code == 0
    assert out == ["ok", f"Ticket: {ticket.id}"]
    assert ticket.subject == f"oldbox workload {WORKLOAD_RUN}"
    assert [user.email for user in requestors_of(db, ticket.id)] == [WORKLOAD_FROM]

    code, out = mailgate(
        monkeypatch,
        capsys,
        workload_message(ticket.id, tag="localhost"),
        "--queue",
        DEFAULT_QUEUE_NAME,
        "--action",
        "correspond",
        "--url",
        "http://127.0.0.1:8084",
        "--debug",
        site_name="localhost",
    )

    assert code == 0
    assert out == ["ok", f"Ticket: {ticket.id}"]
    assert len(tickets_of(db)) == 1
    rows = history(db, ticket.id)
    assert [row.type for row in rows] == [
        TransactionType.CREATE,
        TransactionType.CORRESPOND,
        TransactionType.STATUS,
    ]
    correspond = body_of(db, rows[1])
    assert correspond is not None and correspond.body.strip() == WORKLOAD_BODY
