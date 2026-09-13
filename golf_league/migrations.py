from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from golf_league.database import ensure_sqlite_directory

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def _make_config(database_url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def upgrade_to_head(database_url: str) -> None:
    """Upgrade the database to the latest revision.

    Safe to call twice: the second call is a no-op that leaves the same
    revision.
    """
    ensure_sqlite_directory(database_url)
    config = _make_config(database_url)
    command.upgrade(config, "head")


def current_revision(database_url: str) -> str | None:
    """Return the revision currently stamped in the database, if any.

    `alembic.command.current` prints to stdout and returns `None`; the
    revision has to be read directly from the database's own migration
    context instead.
    """
    ensure_sqlite_directory(database_url)
    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()
