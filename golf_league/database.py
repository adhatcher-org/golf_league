from collections.abc import Iterator
from pathlib import Path

from fastapi import Request
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

_SQLITE_PREFIX = "sqlite:///"


def ensure_sqlite_directory(database_url: str) -> None:
    """Create the parent directory of a SQLite file URL, if any.

    SQLite happily creates the database *file* on first connection but never
    its parent directory. Non-SQLite URLs are left untouched.
    """
    if not database_url.startswith(_SQLITE_PREFIX):
        return
    raw_path = database_url[len(_SQLITE_PREFIX) :]
    if not raw_path or raw_path == ":memory:":
        return
    path = Path(raw_path)
    parent = path.parent
    if str(parent) in ("", "."):
        return
    parent.mkdir(parents=True, exist_ok=True)


def make_engine(database_url: str):
    """Create SQLAlchemy engine with foreign key enforcement."""
    ensure_sqlite_directory(database_url)
    engine = create_engine(database_url)

    # Enable foreign key enforcement for SQLite
    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def get_session(request: Request) -> Iterator[Session]:
    """FastAPI dependency that yields a session and closes it.

    One transaction per request: callers commit once, not per row.
    """
    engine = request.app.state.engine
    session = Session(bind=engine, autoflush=False)
    try:
        yield session
    finally:
        session.close()
