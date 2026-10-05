"""Fresh and prior-head migration acceptance for GL-33 weeks."""

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.exc import IntegrityError

from golf_league.migrations import _make_config as make_config
from golf_league.migrations import upgrade_to_head

PRIOR_HEAD = "3c7d9a1e5b42"


def _engine(database_url):
    engine = create_engine(database_url)

    @event.listens_for(engine, "connect")
    def _foreign_keys(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def _seed_prior(connection):
    connection.exec_driver_sql("INSERT INTO courses (name,total_holes) VALUES ('Migration Club',18)")
    course_id = connection.exec_driver_sql("SELECT id FROM courses").scalar_one()
    connection.exec_driver_sql(
        "INSERT INTO seasons (year,course_id,start_date,end_date,play_weekday,status,first_week_nine) VALUES (2026,?,'2026-08-27','2026-10-29',3,'draft','front')",
        (course_id,),
    )
    return course_id, connection.exec_driver_sql("SELECT id FROM seasons").scalar_one()


def _assert_schema(engine):
    inspector = inspect(engine)
    assert "weeks" in inspector.get_table_names()
    assert {col["name"] for col in inspector.get_columns("weeks")} == {
        "id", "season_id", "index", "play_date", "nine", "week_type",
        "makeup_for_week_id", "status", "notes",
    }
    assert {item["name"] for item in inspector.get_unique_constraints("weeks")} == {"uq_weeks_season_index"}
    assert {item["name"] for item in inspector.get_indexes("weeks")} == {
        "ix_weeks_season_id", "ix_weeks_makeup_for_week_id",
    }
    assert {item["name"] for item in inspector.get_foreign_keys("weeks")} == {
        "fk_weeks_season", "fk_weeks_makeup_for",
    }


def test_fresh_upgrade_has_one_head_and_weeks_constraints(tmp_path):
    url = f"sqlite:///{tmp_path / 'fresh.db'}"
    config = make_config(url)
    assert ScriptDirectory.from_config(config).get_heads() == ["6a2e9d4b7c31"]
    upgrade_to_head(url)
    engine = _engine(url)
    try:
        _assert_schema(engine)
        with engine.begin() as connection:
            _, season_id = _seed_prior(connection)
            connection.exec_driver_sql(
                "INSERT INTO weeks (season_id,\"index\",play_date,nine,week_type,status) VALUES (?,1,'2026-08-27','front','match','scheduled')",
                (season_id,),
            )
            first_id = connection.exec_driver_sql("SELECT id FROM weeks").scalar_one()
            connection.exec_driver_sql(
                "INSERT INTO weeks (season_id,\"index\",play_date,nine,week_type,makeup_for_week_id,status) VALUES (?,2,'2026-09-03',NULL,'rain_date',?,'scheduled')",
                (season_id, first_id),
            )
        invalid = [
            "INSERT INTO weeks (season_id,\"index\",play_date,nine,week_type,status) VALUES (1,1,'2026-08-27','front','match','scheduled')",
            "INSERT INTO weeks (season_id,\"index\",play_date,nine,week_type,status) VALUES (1,3,'2026-09-10',NULL,'match','scheduled')",
            "INSERT INTO weeks (season_id,\"index\",play_date,nine,week_type,status) VALUES (1,4,'2026-09-17','front','rain_date','scheduled')",
        ]
        for statement in invalid:
            with pytest.raises(IntegrityError):
                with engine.begin() as connection:
                    connection.exec_driver_sql(statement)
    finally:
        engine.dispose()


def test_upgrade_from_current_prior_head_preserves_data_and_downgrades_cleanly(tmp_path):
    url = f"sqlite:///{tmp_path / 'prior.db'}"
    config = make_config(url)
    command.upgrade(config, PRIOR_HEAD)
    engine = _engine(url)
    try:
        with engine.begin() as connection:
            course_id, season_id = _seed_prior(connection)
    finally:
        engine.dispose()
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    assert ScriptDirectory.from_config(config).get_heads() == ["6a2e9d4b7c31"]
    engine = _engine(url)
    try:
        _assert_schema(engine)
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT id FROM courses").scalar_one() == course_id
            assert connection.exec_driver_sql("SELECT id FROM seasons").scalar_one() == season_id
    finally:
        engine.dispose()
    command.downgrade(config, PRIOR_HEAD)
    engine = create_engine(url)
    try:
        assert "weeks" not in inspect(engine).get_table_names()
        assert {"courses", "seasons", "teams"} <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
