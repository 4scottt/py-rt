"""The user admin pages (FP U01, U02, U03).

The list with Nobody out and the disabled hidden, Create with its unique
name and optional password, and Modify, where a password changes only when
the field is filled and disabling — never deleting — takes a person out of
sign-in.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pyrt.db.models import NOBODY_USER_NAME, ROOT_USER_NAME, User
from pyrt.users import service
from tests.conftest import ROOT_TEST_PASSWORD
from tests.test_acl import grant
from tests.test_auth import make_user, sign_in

ADMIN_USERS = "AdminUsers"


def user_by_name(db: Session, name: str) -> User:
    found = db.scalar(select(User).where(User.name == name))
    assert found is not None, name
    return found


def user_id(db: Session, name: str) -> int:
    return user_by_name(db, name).id


def user_count(db: Session) -> int:
    """How many user rows there are; a refused form must add none."""
    count = db.scalar(select(func.count()).select_from(User))
    assert count is not None
    return count


def form(**fields: str) -> dict[str, str]:
    """The modify form's fields, with the ones a test does not name left out."""
    values = {"name": "", "email": "", "real_name": "", "password": ""}
    values.update(fields)
    return values


def can_sign_in(client: TestClient, name: str, password: str) -> bool:
    """Whether ``name`` can sign in; the client is left signed out."""
    client.cookies.clear()
    response = client.post("/login", data={"user": name, "pass": password}, follow_redirects=False)
    client.cookies.clear()
    return response.status_code == 303


def test_fp_u01_the_users_list_hides_nobody_and_the_disabled_and_needs_admin_users(
    client: TestClient, db: Session
) -> None:
    """The Select list of plan §8, its columns, its tabs and its gate."""
    make_user(db, "alice", "alice-password", privileged=True)
    make_user(db, "retired", "retired-password", disabled=True)

    sign_in(client)
    page = client.get("/admin/users")
    assert page.status_code == 200
    assert "<h1>Users</h1>" in page.text

    # The columns of FP U01 and the tabs of plan §8.
    for column in ("Name", "Real Name", "Email", "Privileged", "Enabled"):
        assert f"<th>{column}</th>" in page.text
    assert ">Select</a>" in page.text
    assert ">Create</a>" in page.text

    assert "alice" in page.text
    assert ROOT_USER_NAME in page.text
    assert "retired" not in page.text
    assert NOBODY_USER_NAME not in page.text
    assert "Include disabled users" in page.text

    # ... unless the page is asked for them, and Nobody still never lists.
    with_disabled = client.get("/admin/users?disabled=1")
    assert "retired" in with_disabled.text
    assert NOBODY_USER_NAME not in with_disabled.text

    # The gate: AdminUsers, global-only (SuperUser covered it for root).
    client.cookies.clear()
    sign_in(client, "alice", "alice-password")
    refused = client.get("/admin/users")
    assert refused.status_code == 403
    assert "You are not allowed" in refused.text
    assert client.get("/admin/users/new").status_code == 403
    assert client.get(f"/admin/users/{user_id(db, 'alice')}").status_code == 403

    grant(db, ("user", user_id(db, "alice")), ADMIN_USERS)
    assert client.get("/admin/users").status_code == 200


def test_fp_u02_create_a_user_with_every_field_then_sign_in_as_them(
    client: TestClient, db: Session
) -> None:
    """A unique name, an optional password, the privileged flag (FP U02)."""
    sign_in(client)
    blank = client.get("/admin/users/new")
    assert blank.status_code == 200
    assert "<h1>Create a user</h1>" in blank.text
    assert "Let this user be granted rights (Privileged)" in blank.text
    assert ">Create</button>" in blank.text

    made = client.post(
        "/admin/users/new",
        data=form(
            name="  bob  ",
            email="bob@example.invalid",
            real_name="Bob Builder",
            password="bob-password",
            privileged="1",
            enabled="1",
        ),
        follow_redirects=False,
    )
    assert made.status_code == 303
    bob = user_by_name(db, "bob")
    assert made.headers["location"] == f"http://testserver/admin/users/{bob.id}?msg=created"

    landed = client.get(made.headers["location"])
    assert landed.status_code == 200
    assert "User created" in landed.text
    assert "<h1>Modify a user bob</h1>" in landed.text

    assert bob.email == "bob@example.invalid"
    assert bob.real_name == "Bob Builder"
    assert bob.privileged is True
    assert bob.disabled is False
    assert bob in service.privileged_users(db)

    # The password the form gave is the one that signs in.
    assert can_sign_in(client, "bob", "bob-password")
    assert not can_sign_in(client, "bob", "not-bobs-password")

    # A name already taken, whatever its case, is refused with the values kept.
    before_the_clash = user_count(db)
    sign_in(client)
    clash = client.post(
        "/admin/users/new",
        data=form(name="BOB", email="other@example.invalid", enabled="1"),
    )
    assert clash.status_code == 200
    assert "A user with that name already exists" in clash.text
    assert 'value="BOB"' in clash.text
    assert 'value="other@example.invalid"' in clash.text
    db.rollback()
    assert user_count(db) == before_the_clash

    # An email that is not one is refused too.
    bad = client.post(
        "/admin/users/new", data=form(name="carol", email="carol at example", enabled="1")
    )
    assert bad.status_code == 200
    assert "email address" in bad.text
    assert db.scalar(select(User).where(User.name == "carol")) is None

    # No password: the user exists, unprivileged, and cannot sign in.
    without_password = client.post(
        "/admin/users/new",
        data=form(name="dave", email="dave@example.invalid", enabled="1"),
        follow_redirects=False,
    )
    assert without_password.status_code == 303
    db.rollback()
    dave = user_by_name(db, "dave")
    assert dave.password_hash is None
    assert dave.privileged is False
    assert not can_sign_in(client, "dave", "")
    assert not can_sign_in(client, "dave", "guess")


def test_fp_u03_modify_a_user_its_password_and_disabling_it(
    client: TestClient, db: Session
) -> None:
    """The password only when filled; disabled, not deleted (FP U03)."""
    make_user(db, "erin", "erin-password", privileged=True)
    erin = user_id(db, "erin")

    sign_in(client)
    page = client.get(f"/admin/users/{erin}")
    assert page.status_code == 200
    assert "<h1>Modify a user erin</h1>" in page.text
    assert ">Save Changes</button>" in page.text
    assert 'value="erin"' in page.text

    # A blank password field leaves the password alone.
    saved = client.post(
        f"/admin/users/{erin}",
        data=form(
            name="erin", email="erin@example.invalid", real_name="Erin", enabled="1", privileged="1"
        ),
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert saved.headers["location"] == f"http://testserver/admin/users/{erin}?msg=saved"
    assert "User updated" in client.get(saved.headers["location"]).text
    db.rollback()
    assert user_by_name(db, "erin").real_name == "Erin"
    assert can_sign_in(client, "erin", "erin-password")

    # A filled one changes it.
    sign_in(client)
    client.post(
        f"/admin/users/{erin}",
        data=form(name="erin", email="erin@example.invalid", password="erin-new", enabled="1"),
        follow_redirects=False,
    )
    assert can_sign_in(client, "erin", "erin-new")
    assert not can_sign_in(client, "erin", "erin-password")

    # Unchecking Enabled disables the row; the user is never deleted, and it
    # leaves sign-in and the owner select behind it.
    sign_in(client)
    client.post(
        f"/admin/users/{erin}",
        data=form(name="erin", email="erin@example.invalid"),
        follow_redirects=False,
    )
    db.rollback()
    disabled = user_by_name(db, "erin")
    assert disabled.disabled is True
    assert not can_sign_in(client, "erin", "erin-new")
    assert disabled not in service.privileged_users(db)
    sign_in(client)
    assert "erin" not in client.get("/admin/users").text
    assert "erin" in client.get("/admin/users?disabled=1").text

    # root cannot be disabled: the page says so and the row does not change.
    sign_in(client)
    root = user_id(db, ROOT_USER_NAME)
    refused = client.post(
        f"/admin/users/{root}", data=form(name=ROOT_USER_NAME, email="root@localhost")
    )
    assert refused.status_code == 200
    assert "The root user cannot be disabled" in refused.text
    db.rollback()
    assert user_by_name(db, ROOT_USER_NAME).disabled is False
    assert can_sign_in(client, ROOT_USER_NAME, ROOT_TEST_PASSWORD)

    # Nobody is not a person: its id is a 404, as an unknown id is.
    sign_in(client)
    nobody = user_id(db, NOBODY_USER_NAME)
    assert client.get(f"/admin/users/{nobody}").status_code == 404
    assert client.post(f"/admin/users/{nobody}", data=form(name="anything")).status_code == 404
    assert client.get("/admin/users/9999").status_code == 404
