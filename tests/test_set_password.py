import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from golf_league.domain.tokens import digest, generate_token
from golf_league.models import (
    Base,
    Golfer,
    GolferSetPasswordToken,
    LeagueInviteLink,
    User,
    UserToken,
)
from golf_league.services.auth import (
    complete_reset_token,
    hash_password,
    issue_token,
    verify_password,
)
from golf_league.services.invites import (
    admit_join_send,
    complete_set_password,
    create_invite,
)


def _csrf(html):
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def _credential(client):
    with Session(bind=client.app.state.engine) as session:
        admin = User(
            username="admin@example.test", email="admin@example.test", display_name="Admin",
            password_hash=hash_password("synthetic-password"), email_verified_at=datetime.now(UTC).replace(tzinfo=None),
            is_admin=True,
        )
        session.add(admin)
        session.flush()
        invite = create_invite(session, created_by_user_id=admin.id, label="Synthetic", now=datetime.now(UTC))
        session.commit()
        invite_raw = invite.raw_token
        course_tee = session.execute(select(Golfer.default_tee_set_id).where(Golfer.default_tee_set_id.is_not(None))).scalar_one_or_none()
        if course_tee is None:
            from golf_league.models import TeeSet
            course_tee = session.scalar(select(TeeSet.id).limit(1))
        session.add(Golfer(
            first_name="Test", last_name="Golfer", email="set.password@example.test",
            default_tee_set_id=course_tee, handicap_source="self_reported", handicap_status="ok",
        ))
        session.commit()
    page = client.get(f"/join/{invite_raw}")
    client.post(f"/join/{invite_raw}", data={
        "email": "set.password@example.test", "csrf_token": _csrf(page.text),
    })
    body = client.app.state.email_sender.sent[-1]["body"]
    return body.rsplit("/set-password/", 1)[1]


def _existing_credential(client, *, email="existing@example.test"):
    with Session(bind=client.app.state.engine) as session:
        from golf_league.models import TeeSet

        golfer = Golfer(
            first_name="Existing", last_name="Player", email=email,
            default_tee_set_id=session.scalar(select(TeeSet.id).limit(1)),
            handicap_source="self_reported", handicap_status="ok",
        )
        session.add(golfer)
        session.flush()
        user = User(
            username="preserved-username", email=email, display_name="Preserved Name",
            password_hash=hash_password("existing-password-1"),
            email_verified_at=datetime(2020, 1, 1), is_admin=True, golfer_id=golfer.id,
        )
        session.add(user)
        session.flush()
        invite = create_invite(session, created_by_user_id=user.id, label="Existing", now=datetime.now(UTC))
        session.commit()
        user_id = user.id
        golfer_id = golfer.id
        invite_raw = invite.raw_token
    page = client.get(f"/join/{invite_raw}")
    client.post(f"/join/{invite_raw}", data={"email": email, "csrf_token": _csrf(page.text)})
    body = client.app.state.email_sender.sent[-1]["body"]
    return body.rsplit("/set-password/", 1)[1], user_id, golfer_id


def _fill_user_count(session, target):
    count = session.scalar(select(func.count()).select_from(User)) or 0
    password_hash = hash_password("synthetic-unused-account-password")
    for number in range(count, target):
        email = f"capacity-{number}@example.test"
        session.add(User(
            username=email, email=email, display_name=f"Capacity {number}",
            password_hash=password_hash, is_admin=False,
        ))
    session.flush()


def _race_completion(factory, winner, shared_raw, reset_raw):
    barrier = threading.Barrier(3)
    lock_held = threading.Event()
    competing_write_started = threading.Event()
    outcomes = {}

    def complete(kind):
        barrier.wait()
        is_winner = kind == winner
        if not is_winner:
            lock_held.wait(timeout=5)
            competing_write_started.set()
        with factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            if is_winner:
                lock_held.set()
                competing_write_started.wait(timeout=5)
            if kind == "shared":
                result = complete_set_password(
                    session, raw_token=shared_raw,
                    password_hash=hash_password("shared-password-2"), now=datetime.now(UTC),
                )
            else:
                result = complete_reset_token(
                    session, raw_token=reset_raw,
                    password_hash=hash_password("reset-password-3"), now=datetime.now(UTC),
                )
            if result is not None:
                session.commit()
            else:
                session.rollback()
            outcomes[kind] = result is not None

    with ThreadPoolExecutor(max_workers=2) as pool:
        shared_job = pool.submit(complete, "shared")
        reset_job = pool.submit(complete, "reset")
        barrier.wait()
        shared_job.result(timeout=10)
        reset_job.result(timeout=10)
    return outcomes


def test_short_password_is_blank_validation_and_does_not_consume(client):
    raw = _credential(client)
    page = client.get(f"/set-password/{raw}")
    response = client.post(
        f"/set-password/{raw}", data={"password": "short", "csrf_token": _csrf(page.text)},
    )
    assert response.status_code == 422
    assert "short" not in response.text
    assert response.headers["cache-control"] == "no-store"
    with Session(bind=client.app.state.engine) as session:
        token = session.scalar(select(GolferSetPasswordToken))
        assert token.consumed_at is None
        assert session.scalar(select(User).where(User.email == "set.password@example.test")) is None


def test_bad_credential_is_404_even_with_short_password(client):
    page = client.get("/login")
    response = client.post(
        "/set-password/not-a-credential",
        data={"password": "x", "csrf_token": _csrf(page.text)},
    )
    assert response.status_code == 404
    assert response.headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize("drift", ["golfer_email", "golfer_inactive", "invite_revoked", "phone_challenge", "identity_link"])
def test_set_password_rejects_identity_or_invite_drift(client, drift):
    raw = _credential(client)
    with Session(bind=client.app.state.engine) as session:
        token = session.scalar(select(GolferSetPasswordToken))
        golfer = session.get(Golfer, token.golfer_id)
        invite = session.get(LeagueInviteLink, token.invite_link_id)
        if drift == "golfer_email":
            golfer.email = "changed@example.test"
        elif drift == "golfer_inactive":
            golfer.is_active = False
        elif drift == "invite_revoked":
            invite.revoked_at = datetime.now(UTC)
        elif drift == "phone_challenge":
            invite.require_phone_last_four = True
        else:
            user = User(
                username="drift@example.test", email="drift@example.test", display_name="Drift",
                password_hash=hash_password("synthetic-drift-password"), golfer_id=golfer.id,
            )
            session.add(user)
        session.commit()
    form = client.get(f"/set-password/{raw}")
    assert form.status_code == 404


@pytest.mark.parametrize("state", ["expired", "revoked", "consumed"])
def test_set_password_rejects_nonlive_child_token(client, state):
    raw = _credential(client)
    with Session(bind=client.app.state.engine) as session:
        token = session.scalar(select(GolferSetPasswordToken))
        now = datetime.now(UTC)
        if state == "expired":
            token.expires_at = now - timedelta(seconds=1)
        elif state == "revoked":
            token.revoked_at = now
        else:
            token.consumed_at = now
        session.commit()
    assert client.get(f"/set-password/{raw}").status_code == 404


@pytest.mark.parametrize("csrf_state", ["missing", "malformed", "foreign"])
def test_set_password_requires_csrf_and_blank_password_does_not_consume(client, csrf_state):
    from golf_league.security import generate_csrf_token

    raw = _credential(client)
    form = client.get(f"/set-password/{raw}")
    bad_data = {"password": "long-enough-password"}
    if csrf_state == "malformed":
        bad_data["csrf_token"] = "malformed"
    elif csrf_state == "foreign":
        bad_data["csrf_token"] = generate_csrf_token("foreign-seed-value")
    no_csrf = client.post(f"/set-password/{raw}", data=bad_data)
    assert no_csrf.status_code == 403
    assert no_csrf.headers["cache-control"] == "no-store"
    assert no_csrf.headers["referrer-policy"] == "no-referrer"
    blank = client.post(
        f"/set-password/{raw}", data={"password": "", "csrf_token": _csrf(form.text)},
    )
    assert blank.status_code == 422
    assert blank.headers["cache-control"] == "no-store"
    with Session(bind=client.app.state.engine) as session:
        assert session.scalar(select(GolferSetPasswordToken)).consumed_at is None
        assert session.scalar(select(User).where(User.email == "set.password@example.test")) is None


@pytest.mark.parametrize("failing_method", ["flush", "commit"])
def test_set_password_write_failure_rolls_back_user_and_child_token(client, monkeypatch, failing_method, caplog):
    raw = _credential(client)
    form = client.get(f"/set-password/{raw}")
    original = getattr(Session, failing_method)

    def fail_write(_session, *args, **kwargs):
        raise RuntimeError("synthetic canary@example.test credential-secret database failure")

    monkeypatch.setattr(Session, failing_method, fail_write)
    response = client.post(
        f"/set-password/{raw}",
        data={"password": "rollback-synthetic-password", "csrf_token": _csrf(form.text)},
        follow_redirects=False,
    )
    monkeypatch.setattr(Session, failing_method, original)
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert response.cookies.get("session") is None
    assert "shared password completion failed" in caplog.text
    assert "canary@example.test" not in caplog.text and "credential-secret" not in caplog.text
    with Session(bind=client.app.state.engine) as session:
        assert session.scalar(select(User).where(User.email == "set.password@example.test")) is None
        token = session.scalar(select(GolferSetPasswordToken))
        assert token.consumed_at is None and token.revoked_at is None


def test_set_password_accepts_only_once_and_invalidates_old_session(client):
    raw = _credential(client)
    form = client.get(f"/set-password/{raw}")
    csrf = _csrf(form.text)
    # New account flow establishes a valid session, and the account is verified.
    response = client.post(f"/set-password/{raw}", data={
        "password": "new-synthetic-password", "csrf_token": csrf,
    }, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["cache-control"] == "no-store"
    assert response.cookies.get("session")
    assert client.get("/").status_code == 200
    second = client.post(f"/set-password/{raw}", data={
        "password": "another-synthetic-password", "csrf_token": csrf,
    }, follow_redirects=False)
    assert second.status_code == 404
    with Session(bind=client.app.state.engine) as session:
        user = session.scalar(select(User).where(User.email == "set.password@example.test"))
        assert user.email_verified_at is not None
        assert verify_password("new-synthetic-password", user.password_hash)


def test_new_account_completes_from_149_to_150_accounts(client):
    raw = _credential(client)
    with Session(bind=client.app.state.engine) as session:
        _fill_user_count(session, 149)
        session.commit()
    form = client.get(f"/set-password/{raw}")
    response = client.post(
        f"/set-password/{raw}",
        data={"password": "capacity-success-password", "csrf_token": _csrf(form.text)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(bind=client.app.state.engine) as session:
        assert session.scalar(select(func.count()).select_from(User)) == 150
        assert session.scalar(select(User).where(User.email == "set.password@example.test")) is not None


def test_new_account_capacity_refusal_is_atomic_and_keeps_credential(client):
    raw = _credential(client)
    with Session(bind=client.app.state.engine) as session:
        _fill_user_count(session, 150)
        session.commit()
    form = client.get(f"/set-password/{raw}")
    response = client.post(
        f"/set-password/{raw}",
        data={"password": "capacity-refused-password", "csrf_token": _csrf(form.text)},
        follow_redirects=False,
    )
    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.cookies.get("session") is None
    with Session(bind=client.app.state.engine) as session:
        assert session.scalar(select(func.count()).select_from(User)) == 150
        token = session.scalar(select(GolferSetPasswordToken))
        assert token.consumed_at is None and token.revoked_at is None
        assert session.scalar(select(User).where(User.email == "set.password@example.test")) is None


def test_existing_account_at_capacity_preserves_identity_and_rotates_session(client):
    from fastapi import Depends

    from golf_league.security import require_user

    raw, user_id, golfer_id = _existing_credential(client)
    with Session(bind=client.app.state.engine) as session:
        _fill_user_count(session, 150)
        before = session.get(User, user_id)
        original_version = before.session_version
        original_verified_at = before.email_verified_at
        session.commit()

    @client.app.get("/__gl43_session_probe")
    async def session_probe(user: User = Depends(require_user)):  # noqa: B008
        return {"id": user.id}

    login_page = client.get("/login")
    login = client.post(
        "/login",
        data={"email": "existing@example.test", "password": "existing-password-1", "csrf_token": _csrf(login_page.text)},
        follow_redirects=False,
    )
    old_cookie = login.cookies["session"]
    form = client.get(f"/set-password/{raw}")
    response = client.post(
        f"/set-password/{raw}",
        data={"password": "existing-password-2", "csrf_token": _csrf(form.text)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    fresh_cookie = response.cookies["session"]
    assert client.get("/__gl43_session_probe", cookies={"session": old_cookie}).status_code == 401
    fresh = client.get("/__gl43_session_probe", cookies={"session": fresh_cookie})
    assert fresh.status_code == 200 and fresh.json() == {"id": user_id}
    with Session(bind=client.app.state.engine) as session:
        assert session.scalar(select(func.count()).select_from(User)) == 150
        user = session.get(User, user_id)
        assert user.username == "preserved-username"
        assert user.email == "existing@example.test"
        assert user.display_name == "Preserved Name"
        assert user.is_admin is True
        assert user.golfer_id == golfer_id
        assert user.session_version == original_version + 1
        assert user.email_verified_at is not None and user.email_verified_at != original_verified_at


def test_existing_completion_revokes_all_outstanding_siblings(client):
    raw, user_id, golfer_id = _existing_credential(client, email="siblings@example.test")
    with Session(bind=client.app.state.engine) as session:
        primary = session.scalar(select(GolferSetPasswordToken))
        parent_id = primary.invite_link_id
        now = datetime.now(UTC)
        sibling = GolferSetPasswordToken(
            golfer_id=golfer_id, invite_link_id=parent_id, email_snapshot="siblings@example.test",
            expected_user_id=user_id, token_digest=digest(generate_token()),
            expires_at=now + timedelta(minutes=45), created_at=now,
        )
        session.add(sibling)
        live_user_tokens = []
        for purpose in ("reset_password", "set_password", "verify_email"):
            token = UserToken(
                user_id=user_id, token_digest=digest(generate_token()), purpose=purpose,
                expires_at=(now + timedelta(hours=1)).replace(tzinfo=None),
            )
            live_user_tokens.append(token)
            session.add(token)
        consumed = UserToken(
            user_id=user_id, token_digest=digest(generate_token()), purpose="verify_email",
            expires_at=(now + timedelta(hours=1)).replace(tzinfo=None), consumed_at=now.replace(tzinfo=None),
        )
        session.add(consumed)
        session.commit()
        sibling_id = sibling.id
        consumed_at = consumed.consumed_at
        user_token_ids = [item.id for item in live_user_tokens]
        consumed_id = consumed.id

    form = client.get(f"/set-password/{raw}")
    response = client.post(
        f"/set-password/{raw}",
        data={"password": "sibling-revocation-password", "csrf_token": _csrf(form.text)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(bind=client.app.state.engine) as session:
        primary = session.scalar(select(GolferSetPasswordToken).where(GolferSetPasswordToken.token_digest == digest(raw)))
        sibling = session.get(GolferSetPasswordToken, sibling_id)
        assert primary.consumed_at is not None and primary.revoked_at is None
        assert sibling.revoked_at is not None and sibling.consumed_at is None
        for token_id in user_token_ids:
            assert session.get(UserToken, token_id).revoked_at is not None
        consumed = session.get(UserToken, consumed_id)
        assert consumed.consumed_at == consumed_at and consumed.revoked_at is None


def test_reset_and_shared_completion_serialize_in_either_order(tmp_path):
    from sqlalchemy import create_engine

    for winner in ("shared", "reset"):
        db_path = tmp_path / f"{winner}.db"
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, autoflush=False)
        with factory() as session:
            golfer = Golfer(
                first_name="Atomic", last_name="Player", email=f"{winner}@example.test",
                handicap_source="self_reported", handicap_status="ok",
            )
            session.add(golfer)
            session.flush()
            user = User(
                username=golfer.email, email=golfer.email, display_name="Atomic Player",
                password_hash=hash_password("initial-password-1"),
                email_verified_at=datetime.now(UTC).replace(tzinfo=None), is_admin=False,
                golfer_id=golfer.id,
            )
            session.add(user)
            session.flush()
            invite = create_invite(session, created_by_user_id=user.id, label="Synthetic", now=datetime.now(UTC))
            parent = invite.raw_token
            reset_raw = issue_token(session, user.id, "reset_password", 3600, commit=False)
            session.commit()
        with factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            delivery = admit_join_send(
                session,
                raw_invite_token=parent,
                email=f"{winner}@example.test",
                client_ip="127.0.0.1",
                secret="synthetic-quota-secret",
                now=datetime.now(UTC),
            )
            assert delivery is not None
            shared_raw = delivery.raw_token
            session.commit()

        outcomes = _race_completion(factory, winner, shared_raw, reset_raw)
        assert outcomes == {"shared": winner == "shared", "reset": winner == "reset"}
        with factory() as session:
            user = session.scalar(select(User).where(User.email == f"{winner}@example.test"))
            expected = "shared-password-2" if winner == "shared" else "reset-password-3"
            assert verify_password(expected, user.password_hash)
            assert not verify_password("initial-password-1", user.password_hash)
        engine.dispose()
