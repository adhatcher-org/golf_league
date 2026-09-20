"""Migration acceptance tests for the roster import tables (GL-11)."""

from datetime import datetime

from alembic import command
from sqlalchemy import create_engine, event, insert, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from golf_league.migrations import _make_config, upgrade_to_head
from golf_league.models import Course, RosterImportBatch, RosterImportRow, TeeSet, User


def _engine_with_fk(database_url):
    engine = create_engine(database_url)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def test_upgrade_to_head_creates_both_import_tables_with_their_columns(tmp_path):
    db_path = tmp_path / "migration.db"
    database_url = f"sqlite:///{db_path}"

    upgrade_to_head(database_url)

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        assert "roster_import_batches" in table_names
        assert "roster_import_rows" in table_names

        batch_columns = {c["name"] for c in inspector.get_columns("roster_import_batches")}
        assert batch_columns == {
            "id", "created_by_user_id", "course_id", "version", "state",
            "is_initial", "source_display_name", "row_count", "created_count",
            "updated_count", "unchanged_count", "skipped_count", "created_at",
            "applied_at", "expires_at",
        }

        row_columns = {c["name"] for c in inspector.get_columns("roster_import_rows")}
        assert row_columns == {
            "id", "batch_id", "position", "version", "first_name", "last_name",
            "email", "phone", "tee_label", "handicap_gold", "handicap_white",
            "handicap_single", "source_role", "source_file", "source_row",
            "raw_line", "included", "update_opt_in", "validation_error", "warnings",
        }
    finally:
        engine.dispose()


def test_batch_and_row_foreign_keys_point_at_users_courses_and_batches(tmp_path):
    db_path = tmp_path / "migration2.db"
    database_url = f"sqlite:///{db_path}"

    upgrade_to_head(database_url)

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)

        batch_fks = inspector.get_foreign_keys("roster_import_batches")
        referred = {fk["referred_table"] for fk in batch_fks}
        assert referred == {"users", "courses"}
        user_fk = next(fk for fk in batch_fks if fk["referred_table"] == "users")
        assert user_fk["constrained_columns"] == ["created_by_user_id"]
        course_fk = next(fk for fk in batch_fks if fk["referred_table"] == "courses")
        assert course_fk["constrained_columns"] == ["course_id"]

        row_fks = inspector.get_foreign_keys("roster_import_rows")
        batch_fk = next(fk for fk in row_fks if fk["referred_table"] == "roster_import_batches")
        assert batch_fk["constrained_columns"] == ["batch_id"]
    finally:
        engine.dispose()


def test_two_rows_of_one_batch_cannot_share_a_position(tmp_path):
    db_path = tmp_path / "migration3.db"
    database_url = f"sqlite:///{db_path}"

    upgrade_to_head(database_url)

    engine = _engine_with_fk(database_url)
    try:
        session = Session(bind=engine)
        try:
            course = Course(name="Migration Course", total_holes=18)
            session.add(course)
            session.flush()
            tee_set = TeeSet(
                course_id=course.id, name="Deer", color_label="White",
                gender="men", total_yards=5000, sort_order=1,
            )
            session.add(tee_set)
            user = User(
                username="admin@example.test", email="admin@example.test",
                display_name="Admin", password_hash="x", is_admin=True,
            )
            session.add(user)
            session.flush()

            batch = RosterImportBatch(
                created_by_user_id=user.id,
                course_id=course.id,
                version=1,
                state="staged",
                is_initial=False,
                source_display_name="roster.csv",
                row_count=2,
                created_count=0,
                updated_count=0,
                unchanged_count=0,
                skipped_count=0,
                created_at=datetime(2026, 9, 20),
                expires_at=datetime(2026, 12, 19),
            )
            session.add(batch)
            session.flush()

            session.execute(
                insert(RosterImportRow),
                [
                    {
                        "batch_id": batch.id, "position": 1, "version": 1,
                        "first_name": "Ann", "last_name": "Example",
                        "email": "ann@example.test", "phone": None,
                        "tee_label": "Gold", "handicap_gold": 5,
                        "handicap_white": None, "handicap_single": None,
                        "source_role": "summer_regular", "source_file": "r.csv",
                        "source_row": 1, "raw_line": "raw1", "included": True,
                        "update_opt_in": False, "validation_error": None,
                        "warnings": "",
                    }
                ],
            )
            session.commit()

            try:
                session.execute(
                    insert(RosterImportRow),
                    [
                        {
                            "batch_id": batch.id, "position": 1, "version": 1,
                            "first_name": "Bob", "last_name": "Sample",
                            "email": "bob@example.test", "phone": None,
                            "tee_label": "White", "handicap_gold": None,
                            "handicap_white": 9, "handicap_single": None,
                            "source_role": "summer_regular", "source_file": "r.csv",
                            "source_row": 2, "raw_line": "raw2", "included": True,
                            "update_opt_in": False, "validation_error": None,
                            "warnings": "",
                        }
                    ],
                )
                session.commit()
                raise AssertionError("expected IntegrityError for duplicate position")
            except IntegrityError:
                session.rollback()
        finally:
            session.close()
    finally:
        engine.dispose()


def test_migration_is_idempotent_and_downgrades_cleanly(tmp_path):
    db_path = tmp_path / "migration4.db"
    database_url = f"sqlite:///{db_path}"

    upgrade_to_head(database_url)
    upgrade_to_head(database_url)  # idempotent: no-op on second call

    config = _make_config(database_url)
    command.downgrade(config, "e5a1c3d9f2b7")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        assert "roster_import_batches" not in table_names
        assert "roster_import_rows" not in table_names
        # Downgrading this revision must not disturb the roster tables
        # it builds on.
        assert "golfers" in table_names
    finally:
        engine.dispose()

    # And upgrading again from the downgraded state must succeed cleanly.
    upgrade_to_head(database_url)
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        assert "roster_import_batches" in table_names
        assert "roster_import_rows" in table_names
    finally:
        engine.dispose()
