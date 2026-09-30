"""Migration acceptance tests for the GL-30 seasons table."""

import pytest
from alembic import command
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.exc import IntegrityError

from golf_league.migrations import _make_config as make_config
from golf_league.migrations import upgrade_to_head


def _fk_engine(database_url):
    engine = create_engine(database_url)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def _insert_course(connection):
    connection.exec_driver_sql(
        "INSERT INTO courses (name, total_holes) VALUES ('Migration Club', 18)"
    )
    return connection.exec_driver_sql("SELECT id FROM courses").scalar_one()


def test_season_migration_schema_constraints_foreign_key_and_index(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'seasons.db'}"
    upgrade_to_head(database_url)
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        columns = {column["name"] for column in inspector.get_columns("seasons")}
        assert columns == {
            "id", "year", "name_override", "course_id", "start_date", "end_date",
            "play_weekday", "status", "first_week_nine",
        }
        checks = {check["name"] for check in inspector.get_check_constraints("seasons")}
        assert checks == {
            "ck_seasons_year_range", "ck_seasons_play_weekday_range", "ck_seasons_status",
            "ck_seasons_first_week_nine", "ck_seasons_dates",
        }
        foreign_keys = inspector.get_foreign_keys("seasons")
        assert foreign_keys == [{
            "name": "fk_seasons_course_id_courses", "constrained_columns": ["course_id"],
            "referred_schema": None, "referred_table": "courses",
            "referred_columns": ["id"], "options": {},
        }]
        assert {index["name"] for index in inspector.get_indexes("seasons")} == {
            "ix_seasons_course_id"
        }
    finally:
        engine.dispose()


def test_season_sql_constraints_and_foreign_key_are_enforced(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'constraints.db'}"
    upgrade_to_head(database_url)
    engine = _fk_engine(database_url)
    valid = """INSERT INTO seasons
        (year, course_id, start_date, end_date, play_weekday, status, first_week_nine)
        VALUES (?, ?, '2026-08-27', '2026-10-29', ?, ?, ?)"""
    try:
        with engine.begin() as connection:
            course_id = _insert_course(connection)
            connection.exec_driver_sql(valid, (2026, course_id, 3, "draft", "front"))
        invalid_values = [
            (0, course_id, 3, "draft", "front"),
            (10000, course_id, 3, "draft", "front"),
            (2026, course_id, -1, "draft", "front"),
            (2026, course_id, 7, "draft", "front"),
            (2026, course_id, 3, "bad", "front"),
            (2026, course_id, 3, "draft", "middle"),
            (2026, 9999, 3, "draft", "front"),
        ]
        for values in invalid_values:
            with pytest.raises(IntegrityError):
                with engine.begin() as connection:
                    connection.exec_driver_sql(valid, values)
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    """INSERT INTO seasons
                    (year, course_id, start_date, end_date, play_weekday, status, first_week_nine)
                    VALUES (2026, ?, '2026-10-29', '2026-08-27', 3, 'draft', 'front')""",
                    (course_id,),
                )
    finally:
        engine.dispose()


def test_prior_head_upgrade_preserves_course_and_downgrade_removes_only_seasons(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'prior.db'}"
    config = make_config(database_url)
    command.upgrade(config, "f6a3b7c9d2e1")
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            course_id = _insert_course(connection)
    finally:
        engine.dispose()

    upgrade_to_head(database_url)
    upgrade_to_head(database_url)
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT id FROM courses").scalar_one() == course_id
    finally:
        engine.dispose()

    command.downgrade(make_config(database_url), "f6a3b7c9d2e1")
    engine = create_engine(database_url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert "seasons" not in tables
        assert {"courses", "holes", "hole_yardages"} <= tables
    finally:
        engine.dispose()
