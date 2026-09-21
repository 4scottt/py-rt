"""Replying, commenting, resolving and modifying a ticket (FP T05-T10, T13).

Plan §8's update and basics forms, §10's lifecycle and transaction record.
The gates are FP R06's: ``ReplyToTicket`` and ``CommentOnTicket`` gate the
update page's radio, ``ModifyTicket`` gates basics and every status change,
``OwnTicket`` on the queue decides who the owner select offers.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from pyrt.db.models import (
    DEFAULT_QUEUE_NAME,
    NOBODY_USER_NAME,
    Message,
    Ticket,
    Transaction,
    TransactionType,
)
from pyrt.mail import send
from pyrt.tickets import lifecycle, update
from tests.test_acl import grant, make_queue
from tests.test_auth import make_user, sign_in
from tests.test_queues import fresh
from tests.test_tickets import general, make_ticket, nobody, root

FORBIDDEN = "You are not allowed"


# --- helpers ---------------------------------------------------------------


def rows(db: Session, ticket: Ticket) -> list[Transaction]:
    """The ticket's transactions, oldest first, after the app's commit."""
    fresh(db)
    return list(
        db.scalars(
            select(Transaction).where(Transaction.ticket_id == ticket.id).order_by(Transaction.id)
        ).all()
    )


def body_of(db: Session, transaction: Transaction) -> str:
    """A transaction's message body, or "" when it carries none."""
    message = db.get(Message, transaction.id)
    return message.body if message else ""


def post_update(client: TestClient, ticket: Ticket, **fields: str) -> Response:
    """POST the update form, the defaults a browser would send."""
    data = {"UpdateType": "respond", "status": "", "content": ""}
    data.update(fields)
    return client.post(f"/ticket/{ticket.id}/update", data=data, follow_redirects=False)


def post_basics(client: TestClient, db: Session, ticket: Ticket, **fields: str) -> Response:
    """POST the basics form, every field as the page would carry it back."""
    fresh(db)
    data = {
        "subject": ticket.subject,
        "queue": str(ticket.queue_id),
        "status": ticket.status,
        "owner": str(ticket.owner_id),
        "priority": str(ticket.priority),
    }
    data.update(fields)
    return client.post(f"/ticket/{ticket.id}/basics", data=data, follow_redirects=False)


# --- T05: reply ------------------------------------------------------------


def test_fp_t05_a_reply_records_a_correspond_and_opens_a_new_ticket(
    client: TestClient, db: Session
) -> None:
    """The Correspond with its body, and the new -> open of plan §10."""
    ticket = make_ticket(db, general(db), root(db))
    sign_in(client)

    posted = post_update(client, ticket, content="We are on it.")
    assert posted.status_code == 303
    assert posted.headers["location"] == f"http://testserver/ticket/{ticket.id}"

    written = rows(db, ticket)
    assert [row.type for row in written] == [
        TransactionType.CREATE,
        TransactionType.CORRESPOND,
        TransactionType.STATUS,
    ]
    assert body_of(db, written[1]) == "We are on it."
    assert written[1].creator_id == root(db).id
    assert (written[2].field, written[2].old_value, written[2].new_value) == (
        "Status",
        "new",
        "open",
    )

    assert ticket.status == "open"
    assert ticket.started is not None  # the first leave from new
    assert ticket.resolved is None
    assert ticket.last_updated_by == root(db).id

    # The second reply is a reply and nothing more: the ticket is open already.
    assert post_update(client, ticket, content="Still on it.").status_code == 303
    again = rows(db, ticket)
    assert [row.type for row in again[3:]] == [TransactionType.CORRESPOND]
    assert ticket.status == "open"


def test_fp_t05_a_reply_needs_reply_to_ticket(client: TestClient, db: Session) -> None:
    """ShowTicket opens the page; only ReplyToTicket writes on it."""
    ada = make_user(db, "ada", "ada-password", privileged=True)
    ticket = make_ticket(db, general(db), root(db))
    grant(db, ("user", ada.id), "ShowTicket", queue=general(db))

    sign_in(client, "ada", "ada-password")
    # Neither reply, nor comment, nor modify: there is no form to show her.
    assert client.get(f"/ticket/{ticket.id}/update").status_code == 403

    refused = post_update(client, ticket, content="Hello")
    assert refused.status_code == 403
    assert FORBIDDEN in refused.text
    assert len(rows(db, ticket)) == 1

    grant(db, ("user", ada.id), "ReplyToTicket", queue=general(db))
    assert post_update(client, ticket, content="Hello").status_code == 303
    assert [row.type for row in rows(db, ticket)][1] == TransactionType.CORRESPOND


def test_fp_t05_an_empty_update_changes_nothing(client: TestClient, db: Session) -> None:
    """No content and no status move is a message, not a transaction."""
    ticket = make_ticket(db, general(db), root(db))
    sign_in(client)

    posted = post_update(client, ticket, content="   ", status="new")
    assert posted.status_code == 200
    assert update.NOTHING_TO_UPDATE in posted.text
    assert len(rows(db, ticket)) == 1
    assert ticket.status == "new"


# --- T06: comment ----------------------------------------------------------


def test_fp_t06_a_comment_is_recorded_and_never_mailed(client: TestClient, db: Session) -> None:
    """The Comment row, and the requestor hearing only about the reply."""
    ada = make_user(db, "ada", "ada-password")
    ticket = make_ticket(db, general(db), root(db), requestor=ada)
    recorder = send.RecordingSender()
    send.configure(recorder)
    try:
        sign_in(client)
        commented = post_update(
            client, ticket, UpdateType="comment", content="She is a known trouble caller"
        )
        assert commented.status_code == 303
        written = rows(db, ticket)
        assert written[1].type == TransactionType.COMMENT
        assert body_of(db, written[1]) == "She is a known trouble caller"
        # A comment is internal: nothing left the process, and the ticket did
        # not move (the new -> open of T05 belongs to a reply).
        assert recorder.sent == []
        assert ticket.status == "new"

        assert post_update(client, ticket, content="We are on it.").status_code == 303
        assert len(recorder.sent) == 1
        assert recorder.recipients == ["ada@example.invalid"]
    finally:
        send.reset()


def test_fp_t06_a_comment_needs_comment_on_ticket(client: TestClient, db: Session) -> None:
    """Replying is not commenting: the second right is its own."""
    ada = make_user(db, "ada", "ada-password", privileged=True)
    ticket = make_ticket(db, general(db), root(db))
    grant(db, ("user", ada.id), "ShowTicket", queue=general(db))
    grant(db, ("user", ada.id), "ReplyToTicket", queue=general(db))

    sign_in(client, "ada", "ada-password")
    refused = post_update(client, ticket, UpdateType="comment", content="Between us")
    assert refused.status_code == 403
    assert FORBIDDEN in refused.text
    assert len(rows(db, ticket)) == 1

    grant(db, ("user", ada.id), "CommentOnTicket", queue=general(db))
    allowed = post_update(client, ticket, UpdateType="comment", content="Between us")
    assert allowed.status_code == 303
    assert rows(db, ticket)[1].type == TransactionType.COMMENT


def test_fp_t06_a_comment_body_is_hidden_from_a_viewer_without_the_right(
    client: TestClient, db: Session
) -> None:
    """T04's rule from the other side: the row stays, the words do not."""
    ada = make_user(db, "ada", "ada-password", privileged=True)
    ticket = make_ticket(db, general(db), root(db))
    grant(db, ("user", ada.id), "ShowTicket", queue=general(db))

    sign_in(client)
    assert (
        post_update(
            client, ticket, UpdateType="comment", content="She is a known trouble caller"
        ).status_code
        == 303
    )

    sign_in(client, "ada", "ada-password")
    page = client.get(f"/ticket/{ticket.id}")
    assert "Comment added" in page.text
    assert "known trouble caller" not in page.text
    assert "This comment is not shown to you" in page.text


# --- T07: the status change ------------------------------------------------


def test_fp_t07_the_lifecycle_offers_only_its_transitions(db: Session) -> None:
    """Plan §10's map, and the select that starts at the current status."""
    assert lifecycle.transitions("new") == ("open", "stalled", "resolved", "rejected", "deleted")
    assert lifecycle.transitions("open") == ("stalled", "resolved", "rejected", "deleted")
    assert lifecycle.transitions("stalled") == ("open", "resolved", "rejected", "deleted")
    assert lifecycle.transitions("resolved") == ("open",)
    assert lifecycle.transitions("rejected") == ("open",)
    assert lifecycle.transitions("deleted") == ("open",)
    assert lifecycle.choices("stalled")[0] == "stalled"
    assert lifecycle.refusal("resolved", "stalled") == "A ticket cannot go from resolved to stalled"
    assert lifecycle.refusal("open", "nonsense") == lifecycle.UNKNOWN_STATUS
    assert lifecycle.refusal("open", "open") == ""


def test_fp_t07_a_status_change_on_the_update_page_records_it(
    client: TestClient, db: Session
) -> None:
    """A Status transaction with old and new, and no message of its own."""
    ticket = make_ticket(db, general(db), root(db), status="open")
    sign_in(client)

    assert post_update(client, ticket, status="stalled").status_code == 303

    written = rows(db, ticket)
    assert [row.type for row in written] == [TransactionType.CREATE, TransactionType.STATUS]
    assert (written[1].field, written[1].old_value, written[1].new_value) == (
        "Status",
        "open",
        "stalled",
    )
    assert body_of(db, written[1]) == ""
    assert ticket.status == "stalled"
    assert "Status changed from open to stalled" in client.get(f"/ticket/{ticket.id}").text


def test_fp_t07_a_status_change_on_basics_records_the_same_row(
    client: TestClient, db: Session
) -> None:
    """The second way in writes the same transaction as the first."""
    ticket = make_ticket(db, general(db), root(db), status="open")
    sign_in(client)

    assert post_basics(client, db, ticket, status="stalled").status_code == 303

    written = rows(db, ticket)
    assert [row.type for row in written] == [TransactionType.CREATE, TransactionType.STATUS]
    assert (written[1].old_value, written[1].new_value) == ("open", "stalled")
    assert ticket.status == "stalled"


def test_fp_t07_an_unreachable_transition_is_refused(client: TestClient, db: Session) -> None:
    """A resolved ticket reopens or stays; the select offers nothing else."""
    ticket = make_ticket(db, general(db), root(db), status="resolved")
    sign_in(client)

    refused = post_update(client, ticket, status="stalled")
    assert refused.status_code == 200
    assert "A ticket cannot go from resolved to stalled" in refused.text
    assert len(rows(db, ticket)) == 1
    assert ticket.status == "resolved"

    page = client.get(f"/ticket/{ticket.id}/update")
    assert '<option value="resolved" selected>' in page.text
    assert '<option value="open">' in page.text
    assert '<option value="stalled"' not in page.text

    # And a status nobody defined is refused the same way.
    unknown = post_update(client, ticket, status="pending")
    assert unknown.status_code == 200
    assert lifecycle.UNKNOWN_STATUS in unknown.text


# --- T08: resolve and reopen -----------------------------------------------


def test_fp_t08_resolve_sets_the_timestamp_and_reopening_clears_it(
    client: TestClient, db: Session
) -> None:
    """The goal's check: one more transaction, and the Resolved date moves."""
    ticket = make_ticket(db, general(db), root(db), status="open")
    sign_in(client)
    before = len(rows(db, ticket))

    assert post_update(client, ticket, status="resolved").status_code == 303
    written = rows(db, ticket)
    assert len(written) == before + 1
    assert (written[-1].type, written[-1].old_value, written[-1].new_value) == (
        TransactionType.STATUS,
        "open",
        "resolved",
    )
    assert ticket.status == "resolved"
    assert ticket.resolved is not None
    assert ticket.resolved.strftime("%Y-%m-%d") in client.get(f"/ticket/{ticket.id}").text

    assert post_update(client, ticket, status="open").status_code == 303
    fresh(db)
    assert ticket.status == "open"
    assert ticket.resolved is None
    assert rows(db, ticket)[-1].new_value == "open"


def test_fp_t08_the_resolve_link_carries_the_status(client: TestClient, db: Session) -> None:
    """The ticket page's Resolve link opens the update page, already resolved."""
    ticket = make_ticket(db, general(db), root(db), status="open")
    sign_in(client)

    page = client.get(f"/ticket/{ticket.id}")
    target = f"http://testserver/ticket/{ticket.id}/update?action=respond&amp;status=resolved"
    assert f'href="{target}"' in page.text
    assert ">Resolve</a>" in page.text

    form = client.get(f"/ticket/{ticket.id}/update?action=respond&status=resolved")
    assert form.status_code == 200
    assert '<option value="resolved" selected>' in form.text
    assert 'value="respond" checked' in form.text


# --- T09: basics -----------------------------------------------------------


def test_fp_t09_the_basics_form_has_every_control(client: TestClient, db: Session) -> None:
    """Plan §8: the five fields, the custom fields' place and Save Changes."""
    ticket = make_ticket(db, general(db), root(db), subject="Printer is on fire")
    sign_in(client)

    page = client.get(f"/ticket/{ticket.id}/basics")
    assert page.status_code == 200
    assert f"<h1>Modify ticket #{ticket.id}: Printer is on fire</h1>" in page.text
    assert '<h2 class="titlebox-title">Basics</h2>' in page.text
    for control in (
        'name="subject"',
        '<select id="queue" name="queue">',
        '<select id="status" name="status">',
        '<select id="owner" name="owner">',
        'name="priority"',
    ):
        assert control in page.text, control
    for label in ("Subject", "Queue", "Status", "Owner", "Priority"):
        assert f">{label}</label>" in page.text, label
    assert ">Save Changes</button>" in page.text
    assert "::" not in page.text
    assert ":</label>" not in page.text


def test_fp_t09_each_changed_field_is_its_own_set_transaction(
    client: TestClient, db: Session
) -> None:
    """Subject, Queue, Owner and Priority, each with its old and new value."""
    support = make_queue(db, "Support")
    ada = make_user(db, "ada", privileged=True)
    grant(db, ("user", ada.id), "OwnTicket", queue=support)
    ticket = make_ticket(db, general(db), root(db), subject="Printer is on fire")
    sign_in(client)

    saved = post_basics(
        client,
        db,
        ticket,
        subject="Printer is still on fire",
        queue=str(support.id),
        status="open",
        owner=str(ada.id),
        priority="42",
    )
    assert saved.status_code == 303
    assert saved.headers["location"] == f"http://testserver/ticket/{ticket.id}"

    written = rows(db, ticket)[1:]
    assert [(row.type, row.field, row.old_value, row.new_value) for row in written] == [
        (TransactionType.SET, "Subject", "Printer is on fire", "Printer is still on fire"),
        (TransactionType.SET, "Queue", DEFAULT_QUEUE_NAME, "Support"),
        (TransactionType.SET, "Owner", NOBODY_USER_NAME, "ada"),
        (TransactionType.SET, "Priority", "0", "42"),
        (TransactionType.STATUS, "Status", "new", "open"),
    ]

    assert ticket.subject == "Printer is still on fire"
    assert ticket.queue_id == support.id
    assert ticket.owner_id == ada.id
    assert ticket.priority == 42
    assert ticket.status == "open"


def test_fp_t09_a_form_that_changed_nothing_writes_nothing(client: TestClient, db: Session) -> None:
    """Saving the page as it stands is a message, not a transaction."""
    ticket = make_ticket(db, general(db), root(db))
    sign_in(client)

    posted = post_basics(client, db, ticket)
    assert posted.status_code == 200
    assert update.NOTHING_CHANGED in posted.text
    assert len(rows(db, ticket)) == 1

    # And a priority that is not a number 0-99 is refused with its own words.
    refused = post_basics(client, db, ticket, priority="500")
    assert refused.status_code == 200
    assert update.BAD_PRIORITY in refused.text
    assert len(rows(db, ticket)) == 1


def test_fp_t09_basics_needs_modify_ticket(client: TestClient, db: Session) -> None:
    """A form nobody may submit is not shown: the GET is gated like the POST."""
    ada = make_user(db, "ada", "ada-password", privileged=True)
    ticket = make_ticket(db, general(db), root(db))
    grant(db, ("user", ada.id), "ShowTicket", queue=general(db))

    sign_in(client, "ada", "ada-password")
    assert client.get(f"/ticket/{ticket.id}").status_code == 200
    refused = client.get(f"/ticket/{ticket.id}/basics")
    assert refused.status_code == 403
    assert FORBIDDEN in refused.text
    assert post_basics(client, db, ticket, subject="Mine now").status_code == 403
    assert len(rows(db, ticket)) == 1

    grant(db, ("user", ada.id), "ModifyTicket", queue=general(db))
    assert client.get(f"/ticket/{ticket.id}/basics").status_code == 200
    assert post_basics(client, db, ticket, subject="Mine now").status_code == 303
    fresh(db)
    assert ticket.subject == "Mine now"


# --- T10: the owner --------------------------------------------------------


def test_fp_t10_the_owner_select_is_nobody_and_the_users_with_own_ticket(
    client: TestClient, db: Session
) -> None:
    """Privileged, enabled and holding OwnTicket on this queue, plus Nobody."""
    ada = make_user(db, "ada", privileged=True)
    make_user(db, "bob", privileged=True)
    carol = make_user(db, "carol")
    grant(db, ("user", ada.id), "OwnTicket", queue=general(db))
    grant(db, ("user", carol.id), "OwnTicket", queue=general(db))
    ticket = make_ticket(db, general(db), root(db))
    sign_in(client)

    page = client.get(f"/ticket/{ticket.id}/basics")
    assert f">{NOBODY_USER_NAME}</option>" in page.text
    assert ">ada</option>" in page.text
    assert ">root</option>" in page.text  # SuperUser holds every right
    assert ">bob</option>" not in page.text  # privileged, but not on this queue
    assert ">carol</option>" not in page.text  # granted, but not privileged

    assert post_basics(client, db, ticket, owner=str(ada.id)).status_code == 303
    written = rows(db, ticket)[-1]
    assert (written.type, written.field, written.old_value, written.new_value) == (
        TransactionType.SET,
        "Owner",
        NOBODY_USER_NAME,
        "ada",
    )
    assert ticket.owner_id == ada.id

    # A user the select never offered is refused, whatever the POST says.
    refused = post_basics(client, db, ticket, owner=str(nobody(db).id + 10_000))
    assert refused.status_code == 200
    assert update.BAD_OWNER in refused.text


# --- T13: the update page --------------------------------------------------


def test_fp_t13_the_update_page_preselects_the_link_and_offers_the_status(
    client: TestClient, db: Session
) -> None:
    """Plan §8's form: the radio from the link, the status select beside it."""
    ticket = make_ticket(db, general(db), root(db), subject="Printer is on fire")
    sign_in(client)

    reply = client.get(f"/ticket/{ticket.id}/update?action=respond")
    assert reply.status_code == 200
    assert f"<h1>Update ticket #{ticket.id}: Printer is on fire</h1>" in reply.text
    assert '<h2 class="titlebox-title">Update</h2>' in reply.text
    assert 'name="UpdateType" value="respond" checked' in reply.text
    assert 'name="UpdateType" value="comment" checked' not in reply.text
    assert ">Reply</label>" in reply.text
    assert ">Comment</label>" in reply.text
    assert '<select id="status" name="status">' in reply.text
    assert '<option value="new" selected>' in reply.text
    assert 'name="content"' in reply.text
    assert ">Update Ticket</button>" in reply.text
    assert "::" not in reply.text
    assert ":</label>" not in reply.text

    comment = client.get(f"/ticket/{ticket.id}/update?action=comment")
    assert 'name="UpdateType" value="comment" checked' in comment.text
    assert 'name="UpdateType" value="respond" checked' not in comment.text

    chosen = client.get(f"/ticket/{ticket.id}/update?action=respond&status=resolved")
    assert '<option value="resolved" selected>' in chosen.text
    # A status the lifecycle does not reach from here is not preselected.
    assert (
        '<option value="new" selected>'
        in client.get(f"/ticket/{ticket.id}/update?action=respond&status=pending").text
    )


def test_fp_t13_the_radio_and_the_status_follow_the_viewers_rights(
    client: TestClient, db: Session
) -> None:
    """Reply only: the Comment half is disabled and no status is offered."""
    ada = make_user(db, "ada", "ada-password", privileged=True)
    ticket = make_ticket(db, general(db), root(db))
    grant(db, ("user", ada.id), "ShowTicket", queue=general(db))
    grant(db, ("user", ada.id), "ReplyToTicket", queue=general(db))

    sign_in(client, "ada", "ada-password")
    page = client.get(f"/ticket/{ticket.id}/update?action=comment")
    assert page.status_code == 200
    assert 'value="comment" disabled' in page.text
    assert 'value="respond" checked' in page.text  # the link's choice, narrowed
    assert '<select id="status" name="status">' not in page.text

    # ModifyTicket alone is a status-only update, with neither half checked.
    grant(db, ("user", ada.id), "ModifyTicket", queue=general(db))
    with_status = client.get(f"/ticket/{ticket.id}/update")
    assert '<select id="status" name="status">' in with_status.text
    assert post_update(client, ticket, status="stalled").status_code == 303
    fresh(db)
    assert ticket.status == "stalled"
