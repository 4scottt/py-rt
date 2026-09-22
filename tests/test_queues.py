"""The queue admin pages (FP Q01-Q04, Q06) and the admin index.

Plan §8: the Select list with its Create tab, the create form, the modify
page with the four sub-tabs. The gates are ``SeeQueue``/``AdminQueue`` for
the list, ``AdminQueue`` globally to create and on the queue to modify.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from pyrt.acl import principals
from pyrt.db.models import DEFAULT_QUEUE_NAME, Queue, User
from pyrt.queues import service
from tests.test_acl import grant, make_queue
from tests.test_auth import make_user, sign_in

QUEUES_HEADING = "<h1>Queues</h1>"
MODIFY_HEADING = "<h1>Modify a queue</h1>"
CREATE_HEADING = "<h1>Create a queue</h1>"
FORBIDDEN = "You are not allowed"


def queue_by_name(db: Session, name: str) -> Queue:
    queue = db.scalar(select(Queue).where(Queue.name == name))
    assert queue is not None, name
    return queue


def user_by_name(db: Session, name: str) -> User:
    user = db.scalar(select(User).where(User.name == name))
    assert user is not None, name
    return user


def fresh(db: Session) -> None:
    """End the test session's snapshot so the app's writes are visible.

    The session factory keeps ``expire_on_commit`` off and MariaDB reads
    repeatably, so a test that looks at a row the app wrote needs both.
    """
    db.commit()
    db.expire_all()


def disable(db: Session, queue: Queue) -> None:
    queue.disabled = True
    db.commit()


def test_fp_q01_the_list_shows_the_queues_and_hides_the_disabled_ones(
    client: TestClient, db: Session
) -> None:
    """Name, description and Enabled; a disabled queue only when asked."""
    make_queue(db, "Support")
    disable(db, make_queue(db, "Retired"))

    sign_in(client)
    page = client.get("/admin/queues")
    assert page.status_code == 200
    assert QUEUES_HEADING in page.text
    assert DEFAULT_QUEUE_NAME in page.text
    assert "Support" in page.text
    assert "Retired" not in page.text

    # The columns and the tab bar of plan §8.
    assert "<th>Name</th>" in page.text
    assert "<th>Description</th>" in page.text
    assert "<th>Enabled</th>" in page.text
    assert ">Select</a>" in page.text
    assert ">Create</a>" in page.text
    assert "http://testserver/admin/queues/new" in page.text
    assert ">Include disabled queues</a>" in page.text

    asked = client.get("/admin/queues?disabled=1")
    assert asked.status_code == 200
    assert "Retired" in asked.text
    assert "Support" in asked.text


def test_fp_q01_the_list_needs_see_queue_or_admin_queue(client: TestClient, db: Session) -> None:
    """No right is the denial page; a queue-scoped right shows that queue."""
    secret = make_queue(db, "Secret")
    agent = make_user(db, "agent", "agent-password", privileged=True)

    sign_in(client, "agent", "agent-password")
    refused = client.get("/admin/queues")
    assert refused.status_code == 403
    assert FORBIDDEN in refused.text

    grant(db, ("user", agent.id), "SeeQueue", queue=secret)
    allowed = client.get("/admin/queues")
    assert allowed.status_code == 200
    assert "Secret" in allowed.text
    assert DEFAULT_QUEUE_NAME not in allowed.text


def test_fp_q02_create_a_queue_with_its_fields(client: TestClient, db: Session) -> None:
    """The form, the redirect with its message, and the row that was written."""
    sign_in(client)
    form = client.get("/admin/queues/new")
    assert form.status_code == 200
    assert CREATE_HEADING in form.text
    for control in (
        'name="name"',
        'name="description"',
        'name="subject_tag"',
        'name="correspond_address"',
        'name="comment_address"',
        'name="enabled"',
    ):
        assert control in form.text
    assert 'name="enabled" value="1" checked' in form.text  # checked by default
    assert ">Create</button>" in form.text

    made = client.post(
        "/admin/queues/new",
        data={
            "name": "Support",
            "description": "The support queue",
            "subject_tag": "SUP",
            "correspond_address": "support@example.invalid",
            "comment_address": "support-comment@example.invalid",
            "enabled": "1",
        },
        follow_redirects=False,
    )
    assert made.status_code == 303
    fresh(db)
    queue = queue_by_name(db, "Support")
    assert made.headers["location"] == f"http://testserver/admin/queues/{queue.id}?msg=created"
    assert queue.description == "The support queue"
    assert queue.subject_tag == "SUP"
    assert queue.correspond_address == "support@example.invalid"
    assert queue.comment_address == "support-comment@example.invalid"
    assert queue.disabled is False

    landed = client.get(made.headers["location"])
    assert landed.status_code == 200
    assert "Queue created" in landed.text
    assert MODIFY_HEADING in landed.text
    assert "Support" in landed.text
    # An unknown message is ignored, not printed.
    assert "nonsense" not in client.get(f"/admin/queues/{queue.id}?msg=nonsense").text

    # The creator sees it in Select (FP Q02's last clause).
    assert "Support" in client.get("/admin/queues").text

    # An unchecked box creates a disabled queue.
    client.post("/admin/queues/new", data={"name": "Archive"})
    fresh(db)
    assert queue_by_name(db, "Archive").disabled is True


def test_fp_q02_a_name_must_be_given_and_unique_case_insensitively(
    client: TestClient, db: Session
) -> None:
    """The refusal re-renders the form with 200 and the values kept."""
    sign_in(client)
    clash = client.post(
        "/admin/queues/new",
        data={"name": "general", "description": "A second General", "enabled": "1"},
        follow_redirects=False,
    )
    assert clash.status_code == 200
    assert "A queue with that name already exists" in clash.text
    assert 'value="general"' in clash.text
    assert 'value="A second General"' in clash.text
    fresh(db)
    # Only the seeded queue: the database's own collation is case-insensitive,
    # so the count is the honest assertion here.
    assert [queue.name for queue in db.scalars(select(Queue))] == [DEFAULT_QUEUE_NAME]

    empty = client.post("/admin/queues/new", data={"name": "  ", "enabled": "1"})
    assert empty.status_code == 200
    assert "A queue needs a name" in empty.text


def test_fp_q02_creating_needs_admin_queue_globally(client: TestClient, db: Session) -> None:
    """A queue-scoped grant does not open the Create tab."""
    general = queue_by_name(db, DEFAULT_QUEUE_NAME)
    agent = make_user(db, "agent", "agent-password", privileged=True)
    grant(db, ("user", agent.id), "AdminQueue", queue=general)

    sign_in(client, "agent", "agent-password")
    assert client.get("/admin/queues/new").status_code == 403
    refused = client.post("/admin/queues/new", data={"name": "Sneaky", "enabled": "1"})
    assert refused.status_code == 403
    assert FORBIDDEN in refused.text
    fresh(db)
    assert db.scalar(select(Queue).where(Queue.name == "Sneaky")) is None


def test_fp_q03_modify_saves_the_basics_and_disabling_hides_the_queue(
    client: TestClient, db: Session
) -> None:
    """Every field is saved; a disabled queue leaves the default list."""
    general = queue_by_name(db, DEFAULT_QUEUE_NAME)
    queue_id = general.id

    sign_in(client)
    page = client.get(f"/admin/queues/{queue_id}")
    assert page.status_code == 200
    assert 'value="General"' in page.text
    assert ">Save Changes</button>" in page.text

    saved = client.post(
        f"/admin/queues/{queue_id}",
        data={
            "name": "General Support",
            "description": "Everything else",
            "subject_tag": "GEN",
            "correspond_address": "reply@example.invalid",
            "comment_address": "comment@example.invalid",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert saved.headers["location"] == f"http://testserver/admin/queues/{queue_id}?msg=saved"

    fresh(db)
    queue = db.get(Queue, queue_id)
    assert queue is not None
    assert queue.name == "General Support"
    assert queue.description == "Everything else"
    assert queue.subject_tag == "GEN"
    assert queue.correspond_address == "reply@example.invalid"
    assert queue.comment_address == "comment@example.invalid"
    assert queue.disabled is True  # the box was not checked

    landed = client.get(saved.headers["location"])
    assert "Queue updated" in landed.text
    assert "General Support" in landed.text

    listed = client.get("/admin/queues")
    assert "General Support" not in listed.text
    assert "General Support" in client.get("/admin/queues?disabled=1").text

    # Keeping its own name is not a clash.
    again = client.post(
        f"/admin/queues/{queue_id}",
        data={"name": "General Support", "enabled": "1"},
        follow_redirects=False,
    )
    assert again.status_code == 303
    fresh(db)
    assert db.get(Queue, queue_id).disabled is False  # type: ignore[union-attr]


def test_fp_q03_modifying_needs_admin_queue_on_that_queue(client: TestClient, db: Session) -> None:
    """A grant on one queue reaches that queue's page and no other."""
    general = queue_by_name(db, DEFAULT_QUEUE_NAME)
    other = make_queue(db, "Other")
    agent = make_user(db, "agent", "agent-password", privileged=True)
    grant(db, ("user", agent.id), "AdminQueue", queue=general)

    sign_in(client, "agent", "agent-password")
    assert client.get(f"/admin/queues/{general.id}").status_code == 200
    refused = client.get(f"/admin/queues/{other.id}")
    assert refused.status_code == 403
    assert FORBIDDEN in refused.text
    assert (
        client.post(
            f"/admin/queues/{other.id}", data={"name": "Taken over", "enabled": "1"}
        ).status_code
        == 403
    )


def test_fp_q04_the_modify_page_links_that_queue_s_sub_tabs(
    client: TestClient, db: Session
) -> None:
    """The four links of plan §8, as pages, not panes."""
    queue_id = queue_by_name(db, DEFAULT_QUEUE_NAME).id
    sign_in(client)
    page = client.get(f"/admin/queues/{queue_id}").text

    base = f"http://testserver/admin/queues/{queue_id}"
    assert f'href="{base}">Basics</a>' in page
    assert f'href="{base}/group-rights">Group Rights</a>' in page
    assert f'href="{base}/user-rights">User Rights</a>' in page
    assert f'href="{base}/custom-fields">Custom Fields</a>' in page


def test_fp_q06_a_queue_s_subject_tag_replaces_the_site_name() -> None:
    """The pure function the mail code uses (plan §8's mail vocabulary)."""
    assert service.subject_tag_for(Queue(name="General"), "example") == "example"
    assert service.subject_tag_for(Queue(name="General", subject_tag=""), "example") == "example"
    assert service.subject_tag_for(Queue(name="General", subject_tag="  "), "example") == "example"
    assert service.subject_tag_for(Queue(name="Support", subject_tag="SUP"), "example") == "SUP"


def test_the_admin_index_links_the_four_sections(client: TestClient, db: Session) -> None:
    """``/admin/`` is a page of links, behind ``ShowConfigTab``."""
    sign_in(client)
    page = client.get("/admin/")
    assert page.status_code == 200
    assert "<h1>Admin</h1>" in page.text
    for label, path in (
        ("Queues", "/admin/queues"),
        ("Users", "/admin/users"),
        ("Groups", "/admin/groups"),
        ("Custom Fields", "/admin/custom-fields"),
    ):
        assert f'href="http://testserver{path}">{label}</a>' in page.text

    client.cookies.clear()
    make_user(db, "agent", "agent-password", privileged=True)
    sign_in(client, "agent", "agent-password")
    refused = client.get("/admin/")
    assert refused.status_code == 403
    assert FORBIDDEN in refused.text


def test_an_unknown_queue_is_the_not_found_page(client: TestClient) -> None:
    sign_in(client)
    missing = client.get("/admin/queues/9999")
    assert missing.status_code == 404
    assert "Page not found" in missing.text


def test_queues_for_create_is_the_enabled_queues_the_user_may_create_in(
    db: Session,
) -> None:
    """What the tickets package asks for: the "New ticket in" list."""
    general = queue_by_name(db, DEFAULT_QUEUE_NAME)
    support = make_queue(db, "Support")
    disable(db, make_queue(db, "Retired"))
    retired = queue_by_name(db, "Retired")
    agent = make_user(db, "agent", "agent-password", privileged=True)
    for queue in (general, support, retired):
        grant(db, ("user", agent.id), "CreateTicket", queue=queue)
        grant(db, ("user", agent.id), "SeeQueue", queue=queue)  # FP R06: both

    held = principals(db, agent)
    names = [queue.name for queue in service.queues_for_create(db, held)]
    assert names == ["General", "Support"]  # disabled is out, by name

    stranger = make_user(db, "stranger", "stranger-password")
    assert service.queues_for_create(db, principals(db, stranger)) == []
