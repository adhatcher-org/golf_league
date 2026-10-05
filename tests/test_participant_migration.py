"""Forward migration acceptance for GL-31 participant overrides."""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError

from golf_league.migrations import _make_config as make_config


def test_participant_migration_constraints_upgrade_and_downgrade(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'participants.db'}"
    config = make_config(database_url)
    command.upgrade(config, "d24f7b6c1a9e")
    command.upgrade(config, "7a31b6e9c204")
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert {column["name"] for column in inspector.get_columns("season_participants")} == {
            "id", "season_id", "golfer_id", "tee_set_id", "seed_handicap_strokes", "seed_source"
        }
        assert {item["name"] for item in inspector.get_unique_constraints("season_participants")} == {
            "uq_season_participants_season_golfer"
        }
        assert {item["name"] for item in inspector.get_indexes("season_participants")} == {
            "ix_season_participants_season_id", "ix_season_participants_golfer_id", "ix_season_participants_tee_set_id"
        }
        with engine.begin() as connection:
            connection.exec_driver_sql("INSERT INTO courses (name,total_holes) VALUES ('Club',18)")
            course_id = connection.exec_driver_sql("SELECT id FROM courses").scalar_one()
            connection.exec_driver_sql("INSERT INTO tee_sets (course_id,name,color_label,gender,total_yards,sort_order) VALUES (?, 'Deer','White','men',1000,0)", (course_id,))
            tee_id = connection.exec_driver_sql("SELECT id FROM tee_sets").scalar_one()
            connection.exec_driver_sql("INSERT INTO seasons (year,course_id,start_date,end_date,play_weekday,status,first_week_nine) VALUES (2026,?,'2026-08-27','2026-10-29',3,'draft','front')", (course_id,))
            season_id = connection.exec_driver_sql("SELECT id FROM seasons").scalar_one()
            connection.exec_driver_sql("INSERT INTO golfers (first_name,last_name,default_tee_set_id,handicap_source,handicap_status) VALUES ('Pat','Example',?,'imported','needs_entry')", (tee_id,))
            golfer_id = connection.exec_driver_sql("SELECT id FROM golfers").scalar_one()
            connection.exec_driver_sql("INSERT INTO season_participants (season_id,golfer_id,seed_handicap_strokes) VALUES (?,?,?)", (season_id,golfer_id,-3))
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "INSERT INTO season_participants (season_id,golfer_id) VALUES (?,?)",
                    (season_id, golfer_id),
                )
        command.downgrade(config, "d24f7b6c1a9e")
        assert "season_participants" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
