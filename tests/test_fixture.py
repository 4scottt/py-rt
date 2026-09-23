"""The fixture loader: ``pyrt seed --fixture`` and ``--check`` (FP O09).

The loader writes a dataset through the services the pages call, stamps the
file's dates on what they wrote, silences the notification hooks while it
runs, and prints one checksum line that a loader on another side prints
identically for the same file. These tests drive it the way the platform
does, ``pyrt --log-level warning seed --fixture -`` with the file on
standard input, against the suite's database, and read back what it wrote.

The line each test expects is recounted from the file itself (and the
sha256 of its bytes) by :func:`expected_line`, not by the loader's code.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
from collections import Counter
from pathlib import Path
from typing import Any, Final

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from pyrt import cli
from pyrt.config import Settings
from pyrt.db.models import (
    NOBODY_USER_NAME,
    SYSTEM_GROUP_EVERYONE,
    TABLE_NAMES,
    CustomField,
    Group,
    GroupMember,
    Queue,
    QueueCustomField,
    Right,
    Ticket,
    TicketCustomFieldValue,
    TicketWatcher,
    Transaction,
    User,
    WatcherRole,
)
from pyrt.db.seed import seed
from pyrt.tickets import hooks
from tests.conftest import ROOT_TEST_PASSWORD, TEST_DSN, truncate_all
from tests.test_acl import grant, group_id, make_queue
from tests.test_queues import fresh
from tests.test_tickets import general, make_ticket, root

#: A small dataset in the platform's format: three queues (General as the
#: side's own), two agents and two requestors, two groups, two custom
#: fields, rights with one global grant, and six tickets that between them
#: take every step type to every final status.
SAMPLE: Final = Path(__file__).resolve().parent / "fixture-sample.json"

#: The platform's own invocation, less ``docker exec -i <container>``.
LOAD: Final = ("--log-level", "warning", "seed", "--fixture", "-")

#: The transaction each step type writes.
WRITES: Final = {
    "correspond": "Correspond",
    "comment": "Comment",
    "status": "Status",
    "custom_field": "CustomField",
}

#: The sample's counts, pinned so an edit to the file is a decision.
SAMPLE_COUNTS: Final = (
    "queues=3 users=4 groups=2 members=3 rights=27 custom_fields=2 applied=2 tickets=6"
    " create=6 correspond=5 comment=4 status=8 custom_field=3 last_ticket=6"
)


def sample() -> bytes:
    return SAMPLE.read_bytes()


def expected_line(raw: bytes) -> str:
    """The checksum line, recounted from the file as its generator counts it."""
    doc = json.loads(raw)
    steps = Counter(step["type"] for ticket in doc["tickets"] for step in ticket["steps"])
    numbers = [
        ("queues", len(doc["queues"])),
        ("users", len(doc["users"])),
        ("groups", len(doc["groups"])),
        ("members", sum(len(group["members"]) for group in doc["groups"])),
        ("rights", sum(len(grant["rights"]) for grant in doc["rights"])),
        ("custom_fields", len(doc["custom_fields"])),
        ("applied", sum(len(field["applies_to"]) for field in doc["custom_fields"])),
        ("tickets", len(doc["tickets"])),
        ("create", len(doc["tickets"])),
        ("correspond", steps["correspond"]),
        ("comment", steps["comment"]),
        ("status", steps["status"]),
        ("custom_field", steps["custom_field"]),
        ("last_ticket", max((ticket["id"] for ticket in doc["tickets"]), default=0)),
    ]
    head = f"fixture v1 seed={doc['seed']} dataset={hashlib.sha256(raw).hexdigest()[:12]}"
    return " ".join([head, *(f"{name}={number}" for name, number in numbers)])


def run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    raw: bytes,
    *extra: str,
    argv: tuple[str, ...] = LOAD,
) -> tuple[int, str, str]:
    """Run the command over ``raw`` on standard input: exit code, stdout, stderr."""
    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    monkeypatch.setenv("ROOT_PASSWORD", ROOT_TEST_PASSWORD)
    code = cli.main([*argv, *extra], stdin=io.BytesIO(raw))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def encode(doc: dict[str, Any]) -> bytes:
    return json.dumps(doc, sort_keys=True).encode()


def when(text_value: str) -> dt.datetime:
    """A time from the file, as the naive UTC datetime a column holds."""
    return dt.datetime.strptime(text_value, "%Y-%m-%dT%H:%M:%SZ")


def final_status(ticket: dict[str, Any]) -> str:
    moves = [step["status"] for step in ticket["steps"] if step["type"] == "status"]
    return moves[-1] if moves else "new"


def dates_of(ticket: dict[str, Any]) -> tuple[dt.datetime | None, dt.datetime | None, dt.datetime]:
    """``started``, ``resolved`` and ``last_updated`` as plan §10 sets them, at the file's times.

    ``started`` on the first leave from ``new`` (a reply to a new ticket
    leaves it too), ``resolved`` on entering ``resolved`` (not ``rejected``)
    and cleared on leaving it, ``last_updated`` at every step.
    """
    status, started, resolved, last = "new", None, None, when(ticket["created"])
    for step in ticket["steps"]:
        at = when(step["at"])
        last = at
        if step["type"] == "status":
            target = step["status"]
        elif step["type"] == "correspond" and status == "new":
            target = "open"
        else:
            target = status
        if target != status:
            if status == "new" and started is None:
                started = at
            if target == "resolved":
                resolved = at
            elif status == "resolved":
                resolved = None
            status = target
    return started, resolved, last


def table_counts(db: Session) -> dict[str, int]:
    return {
        table: db.scalar(select(func.count()).select_from(text(f"`{table}`"))) or 0
        for table in TABLE_NAMES
    }


#: What two loads of one file must agree on, row for row: every column but
#: the moments a user, queue, group, field or grant was made (those are the
#: load's own time, not the file's) and root's password hash (bcrypt salts
#: each seed afresh; whether a user has a hash at all is compared).
SNAPSHOT: Final = (
    "SELECT id, name, email, real_name, privileged, disabled, password_hash IS NULL FROM users",
    "SELECT id, name, description, subject_tag, disabled FROM queues",
    "SELECT id, name, description, kind, disabled FROM `groups`",
    "SELECT group_id, user_id FROM group_members",
    "SELECT principal_kind, principal_id, right_name, object_kind, object_id FROM rights",
    "SELECT id, name, description, kind, disabled FROM custom_fields",
    "SELECT queue_id, custom_field_id FROM queue_custom_fields",
    "SELECT * FROM tickets",
    "SELECT * FROM ticket_watchers",
    "SELECT * FROM transactions",
    "SELECT * FROM messages",
    "SELECT * FROM ticket_custom_field_values",
)


def snapshot(db: Session) -> list[list[tuple[Any, ...]]]:
    return [sorted(tuple(row) for row in db.execute(text(query))) for query in SNAPSHOT]


# --- O09 ------------------------------------------------------------------------------


def test_fp_o09_the_fixture_loads_and_prints_its_checksum(
    db: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One line on stdout, nothing on stderr, and the database is the file."""
    raw = sample()
    doc = json.loads(raw)
    line = expected_line(raw)
    assert line.endswith(" " + SAMPLE_COUNTS)

    assert run(monkeypatch, capsys, raw) == (0, line + "\n", "")

    fresh(db)
    user_names = {user.id: user.name for user in db.scalars(select(User))}
    queue_names = {queue.id: queue.name for queue in db.scalars(select(Queue))}
    group_names = {group.id: group.name for group in db.scalars(select(Group))}
    field_names = {made.id: made.name for made in db.scalars(select(CustomField))}

    # The people, the queues, the groups and the fields, as the file names them.
    for person in doc["users"]:
        row = db.scalar(select(User).where(User.name == person["name"]))
        assert row is not None, person["name"]
        assert (row.email, row.real_name, row.privileged, row.disabled) == (
            person["email"],
            person["real_name"],
            person["privileged"],
            False,
        )
        assert row.password_hash is None  # nobody signs in as a fixture user
    for queue in doc["queues"]:
        row = db.scalar(select(Queue).where(Queue.name == queue["name"]))
        assert row is not None, queue["name"]
        if queue.get("existing"):
            assert (row.description, row.subject_tag) == ("General", None)  # left as seeded
        else:
            assert (row.description, row.subject_tag) == (
                queue["description"],
                queue["subject_tag"],
            )
    members = {
        (group_names[group_id_], user_names[user_id])
        for group_id_, user_id in db.execute(select(GroupMember.group_id, GroupMember.user_id))
    }
    assert members == {
        (group["name"], member) for group in doc["groups"] for member in group["members"]
    }
    carried = {
        (field_names[field_id], queue_names[queue_id])
        for queue_id, field_id in db.execute(
            select(QueueCustomField.queue_id, QueueCustomField.custom_field_id)
        )
    }
    assert carried == {
        (field["name"], queue) for field in doc["custom_fields"] for queue in field["applies_to"]
    }

    # The rights: exactly the file's grants beside root's SuperUser, and
    # nothing for Everyone (or anyone else) that the file did not give.
    granted = set()
    for row in db.scalars(select(Right)):
        names = user_names if row.principal_kind == "user" else group_names
        on = queue_names[row.object_id] if row.object_kind == "queue" else None
        granted.add((row.principal_kind, names[row.principal_id], row.right_name, on))
    assert granted == {
        (grant["kind"], grant["principal"], right, grant["queue"])
        for grant in doc["rights"]
        for right in grant["rights"]
    } | {("user", "root", "SuperUser", None)}
    assert not any(name == SYSTEM_GROUP_EVERYONE for _, name, _, _ in granted)

    # The tickets: numbered as the file numbers them, dated as it dates them.
    rows = list(db.scalars(select(Ticket).order_by(Ticket.id)))
    assert (
        [row.id for row in rows]
        == [ticket["id"] for ticket in doc["tickets"]]
        == [
            1,
            2,
            3,
            4,
            5,
            6,
        ]
    )
    for row, ticket in zip(rows, doc["tickets"], strict=True):
        started, resolved, last = dates_of(ticket)
        assert row.subject == ticket["subject"]
        assert queue_names[row.queue_id] == ticket["queue"]
        assert row.status == final_status(ticket)
        assert user_names[row.owner_id] == (ticket["owner"] or NOBODY_USER_NAME)
        assert user_names[row.creator_id] == ticket["creator"]
        assert row.created == when(ticket["created"])
        assert (row.started, row.resolved, row.last_updated) == (started, resolved, last)
        acted = ticket["steps"][-1]["actor"] if ticket["steps"] else ticket["creator"]
        assert user_names[row.last_updated_by] == acted

        history = db.scalars(
            select(Transaction).where(Transaction.ticket_id == row.id).order_by(Transaction.id)
        ).all()
        assert [(t.type, user_names[t.creator_id], t.created) for t in history] == [
            ("Create", ticket["creator"], when(ticket["created"]))
        ] + [(WRITES[step["type"]], step["actor"], when(step["at"])) for step in ticket["steps"]]

        requestors = db.scalars(
            select(User.email)
            .join(TicketWatcher, TicketWatcher.user_id == User.id)
            .where(TicketWatcher.ticket_id == row.id, TicketWatcher.role == WatcherRole.REQUESTOR)
        ).all()
        assert requestors == [ticket["requestor"]]

        values = {
            field_names[field_id]: value
            for field_id, value in db.execute(
                select(TicketCustomFieldValue.custom_field_id, TicketCustomFieldValue.value).where(
                    TicketCustomFieldValue.ticket_id == row.id
                )
            )
        }
        assert values == {
            step["field"]: step["value"]
            for step in ticket["steps"]
            if step["type"] == "custom_field"
        }


def test_fp_o09_two_loads_on_a_truncated_database_print_the_same_line(
    engine: Engine,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Deterministic: the same file makes the same line and the same rows."""
    raw = sample()
    first = run(monkeypatch, capsys, raw)
    fresh(db)
    rows = snapshot(db)
    db.rollback()  # no snapshot held open across the TRUNCATE

    truncate_all(engine)
    seed(db, ROOT_TEST_PASSWORD)
    # The second load names the file instead of piping it.
    second = run(monkeypatch, capsys, b"", argv=(*LOAD[:-1], str(SAMPLE)))

    assert first == second == (0, expected_line(raw) + "\n", "")
    fresh(db)
    assert snapshot(db) == rows


def test_fp_o09_check_verifies_without_writing(
    engine: Engine,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--check`` reads the side against the file, and writes nothing, ever."""
    raw = sample()
    line = expected_line(raw)

    # Nothing loaded yet: absent, with the first thing it missed.
    before = table_counts(db)
    assert run(monkeypatch, capsys, raw, "--check") == (
        1,
        "",
        "fixture absent: queue Accounts is missing\n",
    )
    fresh(db)
    assert table_counts(db) == before

    # Loaded: the load's own line, and not a row more or less for asking.
    assert run(monkeypatch, capsys, raw) == (0, line + "\n", "")
    fresh(db)
    before = table_counts(db)
    assert run(monkeypatch, capsys, raw, "--check") == (0, line + "\n", "")
    fresh(db)
    assert table_counts(db) == before

    # A ticket written after the load moves only last_ticket (the goals ran).
    make_ticket(db, general(db), root(db))
    later = line.replace("last_ticket=6", "last_ticket=7")
    assert run(monkeypatch, capsys, raw, "--check") == (0, later + "\n", "")

    # A ticket that is no longer what the file says is found out.
    db.execute(text("UPDATE tickets SET status = 'open' WHERE id = 4"))
    db.commit()
    assert run(monkeypatch, capsys, raw, "--check") == (
        1,
        "",
        "fixture absent: ticket 4 is open, not resolved\n",
    )

    # It neither migrates nor seeds: an empty side stays empty.
    db.rollback()
    truncate_all(engine)
    assert run(monkeypatch, capsys, raw, "--check") == (
        1,
        "",
        "fixture absent: queue General is missing\n",
    )
    fresh(db)
    assert sum(table_counts(db).values()) == 0

    # --check is a question about a dataset: without one it is a usage error.
    assert run(monkeypatch, capsys, raw, argv=("seed", "--check")) == (
        2,
        "",
        "pyrt seed: --check needs --fixture\n",
    )


def test_fp_o09_refuses_a_side_that_has_tickets(
    engine: Engine,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Only an empty side is loaded, so the file's ticket numbers are the side's."""
    make_ticket(db, general(db), root(db))
    fresh(db)
    before = table_counts(db)
    assert run(monkeypatch, capsys, sample()) == (
        1,
        "",
        "fixture: refusing to load: the tickets table is not empty (1 ticket); "
        "reset the side first\n",
    )
    fresh(db)
    assert table_counts(db) == before  # not a user, not a queue: nothing was written

    # No tickets, but a name the file creates is taken: refused the same way.
    db.rollback()
    truncate_all(engine)
    seed(db, ROOT_TEST_PASSWORD)
    make_queue(db, "Accounts")
    before = table_counts(db)
    assert run(monkeypatch, capsys, sample()) == (
        1,
        "",
        "fixture: refusing to load: queue Accounts already exists; reset the side first\n",
    )
    fresh(db)
    assert table_counts(db) == before

    # A file this loader does not read is turned away before the database
    # is asked anything (exit 2, the usage status).
    newer = json.loads(sample())
    newer["schema"] = 2
    assert run(monkeypatch, capsys, encode(newer)) == (
        2,
        "",
        "fixture: schema must be 1; the file says 2\n",
    )
    code, out, err = run(monkeypatch, capsys, b"{not json")
    assert (code, out) == (2, "")
    assert err.startswith("fixture: the file is not JSON: ")
    missing = str(SAMPLE.with_name("no-such-dataset.json"))
    assert run(monkeypatch, capsys, b"", argv=(*LOAD[:-1], missing)) == (
        2,
        "",
        f"fixture: cannot read {missing}: No such file or directory\n",
    )
    # A grant the rights pages could never save (a requestor is unprivileged)
    # is the file's mistake, found before the first write, not after users.
    ungrantable = json.loads(sample())
    ungrantable["rights"].append(
        {"kind": "user", "principal": "cy@home.example", "queue": "General", "rights": ["SeeQueue"]}
    )
    assert run(monkeypatch, capsys, encode(ungrantable)) == (
        2,
        "",
        "fixture: the grant to cy@home.example: an unprivileged user holds no rights\n",
    )
    fresh(db)
    assert table_counts(db) == before
    fresh(db)
    assert table_counts(db) == before


def test_fp_o09_an_implicit_status_move_is_reconciled(
    db: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A reply opens a ``new`` ticket by itself; the file's own ``open`` step is then a no-op.

    The shipped dataset never does this (its first reply always follows an
    explicit ``open``, because Request Tracker does not open a ticket on a
    reply), but a file may, and the load must not fail or write a second
    Status row for it.
    """
    created, reply_at, open_at = (
        "2025-05-05T10:00:00Z",
        "2025-05-05T10:05:05Z",
        "2025-05-05T10:06:06Z",
    )
    doc = {
        "schema": 1,
        "seed": 7,
        "clock": "2026-01-01T00:00:00Z",
        "window_start": "2025-01-01T00:00:00Z",
        "queues": [{"existing": True, "name": "General"}],
        "users": [
            {"name": "agent", "email": "agent@sample.example", "real_name": "", "privileged": True},
            {
                "name": "req@home.example",
                "email": "req@home.example",
                "real_name": "",
                "privileged": False,
            },
        ],
        "groups": [],
        "custom_fields": [],
        "rights": [],
        "tickets": [
            {
                "id": 1,
                "queue": "General",
                "subject": "A reply before the open",
                "content": "Hello.",
                "requestor": "req@home.example",
                "owner": None,
                "creator": "req@home.example",
                "created": created,
                "steps": [
                    {"type": "correspond", "actor": "agent", "at": reply_at, "content": "On it."},
                    {"type": "status", "actor": "agent", "at": open_at, "status": "open"},
                ],
            }
        ],
    }
    raw = encode(doc)
    assert run(monkeypatch, capsys, raw) == (0, expected_line(raw) + "\n", "")
    assert " status=1 " in expected_line(raw)  # the file's count, not the side's rows

    fresh(db)
    ticket = db.get(Ticket, 1)
    assert ticket is not None
    history = db.scalars(
        select(Transaction).where(Transaction.ticket_id == 1).order_by(Transaction.id)
    ).all()
    assert [(t.type, t.old_value, t.new_value, t.created) for t in history] == [
        ("Create", None, None, when(created)),
        ("Correspond", None, None, when(reply_at)),
        # the product's own move, dated with the reply that made it
        ("Status", "new", "open", when(reply_at)),
    ]
    assert ticket.status == "open"
    assert ticket.started == when(reply_at)
    assert ticket.last_updated == when(open_at)  # the step still happened, at its time


def test_fp_o09_the_load_silences_the_hooks_and_puts_them_back(
    engine: Engine,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No notification fires during a load, and every listener is back after it.

    A tripwire on all four moments would record (and raise) if any fired;
    the lists are emptied in place and refilled, so the objects the mail
    package appended to are the same objects afterwards, even when the load
    fails half-way.
    """
    fired: list[tuple[int, str]] = []

    def tripwire(
        session: Session, settings: Settings, ticket: Ticket, transaction: Transaction, actor: User
    ) -> None:
        fired.append((ticket.id, transaction.type))
        raise RuntimeError("a notification fired during the fixture load")

    names = (hooks.CREATED, hooks.CORRESPONDED, hooks.COMMENTED, hooks.STATUS_CHANGED)
    lists = [hooks.listeners(name) for name in names]
    saved = [list(listeners) for listeners in lists]
    for listeners in lists:
        listeners.append(tripwire)
    try:
        assert run(monkeypatch, capsys, sample())[0] == 0
        assert fired == []
        for listeners, kept in zip(lists, saved, strict=True):
            assert listeners == [*kept, tripwire]

        # A load that stops after writing puts them back all the same.
        db.rollback()
        truncate_all(engine)
        seed(db, ROOT_TEST_PASSWORD)
        broken = json.loads(sample())
        broken["tickets"][1]["owner"] = "cy@home.example"  # a requestor cannot own a ticket
        assert run(monkeypatch, capsys, encode(broken)) == (
            1,
            "",
            "fixture: ticket 2: That user cannot own a ticket (cy@home.example in Accounts); "
            "the side is partly loaded: reset the side and load again\n",
        )
        assert fired == []  # ticket 1 was created, and still nobody heard
        for listeners, kept in zip(lists, saved, strict=True):
            assert listeners == [*kept, tripwire]
    finally:
        for listeners, kept in zip(lists, saved, strict=True):
            listeners[:] = kept


def test_fp_o09_root_keeps_superuser_and_grants_the_file_does_not_name(
    db: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The rights are saved page by page with only the file's principals on the page.

    ``save_rights`` makes the ticked boxes the truth for every row it is
    handed, so a page handed in whole would revoke root's global
    ``SuperUser`` (on the global User Rights page, beside the file's global
    user grant) and Everyone's grants (on the pages the file's group grants
    are saved on). Neither is handed in, so both survive.
    """
    everyone = ("group", group_id(db, SYSTEM_GROUP_EVERYONE))
    grant(db, everyone, "CreateTicket", queue=general(db))  # the mail goal's grant
    grant(db, everyone, "SeeQueue")  # a global one, beside the file's global group grant
    doc = json.loads(sample())
    doc["rights"].append(
        {"kind": "user", "principal": "akim", "queue": None, "rights": ["ShowConfigTab"]}
    )
    raw = encode(doc)
    assert run(monkeypatch, capsys, raw) == (0, expected_line(raw) + "\n", "")
    assert " rights=28 " in expected_line(raw)

    fresh(db)
    root_id = root(db).id
    akim = db.scalar(select(User.id).where(User.name == "akim"))
    auditors = group_id(db, "Auditors")
    general_id = general(db).id
    held = set(
        db.execute(
            select(
                Right.principal_kind,
                Right.principal_id,
                Right.right_name,
                Right.object_kind,
                Right.object_id,
            )
        ).tuples()
    )
    assert ("user", root_id, "SuperUser", "system", 0) in held
    assert ("user", akim, "ShowConfigTab", "system", 0) in held
    assert (*everyone, "CreateTicket", "queue", general_id) in held
    assert (*everyone, "SeeQueue", "system", 0) in held
    assert ("group", auditors, "SeeQueue", "system", 0) in held
    assert ("group", auditors, "ShowTicket", "system", 0) in held
