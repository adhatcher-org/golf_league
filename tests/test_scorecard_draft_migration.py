"""Fresh and prior-head migration acceptance for GL-81 scorecard drafts."""

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.exc import IntegrityError

from golf_league.migrations import _make_config as make_config
from golf_league.migrations import upgrade_to_head

PRIOR_HEAD = "c3f8a1d27b45"
DRAFT_REVISION = "d7e2b9a4c618"
NOW, LATER = "2026-08-27 18:00:00.000000", "2026-08-28 18:00:00.000000"


def _engine(url):
    engine = create_engine(url)

    @event.listens_for(engine, "connect")
    def _foreign_keys(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def _seed(connection):
    """Minimum synthetic graph: a user and one week."""
    x = connection.exec_driver_sql
    x("INSERT INTO courses (name,total_holes) VALUES ('Migration Club',18)")
    course = x("SELECT id FROM courses").scalar_one()
    x("INSERT INTO users (username,email,display_name,password_hash,is_admin,session_version) "
      "VALUES ('a@example.test','a@example.test','Admin','hash',1,1)")
    x("INSERT INTO seasons (year,course_id,start_date,end_date,play_weekday,status,first_week_nine) "
      "VALUES (2026,?,'2026-08-27','2026-10-29',3,'draft','front')", (course,))
    season = x("SELECT id FROM seasons").scalar_one()
    x("INSERT INTO weeks (season_id,\"index\",play_date,nine,week_type,status) "
      "VALUES (?,1,'2026-08-27','front','match','scheduled')", (season,))
    return {"week": x("SELECT id FROM weeks").scalar_one(), "user": x("SELECT id FROM users").scalar_one()}


def _assert_schema(engine):
    inspector = inspect(engine)
    columns = {c["name"]: c for c in inspector.get_columns("scorecard_drafts")}
    assert set(columns) == {
        "id", "week_id", "created_by_user_id", "status", "model_name", "image_path", "extracted_json",
        "error_code", "created_at", "expires_at",
    }
    nullable = {name for name, column in columns.items() if column["nullable"]}
    assert nullable == {"model_name", "image_path", "extracted_json", "error_code"}
    assert {i["name"] for i in inspector.get_indexes("scorecard_drafts")} == {"ix_scorecard_drafts_week_id"}
    foreign_keys = {f["name"]: f for f in inspector.get_foreign_keys("scorecard_drafts")}
    assert set(foreign_keys) == {"fk_scorecard_drafts_week", "fk_scorecard_drafts_user"}
    assert foreign_keys["fk_scorecard_drafts_week"]["options"].get("ondelete") == "CASCADE"
    assert "ondelete" not in foreign_keys["fk_scorecard_drafts_user"]["options"]
    assert {c["name"] for c in inspector.get_check_constraints("scorecard_drafts")} == {"ck_scorecard_drafts_status"}


def _insert(connection, ids, status="extracting", **overrides):
    values = {"week": ids["week"], "user": ids["user"], **overrides}
    connection.exec_driver_sql(
        "INSERT INTO scorecard_drafts (week_id,created_by_user_id,status,created_at,expires_at) VALUES (?,?,?,?,?)",
        (values["week"], values["user"], status, NOW, LATER),
    )


def test_fresh_upgrade_creates_table_with_enforced_constraints(tmp_path):
    url = f"sqlite:///{tmp_path / 'fresh.db'}"
    script = ScriptDirectory.from_config(make_config(url))
    assert script.get_heads() == [script.get_current_head()]  # one head, read dynamically
    assert script.get_revision(DRAFT_REVISION).down_revision == PRIOR_HEAD
    upgrade_to_head(url)
    engine = _engine(url)
    try:
        _assert_schema(engine)
        with engine.begin() as connection:
            ids = _seed(connection)
            for status in ("extracting", "ready", "failed"):
                _insert(connection, ids, status)
        for bad in ({"status": "submitted"}, {"week": 999999}, {"user": 999999}):
            with pytest.raises(IntegrityError):
                with engine.begin() as connection:
                    _insert(connection, ids, **bad)
        with engine.begin() as connection:
            # Deleting the week takes its drafts with it; deleting the user is refused.
            with pytest.raises(IntegrityError):
                with connection.begin_nested():
                    connection.exec_driver_sql("DELETE FROM users")
            connection.exec_driver_sql("DELETE FROM weeks")
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM scorecard_drafts").scalar_one() == 0
    finally:
        engine.dispose()


def test_a_deleted_drafts_id_is_never_reused(tmp_path):
    """Checked on the migrated schema and on `create_all`, which the service tests use."""
    from golf_league.models import Base

    migrated, created = _engine(f"sqlite:///{tmp_path / 'm.db'}"), _engine(f"sqlite:///{tmp_path / 'c.db'}")
    upgrade_to_head(str(migrated.url))
    Base.metadata.create_all(created)
    try:
        for engine in (migrated, created):
            with engine.begin() as connection:
                ids = _seed(connection)
                _insert(connection, ids)
                first = connection.exec_driver_sql("SELECT id FROM scorecard_drafts").scalar_one()
                connection.exec_driver_sql("DELETE FROM scorecard_drafts")
                _insert(connection, ids)
                assert connection.exec_driver_sql("SELECT id FROM scorecard_drafts").scalar_one() > first
    finally:
        migrated.dispose()
        created.dispose()


def test_upgrade_from_prior_head_preserves_data_and_downgrades(tmp_path):
    url = f"sqlite:///{tmp_path / 'prior.db'}"
    config = make_config(url)
    command.upgrade(config, PRIOR_HEAD)
    engine = _engine(url)
    try:
        assert "scorecard_drafts" not in inspect(engine).get_table_names()
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
            assert connection.exec_driver_sql("SELECT id FROM weeks").scalar_one() == ids["week"]
            assert connection.exec_driver_sql("SELECT id FROM users").scalar_one() == ids["user"]
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM scorecard_drafts").scalar_one() == 0
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert "hole_scores" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
    command.downgrade(config, PRIOR_HEAD)
    engine = create_engine(url)
    try:
        tables = inspect(engine).get_table_names()
        assert "scorecard_drafts" not in tables and "hole_scores" in tables and "weeks" in tables
    finally:
        engine.dispose()


def test_model_and_migration_agree(tmp_path):
    """`create_all` (used by service tests) and Alembic build the same table."""
    from golf_league.models import Base

    migrated, created = _engine(f"sqlite:///{tmp_path / 'm.db'}"), _engine(f"sqlite:///{tmp_path / 'c.db'}")
    upgrade_to_head(str(migrated.url))
    Base.metadata.create_all(created)
    try:
        def shape(engine):
            inspector = inspect(engine)
            return (
                sorted((c["name"], str(c["type"]), c["nullable"]) for c in inspector.get_columns("scorecard_drafts")),
                sorted(c["sqltext"] for c in inspector.get_check_constraints("scorecard_drafts")),
                sorted((tuple(f["constrained_columns"]), f["referred_table"], f["options"].get("ondelete"))
                       for f in inspector.get_foreign_keys("scorecard_drafts")),
                sorted(tuple(i["column_names"]) for i in inspector.get_indexes("scorecard_drafts")),
            )
        assert shape(migrated) == shape(created)
    finally:
        migrated.dispose()
        created.dispose()
