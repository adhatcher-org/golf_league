"""Fresh and prior-head upgrade checks for M13 request and RSVP tables."""

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.exc import IntegrityError

from golf_league.migrations import _make_config, current_revision, upgrade_to_head
from golf_league.models import Base

PRIOR_HEAD = "cfe980076271c31cba6dd6c4"
REQUEST_REVISION = "ab74c5d6e105"
RSVP_REVISION = "b4e73c9a1062"


def _engine(url):
    engine = create_engine(url)

    @event.listens_for(engine, "connect")
    def _foreign_keys(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def _shape(engine, table):
    inspector = inspect(engine)
    return (
        sorted((c["name"], str(c["type"]), c["nullable"]) for c in inspector.get_columns(table)),
        sorted(c["name"] for c in inspector.get_check_constraints(table)),
        sorted((tuple(f["constrained_columns"]), f["referred_table"])
               for f in inspector.get_foreign_keys(table)),
        sorted(tuple(u["column_names"]) for u in inspector.get_unique_constraints(table)),
    )


def test_m13_fresh_and_prior_head_upgrades_preserve_data(tmp_path):
    fresh_url = f"sqlite:///{tmp_path / 'fresh.db'}"
    prior_url = f"sqlite:///{tmp_path / 'prior.db'}"
    script = ScriptDirectory.from_config(_make_config(fresh_url))
    assert script.get_heads() == [RSVP_REVISION]
    assert script.get_revision(REQUEST_REVISION).down_revision == PRIOR_HEAD
    assert script.get_revision(RSVP_REVISION).down_revision == REQUEST_REVISION

    upgrade_to_head(fresh_url)
    upgrade_to_head(fresh_url)
    assert current_revision(fresh_url) == RSVP_REVISION

    command.upgrade(_make_config(prior_url), PRIOR_HEAD)
    prior_engine = _engine(prior_url)
    with prior_engine.begin() as connection:
        connection.exec_driver_sql("INSERT INTO courses (name,total_holes) VALUES ('M13 Test',18)")
        connection.exec_driver_sql(
            "INSERT INTO golfers (first_name,last_name,handicap_source,handicap_status) "
            "VALUES ('Prior','Golfer','seeded','ok')"
        )
        connection.exec_driver_sql(
            "INSERT INTO seasons (year,course_id,start_date,end_date,first_week_nine) "
            "VALUES (2026,1,'2026-01-01','2026-12-31','front')"
        )
        connection.exec_driver_sql(
            'INSERT INTO weeks (season_id,"index",play_date,nine) '
            "VALUES (1,1,'2026-03-15','front')"
        )
    prior_engine.dispose()

    upgrade_to_head(prior_url)
    assert current_revision(prior_url) == RSVP_REVISION
    migrated = _engine(prior_url)
    created = _engine(f"sqlite:///{tmp_path / 'created.db'}")
    Base.metadata.create_all(created)
    try:
        for table in ("substitute_requests", "week_rsvps"):
            assert _shape(migrated, table) == _shape(created, table)
        with migrated.begin() as connection:
            assert connection.exec_driver_sql(
                "SELECT first_name FROM golfers WHERE id=1"
            ).scalar_one() == "Prior"
            connection.exec_driver_sql(
                "INSERT INTO week_rsvps (week_id,golfer_id,status) VALUES (1,1,'playing')"
            )
        for statement in (
            "INSERT INTO week_rsvps (week_id,golfer_id,status) VALUES (1,1,'maybe')",
            "INSERT INTO week_rsvps (week_id,golfer_id,status) VALUES (1,999,'playing')",
            "INSERT INTO week_rsvps (week_id,golfer_id,status) VALUES (1,1,'invalid')",
        ):
            with pytest.raises(IntegrityError):
                with migrated.begin() as connection:
                    connection.exec_driver_sql(statement)
    finally:
        migrated.dispose()
        created.dispose()
