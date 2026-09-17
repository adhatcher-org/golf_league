import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from golf_league.models import Base


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
