"""Tests for `golf_league.routers.identity`."""

import re
from datetime import UTC, datetime, timedelta

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from golf_league.domain.rate_limit import RateLimiter
from golf_league.domain.tokens import digest
from golf_league.models import GolferSetPasswordToken, LeagueInviteLink, User, UserToken
from golf_league.security import (
    generate_csrf_token,
    require_admin,
    require_user,
    require_verified_user,
)
from golf_league.services.auth import hash_password, issue_token, verify_password

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]*)"')


def _extract_csrf(html: str) -> str:
    match = CSRF_RE.search(html)
    assert match, f"no csrf_token field found in: {html!r}"
    return match.group(1)


def _make_user(client, *, email, password, verified=True, is_admin=False):
    session = Session(bind=client.app.state.engine)
    try:
        user = User(
            username=email,
            email=email,
            display_name="Test User",
            password_hash=hash_password(password),
            email_verified_at=datetime.now(UTC).replace(tzinfo=None) if verified else None,
            is_admin=is_admin,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user.id
    finally:
        session.close()


def _add_protected_routes(client) -> None:
    """Add throwaway routes for exercising the dependency ladder in tests."""

    if getattr(client.app.state, "_test_routes_added", False):
        return

    @client.app.get("/__test_user_only__")
    async def user_only(user: User = Depends(require_user)):  # noqa: B008
        return {"id": user.id}

    @client.app.get("/__test_verified_only__")
    async def verified_only(user: User = Depends(require_verified_user)):  # noqa: B008
        return {"id": user.id}

    @client.app.get("/__test_admin_only__")
    async def admin_only(user: User = Depends(require_admin)):  # noqa: B008
        return {"id": user.id}

    client.app.state._test_routes_added = True


def test_correct_credentials_redirect_and_set_session_cookie(client):
    _make_user(client, email="user@example.test", password="s3cret-pw!")

    login_page = client.get("/login")
    csrf_token = _extract_csrf(login_page.text)

    response = client.post(
        "/login",
        data={"email": "user@example.test", "password": "s3cret-pw!", "csrf_token": csrf_token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/roster"
    assert "session" in response.cookies


def test_wrong_password_and_unknown_email_are_identical(client):
    _make_user(client, email="known@example.test", password="the-real-password")

    login_page = client.get("/login")
    csrf_token = _extract_csrf(login_page.text)

    wrong_password = client.post(
        "/login",
        data={"email": "known@example.test", "password": "not-it", "csrf_token": csrf_token},
    )
    unknown_email = client.post(
        "/login",
        data={"email": "nobody@example.test", "password": "not-it", "csrf_token": csrf_token},
    )

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.text == unknown_email.text


def test_post_without_valid_csrf_is_rejected(client):
    _make_user(client, email="user2@example.test", password="the-real-password")
    client.get("/login")  # sets the csrf seed cookie

    response = client.post(
        "/login",
        data={
            "email": "user2@example.test",
            "password": "the-real-password",
            "csrf_token": "not-the-right-token",
        },
    )

    assert response.status_code == 403


def test_logout_clears_cookie_and_protected_page_becomes_unreachable(client):
    _add_protected_routes(client)
    _make_user(client, email="user3@example.test", password="the-real-password")

    login_page = client.get("/login")
    csrf_token = _extract_csrf(login_page.text)
    client.post(
        "/login",
        data={"email": "user3@example.test", "password": "the-real-password", "csrf_token": csrf_token},
        follow_redirects=False,
    )

    protected = client.get("/__test_user_only__")
    assert protected.status_code == 200

    session_cookie = client.cookies.get("session")
    logout_csrf = generate_csrf_token(session_cookie)
    logout_response = client.post(
        "/logout", data={"csrf_token": logout_csrf}, follow_redirects=False
    )
    assert logout_response.status_code == 303

    protected_after = client.get("/__test_user_only__")
    assert protected_after.status_code == 401


def test_verified_user_reaches_verified_page_unverified_does_not(client):
    _add_protected_routes(client)
    _make_user(client, email="verified@example.test", password="the-real-password", verified=True)
    _make_user(client, email="unverified@example.test", password="the-real-password", verified=False)

    login_page = client.get("/login")
    csrf_token = _extract_csrf(login_page.text)
    client.post(
        "/login",
        data={"email": "verified@example.test", "password": "the-real-password", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert client.get("/__test_verified_only__").status_code == 200

    logout_csrf = generate_csrf_token(client.cookies.get("session"))
    client.post("/logout", data={"csrf_token": logout_csrf}, follow_redirects=False)

    login_page = client.get("/login")
    csrf_token = _extract_csrf(login_page.text)
    client.post(
        "/login",
        data={"email": "unverified@example.test", "password": "the-real-password", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert client.get("/__test_verified_only__").status_code == 403


def test_non_admin_gets_403_on_admin_page_even_when_verified(client):
    _add_protected_routes(client)
    _make_user(client, email="plain@example.test", password="the-real-password", verified=True, is_admin=False)

    login_page = client.get("/login")
    csrf_token = _extract_csrf(login_page.text)
    client.post(
        "/login",
        data={"email": "plain@example.test", "password": "the-real-password", "csrf_token": csrf_token},
        follow_redirects=False,
    )

    assert client.get("/__test_admin_only__").status_code == 403


def test_sixth_login_attempt_in_window_is_blocked_with_same_neutral_page(client):
    fake_time = {"now": 0.0}
    client.app.state.login_rate_limiter = RateLimiter(
        limit=5, window_seconds=900, clock=lambda: fake_time["now"]
    )
    _make_user(client, email="ratelimited@example.test", password="the-real-password")

    login_page = client.get("/login")
    csrf_token = _extract_csrf(login_page.text)

    wrong_password_response = None
    for _ in range(5):
        wrong_password_response = client.post(
            "/login",
            data={"email": "ratelimited@example.test", "password": "nope", "csrf_token": csrf_token},
            follow_redirects=False,
        )
        fake_time["now"] += 1

    # The sixth attempt uses the CORRECT password: only the limiter can refuse it.
    sixth = client.post(
        "/login",
        data={
            "email": "ratelimited@example.test",
            "password": "the-real-password",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert sixth.status_code == wrong_password_response.status_code == 401
    assert sixth.status_code != 429
    assert sixth.text == wrong_password_response.text
    assert "session" not in sixth.cookies

    # Past the window the same correct password succeeds, proving the block was the limiter.
    fake_time["now"] += 901
    after_window = client.post(
        "/login",
        data={
            "email": "ratelimited@example.test",
            "password": "the-real-password",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert after_window.status_code == 303


def test_completed_reset_invalidates_a_prior_session(client):
    _add_protected_routes(client)
    _make_user(client, email="resetme@example.test", password="old-password-1")

    login_page = client.get("/login")
    csrf_token = _extract_csrf(login_page.text)
    client.post(
        "/login",
        data={"email": "resetme@example.test", "password": "old-password-1", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    old_session_cookie = client.cookies.get("session")
    assert client.get("/__test_user_only__").status_code == 200

    reset_request_page = client.get("/reset")
    reset_csrf = _extract_csrf(reset_request_page.text)
    client.post("/reset", data={"email": "resetme@example.test", "csrf_token": reset_csrf})

    sent = client.app.state.email_sender.sent
    assert len(sent) == 1
    token = sent[-1]["body"].rsplit("/reset/", 1)[1]

    reset_form_page = client.get(f"/reset/{token}")
    assert reset_form_page.status_code == 200
    reset_form_csrf = _extract_csrf(reset_form_page.text)

    complete = client.post(
        f"/reset/{token}",
        data={"password": "brand-new-password-2", "csrf_token": reset_form_csrf},
        follow_redirects=False,
    )
    assert complete.status_code == 303

    # Reuse the *old* session cookie explicitly against the protected route.
    client.cookies.set("session", old_session_cookie)
    stale = client.get("/__test_user_only__")
    assert stale.status_code == 401


def test_verify_token_is_idempotent_and_never_500s(client):
    first = client.get("/verify/not-a-real-token")
    second = client.get("/verify/not-a-real-token")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.text == second.text


def test_verify_with_a_real_token_sets_email_verified_and_repeats_neutrally(client):
    from golf_league.services.auth import issue_token

    user_id = _make_user(
        client, email="toverify@example.test", password="the-real-password", verified=False
    )
    session = Session(bind=client.app.state.engine)
    try:
        raw_token = issue_token(session, user_id, "verify_email", 3600)
    finally:
        session.close()

    first = client.get(f"/verify/{raw_token}")
    assert first.status_code == 200

    session = Session(bind=client.app.state.engine)
    try:
        assert session.get(User, user_id).email_verified_at is not None
    finally:
        session.close()

    second = client.get(f"/verify/{raw_token}")
    assert second.status_code == 200
    assert second.text == first.text                      # consumed token is neutral, not a 500
    assert first.text == client.get("/verify/bogus").text  # and indistinguishable from unknown
    assert raw_token not in first.text                     # the token is never echoed back


def test_reset_form_and_submit_reject_an_unknown_token_with_404(client):
    assert client.get("/reset/unknown-token").status_code == 404
    client.get("/reset")  # sets the csrf seed cookie
    seed_page = client.get("/reset")
    csrf_token = _extract_csrf(seed_page.text)
    response = client.post(
        "/reset/unknown-token",
        data={"password": "a-long-enough-password", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 404


def test_short_password_re_renders_the_reset_form_with_422(client):
    from golf_league.services.auth import issue_token

    user_id = _make_user(
        client, email="shortpw@example.test", password="the-original-password"
    )
    session = Session(bind=client.app.state.engine)
    try:
        raw_token = issue_token(session, user_id, "reset_password", 3600)
        original_hash = session.get(User, user_id).password_hash
    finally:
        session.close()

    reset_form_page = client.get(f"/reset/{raw_token}")
    assert reset_form_page.status_code == 200
    csrf_token = _extract_csrf(reset_form_page.text)

    response = client.post(
        f"/reset/{raw_token}",
        data={"password": "short", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert "csrf_token" in response.text

    session = Session(bind=client.app.state.engine)
    try:
        assert session.get(User, user_id).password_hash == original_hash
    finally:
        session.close()


def test_fake_email_sender_records_only_on_a_matched_reset(client):
    _make_user(client, email="hasaccount@example.test", password="the-real-password")

    reset_request_page = client.get("/reset")
    reset_csrf = _extract_csrf(reset_request_page.text)

    matched = client.post(
        "/reset", data={"email": "hasaccount@example.test", "csrf_token": reset_csrf}
    )
    unmatched = client.post(
        "/reset", data={"email": "noaccount@example.test", "csrf_token": reset_csrf}
    )

    assert matched.status_code == unmatched.status_code == 200
    assert matched.text == unmatched.text
    assert len(client.app.state.email_sender.sent) == 1


def test_reset_commit_failure_rolls_back_password_and_token(client, monkeypatch):
    user_id = _make_user(
        client, email="atomic-reset@example.test", password="original-password-1"
    )
    with Session(bind=client.app.state.engine) as session:
        raw = issue_token(session, user_id, "reset_password", 3600)
        session_version = session.get(User, user_id).session_version

    page = client.get(f"/reset/{raw}")
    csrf = _extract_csrf(page.text)
    original_commit = Session.commit

    def fail_commit(_session):
        raise RuntimeError("synthetic database failure")

    monkeypatch.setattr(Session, "commit", fail_commit)
    response = client.post(
        f"/reset/{raw}",
        data={"password": "replacement-password-2", "csrf_token": csrf},
        follow_redirects=False,
    )
    monkeypatch.setattr(Session, "commit", original_commit)
    assert response.status_code == 404
    with Session(bind=client.app.state.engine) as session:
        user = session.get(User, user_id)
        token = session.scalar(select(UserToken).where(UserToken.purpose == "reset_password"))
        assert verify_password("original-password-1", user.password_hash)
        assert user.session_version == session_version
        assert token.consumed_at is None and token.revoked_at is None


def test_ordinary_reset_preserves_account_and_revokes_other_credentials(client):
    with Session(bind=client.app.state.engine) as session:
        from golf_league.models import Golfer, TeeSet

        golfer = Golfer(
            first_name="Reset", last_name="Player", email="preserve-reset@example.test",
            default_tee_set_id=session.scalar(select(TeeSet.id).limit(1)),
            handicap_source="self_reported", handicap_status="ok",
        )
        session.add(golfer)
        session.flush()
        user = User(
            username="preserve-username", email="preserve-reset@example.test",
            display_name="Preserve Name", password_hash=hash_password("before-reset-password"),
            email_verified_at=datetime(2020, 1, 1), is_admin=True, golfer_id=golfer.id,
        )
        session.add(user)
        session.flush()
        user_id, golfer_id = user.id, golfer.id
        reset_raw = issue_token(session, user.id, "reset_password", 3600, commit=False)
        sibling = issue_token(session, user.id, "verify_email", 3600, commit=False)
        invite = LeagueInviteLink(
            token_hash="a" * 64, label="Synthetic", created_by_user_id=user.id,
            created_at=datetime.now(UTC), expires_at=datetime.now(UTC) + timedelta(days=2),
        )
        session.add(invite)
        session.flush()
        child = GolferSetPasswordToken(
            golfer_id=golfer.id, invite_link_id=invite.id, email_snapshot=user.email,
            expected_user_id=user.id, token_digest="b" * 64,
            expires_at=datetime.now(UTC) + timedelta(hours=1), created_at=datetime.now(UTC),
        )
        session.add(child)
        session.commit()
        child_id = child.id

    page = client.get(f"/reset/{reset_raw}")
    response = client.post(
        f"/reset/{reset_raw}",
        data={"password": "after-reset-password", "csrf_token": _extract_csrf(page.text)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert "session" not in response.cookies
    with Session(bind=client.app.state.engine) as session:
        user = session.get(User, user_id)
        assert user.username == "preserve-username" and user.email == "preserve-reset@example.test"
        assert user.display_name == "Preserve Name" and user.is_admin and user.golfer_id == golfer_id
        assert verify_password("after-reset-password", user.password_hash)
        reset_token = session.scalar(select(UserToken).where(UserToken.token_digest == digest(reset_raw)))
        assert reset_token.consumed_at is not None
        other = session.scalar(select(UserToken).where(UserToken.token_digest == digest(sibling)))
        assert other.revoked_at is not None
        assert session.get(GolferSetPasswordToken, child_id).revoked_at is not None


def test_reset_csrf_and_short_password_leave_token_usable(client):
    user_id = _make_user(client, email="reset-errors@example.test", password="original-reset-password")
    with Session(bind=client.app.state.engine) as session:
        raw = issue_token(session, user_id, "reset_password", 3600)
    page = client.get(f"/reset/{raw}")
    missing_csrf = client.post(
        f"/reset/{raw}", data={"password": "replacement-reset-password"}, follow_redirects=False,
    )
    assert missing_csrf.status_code == 403
    short = client.post(
        f"/reset/{raw}", data={"password": "short", "csrf_token": _extract_csrf(page.text)},
        follow_redirects=False,
    )
    assert short.status_code == 422
    with Session(bind=client.app.state.engine) as session:
        token = session.scalar(select(UserToken).where(UserToken.user_id == user_id))
        assert token.consumed_at is None and token.revoked_at is None


# --- GL-45: absolute reset links, sent after the response ---------------------

SYNTHETIC_ORIGIN = "https://league.example.test"


class _RaisingSender:
    def __init__(self):
        self.attempts = 0

    def send(self, to, subject, body):
        self.attempts += 1
        raise OSError("simulated relay outage for reset@example.test")


def _use_origin(client):
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"external_base_url": SYNTHETIC_ORIGIN}
    )


def _request_reset(client, email, headers=None):
    page = client.get("/reset")
    return client.post("/reset", data={"email": email, "csrf_token": _extract_csrf(page.text)},
                       headers=headers or {})


def test_reset_email_link_uses_configured_origin_not_host_header(client):
    _use_origin(client)
    _make_user(client, email="reset@example.test", password="old-password-1")

    response = _request_reset(
        client, "reset@example.test",
        headers={"host": "attacker.example.test", "x-forwarded-host": "attacker.example.test",
                 "x-forwarded-proto": "http"},
    )

    assert response.status_code == 200
    (message,) = client.app.state.email_sender.sent
    assert f"{SYNTHETIC_ORIGIN}/reset/" in message["body"]
    assert "attacker" not in message["body"]
    token = message["body"].rsplit("/reset/", 1)[1]
    assert client.get(f"/reset/{token}").status_code == 200


def test_reset_send_failure_keeps_the_neutral_response_and_logs_fixed_text(client, caplog):
    _make_user(client, email="reset@example.test", password="old-password-1")
    succeeded = _request_reset(client, "reset@example.test")

    raising = _RaisingSender()
    client.app.state.email_sender = raising
    with caplog.at_level("WARNING"):
        failed = _request_reset(client, "reset@example.test")

    assert raising.attempts == 1
    assert (failed.status_code, failed.text) == (succeeded.status_code, succeeded.text)
    assert "password reset email send failed" in caplog.text
    assert "reset@example.test" not in caplog.text
    assert "simulated relay outage" not in caplog.text
    assert "/reset/" not in caplog.text


def test_reset_for_unknown_address_attaches_no_send(client):
    known_page = client.get("/reset")
    response = client.post("/reset", data={"email": "nobody@example.test",
                                           "csrf_token": _extract_csrf(known_page.text)})
    assert response.status_code == 200
    assert "If that address has an account" in response.text
    assert client.app.state.email_sender.sent == []
