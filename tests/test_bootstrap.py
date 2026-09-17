"""Tests for `golf_league.admin_config.bootstrap_admin`."""

import logging
import threading

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from golf_league.admin_config import bootstrap_admin
from golf_league.models import Base, User


def _count_users(engine) -> int:
    with Session(bind=engine) as session:
        return session.query(User).count()


def test_empty_database_creates_exactly_one_verified_admin(engine, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.test")
    monkeypatch.setenv("ADMIN_PASSWORD", "correct-horse-battery-staple")

    with Session(bind=engine) as session:
        bootstrap_admin(session)

    with Session(bind=engine) as session:
        users = session.query(User).all()
        assert len(users) == 1
        admin = users[0]
        assert admin.email == "admin@example.test"
        assert admin.is_admin is True
        assert admin.email_verified_at is not None


def test_running_bootstrap_twice_creates_no_second_user(engine, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.test")
    monkeypatch.setenv("ADMIN_PASSWORD", "correct-horse-battery-staple")

    with Session(bind=engine) as session:
        bootstrap_admin(session)
    with Session(bind=engine) as session:
        bootstrap_admin(session)  # must not raise

    assert _count_users(engine) == 1


def test_concurrent_bootstraps_create_exactly_one_admin(tmp_path, monkeypatch):
    # A real file-backed database with its own connection per "worker",
    # rather than the shared single StaticPool connection the `engine`
    # fixture uses (which two real threads cannot safely drive at once).
    # `timeout` lets a writer wait out the other's transaction instead of
    # raising "database is locked", so the only outcome under test is the
    # unique-constraint race itself.
    db_path = tmp_path / "concurrent.db"
    database_url = f"sqlite:///{db_path}"
    setup_engine = create_engine(database_url, connect_args={"timeout": 5})
    Base.metadata.create_all(setup_engine)
    setup_engine.dispose()

    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.test")
    monkeypatch.setenv("ADMIN_PASSWORD", "correct-horse-battery-staple")

    errors: list[BaseException] = []

    def run() -> None:
        worker_engine = create_engine(database_url, connect_args={"timeout": 5})
        try:
            with Session(bind=worker_engine) as session:
                bootstrap_admin(session)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            worker_engine.dispose()

    threads = [threading.Thread(target=run) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []

    check_engine = create_engine(database_url)
    try:
        assert _count_users(check_engine) == 1
    finally:
        check_engine.dispose()


def test_missing_admin_password_creates_no_user_and_logs_clearly(
    engine, monkeypatch, caplog
):
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.test")
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)

    with caplog.at_level(logging.WARNING):
        with Session(bind=engine) as session:
            bootstrap_admin(session)  # must not raise

    assert _count_users(engine) == 0
    assert any("ADMIN_EMAIL" in record.message for record in caplog.records)


def test_non_empty_database_leaves_existing_admin_unchanged(engine, monkeypatch):
    from golf_league.services.auth import hash_password

    with Session(bind=engine) as session:
        existing = User(
            username="existing@example.test",
            email="existing@example.test",
            display_name="Existing",
            password_hash=hash_password("original-password"),
            is_admin=True,
        )
        session.add(existing)
        session.commit()
        existing_id = existing.id
        original_hash = existing.password_hash

    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.test")
    monkeypatch.setenv("ADMIN_PASSWORD", "correct-horse-battery-staple")

    with Session(bind=engine) as session:
        bootstrap_admin(session)

    with Session(bind=engine) as session:
        users = session.query(User).all()
        assert len(users) == 1
        assert users[0].id == existing_id
        assert users[0].password_hash == original_hash
        assert users[0].email_verified_at is None
