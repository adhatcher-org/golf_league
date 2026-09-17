"""GL-03: identity schema, session cookies, and the dependency ladder."""

from datetime import UTC, datetime

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from golf_league.config import Settings
from golf_league.database import get_session
from golf_league.domain.identity import is_valid_email, normalize_email
from golf_league.models import User
from golf_league.security import (
    SESSION_COOKIE_NAME,
    generate_csrf_token,
    require_admin,
    require_user,
    require_verified_user,
    validate_csrf,
)
from golf_league.services.auth import (
    create_session_cookie,
    hash_password,
    load_session_cookie,
    verify_password,
)

SECRET = "test-only-not-a-secret"


def make_user(session, *, username=None, email="player@example.com", is_admin=False, verified=True):
    user = User(
        username=username or email,
        email=email,
        display_name="Test Player",
        password_hash=hash_password("correct horse battery staple"),
        email_verified_at=datetime.now(UTC).replace(tzinfo=None) if verified else None,
        is_admin=is_admin,
        session_version=1,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def build_ladder_app(session):
    """A throwaway app exercising the GL-03 dependency ladder — no GL-04 routes."""
    app = FastAPI()
    app.state.settings = Settings(session_secret=SECRET, database_url="sqlite://")
    app.dependency_overrides[get_session] = lambda: session

    @app.get("/needs-user")
    def needs_user(user: User = Depends(require_user)):  # noqa: B008
        return {"id": user.id}

    @app.get("/needs-verified")
    def needs_verified(user: User = Depends(require_verified_user)):  # noqa: B008
        return {"id": user.id}

    @app.get("/needs-admin")
    def needs_admin(user: User = Depends(require_admin)):  # noqa: B008
        return {"id": user.id}

    return app


# 1. A 254-character username round-trips through the database.
def test_254_char_username_round_trips(session):
    long_username = ("a" * 242) + "@example.com"
    assert len(long_username) == 254

    user = User(
        username=long_username,
        email="round-trip@example.com",
        display_name="Round Trip",
        password_hash=hash_password("x" * 12),
        is_admin=False,
        session_version=1,
    )
    session.add(user)
    session.commit()

    fetched = session.get(User, user.id)
    assert fetched.username == long_username
    assert len(fetched.username) == 254


# 2. Correct password verifies; wrong password does not.
def test_password_hash_and_verify():
    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed) is True
    assert verify_password("wrong password", hashed) is False


# 3. A tampered cookie signature loads as None.
def test_tampered_cookie_signature_loads_as_none():
    cookie = create_session_cookie(user_id=1, session_version=1, secret=SECRET)
    user_id_part, version_part, signature = cookie.split(".")
    tampered_signature = ("0" if signature[0] != "0" else "1") + signature[1:]
    tampered = f"{user_id_part}.{version_part}.{tampered_signature}"

    assert load_session_cookie(tampered, SECRET) is None


def test_malformed_cookie_loads_as_none():
    assert load_session_cookie("not-a-valid-cookie", SECRET) is None
    assert load_session_cookie("", SECRET) is None


# 4. A cookie carrying a stale session_version loads as None (via require_user).
def test_stale_session_version_is_rejected(session):
    user = make_user(session)
    stale_cookie = create_session_cookie(user_id=user.id, session_version=0, secret=SECRET)

    app = build_ladder_app(session)
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, stale_cookie)

    response = client.get("/needs-user")
    assert response.status_code == 401


# 5. Incrementing session_version invalidates a previously valid cookie.
def test_incrementing_session_version_invalidates_existing_cookie(session):
    user = make_user(session)
    cookie = create_session_cookie(user_id=user.id, session_version=user.session_version, secret=SECRET)

    app = build_ladder_app(session)
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, cookie)

    # Valid before the bump.
    assert client.get("/needs-user").status_code == 200

    user.session_version += 1
    session.add(user)
    session.commit()

    # Same cookie, now stale, rejected after the bump.
    assert client.get("/needs-user").status_code == 401


# 11. Each rung of the dependency ladder rejects the case below it.
def test_ladder_anonymous_rejected_by_require_user(session):
    app = build_ladder_app(session)
    client = TestClient(app)
    assert client.get("/needs-user").status_code == 401
    assert client.get("/needs-verified").status_code == 401
    assert client.get("/needs-admin").status_code == 401


def test_ladder_unverified_user_rejected_by_require_verified_user(session):
    user = make_user(session, verified=False)
    cookie = create_session_cookie(user.id, user.session_version, SECRET)
    app = build_ladder_app(session)
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, cookie)

    assert client.get("/needs-user").status_code == 200
    assert client.get("/needs-verified").status_code == 403
    assert client.get("/needs-admin").status_code == 403


def test_ladder_verified_non_admin_rejected_by_require_admin(session):
    user = make_user(session, verified=True, is_admin=False)
    cookie = create_session_cookie(user.id, user.session_version, SECRET)
    app = build_ladder_app(session)
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, cookie)

    assert client.get("/needs-user").status_code == 200
    assert client.get("/needs-verified").status_code == 200
    assert client.get("/needs-admin").status_code == 403


def test_ladder_unverified_admin_is_still_rejected_by_require_verified_user(session):
    """is_admin is never a verification bypass."""
    user = make_user(session, verified=False, is_admin=True)
    cookie = create_session_cookie(user.id, user.session_version, SECRET)
    app = build_ladder_app(session)
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, cookie)

    assert client.get("/needs-verified").status_code == 403
    assert client.get("/needs-admin").status_code == 403


def test_ladder_verified_admin_passes_every_rung(session):
    user = make_user(session, verified=True, is_admin=True)
    cookie = create_session_cookie(user.id, user.session_version, SECRET)
    app = build_ladder_app(session)
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, cookie)

    assert client.get("/needs-user").status_code == 200
    assert client.get("/needs-verified").status_code == 200
    assert client.get("/needs-admin").status_code == 200


# 12. Missing, malformed and mismatched CSRF tokens each fail closed.
def test_csrf_missing_malformed_mismatched_all_fail_closed():
    session_value = "some-signed-session-cookie-value"
    valid_token = generate_csrf_token(session_value)

    assert validate_csrf(session_value, valid_token) is True
    assert validate_csrf(session_value, "") is False  # missing
    assert validate_csrf(session_value, None or "") is False  # missing
    assert validate_csrf(session_value, "not-hex-garbage!!") is False  # malformed
    assert validate_csrf(session_value, valid_token[:-1] + ("0" if valid_token[-1] != "0" else "1")) is False  # mismatched
    assert validate_csrf("", valid_token) is False  # no session at all


def test_csrf_token_is_deterministic_per_session_value():
    assert generate_csrf_token("abc") == generate_csrf_token("abc")
    assert generate_csrf_token("abc") != generate_csrf_token("xyz")


# Domain helpers (identity.py) — pure functions.
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("  Player@Example.com  ", "player@example.com"),
        ("ALREADY@LOWER.COM", "already@lower.com"),
        ("no-op@example.com", "no-op@example.com"),
    ],
)
def test_normalize_email(raw, expected):
    assert normalize_email(raw) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        ("player@example.com", True),
        ("no-at-sign.example.com", False),
        ("missing-domain@", False),
        ("@missing-local.com", False),
        ("has spaces@example.com", False),
        ("player@example", False),
    ],
)
def test_is_valid_email(value, expected):
    assert is_valid_email(value) is expected
