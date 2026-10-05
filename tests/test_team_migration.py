"""Forward-only schema and integrity acceptance for GL-32 teams."""

import pytest
from alembic import command
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.exc import IntegrityError

from golf_league.migrations import _make_config as make_config

PRIOR_HEAD = "7a31b6e9c204"


def _engine_with_fk(database_url):
    engine = create_engine(database_url)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def _seed_prior_data(connection):
    connection.exec_driver_sql("INSERT INTO courses (name,total_holes) VALUES ('Synthetic Club',18)")
    course_id = connection.exec_driver_sql("SELECT id FROM courses").scalar_one()
    connection.exec_driver_sql(
        "INSERT INTO tee_sets (course_id,name,color_label,gender,total_yards,sort_order) VALUES (?, 'Synthetic','Blue','men',5000,0)",
        (course_id,),
    )
    tee_id = connection.exec_driver_sql("SELECT id FROM tee_sets").scalar_one()
    connection.exec_driver_sql(
        "INSERT INTO seasons (year,course_id,start_date,end_date,play_weekday,status,first_week_nine) VALUES (2026,?,'2026-08-27','2026-10-29',3,'draft','front')",
        (course_id,),
    )
    season_id = connection.exec_driver_sql("SELECT id FROM seasons").scalar_one()
    connection.exec_driver_sql(
        "INSERT INTO golfers (first_name,last_name,default_tee_set_id,handicap_strokes,handicap_source,handicap_status) VALUES ('Synthetic','Player',?,0,'imported','ok')",
        (tee_id,),
    )
    golfer_id = connection.exec_driver_sql("SELECT id FROM golfers").scalar_one()
    connection.exec_driver_sql(
        "INSERT INTO golfers (first_name,last_name,default_tee_set_id,handicap_strokes,handicap_source,handicap_status) VALUES ('Synthetic','Second',?,3,'imported','ok')",
        (tee_id,),
    )
    second_golfer_id = connection.exec_driver_sql("SELECT max(id) FROM golfers").scalar_one()
    connection.exec_driver_sql(
        "INSERT INTO season_participants (season_id,golfer_id,seed_handicap_strokes) VALUES (?,?,?)",
        (season_id, golfer_id, -2),
    )
    return course_id, season_id, golfer_id, second_golfer_id


def test_team_migration_constraints_upgrade_and_downgrade(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'teams.db'}"
    config = make_config(database_url)
    command.upgrade(config, PRIOR_HEAD)
    engine = _engine_with_fk(database_url)
    try:
        with engine.begin() as connection:
            _, season_one, golfer_one, golfer_two = _seed_prior_data(connection)
            connection.exec_driver_sql(
                "INSERT INTO seasons (year,course_id,start_date,end_date,play_weekday,status,first_week_nine) VALUES (2027,(SELECT id FROM courses),'2027-08-26','2027-10-28',3,'draft','front')"
            )
            season_two = connection.exec_driver_sql("SELECT max(id) FROM seasons").scalar_one()
        command.upgrade(config, "b8d4c0e2f671")
        inspector = inspect(engine)
        assert {"teams", "team_members"} <= set(inspector.get_table_names())
        assert {c["name"] for c in inspector.get_columns("teams")} == {
            "id", "season_id", "name", "number", "sort_order",
        }
        assert {c["name"] for c in inspector.get_columns("team_members")} == {
            "id", "team_id", "season_id", "golfer_id", "position",
        }
        assert {item["name"] for item in inspector.get_indexes("teams")} == {"ix_teams_season_id"}
        assert {item["name"] for item in inspector.get_indexes("team_members")} == {
            "ix_team_members_team_id", "ix_team_members_season_id", "ix_team_members_golfer_id",
        }
        assert {item["name"] for item in inspector.get_unique_constraints("teams")} == {
            "uq_teams_season_number", "uq_teams_id_season",
        }
        assert {item["name"] for item in inspector.get_unique_constraints("team_members")} == {
            "uq_team_members_team_position", "uq_team_members_team_golfer",
            "uq_team_members_season_golfer",
        }
        member_fks = inspector.get_foreign_keys("team_members")
        composite = next(fk for fk in member_fks if fk["name"] == "fk_team_members_team_season")
        assert composite["constrained_columns"] == ["team_id", "season_id"]
        assert composite["referred_columns"] == ["id", "season_id"]
        check = next(item for item in inspector.get_check_constraints("team_members")
                     if item["name"] == "ck_team_members_position")
        assert "position >= 1" in check["sqltext"] and "position <= 4" in check["sqltext"]

        with engine.begin() as connection:
            assert connection.exec_driver_sql(
                "SELECT seed_handicap_strokes FROM season_participants WHERE season_id=? AND golfer_id=?",
                (season_one, golfer_one),
            ).scalar_one() == -2
            connection.exec_driver_sql(
                "INSERT INTO teams (season_id,name,number,sort_order) VALUES (?, 'Synthetic Team',1,0)",
                (season_one,),
            )
            team_id = connection.exec_driver_sql("SELECT last_insert_rowid()").scalar_one()
            connection.exec_driver_sql(
                "INSERT INTO team_members (team_id,season_id,golfer_id,position) VALUES (?,?,?,1)",
                (team_id, season_one, golfer_one),
            )

        for statement, params in (
            ("INSERT INTO teams (season_id,name,number,sort_order) VALUES (?, 'Duplicate Number',1,1)",
             (season_one,)),
            ("INSERT INTO team_members (team_id,season_id,golfer_id,position) VALUES (?,?,?,1)",
             (team_id, season_one, golfer_two)),
            ("INSERT INTO team_members (team_id,season_id,golfer_id,position) VALUES (?,?,?,2)",
             (team_id, season_one, golfer_one)),
            ("INSERT INTO team_members (team_id,season_id,golfer_id,position) VALUES (?,?,?,5)",
             (team_id, season_one, golfer_two)),
            ("INSERT INTO team_members (team_id,season_id,golfer_id,position) VALUES (?,?,?,2)",
             (team_id, season_two, golfer_two)),
        ):
            with pytest.raises(IntegrityError):
                with engine.begin() as connection:
                    connection.exec_driver_sql(statement, params)
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO teams (season_id,name,number,sort_order) VALUES (?, 'Second Team',2,1)",
                (season_one,),
            )
            second_team_id = connection.exec_driver_sql("SELECT last_insert_rowid()").scalar_one()
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "INSERT INTO team_members (team_id,season_id,golfer_id,position) VALUES (?,?,?,1)",
                    (second_team_id, season_one, golfer_one),
                )

        command.downgrade(config, PRIOR_HEAD)
        assert {"teams", "team_members"}.isdisjoint(set(inspect(engine).get_table_names()))
        assert {"courses", "tee_sets", "seasons", "golfers", "season_participants"} <= set(
            inspect(engine).get_table_names()
        )
        with engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT seed_handicap_strokes FROM season_participants WHERE season_id=? AND golfer_id=?",
                (season_one, golfer_one),
            ).scalar_one() == -2
    finally:
        engine.dispose()
