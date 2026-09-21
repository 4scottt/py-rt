"""Sign-in and the session cookie (FP L01, L03).

The login page's time is the hash on every side, so a name that does not
exist costs one bcrypt verification too; these tests assert it.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from pyrt import auth
from pyrt.app import create_app
from pyrt.config import Settings
from pyrt.db.models import User
from tests.conftest import ROOT_TEST_PASSWORD

#: A wrong password must still cost the hash; bcrypt at cost 12 is hundreds
#: of milliseconds, and this floor is well under the slowest machine's.
HASH_FLOOR_SECONDS = 0.05

SETTINGS = Settings(base_url="http://testserver", session_secret="test")


def make_user(
    db: Session,
    name: str,
    password: str | None = None,
    *,
    privileged: bool = False,
    disabled: bool = False,
) -> User:
    """A committed user the app's own session can see."""
    user = User(
        name=name,
        password_hash=auth.hash_password(password) if password else None,
        email=f"{name}@example.invalid",
        real_name=name,
        privileged=privileged,
        disabled=disabled,
    )
    db.add(user)
    db.commit()
    return user


def sign_in(client: TestClient, name: str = "root", password: str = ROOT_TEST_PASSWORD) -> None:
    """Sign the client in and fail the test when the password is refused."""
    response = client.post("/login", data={"user": name, "pass": password}, follow_redirects=False)
    assert response.status_code == 303, response.text
    assert auth.SESSION_COOKIE in client.cookies


def test_fp_l01_login_form_wrong_password_and_disabled_user(
    client: TestClient, db: Session
) -> None:
    """The form signed out; a refusal after the hash's time; disabled is out."""
    page = client.get("/")
    assert page.status_code == 200
    assert 'name="user"' in page.text
    assert 'name="pass"' in page.text
    assert ">Login</button>" in page.text
    assert auth.SESSION_COOKIE not in client.cookies

    started = time.monotonic()
    wrong = client.post("/login", data={"user": "root", "pass": "nope"}, follow_redirects=False)
    elapsed = time.monotonic() - started
    assert wrong.status_code == 200
    assert "Your username or password is incorrect" in wrong.text
    assert 'name="pass"' in wrong.text  # the form is back
    assert auth.SESSION_COOKIE not in client.cookies
    assert elapsed >= HASH_FLOOR_SECONDS, "a wrong password must cost the hash"

    started = time.monotonic()
    unknown = client.post(
        "/login", data={"user": "nobody-at-all", "pass": "nope"}, follow_redirects=False
    )
    elapsed = time.monotonic() - started
    assert unknown.status_code == 200
    assert "Your username or password is incorrect" in unknown.text
    assert elapsed >= HASH_FLOOR_SECONDS, "an unknown name must cost the hash too"

    make_user(db, "gone", "correct horse", disabled=True)
    refused = client.post(
        "/login", data={"user": "gone", "pass": "correct horse"}, follow_redirects=False
    )
    assert refused.status_code == 200
    assert "Your username or password is incorrect" in refused.text
    assert auth.SESSION_COOKIE not in client.cookies

    good = client.post(
        "/login", data={"user": "root", "pass": ROOT_TEST_PASSWORD}, follow_redirects=False
    )
    assert good.status_code == 303
    assert good.headers["location"] == "/"
    assert auth.SESSION_COOKIE in client.cookies


def test_fp_l03_session_cookie_flags_tampering_and_logout(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Signed, HttpOnly, SameSite=Lax, Secure only on https; tampering signs out."""
    response = client.post(
        "/login", data={"user": "root", "pass": ROOT_TEST_PASSWORD}, follow_redirects=False
    )
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "samesite=lax" in cookie.lower()
    assert "Path=/" in cookie
    assert "Max-Age=43200" in cookie  # 12 hours
    assert "Secure" not in cookie  # BASE_URL is http here

    # The value is a signed token, not the id.
    token = client.cookies[auth.SESSION_COOKIE]
    root = db.scalar(select(User).where(User.name == "root"))
    assert root is not None
    assert str(root.id) not in token.split(".")[0]
    assert auth.read_session(SETTINGS, token) == root.id

    # A tampered cookie is no session at all: the page is the login form again.
    client.cookies.set(auth.SESSION_COOKIE, token[:-2] + "xy")
    back = client.get("/")
    assert back.status_code == 200
    assert 'name="pass"' in back.text
    assert "RT at a glance" not in back.text

    # And it is refused for a private path.
    gated = client.get("/admin/users", follow_redirects=False)
    assert gated.status_code == 303
    assert gated.headers["location"].startswith("/?next=")

    # Logout clears the cookie.
    client.cookies.delete(auth.SESSION_COOKIE)
    sign_in(client)
    out = client.get("/logout", follow_redirects=False)
    assert out.status_code == 303
    assert out.headers["location"] == "/"
    cleared = out.headers["set-cookie"]
    assert 'pyrt_session=""' in cleared or "pyrt_session=;" in cleared
    assert "Max-Age=0" in cleared or "01 Jan 1970" in cleared
    assert auth.SESSION_COOKIE not in client.cookies

    # https makes the cookie Secure.
    monkeypatch.setenv("BASE_URL", "https://rt.example")
    secure_app = create_app()
    try:
        with TestClient(secure_app) as secure_client:
            secure = secure_client.post(
                "/login",
                data={"user": "root", "pass": ROOT_TEST_PASSWORD},
                follow_redirects=False,
            )
            assert "Secure" in secure.headers["set-cookie"]
    finally:
        secure_app.state.engine.dispose()


def test_a_forged_or_expired_token_is_no_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """The unit side of L03: the serializer's three refusals."""
    other = Settings(base_url="http://testserver", session_secret="another secret")
    user = User(id=7, name="someone")
    token = auth.issue_session(SETTINGS, user)

    assert auth.read_session(SETTINGS, token) == 7
    assert auth.read_session(SETTINGS, "") is None
    assert auth.read_session(SETTINGS, "not-a-token") is None
    assert auth.read_session(other, token) is None, "another secret must not verify"

    monkeypatch.setattr(auth, "SESSION_MAX_AGE", -1)
    assert auth.read_session(SETTINGS, token) is None, "an old token is no session"


def test_the_cookie_is_secure_only_for_an_https_base_url() -> None:
    assert auth.cookie_is_secure(SETTINGS) is False
    assert auth.cookie_is_secure(Settings(base_url="https://rt.example", session_secret="s"))


def test_disabling_a_user_ends_the_session_it_already_had(client: TestClient, db: Session) -> None:
    """The cookie survives, the session does not: the user is resolved per request."""
    make_user(db, "temp", "temp-password")
    sign_in(client, "temp", "temp-password")
    assert "RT at a glance" in client.get("/").text

    temp = db.scalar(select(User).where(User.name == "temp"))
    assert temp is not None
    temp.disabled = True
    db.commit()

    back = client.get("/")
    assert "RT at a glance" not in back.text
    assert 'name="pass"' in back.text


def test_authenticate_refuses_a_user_without_a_password(db: Session) -> None:
    """Nobody has no hash and cannot sign in whatever is typed."""
    assert auth.authenticate(db, "Nobody", "") is None
    assert auth.authenticate(db, "Nobody", "anything") is None
    assert auth.authenticate(db, "", "") is None
