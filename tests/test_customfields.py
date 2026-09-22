"""Custom fields: the admin pages, the values, the gates (FP F01-F07).

Plan §8's Admin paragraph and §9's F rows. The goal the walk runs is the one
these tests hold to: "Create a ticket custom field called *Printer model*
(Admin, Custom Fields, Create; type ``Enter one value``, applies to
``Tickets``) and apply it to the Support queue" — then set its value on a
ticket and read it back off the ticket page, with a ``CustomField``
transaction behind every change (plan §10).

``root`` is ``SuperUser`` and passes every gate; the second actor is a
privileged user holding nothing at all until a test grants it, the shape
``tests/test_gates.py`` uses.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from pyrt.customfields import service
from pyrt.db.models import (
    CustomField,
    CustomFieldKind,
    Queue,
    QueueCustomField,
    Ticket,
    TicketCustomFieldValue,
    Transaction,
    TransactionType,
    User,
)
from tests.test_acl import grant, make_queue
from tests.test_auth import make_user, sign_in
from tests.test_queues import fresh
from tests.test_ticket_update import post_basics
from tests.test_tickets import create_form, general, make_ticket, root

LIST_HEADING = "<h1>Custom Fields</h1>"
CREATE_HEADING = "<h1>Create a custom field</h1>"
MODIFY_HEADING = "<h1>Modify a custom field</h1>"
APPLIES_TO_HEADING = "<h1>Applies to</h1>"
FORBIDDEN = "You are not allowed"

#: The goal's own words.
PRINTER_MODEL = "Printer model"
SUPPORT = "Support"


# --- helpers ----------------------------------------------------------------


def agent_user(db: Session) -> User:
    """A privileged user with no right anywhere (test_gates' shape)."""
    return make_user(db, "agent", "agent-password", privileged=True)


def as_agent(client: TestClient) -> None:
    client.cookies.clear()
    sign_in(client, "agent", "agent-password")


def make_field(
    db: Session,
    name: str,
    *,
    description: str = "",
    disabled: bool = False,
    sort_order: int = 0,
) -> CustomField:
    """A custom field written straight to the database."""
    field = CustomField(
        name=name,
        description=description or name,
        kind=CustomFieldKind.FREEFORM_SINGLE,
        sort_order=sort_order,
        disabled=disabled,
    )
    db.add(field)
    db.commit()
    return field


def apply_to(db: Session, field: CustomField, queue: Queue, *, sort_order: int = 0) -> None:
    """Apply ``field`` to ``queue`` the way both admin pages write it."""
    db.add(QueueCustomField(queue_id=queue.id, custom_field_id=field.id, sort_order=sort_order))
    db.commit()


def set_value(db: Session, ticket: Ticket, field: CustomField, value: str) -> None:
    """A stored value, for a test that starts from one."""
    db.add(TicketCustomFieldValue(ticket_id=ticket.id, custom_field_id=field.id, value=value))
    db.commit()


def value_of(db: Session, ticket: Ticket, field: CustomField) -> str | None:
    """The ticket's value for that field after the app's commit, or None."""
    fresh(db)
    row = db.get(TicketCustomFieldValue, (ticket.id, field.id))
    return None if row is None else row.value


def applied(db: Session, field: CustomField) -> set[int]:
    fresh(db)
    return service.applied_queue_ids(db, field.id)


def field_count(db: Session) -> int:
    fresh(db)
    return len(list(db.scalars(select(CustomField.id)).all()))


def cf_rows(db: Session, ticket: Ticket) -> list[Transaction]:
    """The ticket's ``CustomField`` transactions, oldest first (plan §10)."""
    fresh(db)
    return list(
        db.scalars(
            select(Transaction)
            .where(
                Transaction.ticket_id == ticket.id,
                Transaction.type == TransactionType.CUSTOM_FIELD,
            )
            .order_by(Transaction.id)
        ).all()
    )


def create_field(client: TestClient, name: str, **fields: str) -> Response:
    """POST the Create form with every control a browser would send."""
    data = {
        "name": name,
        "description": "",
        "type": service.TYPE_VALUE,
        "applies_to": service.APPLIES_TO_VALUE,
        "enabled": "1",
    }
    data.update(fields)
    return client.post("/admin/custom-fields/new", data=data, follow_redirects=False)


# --- F01: create a custom field ---------------------------------------------


def test_fp_f01_the_list_and_the_create_form_carry_plan_8s_words(
    client: TestClient, db: Session
) -> None:
    """The Select list, its Create tab, and the two one-option selects."""
    make_field(db, PRINTER_MODEL, description="The model on the label")
    make_field(db, "Retired field", disabled=True)
    sign_in(client)

    page = client.get("/admin/custom-fields")
    assert page.status_code == 200
    assert LIST_HEADING in page.text
    for column in ("Name", "Description", "Type", "Applies to", "Enabled"):
        assert f"<th>{column}</th>" in page.text
    assert PRINTER_MODEL in page.text
    assert service.TYPE_LABEL in page.text  # "Enter one value"
    assert service.APPLIES_TO_LABEL in page.text  # "Tickets"
    assert "Retired field" not in page.text  # disabled, and nobody asked
    assert ">Select</a>" in page.text
    assert ">Create</a>" in page.text

    asked = client.get("/admin/custom-fields?disabled=1")
    assert "Retired field" in asked.text

    form = client.get("/admin/custom-fields/new")
    assert form.status_code == 200
    assert CREATE_HEADING in form.text
    for control in ("name", "description", "type", "applies_to", "enabled"):
        assert f'name="{control}"' in form.text
    assert f'<option value="{service.TYPE_VALUE}" selected>{service.TYPE_LABEL}</option>' in (
        form.text
    )
    assert f">{service.APPLIES_TO_LABEL}</option>" in form.text
    assert ">Create</button>" in form.text


def test_fp_f01_creating_the_goals_field_lands_on_its_modify_page(
    client: TestClient, db: Session
) -> None:
    """The goal's first half: Admin, Custom Fields, Create, ``Printer model``."""
    sign_in(client)
    made = create_field(client, PRINTER_MODEL, description="The model on the label")
    assert made.status_code == 303

    fresh(db)
    field = db.scalar(select(CustomField).where(CustomField.name == PRINTER_MODEL))
    assert field is not None
    assert field.kind == CustomFieldKind.FREEFORM_SINGLE
    assert field.disabled is False
    assert made.headers["location"] == (
        f"http://testserver/admin/custom-fields/{field.id}?msg=created"
    )

    page = client.get(made.headers["location"])
    assert page.status_code == 200
    assert MODIFY_HEADING in page.text
    assert PRINTER_MODEL in page.text
    assert service.CREATED in page.text
    assert ">Save Changes</button>" in page.text
    # The two sub-tabs of plan §8.
    assert f'/admin/custom-fields/{field.id}/applies-to">Applies to</a>' in page.text

    saved = client.post(
        f"/admin/custom-fields/{field.id}",
        data={
            "name": PRINTER_MODEL,
            "description": "The model printed on the label",
            "type": service.TYPE_VALUE,
            "applies_to": service.APPLIES_TO_VALUE,
            "enabled": "1",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert service.UPDATED in client.get(saved.headers["location"]).text
    fresh(db)
    assert field.description == "The model printed on the label"


def test_fp_f01_a_name_is_required_and_unique_without_case(client: TestClient, db: Session) -> None:
    """A second field of the same name is refused, and so is a blank one."""
    sign_in(client)
    assert create_field(client, PRINTER_MODEL).status_code == 303
    assert field_count(db) == 1

    again = create_field(client, "printer MODEL")
    assert again.status_code == 200
    assert service.NAME_TAKEN in again.text
    assert field_count(db) == 1

    blank = create_field(client, "   ")
    assert blank.status_code == 200
    assert service.NAME_REQUIRED in blank.text
    assert field_count(db) == 1

    # A field may keep its own name on a save.
    field = db.scalar(select(CustomField).where(CustomField.name == PRINTER_MODEL))
    assert field is not None
    kept = client.post(
        f"/admin/custom-fields/{field.id}",
        data={
            "name": PRINTER_MODEL,
            "description": "",
            "type": service.TYPE_VALUE,
            "applies_to": service.APPLIES_TO_VALUE,
            "enabled": "1",
        },
        follow_redirects=False,
    )
    assert kept.status_code == 303


def test_fp_f01_admin_custom_field_gates_every_page_of_the_field(
    client: TestClient, db: Session
) -> None:
    """The right is global-only (plan §8); without it none of them exist."""
    agent = agent_user(db)
    field = make_field(db, PRINTER_MODEL)
    as_agent(client)

    for path in (
        "/admin/custom-fields",
        "/admin/custom-fields/new",
        f"/admin/custom-fields/{field.id}",
        f"/admin/custom-fields/{field.id}/applies-to",
    ):
        refused = client.get(path)
        assert refused.status_code == 403, path
        assert FORBIDDEN in refused.text
    assert create_field(client, "Smuggled").status_code == 403
    assert field_count(db) == 1

    grant(db, ("user", agent.id), service.ADMIN_CUSTOM_FIELD)
    for path in (
        "/admin/custom-fields",
        "/admin/custom-fields/new",
        f"/admin/custom-fields/{field.id}",
        f"/admin/custom-fields/{field.id}/applies-to",
    ):
        assert client.get(path).status_code == 200, path


# --- F02: applies to --------------------------------------------------------


def test_fp_f02_the_fields_applies_to_page_writes_and_removes_the_rows(
    client: TestClient, db: Session
) -> None:
    """The goal's second half: apply ``Printer model`` to the Support queue."""
    support = make_queue(db, SUPPORT)
    field = make_field(db, PRINTER_MODEL)
    sign_in(client)

    page = client.get(f"/admin/custom-fields/{field.id}/applies-to")
    assert page.status_code == 200
    assert APPLIES_TO_HEADING in page.text
    assert PRINTER_MODEL in page.text  # the field the page is for
    assert f'name="queue-{support.id}" value="1">' in page.text  # offered, unticked
    assert f">{SUPPORT}</label>" in page.text  # the label after the input
    assert ">Save Changes</button>" in page.text

    saved = client.post(
        f"/admin/custom-fields/{field.id}/applies-to",
        data={f"queue-{support.id}": "1", "submit": "save"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    target = f"http://testserver/admin/custom-fields/{field.id}/applies-to?msg=applied"
    assert saved.headers["location"] == target

    back = client.get(target)
    assert service.APPLIES_TO_SAVED in back.text
    assert f'name="queue-{support.id}" value="1" checked' in back.text
    assert applied(db, field) == {support.id}

    # An empty save takes it off again: the ticked set is the whole truth.
    cleared = client.post(
        f"/admin/custom-fields/{field.id}/applies-to",
        data={"submit": "save"},
        follow_redirects=False,
    )
    assert cleared.status_code == 303
    assert applied(db, field) == set()


def test_fp_f02_the_queues_custom_fields_page_writes_the_same_rows(
    client: TestClient, db: Session
) -> None:
    """Either side applies a field; both write ``queue_custom_fields``."""
    support = make_queue(db, SUPPORT)
    field = make_field(db, PRINTER_MODEL)
    sign_in(client)

    page = client.get(f"/admin/queues/{support.id}/custom-fields")
    assert page.status_code == 200
    assert "<h1>Custom Fields</h1>" in page.text
    assert SUPPORT in page.text
    assert f'name="field-{field.id}" value="1">' in page.text
    assert f">{PRINTER_MODEL}</label>" in page.text
    # The queue modify page's sub-tabs, this one current.
    assert f'href="http://testserver/admin/queues/{support.id}">Basics</a>' in page.text
    assert f"/admin/queues/{support.id}/group-rights" in page.text

    saved = client.post(
        f"/admin/queues/{support.id}/custom-fields",
        data={f"field-{field.id}": "1", "submit": "save"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    back = client.get(saved.headers["location"])
    assert service.QUEUE_FIELDS_SAVED in back.text
    assert f'name="field-{field.id}" value="1" checked' in back.text
    assert applied(db, field) == {support.id}

    # And the field's own page agrees, because it is the same row.
    other = client.get(f"/admin/custom-fields/{field.id}/applies-to")
    assert f'name="queue-{support.id}" value="1" checked' in other.text

    cleared = client.post(
        f"/admin/queues/{support.id}/custom-fields",
        data={"submit": "save"},
        follow_redirects=False,
    )
    assert cleared.status_code == 303
    assert applied(db, field) == set()


def test_fp_f02_admin_queue_on_that_queue_gates_the_queues_page(
    client: TestClient, db: Session
) -> None:
    """A queue's administrator says which fields its tickets carry."""
    agent = agent_user(db)
    support = make_queue(db, SUPPORT)
    field = make_field(db, PRINTER_MODEL)
    as_agent(client)

    assert client.get(f"/admin/queues/{support.id}/custom-fields").status_code == 403
    refused = client.post(
        f"/admin/queues/{support.id}/custom-fields",
        data={f"field-{field.id}": "1"},
        follow_redirects=False,
    )
    assert refused.status_code == 403
    assert applied(db, field) == set()

    grant(db, ("user", agent.id), "AdminQueue", queue=support)
    assert client.get(f"/admin/queues/{support.id}/custom-fields").status_code == 200
    allowed = client.post(
        f"/admin/queues/{support.id}/custom-fields",
        data={f"field-{field.id}": "1"},
        follow_redirects=False,
    )
    assert allowed.status_code == 303
    assert applied(db, field) == {support.id}


# --- F03: the fields on the three ticket screens ----------------------------


def test_fp_f03_a_queues_fields_are_on_its_forms_and_page_in_sort_order(
    client: TestClient, db: Session
) -> None:
    """Two fields on Support, in sort order; General carries neither."""
    support = make_queue(db, SUPPORT)
    zebra = make_field(db, "Zebra")
    alpha = make_field(db, "Alpha")
    apply_to(db, zebra, support, sort_order=1)
    apply_to(db, alpha, support, sort_order=2)
    ticket = make_ticket(db, support, root(db))
    sign_in(client)

    form = client.get(f"/ticket/new?queue={support.id}")
    assert form.status_code == 200
    assert f'name="cf-{zebra.id}"' in form.text
    assert f'name="cf-{alpha.id}"' in form.text
    assert ">Zebra</label>" in form.text
    # Sort order, not the alphabet: Zebra was applied first.
    assert form.text.index(f'name="cf-{zebra.id}"') < form.text.index(f'name="cf-{alpha.id}"')

    basics = client.get(f"/ticket/{ticket.id}/basics")
    assert basics.status_code == 200
    assert basics.text.index(f'name="cf-{zebra.id}"') < basics.text.index(f'name="cf-{alpha.id}"')

    page = client.get(f"/ticket/{ticket.id}")
    assert page.status_code == 200
    assert '<h2 class="titlebox-title">Custom Fields</h2>' in page.text
    assert ">Zebra</th>" in page.text
    assert page.text.count(service.NOT_SET) == 2  # neither is set yet

    # Another queue carries neither.
    elsewhere = client.get(f"/ticket/new?queue={general(db).id}")
    assert f'name="cf-{zebra.id}"' not in elsewhere.text
    assert f'name="cf-{alpha.id}"' not in elsewhere.text


# --- F04: setting a value ---------------------------------------------------


def test_fp_f04_setting_changing_and_clearing_a_value_on_basics_records_it(
    client: TestClient, db: Session
) -> None:
    """A row and a ``CustomField`` transaction per move, old and new (plan §10)."""
    support = make_queue(db, SUPPORT)
    field = make_field(db, PRINTER_MODEL)
    apply_to(db, field, support)
    ticket = make_ticket(db, support, root(db))
    sign_in(client)
    control = f"cf-{field.id}"

    set_it = post_basics(client, db, ticket, **{control: "HP LaserJet 4050"})
    assert set_it.status_code == 303
    assert value_of(db, ticket, field) == "HP LaserJet 4050"
    rows = cf_rows(db, ticket)
    assert len(rows) == 1
    assert rows[0].field == PRINTER_MODEL
    assert rows[0].old_value is None
    assert rows[0].new_value == "HP LaserJet 4050"

    # The page shows it, and the history says what moved.
    page = client.get(f"/ticket/{ticket.id}")
    assert "HP LaserJet 4050" in page.text
    assert f"Custom field {PRINTER_MODEL} changed from (none) to HP LaserJet 4050" in page.text

    # Saving the same value again is no change at all.
    again = post_basics(client, db, ticket, **{control: "HP LaserJet 4050"})
    assert again.status_code == 200
    assert "Nothing changed" in again.text
    assert len(cf_rows(db, ticket)) == 1

    changed = post_basics(client, db, ticket, **{control: "HP LaserJet 4100"})
    assert changed.status_code == 303
    assert value_of(db, ticket, field) == "HP LaserJet 4100"
    rows = cf_rows(db, ticket)
    assert len(rows) == 2
    assert (rows[1].old_value, rows[1].new_value) == ("HP LaserJet 4050", "HP LaserJet 4100")

    cleared = post_basics(client, db, ticket, **{control: ""})
    assert cleared.status_code == 303
    assert value_of(db, ticket, field) is None  # the row is gone
    rows = cf_rows(db, ticket)
    assert len(rows) == 3
    assert (rows[2].old_value, rows[2].new_value) == ("HP LaserJet 4100", None)
    assert service.NOT_SET in client.get(f"/ticket/{ticket.id}").text


def test_fp_f04_a_value_set_on_the_create_form_is_written_with_its_transaction(
    client: TestClient, db: Session
) -> None:
    """On create the value rides the same unit of work, after ``Create``."""
    support = make_queue(db, SUPPORT)
    field = make_field(db, PRINTER_MODEL)
    empty = make_field(db, "Serial number")
    apply_to(db, field, support)
    apply_to(db, empty, support)
    sign_in(client)

    made = create_form(
        client,
        db,
        queue=str(support.id),
        **{f"cf-{field.id}": "HP LaserJet 4050", f"cf-{empty.id}": "  "},
    )
    assert made.status_code == 303

    fresh(db)
    ticket = db.scalar(select(Ticket).order_by(Ticket.id.desc()))
    assert ticket is not None
    assert value_of(db, ticket, field) == "HP LaserJet 4050"
    assert value_of(db, ticket, empty) is None  # an empty input writes no row

    rows = cf_rows(db, ticket)
    assert len(rows) == 1
    assert rows[0].field == PRINTER_MODEL
    assert rows[0].new_value == "HP LaserJet 4050"
    # After the Create transaction, as plan §10's order says.
    first = db.scalar(
        select(Transaction.id)
        .where(Transaction.ticket_id == ticket.id, Transaction.type == TransactionType.CREATE)
        .limit(1)
    )
    assert first is not None and rows[0].id > first


# --- F05: SeeCustomField and ModifyCustomField ------------------------------


def test_fp_f05_see_custom_field_shows_the_box_and_modify_custom_field_sets_it(
    client: TestClient, db: Session
) -> None:
    """Two rights, two halves: reading the value and writing it."""
    agent = agent_user(db)
    field = make_field(db, PRINTER_MODEL)
    queue = general(db)
    apply_to(db, field, queue)
    ticket = make_ticket(db, queue, root(db))
    set_value(db, ticket, field, "HP LaserJet 4050")
    grant(db, ("user", agent.id), "ShowTicket", queue=queue)
    grant(db, ("user", agent.id), "ModifyTicket", queue=queue)
    as_agent(client)
    control = f"cf-{field.id}"

    # Neither right: the box says so, and basics offers nothing.
    page = client.get(f"/ticket/{ticket.id}")
    assert page.status_code == 200
    assert service.NOT_SHOWN in page.text
    assert "HP LaserJet 4050" not in page.text
    basics = client.get(f"/ticket/{ticket.id}/basics")
    assert basics.status_code == 200
    assert f'name="{control}"' not in basics.text

    # SeeCustomField alone: the value reads, on the page and on basics, but
    # there is no input to change it with and a posted one is dropped.
    grant(db, ("user", agent.id), service.SEE_CUSTOM_FIELD, queue=queue)
    page = client.get(f"/ticket/{ticket.id}")
    assert service.NOT_SHOWN not in page.text
    assert "HP LaserJet 4050" in page.text
    basics = client.get(f"/ticket/{ticket.id}/basics")
    assert f'name="{control}"' not in basics.text
    assert "HP LaserJet 4050" in basics.text  # read-only text
    ignored = post_basics(client, db, ticket, **{control: "Brother HL-2030"})
    assert ignored.status_code == 200
    assert "Nothing changed" in ignored.text
    assert value_of(db, ticket, field) == "HP LaserJet 4050"
    assert cf_rows(db, ticket) == []

    # ModifyCustomField: the input is there and the save lands.
    grant(db, ("user", agent.id), service.MODIFY_CUSTOM_FIELD, queue=queue)
    basics = client.get(f"/ticket/{ticket.id}/basics")
    assert f'name="{control}"' in basics.text
    saved = post_basics(client, db, ticket, **{control: "Brother HL-2030"})
    assert saved.status_code == 303
    assert value_of(db, ticket, field) == "Brother HL-2030"
    assert len(cf_rows(db, ticket)) == 1


def test_fp_f05_modify_custom_field_gates_the_create_forms_inputs(
    client: TestClient, db: Session
) -> None:
    """The same rule on the create form: no right, no input and no value."""
    agent = agent_user(db)
    field = make_field(db, PRINTER_MODEL)
    queue = general(db)
    apply_to(db, field, queue)
    grant(db, ("user", agent.id), "CreateTicket", queue=queue)
    grant(db, ("user", agent.id), "SeeQueue", queue=queue)
    as_agent(client)

    form = client.get(f"/ticket/new?queue={queue.id}")
    assert form.status_code == 200
    assert f'name="cf-{field.id}"' not in form.text

    made = create_form(client, db, **{f"cf-{field.id}": "Smuggled in"})
    assert made.status_code == 303
    fresh(db)
    ticket = db.scalar(select(Ticket).order_by(Ticket.id.desc()))
    assert ticket is not None
    assert value_of(db, ticket, field) is None
    assert cf_rows(db, ticket) == []

    grant(db, ("user", agent.id), service.MODIFY_CUSTOM_FIELD, queue=queue)
    assert f'name="cf-{field.id}"' in client.get(f"/ticket/new?queue={queue.id}").text


# --- F06: disabling a field -------------------------------------------------


def test_fp_f06_a_disabled_field_leaves_every_form_and_keeps_its_value(
    client: TestClient, db: Session
) -> None:
    """It falls off the create form, basics and the ticket page; the row stays."""
    support = make_queue(db, SUPPORT)
    field = make_field(db, PRINTER_MODEL)
    apply_to(db, field, support)
    ticket = make_ticket(db, support, root(db))
    set_value(db, ticket, field, "HP LaserJet 4050")
    sign_in(client)
    control = f"cf-{field.id}"

    assert control in client.get(f"/ticket/new?queue={support.id}").text
    assert control in client.get(f"/ticket/{ticket.id}/basics").text
    assert "HP LaserJet 4050" in client.get(f"/ticket/{ticket.id}").text

    disabled = client.post(
        f"/admin/custom-fields/{field.id}",
        data={
            "name": PRINTER_MODEL,
            "description": "",
            "type": service.TYPE_VALUE,
            "applies_to": service.APPLIES_TO_VALUE,
        },
        follow_redirects=False,
    )
    assert disabled.status_code == 303
    fresh(db)
    assert field.disabled is True

    assert control not in client.get(f"/ticket/new?queue={support.id}").text
    assert control not in client.get(f"/ticket/{ticket.id}/basics").text
    page = client.get(f"/ticket/{ticket.id}")
    assert "HP LaserJet 4050" not in page.text
    assert PRINTER_MODEL not in page.text

    # The value is still in the table, and a stale POST writes nothing.
    assert value_of(db, ticket, field) == "HP LaserJet 4050"
    stale = post_basics(client, db, ticket, **{control: "Brother HL-2030"})
    assert stale.status_code == 200
    assert "Nothing changed" in stale.text
    assert value_of(db, ticket, field) == "HP LaserJet 4050"

    # Enabling it again brings the value back to the page untouched.
    client.post(
        f"/admin/custom-fields/{field.id}",
        data={
            "name": PRINTER_MODEL,
            "description": "",
            "type": service.TYPE_VALUE,
            "applies_to": service.APPLIES_TO_VALUE,
            "enabled": "1",
        },
        follow_redirects=False,
    )
    assert "HP LaserJet 4050" in client.get(f"/ticket/{ticket.id}").text


# --- F07: a ticket moved to a queue without the field -----------------------


def test_fp_f07_a_moved_ticket_keeps_the_value_and_shows_it_as_not_applied(
    client: TestClient, db: Session
) -> None:
    """The value stays, read-only and marked, after the queue's own fields."""
    support = make_queue(db, SUPPORT)
    field = make_field(db, PRINTER_MODEL)
    apply_to(db, field, support)
    ticket = make_ticket(db, support, root(db))
    sign_in(client)
    control = f"cf-{field.id}"

    assert post_basics(client, db, ticket, **{control: "HP LaserJet 4050"}).status_code == 303
    moved = post_basics(client, db, ticket, queue=str(general(db).id))
    assert moved.status_code == 303
    fresh(db)
    assert ticket.queue_id == general(db).id
    assert value_of(db, ticket, field) == "HP LaserJet 4050"

    page = client.get(f"/ticket/{ticket.id}")
    assert page.status_code == 200
    assert PRINTER_MODEL in page.text
    assert "HP LaserJet 4050" in page.text
    assert service.NOT_APPLIED in page.text

    # Not editable there: the queue it is in now does not carry the field.
    basics = client.get(f"/ticket/{ticket.id}/basics")
    assert f'name="{control}"' not in basics.text
    stale = post_basics(client, db, ticket, **{control: ""})
    assert stale.status_code == 200
    assert "Nothing changed" in stale.text
    assert value_of(db, ticket, field) == "HP LaserJet 4050"

    # Moved back, it is an ordinary field again.
    assert post_basics(client, db, ticket, queue=str(support.id)).status_code == 303
    back = client.get(f"/ticket/{ticket.id}")
    assert service.NOT_APPLIED not in back.text
    assert "HP LaserJet 4050" in back.text
