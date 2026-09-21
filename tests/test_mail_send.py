"""Outgoing mail: the senders (M06), the notifications (M07), the tag (M08).

The rule these tests hold to the code is plan §6's: no real mail leaves the
container unless ``MAIL_MODE=smtp`` says so. ``smtplib.SMTP`` is
monkeypatched wherever the SMTP path is exercised, so the suite never opens
a socket.
"""

from __future__ import annotations

import re
import smtplib
from collections.abc import Iterator
from email import message_from_string
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from sqlalchemy.orm import Session

from pyrt.config import Settings
from pyrt.db.models import (
    Message,
    Queue,
    Ticket,
    TicketWatcher,
    Transaction,
    TransactionType,
    User,
    WatcherRole,
)
from pyrt.mail import notify, send
from pyrt.queues.service import subject_tag_for
from tests.test_acl import make_queue
from tests.test_auth import make_user

SITE = "test"
BASE_URL = "http://localhost:8082"


# --- fixtures and builders -------------------------------------------------


@pytest.fixture(autouse=True)
def _no_configured_sender() -> Iterator[None]:
    """Every test starts and ends with no process-wide sender."""
    send.reset()
    yield
    send.reset()


def settings(**overrides: Any) -> Settings:
    """The §6 environment a mail test needs, defaults as the card leaves them."""
    return Settings(base_url=BASE_URL, site_name=SITE, **overrides)


def make_ticket(
    db: Session,
    queue: Queue,
    owner: User,
    *,
    subject: str = "The printer is on fire",
    status: str = "new",
) -> Ticket:
    ticket = Ticket(
        queue_id=queue.id,
        owner_id=owner.id,
        subject=subject,
        status=status,
        creator_id=owner.id,
        last_updated_by=owner.id,
    )
    db.add(ticket)
    db.commit()
    return ticket


def add_requestor(db: Session, ticket: Ticket, user: User) -> None:
    db.add(TicketWatcher(ticket_id=ticket.id, user_id=user.id, role=WatcherRole.REQUESTOR))
    db.commit()


def make_transaction(
    db: Session,
    ticket: Ticket,
    actor: User,
    kind: TransactionType,
    *,
    body: str = "",
    new_value: str | None = None,
    old_value: str | None = None,
) -> Transaction:
    transaction = Transaction(
        ticket_id=ticket.id,
        type=kind,
        creator_id=actor.id,
        old_value=old_value,
        new_value=new_value,
    )
    db.add(transaction)
    db.commit()
    if body:
        db.add(Message(transaction_id=transaction.id, body=body))
        db.commit()
    return transaction


def a_ticket_with_a_requestor(
    db: Session, *, requestor: str = "customer"
) -> tuple[Ticket, User, User]:
    """A General-queue ticket owned by root with one mailable requestor."""
    queue = make_queue(db, "Support")
    owner = make_user(db, "owner", privileged=True)
    asker = make_user(db, requestor)
    ticket = make_ticket(db, queue, owner)
    add_requestor(db, ticket, asker)
    return ticket, owner, asker


class FakeSMTP:
    """A stand-in for ``smtplib.SMTP`` that records instead of connecting."""

    def __init__(self, log: list[Any], *, offers_starttls: bool = True) -> None:
        self.log = log
        self.offers_starttls = offers_starttls
        self.sent: list[Any] = []

    def __call__(self, host: str, port: int, timeout: int | None = None) -> FakeSMTP:
        self.log.append(("connect", host, port, timeout))
        return self

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> bool:
        self.log.append(("quit",))
        return False

    def ehlo(self) -> None:
        self.log.append(("ehlo",))

    def has_extn(self, name: str) -> bool:
        self.log.append(("has_extn", name))
        return self.offers_starttls

    def starttls(self) -> None:
        self.log.append(("starttls",))

    def login(self, user: str, password: str) -> None:
        self.log.append(("login", user, password))

    def send_message(self, message: Any) -> None:
        self.log.append(("send_message", message["To"], message["Subject"]))
        self.sent.append(message)


def refuse_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any attempt to open an SMTP connection a test failure."""

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("mail must not reach a relay unless MAIL_MODE=smtp")

    monkeypatch.setattr(smtplib, "SMTP", boom)


# --- M06: the senders ------------------------------------------------------


def test_fp_m06_unset_mail_mode_is_the_log_sender_and_never_connects(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``MAIL_MODE`` unset means ``log``: one line, nothing leaves (M06)."""
    refuse_smtp(monkeypatch)
    sender = send.sender_from_settings(settings())
    assert isinstance(sender, send.LogSender)

    mail = send.Mail.for_ticket(
        settings(), ticket_id=7, to=["a@example.invalid"], subject="[test #7] hi", body="x" * 500
    )
    with caplog.at_level("INFO", logger="pyrt.mail.send"):
        sender.send(mail)

    (record,) = [r for r in caplog.records if r.getMessage() == "mail"]
    assert record.to == "a@example.invalid"  # type: ignore[attr-defined]
    assert record.subject == "[test #7] hi"  # type: ignore[attr-defined]
    assert record.ticket == 7  # type: ignore[attr-defined]
    assert len(record.body) == send.LOG_BODY_CHARS  # type: ignore[attr-defined]


def test_fp_m06_smtp_settings_under_another_mode_send_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``SMTP_HOST`` set with ``MAIL_MODE`` unset is still the log sender."""
    refuse_smtp(monkeypatch)
    sender = send.sender_from_settings(
        settings(smtp_host="relay.example.invalid", smtp_port=2525, smtp_user="bob")
    )
    assert isinstance(sender, send.LogSender)
    sender.send(
        send.Mail.for_ticket(settings(), ticket_id=1, to=["a@b.invalid"], subject="s", body="b")
    )

    # And the same under MAIL_MODE=file: only "smtp" opens a socket.
    assert isinstance(
        send.sender_from_settings(settings(mail_mode="file", smtp_host="relay.example.invalid")),
        send.FileSender,
    )


def test_fp_m06_the_file_sender_appends_each_message_with_the_tag_and_the_link(
    db: Session, tmp_path: Path
) -> None:
    """``MAIL_MODE=file`` appends RFC 5322 messages to ``MAIL_FILE``."""
    mail_file = tmp_path / "mail" / "mail.log"
    config = settings(mail_mode="file", mail_file=str(mail_file))
    sender = send.sender_from_settings(config)
    assert isinstance(sender, send.FileSender)
    send.configure(sender)

    ticket, owner, asker = a_ticket_with_a_requestor(db)
    transaction = make_transaction(
        db, ticket, asker, TransactionType.CREATE, body="It is really on fire."
    )
    notify.on_created(db, config, ticket, transaction, asker)
    notify.on_status_changed(
        db,
        config,
        ticket,
        make_transaction(db, ticket, owner, TransactionType.STATUS, new_value="resolved"),
        owner,
    )

    text = mail_file.read_text(encoding="utf-8")
    assert f"[{SITE} #{ticket.id}]" in text
    assert f"{BASE_URL}/ticket/{ticket.id}" in text
    assert "It is really on fire." in text

    assert "\n\nFrom: py-rt@" in text, "a blank line separates the messages"
    blocks = [block for block in re.split(r"(?m)^(?=From: py-rt@)", text) if block.strip()]
    assert len(blocks) == 2, text
    first = message_from_string(blocks[0])
    assert first["To"] == str(asker.email)
    assert first[send.TICKET_HEADER] == str(ticket.id)
    assert first["From"] == "py-rt@localhost"


def test_fp_m06_the_smtp_sender_is_chosen_only_for_smtp_mode_and_talks_to_the_relay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``MAIL_MODE=smtp``: STARTTLS when offered, login when a user is set."""
    calls: list[Any] = []
    fake = FakeSMTP(calls)
    monkeypatch.setattr(smtplib, "SMTP", fake)

    config = settings(
        mail_mode="smtp",
        smtp_host="relay.example.invalid",
        smtp_port=2525,
        smtp_user="postmaster",
        smtp_password="secret",
    )
    sender = send.sender_from_settings(config)
    assert isinstance(sender, send.SmtpSender)
    sender.send(
        send.Mail.for_ticket(
            config, ticket_id=9, to=["a@example.invalid"], subject="[test #9] hi", body="body"
        )
    )

    assert calls[0] == ("connect", "relay.example.invalid", 2525, send.SMTP_TIMEOUT)
    assert ("starttls",) in calls
    assert ("login", "postmaster", "secret") in calls
    assert ("send_message", "a@example.invalid", "[test #9] hi") in calls
    assert fake.sent[0][send.TICKET_HEADER] == "9"


def test_fp_m06_the_smtp_sender_skips_starttls_and_login_when_they_do_not_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain local relay: no TLS offered, no user set, still delivered."""
    calls: list[Any] = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP(calls, offers_starttls=False))

    config = settings(mail_mode="smtp", smtp_host="localhost")
    send.sender_from_settings(config).send(
        send.Mail.for_ticket(config, ticket_id=3, to=["a@b.invalid"], subject="s", body="b")
    )

    assert ("starttls",) not in calls
    assert not [call for call in calls if call[0] == "login"]
    assert [call for call in calls if call[0] == "send_message"]


def test_fp_m06_smtp_mode_without_a_host_falls_back_to_the_log_sender(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misconfiguration logs rather than guessing at localhost."""
    refuse_smtp(monkeypatch)
    assert isinstance(send.sender_from_settings(settings(mail_mode="smtp")), send.LogSender)


def test_fp_m06_current_is_the_configured_sender_and_derives_one_lazily() -> None:
    """The app configures once; until it does, settings decide."""
    recorder = send.RecordingSender()
    send.configure(recorder)
    assert send.current(settings()) is recorder

    send.reset()
    assert isinstance(send.current(settings(mail_mode="file", mail_file="/tmp/x")), send.FileSender)


# --- M07: the three notifications ------------------------------------------


def recorder_for(config: Settings) -> send.RecordingSender:
    recorder = send.RecordingSender()
    send.configure(recorder)
    return recorder


def test_fp_m07_create_mails_the_requestors_with_the_link_and_the_message(
    db: Session,
) -> None:
    """The autoreply: every requestor with an address, the body, the link."""
    config = settings()
    recorder = recorder_for(config)
    ticket, _owner, asker = a_ticket_with_a_requestor(db)
    silent = make_user(db, "nomail")
    silent.email = None
    db.commit()
    add_requestor(db, ticket, silent)
    transaction = make_transaction(
        db, ticket, asker, TransactionType.CREATE, body="Smoke everywhere."
    )

    notify.on_created(db, config, ticket, transaction, asker)

    (mail,) = recorder.sent
    assert mail.to == [str(asker.email)]  # the requestor without an address is skipped
    assert mail.subject == f"[{SITE} #{ticket.id}] {ticket.subject}"
    assert f"{BASE_URL}/ticket/{ticket.id}" in mail.body
    assert "Smoke everywhere." in mail.body
    assert mail.headers[send.TICKET_HEADER] == str(ticket.id)
    assert mail.from_address == "py-rt@localhost"


def test_fp_m07_correspond_mails_the_requestors_but_never_the_actor(db: Session) -> None:
    """A reply from the requestor is not mailed back to them (M07)."""
    config = settings()
    recorder = recorder_for(config)
    ticket, owner, asker = a_ticket_with_a_requestor(db)
    second = make_user(db, "colleague")
    add_requestor(db, ticket, second)

    notify.on_corresponded(
        db,
        config,
        ticket,
        make_transaction(db, ticket, asker, TransactionType.CORRESPOND, body="Any news?"),
        asker,
    )
    assert recorder.recipients == [str(second.email)]

    notify.on_corresponded(
        db,
        config,
        ticket,
        make_transaction(db, ticket, owner, TransactionType.CORRESPOND, body="On our way."),
        owner,
    )
    reply = recorder.sent[-1]
    assert sorted(reply.to) == sorted([str(asker.email), str(second.email)])
    assert "On our way." in reply.body
    assert f"{BASE_URL}/ticket/{ticket.id}" in reply.body


def test_fp_m07_resolve_mails_the_requestors_and_another_status_does_not(db: Session) -> None:
    """Only ``resolved`` notifies; ``open`` and ``stalled`` are silent."""
    config = settings()
    recorder = recorder_for(config)
    ticket, owner, asker = a_ticket_with_a_requestor(db)

    for status in ("open", "stalled", "rejected"):
        notify.on_status_changed(
            db,
            config,
            ticket,
            make_transaction(db, ticket, owner, TransactionType.STATUS, new_value=status),
            owner,
        )
    assert recorder.sent == []

    notify.on_status_changed(
        db,
        config,
        ticket,
        make_transaction(
            db, ticket, owner, TransactionType.STATUS, old_value="open", new_value="resolved"
        ),
        owner,
    )
    (mail,) = recorder.sent
    assert mail.to == [str(asker.email)]
    assert "resolved" in mail.body
    assert f"{BASE_URL}/ticket/{ticket.id}" in mail.body


def test_fp_m07_a_comment_never_mails_the_requestor(db: Session) -> None:
    """T06's rule, held by the hook that deliberately does nothing."""
    config = settings()
    recorder = recorder_for(config)
    ticket, owner, _asker = a_ticket_with_a_requestor(db)

    notify.on_commented(
        db,
        config,
        ticket,
        make_transaction(db, ticket, owner, TransactionType.COMMENT, body="Internal note."),
        owner,
    )
    assert recorder.sent == []


def test_fp_m07_register_appends_the_four_hooks_to_the_tickets_seam() -> None:
    """The registration the app makes, without importing pyrt.tickets."""
    hooks = ModuleType("fake_hooks")
    hooks.created = []  # type: ignore[attr-defined]
    hooks.corresponded = []  # type: ignore[attr-defined]
    hooks.commented = []  # type: ignore[attr-defined]
    hooks.status_changed = []  # type: ignore[attr-defined]

    notify.register(hooks)
    notify.register(hooks)  # idempotent: many apps in one process, one mail

    assert hooks.created == [notify.on_created]  # type: ignore[attr-defined]
    assert hooks.corresponded == [notify.on_corresponded]  # type: ignore[attr-defined]
    assert hooks.commented == [notify.on_commented]  # type: ignore[attr-defined]
    assert hooks.status_changed == [notify.on_status_changed]  # type: ignore[attr-defined]


def test_a_failing_sender_does_not_break_the_ticket_write(db: Session) -> None:
    """Mail is a notification: a back end that throws is logged, not raised."""
    config = settings()

    class Broken:
        def send(self, mail: send.Mail) -> None:
            raise RuntimeError("relay down")

    send.configure(Broken())
    ticket, _owner, asker = a_ticket_with_a_requestor(db)
    transaction = make_transaction(db, ticket, asker, TransactionType.CREATE, body="hello")

    notify.on_created(db, config, ticket, transaction, asker)  # must not raise


# --- M08: the subject tag round-trips --------------------------------------


def test_fp_m08_the_subject_carries_the_tag_a_reply_threads_on(db: Session) -> None:
    """``[<SITE_NAME> #<id>]`` in the subject, matched by the parser's regex."""
    config = settings()
    recorder = recorder_for(config)
    ticket, _owner, asker = a_ticket_with_a_requestor(db)
    queue = db.get(Queue, ticket.queue_id)
    assert queue is not None

    notify.on_created(
        db, config, ticket, make_transaction(db, ticket, asker, TransactionType.CREATE), asker
    )

    (mail,) = recorder.sent
    match = notify.TAG_RE.search(mail.subject)
    assert match is not None, mail.subject
    assert match.group("name") == subject_tag_for(queue, SITE) == SITE
    assert int(match.group("id")) == ticket.id
    assert mail.subject.endswith(ticket.subject)


def test_fp_m08_a_queues_own_subject_tag_replaces_the_site_name(db: Session) -> None:
    """FP Q06 seen from the mail side: the queue's tag wins."""
    config = settings()
    recorder = recorder_for(config)
    ticket, owner, _asker = a_ticket_with_a_requestor(db)
    queue = db.get(Queue, ticket.queue_id)
    assert queue is not None
    queue.subject_tag = "Support Desk"
    db.commit()

    notify.on_status_changed(
        db,
        config,
        ticket,
        make_transaction(db, ticket, owner, TransactionType.STATUS, new_value="resolved"),
        owner,
    )

    (mail,) = recorder.sent
    match = notify.TAG_RE.search(mail.subject)
    assert match is not None, mail.subject
    assert match.group("name") == subject_tag_for(queue, SITE) == "Support Desk"
    assert int(match.group("id")) == ticket.id
