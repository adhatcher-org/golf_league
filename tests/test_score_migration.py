"""Fresh and prior-head migration acceptance for GL-79 hole scores."""

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.exc import IntegrityError

from golf_league.migrations import _make_config as make_config
from golf_league.migrations import upgrade_to_head

PRIOR_HEAD = "ab42c7d9e013"
SCORE_REVISION = "c3f8a1d27b45"


def _engine(url):
    engine = create_engine(url)

    @event.listens_for(engine, "connect")
    def _foreign_keys(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def _seed(connection):
    """Minimum synthetic graph: user, hole and one player match."""
    x = connection.exec_driver_sql
    x("INSERT INTO courses (name,total_holes) VALUES ('Migration Club',18)")
    course = x("SELECT id FROM courses").scalar_one()
    x("INSERT INTO holes (course_id,number,nine,par,stroke_index_18,stroke_index_9) VALUES (?,1,'front',4,1,1)", (course,))
    x("INSERT INTO tee_sets (course_id,name,color_label,gender,total_yards,sort_order) VALUES (?,'White','White','men',6000,1)", (course,))
    tee = x("SELECT id FROM tee_sets").scalar_one()
    for name in ("One", "Two"):
        x("INSERT INTO golfers (first_name,last_name,default_tee_set_id,handicap_source,handicap_status) "
          "VALUES (?,'Example',?,'self_reported','ok')", (name, tee))
    x("INSERT INTO users (username,email,display_name,password_hash,is_admin,session_version) "
      "VALUES ('a@example.test','a@example.test','Admin','hash',1,1)")
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
    x("INSERT INTO player_matches (team_match_id,position_label,a_golfer_id,b_golfer_id) VALUES (?,'1',?,?)",
      (team_match, golfers[0], golfers[1]))
    return {
        "match": x("SELECT id FROM player_matches").scalar_one(),
        "golfer": golfers[0],
        "hole": x("SELECT id FROM holes").scalar_one(),
        "user": x("SELECT id FROM users").scalar_one(),
    }


def _assert_schema(engine):
    inspector = inspect(engine)
    assert {c["name"] for c in inspector.get_columns("hole_scores")} == {
        "id", "player_match_id", "golfer_id", "hole_id", "strokes", "entered_by_user_id", "entered_at",
    }
    assert {u["name"] for u in inspector.get_unique_constraints("hole_scores")} == {"uq_hole_scores_match_golfer_hole"}
    assert {i["name"] for i in inspector.get_indexes("hole_scores")} == {
        "ix_hole_scores_player_match_id", "ix_hole_scores_golfer_id",
    }
    assert {f["name"] for f in inspector.get_foreign_keys("hole_scores")} == {
        "fk_hole_scores_player_match", "fk_hole_scores_golfer", "fk_hole_scores_hole", "fk_hole_scores_user",
    }
    assert {c["name"] for c in inspector.get_check_constraints("hole_scores")} == {"ck_hole_scores_strokes"}


def _insert(connection, ids, strokes):
    connection.exec_driver_sql(
        "INSERT INTO hole_scores (player_match_id,golfer_id,hole_id,strokes,entered_by_user_id) VALUES (?,?,?,?,?)",
        (ids["match"], ids["golfer"], ids["hole"], strokes, ids["user"]),
    )


def test_fresh_upgrade_creates_table_with_enforced_constraints(tmp_path):
    url = f"sqlite:///{tmp_path / 'fresh.db'}"
    script = ScriptDirectory.from_config(make_config(url))
    assert len(script.get_heads()) == 1
    assert script.get_revision(SCORE_REVISION).down_revision == PRIOR_HEAD
    upgrade_to_head(url)
    engine = _engine(url)
    try:
        _assert_schema(engine)
        with engine.begin() as connection:
            ids = _seed(connection)
            _insert(connection, ids, 4)
            stored = connection.exec_driver_sql("SELECT entered_at FROM hole_scores").scalar_one()
            assert stored is not None
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                _insert(connection, ids, 5)  # duplicate (match, golfer, hole)
        for bad in (0, 16):
            with pytest.raises(IntegrityError):
                with engine.begin() as connection:
                    connection.exec_driver_sql("UPDATE hole_scores SET strokes=?", (bad,))
    finally:
        engine.dispose()


def test_upgrade_from_prior_head_preserves_data_and_downgrades(tmp_path):
    url = f"sqlite:///{tmp_path / 'prior.db'}"
    config = make_config(url)
    command.upgrade(config, PRIOR_HEAD)
    engine = _engine(url)
    try:
        with engine.begin() as connection:
            ids = _seed(connection)
    finally:
        engine.dispose()
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    engine = _engine(url)
    try:
        _assert_schema(engine)
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT id FROM player_matches").scalar_one() == ids["match"]
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM hole_scores").scalar_one() == 0
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()
    command.downgrade(config, PRIOR_HEAD)
    engine = create_engine(url)
    try:
        assert "hole_scores" not in inspect(engine).get_table_names()
        assert "player_matches" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
