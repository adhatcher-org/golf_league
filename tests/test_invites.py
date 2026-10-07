"""Synthetic invitation lifecycle, administration and identity policy coverage."""

import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from golf_league.domain.tokens import digest
from golf_league.models import (
    Golfer,
    GolferSetPasswordToken,
    InviteRateWindow,
    LeagueInviteLink,
    User,
    UserToken,
)
from golf_league.routers.admin_invites import RECEIPT_COOKIE, InviteReceiptStore
from golf_league.security import generate_csrf_token
from golf_league.services.auth import create_session_cookie, issue_token
from golf_league.services.invites import (
    InviteConflict,
    InviteNotFound,
    InviteValidationError,
    admit_join_send,
    create_invite,
    get_valid_invite,
    peek_set_password_token,
    record_send_failure,
    record_send_success,
    revoke_invite,
    rotate_invite,
)

NOW = datetime(2026, 10, 6, tzinfo=UTC)
SECRET = "synthetic-invite-secret"


def seed_environment(engine, count=120):
    with Session(engine) as session:
        admin = User(username="owner@example.test", email="owner@example.test", display_name="Owner", password_hash="synthetic", is_admin=True)
        session.add(admin)
        golfers = [Golfer(first_name="Synthetic", last_name=str(i), email=f"golfer{i}@example.test", handicap_source="self_reported", handicap_status="needs_entry") for i in range(count)]
        session.add_all(golfers)
        session.flush()
        created = create_invite(session, created_by_user_id=admin.id, label=" League ", now=NOW)
        result = admin.id, [g.id for g in golfers], created
        session.commit()
        return result


def admission(engine, created, email="golfer0@example.test", ip="192.0.2.1", now=NOW, commit=True):
    with Session(engine) as session:
        session.execute(text("BEGIN IMMEDIATE"))
        result = admit_join_send(session, raw_invite_token=created.raw_token, email=email, client_ip=ip, secret=SECRET, now=now)
        if commit:
            session.commit()
        else:
            session.rollback()
        return result


@pytest.fixture
def invite_env(engine):
    return engine, *seed_environment(engine)


def test_creation_digest_default_explicit_and_uniqueness(invite_env):
    engine, admin, _, first = invite_env
    with Session(engine) as session:
        row = session.get(LeagueInviteLink, first.invite_id)
        assert row.token_hash == digest(first.raw_token) and row.label == "League"
        assert row.expires_at == NOW + timedelta(days=30)
        assert row.send_count == row.failed_send_count == row.suppressed_count == 0
        assert not row.require_phone_last_four
        assert first.raw_token not in repr(row.__dict__) and first.raw_token not in repr(first)
        second = create_invite(session, created_by_user_id=admin, label="Explicit", expiry_days="366", now=NOW)
        assert second.raw_token != first.raw_token
        assert session.get(LeagueInviteLink, second.invite_id).expires_at == NOW + timedelta(days=366)
        assert get_valid_invite(session, raw_token=first.raw_token, now=NOW + timedelta(days=30)) is None
        assert get_valid_invite(session, raw_token="unknown", now=NOW) is None
        assert get_valid_invite(session, raw_token=first.raw_token, now=NOW)
        session.rollback()
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(LeagueInviteLink)) == 1


@pytest.mark.parametrize("days", ["0", "-1", "1.5", str(2**63), "9" * 5000, "²", "9999999"])
def test_invalid_expiry_never_converts_unsafe_input(invite_env, days):
    engine, admin, _, _ = invite_env
    with Session(engine) as session:
        with pytest.raises(InviteValidationError) as error:
            create_invite(session, created_by_user_id=admin, label="Valid", expiry_days=days, now=NOW)
        assert "expiry_days" in error.value.errors


def test_naive_now_rejected(invite_env):
    engine, admin, _, _ = invite_env
    with Session(engine) as session, pytest.raises(ValueError, match="timezone-aware"):
        create_invite(session, created_by_user_id=admin, label="Valid", now=NOW.replace(tzinfo=None))


def test_rotation_revoke_and_rollback(invite_env):
    engine, admin, _, created = invite_env
    delivery = admission(engine, created)
    with Session(engine) as session:
        session.execute(text("BEGIN IMMEDIATE"))
        replacement = rotate_invite(session, invite_id=created.invite_id, created_by_user_id=admin, now=NOW)
        assert session.get(LeagueInviteLink, created.invite_id).revoked_at == NOW
        assert session.get(GolferSetPasswordToken, delivery.token_id).revoked_at == NOW
        session.rollback()
    with Session(engine) as session:
        assert session.get(LeagueInviteLink, replacement.invite_id) is None
        assert peek_set_password_token(session, raw_token=delivery.raw_token, now=NOW)
        session.rollback()
        session.execute(text("BEGIN IMMEDIATE"))
        replacement = rotate_invite(session, invite_id=created.invite_id, created_by_user_id=admin, now=NOW + timedelta(days=1))
        session.commit()
        new = session.get(LeagueInviteLink, replacement.invite_id)
        assert new.label == "League" and new.expires_at == NOW + timedelta(days=31)
        assert new.send_count == new.suppressed_count == new.failed_send_count == 0
        assert not new.require_phone_last_four
        assert peek_set_password_token(session, raw_token=delivery.raw_token, now=NOW) is None
        assert revoke_invite(session, invite_id=replacement.invite_id, now=NOW)
        assert revoke_invite(session, invite_id=replacement.invite_id, now=NOW + timedelta(seconds=1))
        assert not revoke_invite(session, invite_id=99999, now=NOW)
        with pytest.raises(InviteConflict):
            rotate_invite(session, invite_id=replacement.invite_id, created_by_user_id=admin, now=NOW)
        with pytest.raises(InviteNotFound):
            rotate_invite(session, invite_id=99999, created_by_user_id=admin, now=NOW)
        with pytest.raises(InviteConflict):
            rotate_invite(session, invite_id=replacement.invite_id, created_by_user_id=admin, now=NOW + timedelta(days=31))


def test_issue_peek_prior_revocation_and_no_identity_creation(invite_env):
    engine, admin, golfers, created = invite_env
    with Session(engine) as session:
        second = create_invite(session, created_by_user_id=admin, label="Second", now=NOW)
        ordinary = issue_token(session, admin, "reset_password", 1000, commit=False)
        session.commit()
    first = admission(engine, created, email=" GOLFER0@EXAMPLE.TEST ")
    with Session(engine) as session:
        assert first.recipient_email == "golfer0@example.test"
        assert first.raw_token not in repr(first) and first.recipient_email not in repr(first)
        row = session.get(GolferSetPasswordToken, first.token_id)
        assert row.token_digest == digest(first.raw_token)
        assert row.expires_at == NOW + timedelta(minutes=45)
        subject = peek_set_password_token(session, raw_token=first.raw_token, now=NOW)
        assert subject.golfer_id == golfers[0] and subject.expected_user_id is None
        assert not session.dirty and row.consumed_at is None
        assert peek_set_password_token(session, raw_token=ordinary, now=NOW) is None
        assert peek_set_password_token(session, raw_token=first.raw_token, now=NOW + timedelta(minutes=45)) is None
    last = admission(engine, second)
    with Session(engine) as session:
        assert peek_set_password_token(session, raw_token=first.raw_token, now=NOW) is None
        assert peek_set_password_token(session, raw_token=last.raw_token, now=NOW)
        assert session.scalar(select(func.count()).select_from(User)) == 1
        assert session.scalar(select(func.count()).select_from(Golfer)) == 120
        assert session.scalar(select(UserToken.revoked_at).where(UserToken.token_digest == digest(ordinary))) is None


@pytest.mark.parametrize("drift", ["email", "inactive", "new_link", "email_collision", "parent_expiry", "consumed"])
def test_peek_rejects_subject_and_parent_drift(invite_env, drift):
    engine, _, golfers, created = invite_env
    delivery = admission(engine, created)
    with Session(engine) as session:
        golfer = session.get(Golfer, golfers[0])
        if drift == "email":
            golfer.email = "changed@example.test"
        elif drift == "inactive":
            golfer.is_active = False
        elif drift in ("new_link", "email_collision"):
            session.add(User(username=golfer.email, email=golfer.email, display_name="New", password_hash="synthetic", golfer_id=golfer.id if drift == "new_link" else None))
        elif drift == "parent_expiry":
            session.get(LeagueInviteLink, created.invite_id).expires_at = NOW
        else:
            session.get(GolferSetPasswordToken, delivery.token_id).consumed_at = NOW
        session.commit()
        assert peek_set_password_token(session, raw_token=delivery.raw_token, now=NOW) is None


@pytest.mark.parametrize("drift", ["email", "unlink", "foreign_link", "delete"])
def test_existing_user_subject_must_still_match(invite_env, drift):
    engine, _, golfers, created = invite_env
    with Session(engine) as session:
        user = User(username="golfer0@example.test", email="golfer0@example.test", display_name="Existing", password_hash="synthetic", golfer_id=golfers[0])
        session.add(user)
        session.commit()
        user_id = user.id
    delivery = admission(engine, created)
    with Session(engine) as session:
        assert peek_set_password_token(session, raw_token=delivery.raw_token, now=NOW).expected_user_id == user_id
        user = session.get(User, user_id)
        if drift == "email":
            user.email = "changed@example.test"
        elif drift == "unlink":
            user.golfer_id = None
        elif drift == "foreign_link":
            user.golfer_id = golfers[1]
        else:
            # Preserve the FK but change expected identity to a now-missing ID
            # is impossible with FKs; simulate stale expected account by unlinking.
            user.golfer_id = None
            user.email = "other@example.test"
        session.commit()
        assert peek_set_password_token(session, raw_token=delivery.raw_token, now=NOW) is None


@pytest.mark.parametrize("refusal", ["inactive", "no_email", "unknown", "malformed", "phone", "unlinked", "foreign", "linked_different", "dual_conflict", "cap"])
def test_issue_refusals_do_not_reserve_or_create(invite_env, refusal):
    engine, _, golfers, created = invite_env
    email = "golfer0@example.test"
    with Session(engine) as session:
        golfer = session.get(Golfer, golfers[0])
        if refusal == "inactive":
            golfer.is_active = False
        elif refusal == "no_email":
            golfer.email = None
        elif refusal == "unknown":
            email = "unknown@example.test"
        elif refusal == "malformed":
            email = "invalid"
        elif refusal == "phone":
            session.get(LeagueInviteLink, created.invite_id).require_phone_last_four = True
        elif refusal in ("unlinked", "foreign", "linked_different", "dual_conflict"):
            user_email = "different@example.test" if refusal in ("linked_different", "dual_conflict") else email
            link = None if refusal == "unlinked" else golfers[1] if refusal == "foreign" else golfers[0]
            session.add(User(username=user_email, email=user_email, display_name="Conflict", password_hash="synthetic", golfer_id=link))
            if refusal == "dual_conflict":
                session.add(User(username=email, email=email, display_name="Other", password_hash="synthetic"))
        else:
            session.add_all([User(username=f"cap{i}@example.test", email=f"cap{i}@example.test", display_name="Cap", password_hash="synthetic") for i in range(149)])
        session.commit()
        counts = [session.scalar(select(func.count()).select_from(model)) for model in (User, Golfer)]
    assert admission(engine, created, email=email) is None
    with Session(engine) as session:
        assert [session.scalar(select(func.count()).select_from(model)) for model in (User, Golfer)] == counts
        assert session.scalar(select(func.count()).select_from(InviteRateWindow)) == 0
        assert session.scalar(select(func.count()).select_from(GolferSetPasswordToken)) == 0
        assert session.get(LeagueInviteLink, created.invite_id).suppressed_count == 0


def test_existing_account_can_issue_at_cap(invite_env):
    engine, _, golfers, created = invite_env
    with Session(engine) as session:
        session.add_all([User(username=f"cap{i}@example.test", email=f"cap{i}@example.test", display_name="Cap", password_hash="synthetic") for i in range(148)])
        user = User(username="golfer0@example.test", email="golfer0@example.test", display_name="Existing", password_hash="synthetic", golfer_id=golfers[0])
        session.add(user)
        session.commit()
        expected = user.id
    delivery = admission(engine, created)
    with Session(engine) as session:
        assert peek_set_password_token(session, raw_token=delivery.raw_token, now=NOW).expected_user_id == expected
        assert session.scalar(select(func.count()).select_from(User)) == 150


def test_send_callbacks_increment_and_failure_revokes_without_refund(invite_env):
    engine, _, _, created = invite_env
    delivery = admission(engine, created)
    with Session(engine) as session:
        session.execute(text("BEGIN IMMEDIATE"))
        record_send_success(session, token_id=delivery.token_id, now=NOW)
        session.commit()
        row = session.get(LeagueInviteLink, created.invite_id)
        assert row.send_count == 1 and row.last_sent_at == NOW
        session.rollback()
        session.execute(text("BEGIN IMMEDIATE"))
        record_send_failure(session, token_id=delivery.token_id, now=NOW)
        session.commit()
        row = session.get(LeagueInviteLink, created.invite_id)
        assert row.failed_send_count == 1 and row.suppressed_count == 0
        assert peek_set_password_token(session, raw_token=delivery.raw_token, now=NOW) is None
        assert {r.count for r in session.scalars(select(InviteRateWindow))} == {1}
        record_send_failure(session, token_id=999999, now=NOW)
        record_send_success(session, token_id=999999, now=NOW)
        assert row.send_count == row.failed_send_count == 1


def csrf(client):
    return generate_csrf_token(client.cookies.get("session"))


def create_admin_invite(client, **fields):
    return client.post("/admin/invites/new", data={"label": "Test invite", "csrf_token": csrf(client), **fields}, follow_redirects=False)


def test_admin_receipt_once_privacy_and_host_spoof(admin_client, caplog):
    admin_client.base_url = "https://testserver"
    response = admin_client.post("/admin/invites/new", data={"label": "Test invite", "csrf_token": csrf(admin_client)}, headers={"Host": "spoof.example.test", "X-Forwarded-Host": "evil.example.test"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/admin/invites/created"
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Secure" in cookie
    nonce = admin_client.cookies.get(RECEIPT_COOKIE)
    result = admin_client.get("/admin/invites/created")
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store" and result.headers["referrer-policy"] == "no-referrer"
    url = re.search(r'value="(https://golfleague.aaronhatcher.com/join/[^"]+)"', result.text).group(1)
    raw = url.rsplit("/", 1)[1]
    assert raw not in cookie and url not in cookie and raw not in response.headers["location"]
    listing = admin_client.get("/admin/invites")
    assert url not in listing.text and raw not in listing.text
    assert "Successful handoffs" in listing.text and "/admin/invites" in listing.text
    with Session(admin_client.app.state.engine) as session:
        row = session.scalar(select(LeagueInviteLink))
        assert row.token_hash == digest(raw) and raw not in repr(row.__dict__)
    assert raw not in caplog.text and url not in caplog.text
    assert admin_client.get("/admin/invites/created").status_code == 404
    admin_client.cookies.set(RECEIPT_COOKIE, nonce, path="/admin/invites/created")
    assert admin_client.get("/admin/invites/created").status_code == 404


def test_receipt_foreign_admin_session_and_exact_expiry():
    clock = [10.0]
    store = InviteReceiptStore(clock=lambda: clock[0])
    nonce = store.put(admin_id=1, session_cookie="first", url="https://example.test/join/synthetic")
    assert store.pop(nonce=nonce, admin_id=2, session_cookie="first") is None
    assert store.pop(nonce=nonce, admin_id=1, session_cookie="second") is None
    assert store.pop(nonce=nonce, admin_id=1, session_cookie="first")
    assert store.pop(nonce=nonce, admin_id=1, session_cookie="first") is None
    nonce = store.put(admin_id=1, session_cookie="first", url="https://example.test/join/synthetic")
    clock[0] = 310
    assert store.pop(nonce=nonce, admin_id=1, session_cookie="first") is None
    store.put(admin_id=1, session_cookie="first", url="https://example.test/join/synthetic")
    clock[0] = 611
    store.put(admin_id=1, session_cookie="first", url="https://example.test/join/other")
    assert len(store._receipts) == 1


def test_receipt_foreign_session_http_and_expiry(admin_client):
    admin_client.base_url = "https://testserver"
    create_admin_invite(admin_client)
    original = admin_client.cookies.get("session")
    with Session(admin_client.app.state.engine) as session:
        admin_id = session.scalar(select(User.id).where(User.is_admin.is_(True)))
        user = session.get(User, admin_id)
        user.session_version += 1
        session.commit()
        changed = create_session_cookie(admin_id, user.session_version, admin_client.app.state.settings.session_secret)
    admin_client.cookies.set("session", changed, domain="testserver.local", path="/")
    assert admin_client.get("/admin/invites/created").status_code == 404
    assert original != changed
    clock = [0]
    admin_client.app.state.invite_receipts = InviteReceiptStore(clock=lambda: clock[0])
    create_admin_invite(admin_client)
    clock[0] = 300
    assert admin_client.get("/admin/invites/created").status_code == 404


@pytest.mark.parametrize("label,days", [("", ""), (" " * 3, "30"), ("x" * 121, "30"), ("Secret submitted", "0"), ("Secret submitted", "bad"), ("Secret submitted", str(2**63)), ("Secret submitted", "9" * 5000), ("Secret submitted", "²")])
def test_admin_validation_html_blank_values(admin_client, label, days):
    result = create_admin_invite(admin_client, label=label, expiry_days=days)
    assert result.status_code == 422 and "text/html" in result.headers["content-type"]
    assert 'name="label" value=""' in result.text
    assert 'name="expiry_days" inputmode="numeric" value=""' in result.text
    assert "Secret submitted" not in result.text
    with Session(admin_client.app.state.engine) as session:
        assert session.scalar(select(func.count()).select_from(LeagueInviteLink)) == 0


def test_admin_new_default_and_explicit_366(admin_client):
    assert 'value="30"' in admin_client.get("/admin/invites/new").text
    assert create_admin_invite(admin_client, expiry_days="366").status_code == 303


@pytest.mark.parametrize("action", ["new", "1/revoke", "1/rotate"])
@pytest.mark.parametrize("submitted", [None, "foreign"])
def test_admin_missing_or_foreign_csrf(admin_client, action, submitted):
    data = {"label": "test"}
    if submitted is not None:
        data["csrf_token"] = submitted
    assert admin_client.post(f"/admin/invites/{action}", data=data).status_code == 403


@pytest.mark.parametrize("path", ["/admin/invites", "/admin/invites/new", "/admin/invites/created", "/admin/invites/new:post", "/admin/invites/1/revoke:post", "/admin/invites/1/rotate:post"])
@pytest.mark.parametrize("persona,status", [("anonymous", 401), ("nonadmin", 403), ("unverifiedadmin", 403)])
def test_admin_auth_ladder(client, path, persona, status):
    if persona != "anonymous":
        with Session(client.app.state.engine) as session:
            user = User(username="viewer@example.test", email="viewer@example.test", display_name="Viewer", password_hash="synthetic", is_admin=persona == "unverifiedadmin", email_verified_at=NOW.replace(tzinfo=None) if persona == "nonadmin" else None)
            session.add(user)
            session.commit()
            cookie = create_session_cookie(user.id, user.session_version, client.app.state.settings.session_secret)
        client.cookies.set("session", cookie)
    if path.endswith(":post"):
        response = client.post(path[:-5], data={})
    else:
        response = client.get(path)
    assert response.status_code == status


@pytest.mark.parametrize("bad_id", ["0", str(2**63), "9" * 5000, "²", "bad", "-1", "99999"])
@pytest.mark.parametrize("action", ["rotate", "revoke"])
def test_admin_malformed_missing_ids_404(admin_client, bad_id, action):
    assert admin_client.post(f"/admin/invites/{bad_id}/{action}", data={"csrf_token": csrf(admin_client)}).status_code == 404


def test_admin_revoke_rotate_codes_and_statuses(admin_client):
    create_admin_invite(admin_client)
    with Session(admin_client.app.state.engine) as session:
        invite_id = session.scalar(select(LeagueInviteLink.id))
    result = admin_client.post(f"/admin/invites/{invite_id}/rotate", data={"csrf_token": csrf(admin_client)}, follow_redirects=False)
    assert result.status_code == 303 and result.headers["location"] == "/admin/invites/created"
    assert admin_client.post(f"/admin/invites/{invite_id}/rotate", data={"csrf_token": csrf(admin_client)}).status_code == 409
    for _ in range(2):
        assert admin_client.post(f"/admin/invites/{invite_id}/revoke", data={"csrf_token": csrf(admin_client)}, follow_redirects=False).status_code == 303
    with Session(admin_client.app.state.engine) as session:
        live = session.scalar(select(LeagueInviteLink).where(LeagueInviteLink.revoked_at.is_(None)))
        live.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
        expired_id = live.id
    assert admin_client.post(f"/admin/invites/{expired_id}/rotate", data={"csrf_token": csrf(admin_client)}).status_code == 409
    listing = admin_client.get("/admin/invites").text
    assert "revoked" in listing and "expired" in listing
