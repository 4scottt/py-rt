"""The gates, right by right, and the denial page (FP R06, R07, R08).

M3's sweep: every right of plan §8's vocabulary that guards a screen is
asserted here from both sides — the refusal without it and the allowance
with it — against the same shape of actor, a privileged user who holds
nothing at all until the test grants it. ``root`` is ``SuperUser`` and so
passes every gate, which is what keeps the acceptance walk working.

``SeeQueue`` is the one rule this package decided: a queue is offered for a
new ticket only where the user holds ``CreateTicket`` **and** ``SeeQueue``
on it, because ``SeeQueue`` is what makes a queue visible at all. The owner
select on the create form is the other: it is the basics page's list, by
``OwnTicket`` on the queue chosen (FP T10).

FP R09 (Everyone with ``CreateTicket``/``ReplyToTicket`` for the mail
gateway) is M5's and is not tested here.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.orm import Session

from pyrt.db.models import User
from pyrt.tickets import service as tickets
from pyrt.web.errors import FORBIDDEN_HEADING
from tests.test_acl import grant, make_group, make_queue
from tests.test_auth import make_user, sign_in
from tests.test_queues import fresh
from tests.test_ticket_update import post_basics, post_update
from tests.test_tickets import (
    create_form,
    general,
    make_ticket,
    nobody,
    only_ticket,
    root,
    ticket_count,
)

#: The actor every gate test uses: privileged (so it may be granted rights
#: and appear in the owner select) and holding nothing else.
AGENT: str = "agent"
AGENT_PASSWORD: str = "agent-password"

#: The Admin menu of plan §11's shell, behind ``ShowConfigTab``: its label is
#: a link to the admin index.
ADMIN_MENU = '<a class="menu-label" href="http://testserver/admin/">Admin</a>'

#: The shell's own words, so a 403 can be told from a bare error body.
SHELL_MARK = "RT for test"


def agent_user(db: Session) -> User:
    """A privileged user with no right anywhere."""
    return make_user(db, AGENT, AGENT_PASSWORD, privileged=True)


def as_agent(client: TestClient) -> None:
    """Sign in as that user, whoever was signed in before."""
    client.cookies.clear()
    sign_in(client, AGENT, AGENT_PASSWORD)


def refused(response: Response) -> bool:
    """Whether this is the denial page of FP R08 and not something else."""
    return response.status_code == 403 and FORBIDDEN_HEADING in response.text


# --- R06: the rights that gate the ticket screens ---------------------------


def test_fp_r06_show_ticket_gates_the_ticket_page_and_its_history(
    client: TestClient, db: Session
) -> None:
    """Without it neither page exists for this user; with it both do."""
    agent = agent_user(db)
    ticket = make_ticket(db, general(db), root(db))
    as_agent(client)

    assert refused(client.get(f"/ticket/{ticket.id}"))
    assert refused(client.get(f"/ticket/{ticket.id}/history"))

    grant(db, ("user", agent.id), "ShowTicket", queue=general(db))
    page = client.get(f"/ticket/{ticket.id}")
    assert page.status_code == 200
    assert f"#{ticket.id}: A ticket" in page.text
    history = client.get(f"/ticket/{ticket.id}/history")
    assert history.status_code == 200
    assert "The first word" in history.text  # the Create transaction's message

    # TODO(search): the third half of FP R06's ShowTicket clause is the search
    # results (plan §16's search package). It has no route yet, and the test
    # for it belongs beside the results table, not here.


def test_fp_r06_see_queue_gates_the_queue_in_the_create_select(
    client: TestClient, db: Session
) -> None:
    """``CreateTicket`` alone does not show a queue: ``SeeQueue`` does.

    The decision this package made, and the rule RT keeps: a queue a person
    may not see is not offered, not in the "New ticket in" menu, not in the
    create form's select, and a POST naming it is the denial page.
    """
    agent = agent_user(db)
    queue = general(db)
    grant(db, ("user", agent.id), "CreateTicket", queue=queue)
    as_agent(client)

    hidden = client.get("/")
    assert hidden.status_code == 200
    assert f"/ticket/new?queue={queue.id}" not in hidden.text
    assert refused(client.get(f"/ticket/new?queue={queue.id}"))
    assert refused(create_form(client, db))
    assert ticket_count(db) == 0

    grant(db, ("user", agent.id), "SeeQueue", queue=queue)
    shown = client.get("/")
    assert f"/ticket/new?queue={queue.id}" in shown.text
    form = client.get(f"/ticket/new?queue={queue.id}")
    assert form.status_code == 200
    assert f'<option value="{queue.id}" selected>' in form.text
    assert create_form(client, db).status_code == 303
    assert ticket_count(db) == 1


def test_fp_r06_create_ticket_gates_creating_by_web(client: TestClient, db: Session) -> None:
    """Seeing a queue is not creating in it; the POST is checked too."""
    agent = agent_user(db)
    queue = general(db)
    grant(db, ("user", agent.id), "SeeQueue", queue=queue)
    as_agent(client)

    assert refused(client.get(f"/ticket/new?queue={queue.id}"))
    assert refused(create_form(client, db))
    assert ticket_count(db) == 0

    grant(db, ("user", agent.id), "CreateTicket", queue=queue)
    assert client.get(f"/ticket/new?queue={queue.id}").status_code == 200
    assert create_form(client, db).status_code == 303
    assert ticket_count(db) == 1


def test_fp_r06_reply_to_ticket_and_comment_on_ticket_gate_the_update_halves(
    client: TestClient, db: Session
) -> None:
    """Each half of the ``UpdateType`` radio has its own right."""
    agent = agent_user(db)
    ticket = make_ticket(db, general(db), root(db))
    grant(db, ("user", agent.id), "ShowTicket", queue=general(db))
    as_agent(client)

    # Reading a ticket does not open the update page at all.
    assert refused(client.get(f"/ticket/{ticket.id}/update?action=respond"))
    assert refused(post_update(client, ticket, content="We are on it."))

    grant(db, ("user", agent.id), "ReplyToTicket", queue=general(db))
    page = client.get(f"/ticket/{ticket.id}/update?action=respond")
    assert page.status_code == 200
    assert 'name="UpdateType" value="respond" checked' in page.text
    assert post_update(client, ticket, content="We are on it.").status_code == 303

    # The other half is still refused: a comment is not a reply.
    assert refused(post_update(client, ticket, UpdateType="comment", content="An aside."))

    grant(db, ("user", agent.id), "CommentOnTicket", queue=general(db))
    commented = post_update(client, ticket, UpdateType="comment", content="An aside.")
    assert commented.status_code == 303


def test_fp_r06_modify_ticket_gates_the_basics_page(client: TestClient, db: Session) -> None:
    """The form and the save alike, and nothing is written when refused."""
    agent = agent_user(db)
    ticket = make_ticket(db, general(db), root(db))
    grant(db, ("user", agent.id), "ShowTicket", queue=general(db))
    as_agent(client)

    assert refused(client.get(f"/ticket/{ticket.id}/basics"))
    assert refused(post_basics(client, db, ticket, subject="Renamed"))
    fresh(db)
    assert ticket.subject == "A ticket"

    grant(db, ("user", agent.id), "ModifyTicket", queue=general(db))
    assert client.get(f"/ticket/{ticket.id}/basics").status_code == 200
    assert post_basics(client, db, ticket, subject="Renamed").status_code == 303
    fresh(db)
    assert ticket.subject == "Renamed"


def test_fp_r06_owner_select_on_create_is_narrowed_by_own_ticket(
    client: TestClient, db: Session
) -> None:
    """FP T10's rule on the create form: the same list as the basics page.

    The list is rendered for the queue the form is for; the POST re-checks
    the owner against the queue it actually chose, which is what makes a
    stale list after a change of the queue select harmless.
    """
    ada = make_user(db, "ada", privileged=True)
    bob = make_user(db, "bob", privileged=True)
    carol = make_user(db, "carol")  # granted below, but not privileged
    support = make_queue(db, "Support")
    grant(db, ("user", ada.id), "OwnTicket", queue=general(db))
    grant(db, ("user", carol.id), "OwnTicket", queue=general(db))
    grant(db, ("user", bob.id), "OwnTicket", queue=support)
    sign_in(client)

    page = client.get(f"/ticket/new?queue={general(db).id}")
    assert page.status_code == 200
    assert f'<option value="{nobody(db).id}"' in page.text
    assert ">ada</option>" in page.text
    assert ">root</option>" in page.text  # SuperUser holds every right
    assert ">bob</option>" not in page.text  # OwnTicket, but on another queue
    assert ">carol</option>" not in page.text  # granted, but not privileged

    refused_post = create_form(client, db, owner=str(bob.id))
    assert refused_post.status_code == 200
    assert tickets.BAD_OWNER in refused_post.text
    assert ticket_count(db) == 0

    assert create_form(client, db, owner=str(ada.id)).status_code == 303
    assert only_ticket(db).owner_id == ada.id

    # The other queue offers the other user: the list follows the queue.
    other = client.get(f"/ticket/new?queue={support.id}")
    assert ">bob</option>" in other.text
    assert ">ada</option>" not in other.text


# --- R07: the rights that gate the admin pages ------------------------------


def test_fp_r07_admin_queue_gates_the_queue_admin_pages(client: TestClient, db: Session) -> None:
    """The list, the Create form and a queue's own page."""
    agent = agent_user(db)
    queue = general(db)
    as_agent(client)

    assert refused(client.get("/admin/queues"))
    assert refused(client.get("/admin/queues/new"))
    assert refused(client.get(f"/admin/queues/{queue.id}"))

    grant(db, ("user", agent.id), "AdminQueue")  # global, as plan §8 allows
    assert client.get("/admin/queues").status_code == 200
    assert client.get("/admin/queues/new").status_code == 200
    assert client.get(f"/admin/queues/{queue.id}").status_code == 200


def test_fp_r07_admin_users_gates_the_user_admin_pages(client: TestClient, db: Session) -> None:
    """``AdminUsers`` is global-only (plan §8's vocabulary)."""
    agent = agent_user(db)
    as_agent(client)

    assert refused(client.get("/admin/users"))
    assert refused(client.get("/admin/users/new"))
    assert refused(client.get(f"/admin/users/{agent.id}"))

    grant(db, ("user", agent.id), "AdminUsers")
    assert client.get("/admin/users").status_code == 200
    assert client.get("/admin/users/new").status_code == 200
    assert client.get(f"/admin/users/{agent.id}").status_code == 200


def test_fp_r07_admin_group_and_group_membership_gate_the_group_pages(
    client: TestClient, db: Session
) -> None:
    """The groups package's two gates, asserted here as part of the sweep.

    The pages are another package's; this test skips itself while they do
    not exist rather than failing for work that has not landed.
    """
    agent = agent_user(db)
    group = make_group(db, "Support staff")
    as_agent(client)

    listing = client.get("/admin/groups")
    if listing.status_code == 404:
        pytest.skip("the group admin pages are not built yet (AdminGroup, AdminGroupMembership)")

    assert refused(listing)
    assert refused(client.get("/admin/groups/new"))
    assert refused(client.get(f"/admin/groups/{group.id}"))
    assert refused(client.get(f"/admin/groups/{group.id}/members"))

    grant(db, ("user", agent.id), "AdminGroup")
    assert client.get("/admin/groups").status_code == 200
    assert client.get(f"/admin/groups/{group.id}").status_code == 200
    # AdminGroup is not AdminGroupMembership: the members page is its own gate.
    assert refused(client.get(f"/admin/groups/{group.id}/members"))

    grant(db, ("user", agent.id), "AdminGroupMembership")
    assert client.get(f"/admin/groups/{group.id}/members").status_code == 200


def test_fp_r07_admin_custom_field_gates_the_custom_field_pages(
    client: TestClient, db: Session
) -> None:
    """M4's pages; the gate is asserted as soon as they answer."""
    agent = agent_user(db)
    as_agent(client)

    listing = client.get("/admin/custom-fields")
    if listing.status_code == 404:
        pytest.skip("the custom field pages are M4's (AdminCustomField)")

    assert refused(listing)
    grant(db, ("user", agent.id), "AdminCustomField")
    assert client.get("/admin/custom-fields").status_code == 200


def test_fp_r07_show_config_tab_gates_the_admin_menu(client: TestClient, db: Session) -> None:
    """The menu of plan §11 and the admin index behind it."""
    agent = agent_user(db)
    as_agent(client)

    without = client.get("/")
    assert without.status_code == 200
    assert ADMIN_MENU not in without.text
    assert refused(client.get("/admin/"))

    grant(db, ("user", agent.id), "ShowConfigTab")
    assert ADMIN_MENU in client.get("/").text
    assert client.get("/admin/").status_code == 200

    # root holds SuperUser, so the menu is there without a grant of its own.
    client.cookies.clear()
    sign_in(client)
    assert ADMIN_MENU in client.get("/").text


# --- R08: the denial page ---------------------------------------------------


def test_fp_r08_a_page_gate_refusal_is_the_denial_page_on_get_and_on_post(
    client: TestClient, db: Session
) -> None:
    """``require_right`` as a dependency: 403 and the page, never a 500."""
    agent_user(db)
    as_agent(client)

    for response in (
        client.get("/admin/users"),
        client.post("/admin/users/new", data={"name": "mallory"}, follow_redirects=False),
    ):
        assert response.status_code == 403
        assert "<h1>You are not allowed</h1>" in response.text
        assert SHELL_MARK in response.text  # the shell is still around it
        assert "Traceback" not in response.text

    # The page stays plain: it does not name the right that was missing, as
    # RT's does not. Nothing here asks for that to change.
    assert "AdminUsers" not in client.get("/admin/users").text


def test_fp_r08_an_in_handler_refusal_is_the_denial_page_on_get_and_on_post(
    client: TestClient, db: Session
) -> None:
    """A ``Forbidden`` raised with the queue in hand renders the same page."""
    agent_user(db)
    ticket = make_ticket(db, general(db), root(db))
    as_agent(client)

    for response in (
        client.get(f"/ticket/{ticket.id}"),
        post_update(client, ticket, content="We are on it."),
    ):
        assert response.status_code == 403
        assert "<h1>You are not allowed</h1>" in response.text
        assert SHELL_MARK in response.text
        assert "Traceback" not in response.text
    assert "ShowTicket" not in client.get(f"/ticket/{ticket.id}").text
