from alembic import command
from alembic.config import Config


def upgrade_to_head(database_url: str) -> None:
    """Upgrade the database to the latest revision."""
    config = Config()
    config.set_main_option("script_location", "migrations")
    config.set_main_option("sqlalchemy.url", database_url)

    command.upgrade(config, "head")


def current_revision(database_url: str) -> str | None:
    """Get the current revision of the database."""
    config = Config()
    config.set_main_option("script_location", "migrations")
    config.set_main_option("sqlalchemy.url", database_url)

    try:
        return command.current(config)
    except Exception:
        return None
