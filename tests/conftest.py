import re
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from golf_league.models import Base, Course, Golfer, User
from golf_league.services.auth import hash_password

_CSRF_RE = re.compile(r'name="csrf_token" value="([^"]*)"')


def _extract_csrf(html: str) -> str:
    """Return the value of the hidden `csrf_token` input in a rendered form."""
    match = _CSRF_RE.search(html)
    assert match, f"no csrf_token field found in: {html!r}"
    return match.group(1)


def _make_admin_client(client: TestClient) -> TestClient:
    """Insert a verified admin `User` and log in through the real `/login` form."""
    email = "admin@example.test"
    password = "s3cret-pw!"
    session = Session(bind=client.app.state.engine)
    try:
        user = User(
            username=email,
            email=email,
            display_name="Admin",
            password_hash=hash_password(password),
            email_verified_at=datetime.now(UTC).replace(tzinfo=None),
            is_admin=True,
        )
        session.add(user)
        session.commit()
    finally:
        session.close()

    login_page = client.get("/login")
    csrf_token = _extract_csrf(login_page.text)
    response = client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client


@pytest.fixture
def engine():
    """Create an in-memory SQLite engine with foreign keys ON and tables created."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    """Create a session that rolls back after each test."""
    connection = engine.connect()
    transaction = connection.begin()
    session = sessionmaker(bind=connection)()

    yield session

    # Rollback the transaction
    transaction.rollback()
    connection.close()


@pytest.fixture
def client(tmp_path):
    """FastAPI TestClient with a temporary SQLite database."""
    from fastapi.testclient import TestClient

    from golf_league.app import create_app
    from golf_league.config import Settings

    # Create a temporary database file
    db_path = tmp_path / "app.db"
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        session_secret="test-only-not-a-secret"
    )

    # Use TestClient context manager to handle lifespan
    with TestClient(create_app(settings=settings)) as test_client:
        yield test_client


@pytest.fixture
def empty_client(tmp_path):
    """Same as `client`, but starts from a genuinely empty database.

    `seed_course=False` disables the Wyandot bootstrap so tests can prove
    a form-only, seed-free path (e.g. creating a course through the admin
    forms starting from zero rows).
    """
    from fastapi.testclient import TestClient

    from golf_league.app import create_app
    from golf_league.config import Settings

    db_path = tmp_path / "app.db"
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        session_secret="test-only-not-a-secret",
        seed_course=False,
    )

    with TestClient(create_app(settings=settings)) as test_client:
        yield test_client


@pytest.fixture
def wyandot_course(session):
    """Factory returning a function that seeds Wyandot into `session`.

    Returns the `Course` row with both tee sets and six ratings persisted,
    for service-level tests that need real data without going through the
    HTTP layer.
    """
    from golf_league.services.course_seed import seed_wyandot

    def _make():
        seed_wyandot(session)
        return session.execute(
            select(Course).where(Course.name == "Wyandot Golf Club")
        ).scalar_one()

    return _make


@pytest.fixture
def golfer(session, wyandot_course):
    """A saved `Golfer` attached to a seeded tee set.

    Builds on `wyandot_course`: seeds Wyandot, then creates a golfer on
    its "Deer" tee set with a real handicap on file, for service-level
    tests that need a persisted starting row.
    """
    course = wyandot_course()
    tee_set = next(t for t in course.tee_sets if t.name == "Deer")
    golfer_row = Golfer(
        first_name="Pat",
        last_name="Example",
        email="pat.example@example.test",
        phone=None,
        default_tee_set_id=tee_set.id,
        handicap_strokes=12,
        handicap_source="self_reported",
        handicap_status="ok",
        notes=None,
    )
    session.add(golfer_row)
    session.commit()
    session.refresh(golfer_row)
    return golfer_row


@pytest.fixture()
def admin_client(client: TestClient) -> Iterator[TestClient]:
    """A logged-in admin `TestClient`, built on the Wyandot-seeded `client`.

    Inserts a verified admin `User` through
    `Session(bind=client.app.state.engine)`, logs in through the real
    `/login` form with a real CSRF token, and yields the logged-in client.
    """
    yield _make_admin_client(client)


@pytest.fixture()
def empty_admin_client(empty_client: TestClient) -> Iterator[TestClient]:
    """Same as `admin_client`, built on the `empty_client` fixture (no seeded course)."""
    yield _make_admin_client(empty_client)
