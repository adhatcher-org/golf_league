"""Migration acceptance tests for courses, tee_sets and tee_ratings."""

from alembic import command
from sqlalchemy import create_engine, inspect

from golf_league.migrations import upgrade_to_head


def test_upgrade_to_head_creates_courses_tee_sets_and_tee_ratings(tmp_path):
    db_path = tmp_path / "migration.db"
    database_url = f"sqlite:///{db_path}"

    upgrade_to_head(database_url)

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        assert {"courses", "tee_sets", "tee_ratings"} <= table_names

        tee_set_columns = {c["name"] for c in inspector.get_columns("tee_sets")}
        assert {"course_id", "name", "color_label", "gender", "total_yards", "sort_order"} <= tee_set_columns

        tee_rating_columns = {c["name"] for c in inspector.get_columns("tee_ratings")}
        assert {"tee_set_id", "scope", "rating", "slope", "par"} <= tee_rating_columns
    finally:
        engine.dispose()


def test_migration_is_idempotent_and_downgrades_cleanly(tmp_path):
    db_path = tmp_path / "migration2.db"
    database_url = f"sqlite:///{db_path}"

    upgrade_to_head(database_url)
    upgrade_to_head(database_url)  # idempotent: no-op on second call

    from golf_league.migrations import _make_config as make_config

    config = make_config(database_url)
    command.downgrade(config, "1d828a2548b3")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        assert "courses" not in table_names
        assert "tee_sets" not in table_names
        assert "tee_ratings" not in table_names
    finally:
        engine.dispose()
