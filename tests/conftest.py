import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from golf_league.models import Base, Course, Golfer


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
