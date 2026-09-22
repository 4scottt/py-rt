"""The rights pages (FP R03-R05).

Plan §8: on Group Rights the system groups, the roles and every
user-defined group each have a section with a checkbox per right, named
``{principal}-{right}``, and one ``Save Changes``; User Rights lists the
privileged users the same way; the global pages are the same again with
object kind ``system`` and the global-only rights offered.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from pyrt.db.models import GLOBAL_ONLY_RIGHTS, ObjectKind, Queue, Right
from tests.test_acl import grant, group_id, make_group, make_queue
from tests.test_auth import make_user, sign_in
from tests.test_queues import fresh, user_by_name

GROUP_RIGHTS_HEADING = "<h1>Group Rights</h1>"
USER_RIGHTS_HEADING = "<h1>User Rights</h1>"
GLOBAL_GROUP_HEADING = "<h1>Global Group Rights</h1>"
GLOBAL_USER_HEADING = "<h1>Global User Rights</h1>"
SAVE_BUTTON = ">Save Changes</button>"
SAVED = "Rights updated"
FORBIDDEN = "You are not allowed"


def rights_on(db: Session, principal: tuple[str, int], queue: Queue | None = None) -> set[str]:
    """The right names that principal holds on that queue, or globally."""
    kind, ident = principal
    return set(
        db.scalars(
            select(Right.right_name).where(
                Right.principal_kind == kind,
                Right.principal_id == ident,
                Right.object_kind == (ObjectKind.QUEUE if queue else ObjectKind.SYSTEM),
                Right.object_id == (queue.id if queue else 0),
            )
        ).all()
    )


def test_fp_r03_the_group_rights_page_has_a_section_per_kind_of_principal(
    client: TestClient, db: Session
) -> None:
    """The three sections, the queue-scoped rights, the sub-tabs, one button."""
    support = make_queue(db, "Support")
    sign_in(client)

    page = client.get(f"/admin/queues/{support.id}/group-rights")
    assert page.status_code == 200
    assert GROUP_RIGHTS_HEADING in page.text
    assert "Support" in page.text  # the queue the rights are for
    for title in ("System groups", "Roles", "User-defined groups"):
        assert f">{title}</h2>" in page.text
    assert "No user-defined groups yet" in page.text

    # The queue modify page's sub-tabs, this one current.
    assert f'href="http://testserver/admin/queues/{support.id}">Basics</a>' in page.text
    assert f"/admin/queues/{support.id}/user-rights" in page.text
    assert f"/admin/queues/{support.id}/custom-fields" in page.text

    # A checkbox per principal and right, named {principal}-{right}.
    for control in (
        "Everyone-CreateTicket",
        "Everyone-ReplyToTicket",
        "Privileged-ShowTicket",
        "Unprivileged-ShowTicket",
        "Owner-ModifyTicket",
        "Requestor-ShowTicket",
        "Cc-ShowTicket",
        "AdminCc-ShowTicket",
    ):
        assert f'name="{control}"' in page.text
    # ... and its label after it.
    assert '<label for="group-' in page.text
    assert ">OwnTicket</label>" in page.text

    # A queue grants no global-only right (plan §8's vocabulary table).
    for right in GLOBAL_ONLY_RIGHTS:
        assert f'name="Everyone-{right}"' not in page.text

    assert page.text.count("<form") == 2  # the shell's search box and this one
    assert SAVE_BUTTON in page.text


def test_fp_r03_user_defined_groups_list_by_name_and_a_disabled_one_does_not(
    client: TestClient, db: Session
) -> None:
    """Every enabled user-defined group has a row; a disabled one has none."""
    support = make_queue(db, "Support")
    make_group(db, "Technicians")
    make_group(db, "Retired", disabled=True)
    sign_in(client)

    page = client.get(f"/admin/queues/{support.id}/group-rights")
    assert page.status_code == 200
    assert "No user-defined groups yet" not in page.text
    assert 'name="Technicians-OwnTicket"' in page.text
    assert 'name="Technicians-ShowTicket"' in page.text
    assert 'name="Retired-OwnTicket"' not in page.text


def test_fp_r03_saving_writes_the_goals_grants_and_leaves_other_scopes_alone(
    client: TestClient, db: Session
) -> None:
    """The goal's two saves land as rows; a global grant and another queue stay."""
    support = make_queue(db, "Support")
    other = make_queue(db, "Other")
    technicians = make_group(db, "Technicians")
    everyone = group_id(db, "Everyone")
    grant(db, ("group", technicians.id), "SeeQueue")  # global
    grant(db, ("group", technicians.id), "OwnTicket", queue=other)  # another queue

    sign_in(client)
    saved = client.post(
        f"/admin/queues/{support.id}/group-rights",
        data={
            "Technicians-OwnTicket": "1",
            "Technicians-ShowTicket": "1",
            "Everyone-CreateTicket": "1",
            "Everyone-ReplyToTicket": "1",
            "submit": "save",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    target = f"http://testserver/admin/queues/{support.id}/group-rights?msg=saved"
    assert saved.headers["location"] == target

    back = client.get(target)
    assert SAVED in back.text
    assert 'name="Technicians-OwnTicket" value="1" checked' in back.text
    assert 'name="Everyone-CreateTicket" value="1" checked' in back.text
    assert 'name="Everyone-ShowTicket" value="1" checked' not in back.text

    fresh(db)
    assert rights_on(db, ("group", technicians.id), support) == {"OwnTicket", "ShowTicket"}
    assert rights_on(db, ("group", everyone), support) == {"CreateTicket", "ReplyToTicket"}
    assert rights_on(db, ("group", technicians.id)) == {"SeeQueue"}  # global, untouched
    assert rights_on(db, ("group", technicians.id), other) == {"OwnTicket"}  # other queue too
    row = db.scalar(
        select(Right).where(
            Right.principal_id == technicians.id,
            Right.right_name == "ShowTicket",
            Right.object_id == support.id,
        )
    )
    assert row is not None
    assert row.creator_id == user_by_name(db, "root").id  # the actor granted it

    # The checked set is the desired set: what is not ticked is revoked here
    # and nowhere else.
    again = client.post(
        f"/admin/queues/{support.id}/group-rights",
        data={"Technicians-OwnTicket": "1"},
        follow_redirects=False,
    )
    assert again.status_code == 303
    fresh(db)
    assert rights_on(db, ("group", technicians.id), support) == {"OwnTicket"}
    assert rights_on(db, ("group", everyone), support) == set()
    assert rights_on(db, ("group", technicians.id)) == {"SeeQueue"}
    assert rights_on(db, ("group", technicians.id), other) == {"OwnTicket"}


def test_fp_r03_a_principal_whose_name_holds_a_dash_saves_too(
    client: TestClient, db: Session
) -> None:
    """``Level-2-OwnTicket`` reaches the group ``Level-2``, not ``Level``."""
    support = make_queue(db, "Support")
    level_two = make_group(db, "Level-2")
    sign_in(client)

    page = client.get(f"/admin/queues/{support.id}/group-rights")
    assert 'name="Level-2-OwnTicket"' in page.text

    client.post(
        f"/admin/queues/{support.id}/group-rights",
        data={"Level-2-OwnTicket": "1"},
        follow_redirects=False,
    )
    fresh(db)
    assert rights_on(db, ("group", level_two.id), support) == {"OwnTicket"}


def test_fp_r03_the_page_needs_admin_queue_on_that_queue(client: TestClient, db: Session) -> None:
    """Without the right it is the denial page, and a POST writes nothing."""
    support = make_queue(db, "Support")
    agent = make_user(db, "agent", "agent-password", privileged=True)
    everyone = group_id(db, "Everyone")

    sign_in(client, "agent", "agent-password")
    refused = client.get(f"/admin/queues/{support.id}/group-rights")
    assert refused.status_code == 403
    assert FORBIDDEN in refused.text

    refused_post = client.post(
        f"/admin/queues/{support.id}/group-rights", data={"Everyone-CreateTicket": "1"}
    )
    assert refused_post.status_code == 403
    fresh(db)
    assert rights_on(db, ("group", everyone), support) == set()

    grant(db, ("user", agent.id), "AdminQueue", queue=support)
    allowed = client.get(f"/admin/queues/{support.id}/group-rights")
    assert allowed.status_code == 200
    assert GROUP_RIGHTS_HEADING in allowed.text


def test_fp_r04_user_rights_lists_the_privileged_users_and_saves_them(
    client: TestClient, db: Session
) -> None:
    """One section of privileged users; an unprivileged and a disabled one out."""
    support = make_queue(db, "Support")
    tech = make_user(db, "tech", privileged=True)
    make_user(db, "visitor")
    make_user(db, "gone", privileged=True, disabled=True)

    sign_in(client)
    page = client.get(f"/admin/queues/{support.id}/user-rights")
    assert page.status_code == 200
    assert USER_RIGHTS_HEADING in page.text
    assert ">Privileged users</h2>" in page.text
    assert 'name="tech-OwnTicket"' in page.text
    assert 'name="root-ShowTicket"' in page.text
    assert 'name="visitor-OwnTicket"' not in page.text
    assert 'name="gone-OwnTicket"' not in page.text
    assert 'name="tech-SuperUser"' not in page.text  # global-only, not here
    assert SAVE_BUTTON in page.text

    saved = client.post(
        f"/admin/queues/{support.id}/user-rights",
        data={"tech-OwnTicket": "1", "tech-ShowTicket": "1"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert saved.headers["location"].endswith(f"/admin/queues/{support.id}/user-rights?msg=saved")
    fresh(db)
    assert rights_on(db, ("user", tech.id), support) == {"OwnTicket", "ShowTicket"}

    revoked = client.post(
        f"/admin/queues/{support.id}/user-rights",
        data={"tech-ShowTicket": "1"},
        follow_redirects=False,
    )
    assert revoked.status_code == 303
    fresh(db)
    assert rights_on(db, ("user", tech.id), support) == {"ShowTicket"}


def test_fp_r05_the_global_pages_offer_every_right_and_write_system_rows(
    client: TestClient, db: Session
) -> None:
    """The global-only rights are on offer and a save is object kind system."""
    technicians = make_group(db, "Technicians")
    everyone = group_id(db, "Everyone")
    sign_in(client)

    page = client.get("/admin/global/group-rights")
    assert page.status_code == 200
    assert GLOBAL_GROUP_HEADING in page.text
    for right in GLOBAL_ONLY_RIGHTS:
        assert f'name="Everyone-{right}"' in page.text
    assert 'name="Technicians-ShowConfigTab"' in page.text
    assert "page-subtitle" not in page.text  # no queue, so no queue's tabs
    assert SAVE_BUTTON in page.text

    saved = client.post(
        "/admin/global/group-rights",
        data={"Everyone-CreateTicket": "1", "Technicians-ShowConfigTab": "1"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert saved.headers["location"] == "http://testserver/admin/global/group-rights?msg=saved"
    fresh(db)
    assert rights_on(db, ("group", everyone)) == {"CreateTicket"}
    assert rights_on(db, ("group", technicians.id)) == {"ShowConfigTab"}
    assert rights_on(db, ("group", everyone), make_queue(db, "Support")) == set()

    users_page = client.get("/admin/global/user-rights")
    assert users_page.status_code == 200
    assert GLOBAL_USER_HEADING in users_page.text
    assert ">Privileged users</h2>" in users_page.text
    assert 'name="root-SuperUser" value="1" checked' in users_page.text  # the seed's grant

    root = user_by_name(db, "root")
    kept = client.post(
        "/admin/global/user-rights",
        data={"root-SuperUser": "1", "root-AdminUsers": "1"},
        follow_redirects=False,
    )
    assert kept.status_code == 303
    fresh(db)
    assert rights_on(db, ("user", root.id)) == {"SuperUser", "AdminUsers"}


def test_fp_r05_the_global_pages_need_super_user(client: TestClient, db: Session) -> None:
    """A queue administrator is refused; SuperUser is let in."""
    admin = make_user(db, "queue-admin", "queue-admin-password", privileged=True)
    grant(db, ("user", admin.id), "AdminQueue")  # global AdminQueue is not enough
    grant(db, ("user", admin.id), "ShowConfigTab")

    sign_in(client, "queue-admin", "queue-admin-password")
    refused = client.get("/admin/global/group-rights")
    assert refused.status_code == 403
    assert FORBIDDEN in refused.text

    refused_post = client.post("/admin/global/user-rights", data={"root-SuperUser": "1"})
    assert refused_post.status_code == 403

    grant(db, ("user", admin.id), "SuperUser")
    allowed = client.get("/admin/global/user-rights")
    assert allowed.status_code == 200
    assert GLOBAL_USER_HEADING in allowed.text
    # The dashed name is a principal name too, and its boxes are its own.
    assert 'name="queue-admin-SuperUser" value="1" checked' in allowed.text
