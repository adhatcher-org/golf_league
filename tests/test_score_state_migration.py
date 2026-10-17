"""Fresh and prior-head migration acceptance for GL-83 match score states."""

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.exc import IntegrityError

from golf_league.migrations import _make_config as make_config
from golf_league.migrations import upgrade_to_head
from golf_league.models import Base

PRIOR_HEAD = "d7e2b9a4c618"
STATE_REVISION = "e41c7f9a2b36"
EARLY, LATE = "2026-08-27 18:00:00.000000", "2026-08-27 19:30:00.000000"


def _engine(url):
    engine = create_engine(url)

    @event.listens_for(engine, "connect")
    def _foreign_keys(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def _seed(connection):
    """Two users, three matches in one week, and the nine holes of a front nine."""
    x = connection.exec_driver_sql
    x("INSERT INTO courses (name,total_holes) VALUES ('Migration Club',18)")
    course = x("SELECT id FROM courses").scalar_one()
    for number in range(1, 10):
        x("INSERT INTO holes (course_id,number,nine,par,stroke_index_18,stroke_index_9) VALUES (?,?,'front',4,?,?)",
          (course, number, number, number))
    x("INSERT INTO tee_sets (course_id,name,color_label,gender,total_yards,sort_order) VALUES (?,'White','White','men',6000,1)", (course,))
    tee = x("SELECT id FROM tee_sets").scalar_one()
    for name in ("One", "Two", "Three", "Four", "Five", "Six"):
        x("INSERT INTO golfers (first_name,last_name,default_tee_set_id,handicap_source,handicap_status) "
          "VALUES (?,'Example',?,'self_reported','ok')", (name, tee))
    for email in ("a@example.test", "b@example.test"):
        x("INSERT INTO users (username,email,display_name,password_hash,is_admin,session_version) "
          "VALUES (?,?,'Synthetic','hash',1,1)", (email, email))
    x("INSERT INTO seasons (year,course_id,start_date,end_date,play_weekday,status,first_week_nine) "
      "VALUES (2026,?,'2026-08-27','2026-10-29',3,'draft','front')", (course,))
    season = x("SELECT id FROM seasons").scalar_one()
    for number in (1, 2):
        x("INSERT INTO teams (season_id,name,number,sort_order) VALUES (?,?,?,?)", (season, f"T{number}", number, number))
    x("INSERT INTO weeks (season_id,\"index\",play_date,nine,week_type,status) VALUES (?,1,'2026-08-27','front','match','scheduled')", (season,))
    week = x("SELECT id FROM weeks").scalar_one()
    teams = [row[0] for row in x("SELECT id FROM teams ORDER BY id")]
    x("INSERT INTO team_matches (week_id,home_team_id,away_team_id) VALUES (?,?,?)", (week, teams[0], teams[1]))
    team_match = x("SELECT id FROM team_matches").scalar_one()
    golfers = [row[0] for row in x("SELECT id FROM golfers ORDER BY id")]
    for slot in range(3):
        x("INSERT INTO player_matches (team_match_id,position_label,a_golfer_id,b_golfer_id) VALUES (?,?,?,?)",
          (team_match, str(slot + 1), golfers[slot * 2], golfers[slot * 2 + 1]))
    return {
        "matches": [row[0] for row in x("SELECT id FROM player_matches ORDER BY id")],
        "golfers": golfers,
        "holes": [row[0] for row in x("SELECT id FROM holes ORDER BY number")],
        "users": [row[0] for row in x("SELECT id FROM users ORDER BY id")],
    }


def _score(connection, ids, match, golfer, user, entered_at):
    for hole in ids["holes"]:
        connection.exec_driver_sql(
            "INSERT INTO hole_scores (player_match_id,golfer_id,hole_id,strokes,entered_by_user_id,entered_at) "
            "VALUES (?,?,?,?,?,?)", (match, golfer, hole, 4, user, entered_at))


def _assert_schema(engine):
    inspector = inspect(engine)
    columns = {c["name"]: c for c in inspector.get_columns("match_score_states")}
    assert set(columns) == {"player_match_id", "revision", "scores_changed_at", "scores_changed_by_user_id",
                            "locked_at", "locked_by_user_id"}
    assert {name for name, column in columns.items() if not column["nullable"]} == {"player_match_id", "revision"}
    assert inspector.get_pk_constraint("match_score_states")["constrained_columns"] == ["player_match_id"]
    assert {f["name"] for f in inspector.get_foreign_keys("match_score_states")} == {
        "fk_match_score_states_player_match", "fk_match_score_states_changed_by", "fk_match_score_states_locked_by",
    }
    assert {c["name"] for c in inspector.get_check_constraints("match_score_states")} == {
        "ck_match_score_states_revision", "ck_match_score_states_changed_pair", "ck_match_score_states_locked_pair",
    }


def test_fresh_upgrade_creates_the_table_with_enforced_constraints(tmp_path):
    url = f"sqlite:///{tmp_path / 'fresh.db'}"
    script = ScriptDirectory.from_config(make_config(url))
    assert script.get_heads() == [script.get_current_head()]  # one head, read dynamically
    assert script.get_revision(STATE_REVISION).down_revision == PRIOR_HEAD
    upgrade_to_head(url)
    engine = _engine(url)
    try:
        _assert_schema(engine)
        with engine.begin() as connection:
            ids = _seed(connection)
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM match_score_states").scalar_one() == 0
            connection.exec_driver_sql(
                "INSERT INTO match_score_states (player_match_id,revision,locked_at,locked_by_user_id) "
                "VALUES (?,1,?,?)", (ids["matches"][0], EARLY, ids["users"][0]))
        bad_rows = [
            ("(player_match_id,revision) VALUES (?,0)", (ids["matches"][1],)),  # revision below 1
            ("(player_match_id,revision) VALUES (?,1)", (ids["matches"][0],)),  # a second row for a match
            ("(player_match_id,revision) VALUES (?,1)", (999999,)),  # no such match
            ("(player_match_id,revision,locked_at) VALUES (?,1,?)", (ids["matches"][1], EARLY)),  # half a lock
            ("(player_match_id,revision,scores_changed_by_user_id) VALUES (?,1,?)",
             (ids["matches"][1], ids["users"][0])),  # half a change
            ("(player_match_id,revision,locked_at,locked_by_user_id) VALUES (?,1,?,?)",
             (ids["matches"][1], EARLY, 999999)),  # no such user
        ]
        for columns, values in bad_rows:
            with pytest.raises(IntegrityError):
                with engine.begin() as connection:
                    connection.exec_driver_sql(f"INSERT INTO match_score_states {columns}", values)
    finally:
        engine.dispose()


def test_upgrade_backfills_one_row_for_each_match_that_already_has_scores(tmp_path):
    url = f"sqlite:///{tmp_path / 'prior.db'}"
    config = make_config(url)
    command.upgrade(config, PRIOR_HEAD)
    engine = _engine(url)
    try:
        assert "match_score_states" not in inspect(engine).get_table_names()
        with engine.begin() as connection:
            ids = _seed(connection)
            first, second, third = ids["matches"]
            admin, other = ids["users"]
            # Match one: both golfers entered together. Match two: the second golfer entered later
            # by someone else. Match three: no scores.
            _score(connection, ids, first, ids["golfers"][0], admin, EARLY)
            _score(connection, ids, first, ids["golfers"][1], admin, EARLY)
            _score(connection, ids, second, ids["golfers"][2], admin, EARLY)
            _score(connection, ids, second, ids["golfers"][3], other, LATE)
            before = connection.exec_driver_sql("SELECT * FROM hole_scores ORDER BY id").all()
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    command.upgrade(config, "head")  # a second upgrade changes nothing
    engine = _engine(url)
    try:
        _assert_schema(engine)
        with engine.connect() as connection:
            rows = connection.exec_driver_sql(
                "SELECT player_match_id, revision, scores_changed_at, scores_changed_by_user_id, "
                "locked_at, locked_by_user_id FROM match_score_states ORDER BY player_match_id").all()
            assert rows == [(first, 1, EARLY, admin, None, None), (second, 1, LATE, other, None, None)]
            assert connection.exec_driver_sql("SELECT * FROM hole_scores ORDER BY id").all() == before
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()

    command.downgrade(config, PRIOR_HEAD)
    engine = create_engine(url)
    try:
        tables = inspect(engine).get_table_names()
        assert "match_score_states" not in tables and "hole_scores" in tables
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM hole_scores").scalar_one() == 36
    finally:
        engine.dispose()


def test_model_and_migration_agree(tmp_path):
    """`create_all` (used by service tests) and Alembic build the same table."""
    migrated, created = _engine(f"sqlite:///{tmp_path / 'm.db'}"), _engine(f"sqlite:///{tmp_path / 'c.db'}")
    upgrade_to_head(str(migrated.url))
    Base.metadata.create_all(created)
    try:
        def shape(engine):
            inspector = inspect(engine)
            return (
                sorted((c["name"], str(c["type"]), c["nullable"]) for c in inspector.get_columns("match_score_states")),
                sorted(c["sqltext"] for c in inspector.get_check_constraints("match_score_states")),
                sorted((tuple(f["constrained_columns"]), f["referred_table"])
                       for f in inspector.get_foreign_keys("match_score_states")),
                inspector.get_pk_constraint("match_score_states")["constrained_columns"],
            )
        assert shape(migrated) == shape(created)
    finally:
        migrated.dispose()
        created.dispose()
