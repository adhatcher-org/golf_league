"""Milestone M1 exit-criteria tests: "M0 -> bootstrap/login/verify/reset with
register closed".

These exercise the whole identity journey end to end through the application's
real entry points (`Settings`, `create_app`, its ASGI lifespan, and the real
`/login`, `/verify/{token}`, `/reset`, `/reset/{token}` routes) starting from a
genuinely empty, temporary SQLite database, exactly the way a fresh boot with
`ADMIN_EMAIL`/`ADMIN_PASSWORD` in the environment behaves. No test touches the
repository's own `data/` directory, and `SESSION_SECRET` is absent from the
ambient environment (supplied explicitly to `Settings`, the way `.env`/
docker-compose supplies it in a real deployment).
"""

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from golf_league.app import create_app
from golf_league.config import Settings
from golf_league.migrations import current_revision
from golf_league.models import User
from golf_league.services.auth import authenticate_user, hash_password, issue_token

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MIGRATIONS_DIR = _REPO_ROOT / "migrations"

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]*)"')

ADMIN_EMAIL = "admin@example.test"
ADMIN_PASSWORD = "correct-horse-battery-staple-1"


def _extract_csrf(html: str) -> str:
    match = CSRF_RE.search(html)
    assert match, f"no csrf_token field found in: {html!r}"
    return match.group(1)


def _head_revision() -> str:
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS_DIR))
    return ScriptDirectory.from_config(config).get_current_head()


@pytest.fixture
def admin_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Boot the real app against a brand new database with an admin to bootstrap.

    Mirrors a genuine first boot: no ambient `SESSION_SECRET`, no `DATABASE_URL`,
    only `ADMIN_EMAIL`/`ADMIN_PASSWORD` set, and a database file that does not
    exist yet.
    """
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("ADMIN_EMAIL", ADMIN_EMAIL)
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_PASSWORD)

    db_path = tmp_path / "app.db"
    assert not db_path.exists()

    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        session_secret="milestone-m1-test-secret-not-a-real-secret",
    )

    with TestClient(create_app(settings=settings)) as client:
        yield client, db_path


def test_boot_migrates_to_head_and_bootstraps_exactly_one_admin(admin_app) -> None:
    """Empty DB + ADMIN_EMAIL/ADMIN_PASSWORD -> real boot migrates to head and
    creates exactly one verified admin (criterion 1)."""
    client, db_path = admin_app

    assert db_path.exists()
    assert current_revision(f"sqlite:///{db_path}") == _head_revision()

    with Session(bind=client.app.state.engine) as session:
        users = session.query(User).all()
        assert len(users) == 1
        admin = users[0]
        assert admin.email == ADMIN_EMAIL
        assert admin.is_admin is True
        assert admin.email_verified_at is not None


def test_bootstrapped_admin_can_log_in_and_receives_a_session_cookie(admin_app) -> None:
    """The bootstrapped admin logs in through the real `/login` route and gets
    a session cookie (criterion 2)."""
    client, _db_path = admin_app

    login_page = client.get("/login")
    assert login_page.status_code == 200
    csrf_token = _extract_csrf(login_page.text)

    response = client.post(
        "/login",
        data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "csrf_token": csrf_token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "session" in response.cookies


def test_email_verification_via_real_token_and_repeat_is_neutral(admin_app) -> None:
    """A real `verify_email` token verifies the account through `/verify/{token}`;
    reusing the same token is neutral, not an error (criterion 3)."""
    client, _db_path = admin_app

    with Session(bind=client.app.state.engine) as session:
        user = User(
            username="player@example.test",
            email="player@example.test",
            display_name="Player",
            password_hash=hash_password("a-real-password-1"),
            email_verified_at=None,
            is_admin=False,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        user_id = user.id

    with Session(bind=client.app.state.engine) as session:
        raw_token = issue_token(session, user_id, "verify_email", 3600)

    first = client.get(f"/verify/{raw_token}")
    assert first.status_code == 200

    with Session(bind=client.app.state.engine) as session:
        assert session.get(User, user_id).email_verified_at is not None

    second = client.get(f"/verify/{raw_token}")
    assert second.status_code == 200
    assert second.text == first.text
    assert raw_token not in first.text


def test_password_reset_end_to_end_then_old_password_fails(admin_app) -> None:
    """Full reset journey: request -> real token -> set new password -> the
    old password stops working through the real `/login` route (criterion 4)."""
    client, _db_path = admin_app
    email = "resetme@example.test"
    old_password = "the-original-password-1"
    new_password = "brand-new-password-2"

    with Session(bind=client.app.state.engine) as session:
        user = User(
            username=email,
            email=email,
            display_name="Reset Me",
            password_hash=hash_password(old_password),
            email_verified_at=datetime.now(UTC).replace(tzinfo=None),
            is_admin=False,
        )
        session.add(user)
        session.commit()

    reset_request_page = client.get("/reset")
    reset_csrf = _extract_csrf(reset_request_page.text)
    request_response = client.post(
        "/reset", data={"email": email, "csrf_token": reset_csrf}
    )
    assert request_response.status_code == 200

    sent = client.app.state.email_sender.sent
    assert len(sent) == 1
    raw_token = sent[-1]["body"].rsplit("/reset/", 1)[1]

    reset_form_page = client.get(f"/reset/{raw_token}")
    assert reset_form_page.status_code == 200
    reset_form_csrf = _extract_csrf(reset_form_page.text)

    complete = client.post(
        f"/reset/{raw_token}",
        data={"password": new_password, "csrf_token": reset_form_csrf},
        follow_redirects=False,
    )
    assert complete.status_code == 303

    # The old password must now fail through the real login route ...
    login_page = client.get("/login")
    csrf_token = _extract_csrf(login_page.text)
    old_password_attempt = client.post(
        "/login",
        data={"email": email, "password": old_password, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert old_password_attempt.status_code == 401

    # ... while the new password succeeds.
    login_page = client.get("/login")
    csrf_token = _extract_csrf(login_page.text)
    new_password_attempt = client.post(
        "/login",
        data={"email": email, "password": new_password, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert new_password_attempt.status_code == 303
    assert "session" in new_password_attempt.cookies

    # Belt and braces: the service-level check agrees with the HTTP behaviour.
    with Session(bind=client.app.state.engine) as session:
        assert authenticate_user(session, email, old_password) is None
        assert authenticate_user(session, email, new_password) is not None


def test_register_is_closed_no_open_self_registration_route(admin_app) -> None:
    """There is no open self-registration route at this milestone: GL-13 owns
    roster-restricted registration and GL-42/43 own invite/join, neither of
    which exists yet (criterion 5)."""
    client, _db_path = admin_app

    assert client.get("/register").status_code == 404
    assert client.post("/register", data={}).status_code == 404

    # Also assert it structurally: no route path anywhere in the app mentions
    # registration, invites, or joining -- this is not just the specific
    # `/register` guess failing to match, there is genuinely no such route.
    route_paths = [route.path for route in client.app.routes if hasattr(route, "path")]
    forbidden_markers = ("register", "invite", "join")
    offending = [
        path
        for path in route_paths
        if any(marker in path.lower() for marker in forbidden_markers)
    ]
    assert offending == [], f"unexpected self-registration/invite routes: {offending}"
