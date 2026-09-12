"""Test configuration."""

import pytest
from golf_league.database import engine, SessionLocal
from golf_league.models import Base

@pytest.fixture(scope="session")
def db_engine():
    """Create database engine for tests."""
    # Create in-memory SQLite database for testing
    test_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=test_engine)
    return test_engine

@pytest.fixture(scope="function")
def db_session(db_engine):
    """Create a database session for each test."""
    connection = db_engine.connect()
    transaction = connection.begin()
    
    # Create a new session for the test
    session = SessionLocal(bind=connection)
    
    yield session
    
    # Clean up after test
    session.close()
    transaction.rollback()
    connection.close()