"""The group admin pages (FP U05, U06, U08).

The Select list of *user-defined* groups only — the system groups and the
roles are the resolver's rows, not a person's pages — Create and Basics
behind ``AdminGroup``, Members behind ``AdminGroupMembership``, and the one
behaviour that makes disabling worth having: a disabled group's grants fall
out of the effective set without a grant being deleted.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pyrt.acl import has_right, principals
from pyrt.db.models import (
    NOBODY_USER_NAME,
    SYSTEM_GROUP_EVERYONE,
    Group,
    GroupKind,
    GroupMember,
    Right,
    User,
    utcnow,
)
from pyrt.groups import service
from tests.test_acl import grant
from tests.test_auth import make_user, sign_in
from tests.test_queues import fresh

ADMIN_GROUP = "AdminGroup"
ADMIN_GROUP_MEMBERSHIP = "AdminGroupMembership"


def make_group(db: Session, name: str, *, description: str = "", disabled: bool = False) -> Group:
    """A committed user-defined group the app's own session can see."""
    group = Group(
        name=name,
        description=description,
        kind=GroupKind.USER_DEFINED,
        disabled=disabled,
        created=utcnow(),
    )
    db.add(group)
    db.commit()
    return group


def group_by_name(db: Session, name: str) -> Group:
    found = db.scalar(select(Group).where(Group.name == name))
    assert found is not None, name
    return found


def reloaded(db: Session, group: Group) -> Group:
    """The group row as it stands now (the app wrote through its own session)."""
    fresh(db)
    found = db.get(Group, group.id)
    assert found is not None
    return found


def group_count(db: Session) -> int:
    """How many group rows there are; a refused form must add none."""
    count = db.scalar(select(func.count()).select_from(Group))
    assert count is not None
    return count


def member_count(db: Session, group: Group) -> int:
    """How many membership rows the group has; a second Add must add none."""
    count = db.scalar(
        select(func.count()).select_from(GroupMember).where(GroupMember.group_id == group.id)
    )
    assert count is not None
    return count


def user_id(db: Session, name: str) -> int:
    user = db.scalar(select(User).where(User.name == name))
    assert user is not None, name
    return user.id


def form(**fields: str) -> dict[str, str]:
    """The Basics form's fields, with the ones a test does not name left out."""
    values = {"name": "", "description": ""}
    values.update(fields)
    return values


def test_fp_u05_the_groups_list_creating_one_and_modifying_it(
    client: TestClient, db: Session
) -> None:
    """The Select list, Create with a unique name, Basics, and the gate."""
    make_group(db, "Retired", disabled=True)

    sign_in(client)
    page = client.get("/admin/groups")
    assert page.status_code == 200
    assert "<h1>Groups</h1>" in page.text

    # The columns of FP U05 and the Select / Create tabs of plan §8.
    for column in ("Name", "Description", "Enabled"):
        assert f"<th>{column}</th>" in page.text
    assert ">Select</a>" in page.text
    assert ">Create</a>" in page.text

    # Only user-defined groups list: never the system groups or the roles.
    assert SYSTEM_GROUP_EVERYONE not in page.text
    assert "Privileged" not in page.text
    assert "Unprivileged" not in page.text
    assert "Owner" not in page.text
    assert "Requestor" not in page.text

    # A disabled group is hidden until the page is asked for it.
    assert "Retired" not in page.text
    assert "Include disabled groups" in page.text
    with_disabled = client.get("/admin/groups?disabled=1")
    assert "Retired" in with_disabled.text
    assert "Hide disabled groups" in with_disabled.text
    assert SYSTEM_GROUP_EVERYONE not in with_disabled.text

    # Create: the form, its fields and its button.
    blank = client.get("/admin/groups/new")
    assert blank.status_code == 200
    assert "<h1>Create a group</h1>" in blank.text
    assert 'name="name"' in blank.text
    assert 'name="description"' in blank.text
    assert 'name="enabled"' in blank.text
    assert ">Create</button>" in blank.text

    made = client.post(
        "/admin/groups/new",
        data=form(name="  Staff  ", description="The support staff", enabled="1"),
        follow_redirects=False,
    )
    assert made.status_code == 303
    fresh(db)
    staff = group_by_name(db, "Staff")
    assert made.headers["location"] == f"http://testserver/admin/groups/{staff.id}?msg=created"
    assert staff.kind == GroupKind.USER_DEFINED
    assert staff.description == "The support staff"
    assert staff.disabled is False

    landed = client.get(made.headers["location"])
    assert landed.status_code == 200
    assert "Group created" in landed.text
    assert "<h1>Modify a group Staff</h1>" in landed.text
    # The sub-tabs of the modify page: Basics and Members, as links.
    assert ">Basics</a>" in landed.text
    assert f'href="http://testserver/admin/groups/{staff.id}/members">Members</a>' in landed.text
    assert "Staff" in client.get("/admin/groups").text

    # A name already taken is refused, whatever its case — and the system
    # groups count, though they never list here.
    before = group_count(db)
    clash = client.post("/admin/groups/new", data=form(name="staff", enabled="1"))
    assert clash.status_code == 200
    assert "A group with that name already exists" in clash.text
    assert 'value="staff"' in clash.text
    system_clash = client.post("/admin/groups/new", data=form(name="everyone", enabled="1"))
    assert "A group with that name already exists" in system_clash.text
    nameless = client.post("/admin/groups/new", data=form(description="no name", enabled="1"))
    assert "A group needs a name" in nameless.text
    fresh(db)
    assert group_count(db) == before

    # Modify: the name and the description save.
    saved = client.post(
        f"/admin/groups/{staff.id}",
        data=form(name="Support Staff", description="Everyone on the desk", enabled="1"),
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert saved.headers["location"] == f"http://testserver/admin/groups/{staff.id}?msg=saved"
    assert "Group updated" in client.get(saved.headers["location"]).text
    fresh(db)
    assert db.get(Group, staff.id) is not None
    assert group_by_name(db, "Support Staff").description == "Everyone on the desk"

    # A system group's id, and an unknown one, are 404s: neither is a page.
    everyone = group_by_name(db, SYSTEM_GROUP_EVERYONE)
    assert client.get(f"/admin/groups/{everyone.id}").status_code == 404
    assert client.post(f"/admin/groups/{everyone.id}", data=form(name="x")).status_code == 404
    assert client.get("/admin/groups/9999").status_code == 404

    # The gate: AdminGroup, global-only (SuperUser covered it for root).
    make_user(db, "alice", "alice-password", privileged=True)
    client.cookies.clear()
    sign_in(client, "alice", "alice-password")
    refused = client.get("/admin/groups")
    assert refused.status_code == 403
    assert "You are not allowed" in refused.text
    assert client.get("/admin/groups/new").status_code == 403
    assert client.get(f"/admin/groups/{staff.id}").status_code == 403
    assert (
        client.post("/admin/groups/new", data=form(name="Sneaky", enabled="1")).status_code == 403
    )

    grant(db, ("user", user_id(db, "alice")), ADMIN_GROUP)
    assert client.get("/admin/groups").status_code == 200
    assert client.get(f"/admin/groups/{staff.id}").status_code == 200


def test_fp_u06_members_are_added_from_the_select_and_removed_per_row(
    client: TestClient, db: Session
) -> None:
    """Add, Remove, at most one row per user, and the membership gate."""
    staff = make_group(db, "Staff")
    make_user(db, "alice", "alice-password", privileged=True)
    make_user(db, "bob", "bob-password")
    make_user(db, "retired", "retired-password", disabled=True)

    sign_in(client)
    page = client.get(f"/admin/groups/{staff.id}/members")
    assert page.status_code == 200
    assert "<h1>Members of Staff</h1>" in page.text
    for column in ("Name", "Real Name", "Email"):
        assert f"<th>{column}</th>" in page.text
    assert ">Basics</a>" in page.text
    assert ">Members</a>" in page.text

    # The select of plan §8: enabled users who are not members, never Nobody.
    assert 'name="user"' in page.text
    assert ">Add</button>" in page.text
    assert f'<option value="{user_id(db, "alice")}">alice</option>' in page.text
    assert f'<option value="{user_id(db, "bob")}">bob</option>' in page.text
    assert "retired" not in page.text
    assert NOBODY_USER_NAME not in page.text

    added = client.post(
        f"/admin/groups/{staff.id}/members",
        data={"submit": "add", "user": str(user_id(db, "alice"))},
        follow_redirects=False,
    )
    assert added.status_code == 303
    assert (
        added.headers["location"] == f"http://testserver/admin/groups/{staff.id}/members?msg=added"
    )
    listed = client.get(added.headers["location"])
    assert "Member added" in listed.text
    assert ">Remove</button>" in listed.text
    fresh(db)
    assert member_count(db, staff) == 1
    assert [user.name for user in service.members(db, staff)] == ["alice"]
    # A member is no longer offered by the select.
    assert f'<option value="{user_id(db, "alice")}">alice</option>' not in listed.text
    assert f'<option value="{user_id(db, "bob")}">bob</option>' in listed.text

    # A second Add — a stale form — writes nothing and says so.
    again = client.post(
        f"/admin/groups/{staff.id}/members",
        data={"submit": "add", "user": str(user_id(db, "alice"))},
        follow_redirects=False,
    )
    assert again.headers["location"].endswith("msg=already")
    assert "Already a member" in client.get(again.headers["location"]).text
    fresh(db)
    assert member_count(db, staff) == 1

    # Neither Nobody nor a disabled user may be added by a stale form.
    nobody = client.post(
        f"/admin/groups/{staff.id}/members",
        data={"submit": "add", "user": str(user_id(db, NOBODY_USER_NAME))},
    )
    assert nobody.status_code == 404
    disabled = client.post(
        f"/admin/groups/{staff.id}/members",
        data={"submit": "add", "user": str(user_id(db, "retired"))},
    )
    assert disabled.status_code == 404
    fresh(db)
    assert member_count(db, staff) == 1

    # Remove, from the row's own form.
    removed = client.post(
        f"/admin/groups/{staff.id}/members",
        data={"submit": "remove", "user": str(user_id(db, "alice"))},
        follow_redirects=False,
    )
    assert removed.headers["location"].endswith("msg=removed")
    empty = client.get(removed.headers["location"])
    assert "Member removed" in empty.text
    assert "No members" in empty.text
    fresh(db)
    assert member_count(db, staff) == 0

    # The gate: AdminGroupMembership, not AdminGroup.
    client.cookies.clear()
    sign_in(client, "alice", "alice-password")
    grant(db, ("user", user_id(db, "alice")), ADMIN_GROUP)
    assert client.get(f"/admin/groups/{staff.id}/members").status_code == 403
    assert (
        client.post(
            f"/admin/groups/{staff.id}/members",
            data={"submit": "add", "user": str(user_id(db, "bob"))},
        ).status_code
        == 403
    )
    fresh(db)
    assert member_count(db, staff) == 0

    grant(db, ("user", user_id(db, "alice")), ADMIN_GROUP_MEMBERSHIP)
    assert client.get(f"/admin/groups/{staff.id}/members").status_code == 200
    client.post(
        f"/admin/groups/{staff.id}/members",
        data={"submit": "add", "user": str(user_id(db, "bob"))},
        follow_redirects=False,
    )
    fresh(db)
    assert [user.name for user in service.members(db, staff)] == ["bob"]


def test_fp_u08_disabling_a_group_drops_its_rights_and_keeps_the_grants(
    client: TestClient, db: Session
) -> None:
    """A member holds the group's right; disabled, the group stops granting."""
    staff = make_group(db, "Staff")
    bob = make_user(db, "bob", "bob-password", privileged=True)
    db.add(GroupMember(group_id=staff.id, user_id=bob.id))
    db.commit()

    grant(db, ("group", staff.id), "ShowTicket")

    def bob_may_show() -> bool:
        fresh(db)
        return has_right(db, principals(db, db.get(User, bob.id)), "ShowTicket")

    def grants_to_staff() -> int:
        count = db.scalar(
            select(func.count())
            .select_from(Right)
            .where(Right.principal_kind == "group", Right.principal_id == staff.id)
        )
        assert count is not None
        return count

    assert bob_may_show() is True
    assert grants_to_staff() == 1

    # Unchecking Enabled on the Basics form disables the group.
    sign_in(client)
    disabled = client.post(
        f"/admin/groups/{staff.id}",
        data=form(name="Staff", description=""),
        follow_redirects=False,
    )
    assert disabled.status_code == 303
    assert reloaded(db, staff).disabled is True

    # The right falls out of the effective set; the grant is still a row, and
    # so is the membership.
    assert bob_may_show() is False
    assert grants_to_staff() == 1
    assert member_count(db, staff) == 1

    # Re-enabling restores it, the grant never having moved.
    client.post(
        f"/admin/groups/{staff.id}",
        data=form(name="Staff", enabled="1"),
        follow_redirects=False,
    )
    assert reloaded(db, staff).disabled is False
    assert bob_may_show() is True
