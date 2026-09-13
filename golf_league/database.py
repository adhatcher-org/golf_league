from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker


def make_engine(database_url: str):
    """Create SQLAlchemy engine with foreign key enforcement."""
    engine = create_engine(database_url)

    # Enable foreign key enforcement for SQLite
    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def get_session() -> Session:
    """FastAPI dependency that yields a session and closes it."""
    from fastapi import Request

    def _get_session(request: Request) -> Session:
        engine = request.app.state.engine
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()

    return _get_session
