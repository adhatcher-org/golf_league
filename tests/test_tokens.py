"""GL-03: token lifecycle — issuance, single-use consumption, and expiry."""

import threading
from datetime import UTC, datetime, timedelta

from golf_league.domain.tokens import digest, is_expired
from golf_league.models import User, UserToken
from golf_league.services.auth import consume_token, hash_password, issue_token


def make_user(session, email="tokens@example.com"):
    user = User(
        username=email,
        email=email,
        display_name="Token Owner",
        password_hash=hash_password("irrelevant-password-1"),
        is_admin=False,
        session_version=1,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


# `is_expired` is pure: `now` is always injected, never read from the clock.
def test_is_expired_uses_injected_clock_only():
    expires_at = datetime(2026, 1, 1, 12, 0, 0)
    before = datetime(2026, 1, 1, 11, 59, 59)
    at_boundary = datetime(2026, 1, 1, 12, 0, 0)
    after = datetime(2026, 1, 1, 12, 0, 1)

    assert is_expired(expires_at, before) is False
    assert is_expired(expires_at, at_boundary) is True
    assert is_expired(expires_at, after) is True


def test_digest_is_sha256_hex_and_never_the_raw_token():
    import hashlib

    raw = "some-raw-token-value"
    expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert digest(raw) == expected
    assert digest(raw) != raw


def test_issue_token_returns_raw_and_stores_only_digest(session):
    user = make_user(session)
    raw = issue_token(session, user.id, "verify_email", ttl_seconds=3600)

    row = session.query(UserToken).filter_by(user_id=user.id).one()
    assert row.token_digest == digest(raw)
    assert row.token_digest != raw
    assert row.consumed_at is None
    assert row.revoked_at is None


def test_issue_token_revokes_prior_outstanding_token_of_same_purpose(session):
    user = make_user(session)
    first_raw = issue_token(session, user.id, "reset_password", ttl_seconds=3600)
    second_raw = issue_token(session, user.id, "reset_password", ttl_seconds=3600)

    first_row = session.query(UserToken).filter_by(token_digest=digest(first_raw)).one()
    assert first_row.revoked_at is not None

    # The old token no longer works; the new one does.
    assert consume_token(session, first_raw, "reset_password") is None
    assert consume_token(session, second_raw, "reset_password") == user.id


# 6. A token consumed twice succeeds once and returns None the second time.
def test_token_consumed_twice_succeeds_once(session):
    user = make_user(session)
    raw = issue_token(session, user.id, "set_password", ttl_seconds=3600)

    first_result = consume_token(session, raw, "set_password")
    second_result = consume_token(session, raw, "set_password")

    assert first_result == user.id
    assert second_result is None


# 7. Two concurrent consumers of one token: exactly one succeeds.
def test_concurrent_consumers_exactly_one_succeeds(tmp_path):
    # A shared in-memory engine (StaticPool) hands every session the *same*
    # underlying sqlite3 connection object, and the sqlite3 module is not
    # safe to call concurrently from two threads on one connection — it
    # raises "bad parameter or other API misuse" instead of exercising the
    # atomic UPDATE this test is meant to check. A real file-backed
    # database gives each thread its own connection, so SQLite's own
    # locking (not Python-level locking) is what has to serialize the two
    # UPDATEs — which is exactly the scenario `consume_token` must survive.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session as OrmSession

    from golf_league.models import Base

    db_path = tmp_path / "concurrent.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"timeout": 30})
    Base.metadata.create_all(engine)

    setup_session = OrmSession(bind=engine)
    user = make_user(setup_session, email="concurrent@example.com")
    user_id = user.id
    raw = issue_token(setup_session, user_id, "verify_email", ttl_seconds=3600)
    setup_session.close()

    results: list[int | None] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def worker():
        worker_session = OrmSession(bind=engine)
        try:
            barrier.wait()
            result = consume_token(worker_session, raw, "verify_email")
            with lock:
                results.append(result)
        finally:
            worker_session.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    engine.dispose()

    successes = [r for r in results if r is not None]
    failures = [r for r in results if r is None]
    assert len(successes) == 1
    assert len(failures) == 1
    assert successes[0] == user_id


# 8. An expired token is rejected (no sleep — the expiry is set directly).
def test_expired_token_is_rejected(session):
    user = make_user(session)
    raw = issue_token(session, user.id, "verify_email", ttl_seconds=3600)

    row = session.query(UserToken).filter_by(user_id=user.id).one()
    row.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
    session.add(row)
    session.commit()

    assert consume_token(session, raw, "verify_email") is None


# 9. A token presented with the wrong purpose is rejected.
def test_token_with_wrong_purpose_is_rejected(session):
    user = make_user(session)
    raw = issue_token(session, user.id, "verify_email", ttl_seconds=3600)

    assert consume_token(session, raw, "reset_password") is None
    # The correct purpose still works — the token itself was never touched.
    assert consume_token(session, raw, "verify_email") == user.id


def test_unknown_token_is_rejected(session):
    assert consume_token(session, "not-a-real-token", "verify_email") is None
