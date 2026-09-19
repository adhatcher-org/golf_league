"""Migration acceptance tests for the roster."""

from alembic import command
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from golf_league.migrations import upgrade_to_head
from golf_league.models import Golfer, User


def _engine_with_fk(database_url):
    engine = create_engine(database_url)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def test_upgrade_to_head_creates_golfers_and_the_user_golfer_foreign_key(tmp_path):
    db_path = tmp_path / "migration.db"
    database_url = f"sqlite:///{db_path}"

    upgrade_to_head(database_url)

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        assert "golfers" in table_names

        golfer_columns = {c["name"] for c in inspector.get_columns("golfers")}
        assert {
            "id", "first_name", "last_name", "email", "phone",
            "default_tee_set_id", "handicap_strokes", "handicap_source",
            "handicap_status", "handicap_index", "is_active", "notes",
            "created_at",
        } <= golfer_columns

        golfer_fks = inspector.get_foreign_keys("golfers")
        tee_set_fk = next(
            fk for fk in golfer_fks if fk["referred_table"] == "tee_sets"
        )
        assert tee_set_fk["constrained_columns"] == ["default_tee_set_id"]

        user_fks = inspector.get_foreign_keys("users")
        golfer_fk = next(fk for fk in user_fks if fk["referred_table"] == "golfers")
        assert golfer_fk["constrained_columns"] == ["golfer_id"]
    finally:
        engine.dispose()


def test_migration_is_idempotent_and_downgrades_cleanly(tmp_path):
    db_path = tmp_path / "migration2.db"
    database_url = f"sqlite:///{db_path}"

    upgrade_to_head(database_url)
    upgrade_to_head(database_url)  # idempotent: no-op on second call

    from golf_league.migrations import _make_config as make_config

    config = make_config(database_url)
    command.downgrade(config, "a3f9c1d2e4b6")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        assert "golfers" not in table_names

        user_fks = inspector.get_foreign_keys("users")
        assert not any(fk["referred_table"] == "golfers" for fk in user_fks)
    finally:
        engine.dispose()


def test_two_users_cannot_link_to_the_same_golfer(tmp_path):
    db_path = tmp_path / "migration3.db"
    database_url = f"sqlite:///{db_path}"

    upgrade_to_head(database_url)

    engine = _engine_with_fk(database_url)
    try:
        session = Session(bind=engine)
        try:
            golfer = Golfer(
                first_name="Shared",
                last_name="Golfer",
                default_tee_set_id=_seed_tee_set_id(session),
                handicap_source="self_reported",
                handicap_status="needs_contact",
            )
            session.add(golfer)
            session.commit()
            session.refresh(golfer)

            user_one = User(
                username="one@example.test",
                email="one@example.test",
                display_name="One",
                password_hash="x",
                golfer_id=golfer.id,
            )
            session.add(user_one)
            session.commit()

            user_two = User(
                username="two@example.test",
                email="two@example.test",
                display_name="Two",
                password_hash="x",
                golfer_id=golfer.id,
            )
            session.add(user_two)
            try:
                session.commit()
                raise AssertionError("expected IntegrityError for duplicate golfer_id")
            except IntegrityError:
                session.rollback()
        finally:
            session.close()
    finally:
        engine.dispose()


def test_a_user_may_link_to_no_golfer(tmp_path):
    db_path = tmp_path / "migration4.db"
    database_url = f"sqlite:///{db_path}"

    upgrade_to_head(database_url)

    engine = _engine_with_fk(database_url)
    try:
        session = Session(bind=engine)
        try:
            user = User(
                username="solo@example.test",
                email="solo@example.test",
                display_name="Solo",
                password_hash="x",
                golfer_id=None,
            )
            session.add(user)
            session.commit()
            session.refresh(user)
            assert user.golfer_id is None
        finally:
            session.close()
    finally:
        engine.dispose()


def _seed_tee_set_id(session) -> int:
    """Insert a minimal course/tee_set pair and return the tee_set id."""
    from golf_league.models import Course, TeeSet

    course = Course(name="Migration Test Club", total_holes=18)
    session.add(course)
    session.flush()
    tee_set = TeeSet(
        course_id=course.id,
        name="Deer",
        color_label="White",
        gender="men",
        total_yards=5000,
        sort_order=1,
    )
    session.add(tee_set)
    session.flush()
    return tee_set.id
