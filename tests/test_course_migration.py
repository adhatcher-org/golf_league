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
        assert {"courses", "tee_sets", "tee_ratings", "holes", "hole_yardages"} <= table_names

        tee_set_columns = {c["name"] for c in inspector.get_columns("tee_sets")}
        assert {"course_id", "name", "color_label", "gender", "total_yards", "sort_order"} <= tee_set_columns

        tee_rating_columns = {c["name"] for c in inspector.get_columns("tee_ratings")}
        assert {"tee_set_id", "scope", "rating", "slope", "par"} <= tee_rating_columns
        hole_columns = {c["name"] for c in inspector.get_columns("holes")}
        assert {"course_id", "number", "nine", "par", "stroke_index_18", "stroke_index_9"} <= hole_columns
    finally:
        engine.dispose()


def test_upgrade_from_prior_head_preserves_existing_course_data(tmp_path):
    db_path = tmp_path / "prior.db"
    database_url = f"sqlite:///{db_path}"
    from golf_league.migrations import _make_config as make_config

    config = make_config(database_url)
    command.upgrade(config, "d24f7b6c1a9e")
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("INSERT INTO courses (name, total_holes) VALUES ('Prior Club', 18)")
    finally:
        engine.dispose()
    upgrade_to_head(database_url)
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT name FROM courses").scalar_one() == "Prior Club"
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
