"""GL-13 roster-restricted registration through the real HTTP routes."""

from concurrent.futures import ThreadPoolExecutor

from conftest import _extract_csrf
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from golf_league.domain.rate_limit import RateLimiter
from golf_league.models import Golfer, TeeSet, User, UserToken
from golf_league.services.auth import verify_password
from golf_league.services.identity import register_roster_user

NEUTRAL = "If that address is on the league roster"


def _add_golfer(client, *, email="player@example.test", active=True):
    session = Session(bind=client.app.state.engine)
    try:
        tee = session.execute(select(TeeSet).where(TeeSet.name == "Deer")).scalar_one()
        golfer = Golfer(
            first_name="Player", last_name="Example", email=email, phone="555-0100",
            default_tee_set_id=tee.id, handicap_strokes=12, handicap_source="self_reported",
            handicap_status="ok", notes="private note", is_active=active,
        )
        session.add(golfer)
        session.commit()
        session.refresh(golfer)
        return golfer.id
    finally:
        session.close()


def _add_users(client, count):
    """Fill the account table cheaply with synthetic, already-verified users."""
    session = Session(bind=client.app.state.engine)
    try:
        password_hash = "not-used-by-this-test"
        session.add_all(
            [
                User(
                    username=f"existing-{number}@example.test",
                    email=f"existing-{number}@example.test",
                    display_name="Existing User",
                    password_hash=password_hash,
                )
                for number in range(count)
            ]
        )
        session.commit()
    finally:
        session.close()


def _submit(client, email, password="a-real-password-1"):
    page = client.get("/register")
    return client.post(
        "/register", data={"email": email, "password": password, "csrf_token": _extract_csrf(page.text)}
    )


def test_registered_roster_email_creates_one_unverified_link_and_verifies(client):
    golfer_id = _add_golfer(client)
    response = _submit(client, " PLAYER@EXAMPLE.TEST ")
    assert response.status_code == 200
    assert NEUTRAL in response.text
    sent = client.app.state.email_sender.sent
    assert len(sent) == 1
    token = sent[0]["body"].rsplit("/verify/", 1)[1]

    session = Session(bind=client.app.state.engine)
    try:
        user = session.execute(select(User).where(User.email == "player@example.test")).scalar_one()
        assert user.golfer_id == golfer_id
        assert user.username == user.email
        assert user.email_verified_at is None
        assert verify_password("a-real-password-1", user.password_hash)
    finally:
        session.close()
    assert client.get(f"/verify/{token}").status_code == 200


def test_verification_disabled_marks_new_registration_verified_without_email(client):
    golfer_id = _add_golfer(client)
    client.app.state.settings.email_verification_required = False

    response = _submit(client, "player@example.test")

    assert response.status_code == 200
    assert "the account is ready to use" in response.text
    assert client.app.state.email_sender.sent == []
    session = Session(bind=client.app.state.engine)
    try:
        user = session.execute(select(User).where(User.email == "player@example.test")).scalar_one()
        assert user.golfer_id == golfer_id
        assert user.email_verified_at is not None
        assert session.execute(select(UserToken).where(UserToken.user_id == user.id)).first() is None
    finally:
        session.close()


def test_unknown_email_is_neutral_hashed_and_has_no_side_effects(client, monkeypatch):
    calls = []
    from golf_league.routers import identity

    actual = identity.hash_password
    monkeypatch.setattr(identity, "hash_password", lambda password: calls.append(password) or actual(password))
    response = _submit(client, "unknown@example.test")
    assert response.status_code == 200
    assert NEUTRAL in response.text
    assert calls == ["a-real-password-1"]
    session = Session(bind=client.app.state.engine)
    try:
        assert session.execute(select(User).where(User.email == "unknown@example.test")).first() is None
    finally:
        session.close()
    assert client.app.state.email_sender.sent == []


def test_short_or_missing_fields_rerender_and_missing_csrf_is_403(client):
    page = client.get("/register")
    csrf = _extract_csrf(page.text)
    assert client.post("/register", data={"email": "", "password": "short", "csrf_token": csrf}).status_code == 422
    assert client.post("/register", data={"email": "x@example.test", "password": "a-real-password-1"}).status_code == 403


def test_retry_only_reissues_token_without_changing_account(client):
    golfer_id = _add_golfer(client)
    _submit(client, "player@example.test", "original-password-1")
    first = client.app.state.email_sender.sent[-1]["body"].rsplit("/verify/", 1)[1]
    session = Session(bind=client.app.state.engine)
    try:
        user = session.execute(select(User).where(User.golfer_id == golfer_id)).scalar_one()
        original_hash, user_id = user.password_hash, user.id
    finally:
        session.close()
    _submit(client, "player@example.test", "different-password-2")
    second = client.app.state.email_sender.sent[-1]["body"].rsplit("/verify/", 1)[1]
    assert second != first
    session = Session(bind=client.app.state.engine)
    try:
        user = session.get(User, user_id)
        assert user.password_hash == original_hash and user.golfer_id == golfer_id
        tokens = session.execute(select(UserToken).where(UserToken.user_id == user_id)).scalars().all()
        assert len(tokens) == 2 and sum(token.revoked_at is not None for token in tokens) == 1
    finally:
        session.close()


def test_limiter_hashes_then_has_no_token_or_mail_side_effects(client, monkeypatch):
    _add_golfer(client)
    clock = {"now": 0.0}
    client.app.state.registration_rate_limiter = RateLimiter(3, 900, lambda: clock["now"])
    from golf_league.routers import identity
    calls = []
    actual = identity.hash_password
    monkeypatch.setattr(identity, "hash_password", lambda password: calls.append(password) or actual(password))
    for _ in range(4):
        assert _submit(client, "player@example.test").status_code == 200
    assert len(calls) == 4
    assert len(client.app.state.email_sender.sent) == 3


def test_email_failure_commits_and_later_retry_recovers(client):
    _add_golfer(client)

    class FailingSender:
        def send(self, **kwargs):
            raise RuntimeError("delivery unavailable")

    client.app.state.email_sender = FailingSender()
    assert _submit(client, "player@example.test").status_code == 200
    session = Session(bind=client.app.state.engine)
    try:
        assert session.execute(select(User).where(User.email == "player@example.test")).first()
    finally:
        session.close()
    from golf_league.services.email import FakeEmailSender
    client.app.state.email_sender = FakeEmailSender()
    assert _submit(client, "player@example.test").status_code == 200
    assert len(client.app.state.email_sender.sent) == 1


def test_account_cap_allows_user_150_then_neutrally_refuses_user_151(client):
    _add_users(client, 149)
    _add_golfer(client, email="one-fifty@example.test")
    _add_golfer(client, email="one-fifty-one@example.test")

    accepted = _submit(client, "one-fifty@example.test")
    before_refusal_mail = list(client.app.state.email_sender.sent)
    refused = _submit(client, "one-fifty-one@example.test")
    assert accepted.status_code == refused.status_code == 200
    assert accepted.text == refused.text

    session = Session(bind=client.app.state.engine)
    try:
        assert session.scalar(select(User).where(User.email == "one-fifty@example.test"))
        assert session.scalar(select(User).where(User.email == "one-fifty-one@example.test")) is None
        assert session.scalar(select(func.count()).select_from(UserToken)) == 1
        assert session.scalar(select(func.count()).select_from(User)) == 150
    finally:
        session.close()
    assert client.app.state.email_sender.sent == before_refusal_mail


def test_concurrent_duplicate_registration_creates_at_most_one_user_and_link(client):
    golfer_id = _add_golfer(client)
    password_hash = "not-used-by-this-race-test"

    def attempt():
        session = Session(bind=client.app.state.engine)
        try:
            return register_roster_user(
                session,
                email="player@example.test",
                password_hash=password_hash,
                max_users=150,
            )
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: attempt(), range(2)))

    # Both outer HTTP requests render the same neutral response. At the
    # persistence boundary, one is the initial creation and one is the
    # permitted unverified retry; neither can create a second account/link.
    assert all(result.token is not None for result in results)
    session = Session(bind=client.app.state.engine)
    try:
        users = session.execute(select(User).where(User.golfer_id == golfer_id)).scalars().all()
        assert len(users) == 1
        assert users[0].email == "player@example.test"
        tokens = session.execute(select(UserToken).where(UserToken.user_id == users[0].id)).scalars().all()
        assert len(tokens) == 2
        assert sum(token.revoked_at is None for token in tokens) == 1
    finally:
        session.close()
