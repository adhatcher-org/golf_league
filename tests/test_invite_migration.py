"""Fresh and prior-head migration coverage for shared league invitations."""

from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, inspect

from golf_league.migrations import _make_config, upgrade_to_head

PRIOR_HEAD = "6a2e9d4b7c31"
NEW_HEAD = "ab42c7d9e013"


def _engine(url):
    engine = create_engine(url)

    @event.listens_for(engine, "connect")
    def _foreign_keys(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def _assert_invite_schema(engine):
    inspector = inspect(engine)
    assert {"league_invite_links", "golfer_set_password_tokens", "invite_rate_windows"} <= set(
        inspector.get_table_names()
    )
    invite_columns = {c["name"]: c for c in inspector.get_columns("league_invite_links")}
    assert {"token_hash", "created_by_user_id", "expires_at", "revoked_at", "send_count",
            "suppressed_count", "failed_send_count", "last_sent_at",
            "require_phone_last_four"} <= invite_columns.keys()
    assert invite_columns["send_count"]["default"] in ("0", "'0'")
    assert invite_columns["require_phone_last_four"]["default"] in ("0", "'0'", "false")
    assert {fk["referred_table"] for fk in inspector.get_foreign_keys("league_invite_links")} == {"users"}
    token_fks = {fk["referred_table"] for fk in inspector.get_foreign_keys("golfer_set_password_tokens")}
    assert token_fks == {"golfers", "league_invite_links", "users"}
    rate_unique = inspector.get_pk_constraint("invite_rate_windows")["constrained_columns"]
    assert rate_unique == ["tier", "key_digest"]


def test_fresh_upgrade_has_invite_tables_one_head_and_defaults(tmp_path):
    url = f"sqlite:///{tmp_path / 'fresh-invites.db'}"
    config = _make_config(url)
    assert ScriptDirectory.from_config(config).get_heads() == [NEW_HEAD]

    upgrade_to_head(url)
    engine = _engine(url)
    try:
        _assert_invite_schema(engine)
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert connection.exec_driver_sql(
                "SELECT send_count, suppressed_count, failed_send_count, require_phone_last_four "
                "FROM league_invite_links"
            ).all() == []
    finally:
        engine.dispose()


def test_upgrade_from_prior_head_preserves_existing_identity_and_repeats_cleanly(tmp_path):
    url = f"sqlite:///{tmp_path / 'prior-invites.db'}"
    config = _make_config(url)
    command.upgrade(config, PRIOR_HEAD)
    engine = _engine(url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO courses (name,total_holes) VALUES ('Synthetic Migration Course',18)"
            )
            course_id = connection.exec_driver_sql("SELECT id FROM courses").scalar_one()
            connection.exec_driver_sql(
                "INSERT INTO tee_sets (course_id,name,color_label,gender,total_yards,sort_order) "
                "VALUES (?, 'White', 'White', 'men', 6000, 1)", (course_id,)
            )
            tee_id = connection.exec_driver_sql("SELECT id FROM tee_sets").scalar_one()
            connection.exec_driver_sql(
                "INSERT INTO golfers (first_name,last_name,email,default_tee_set_id,handicap_source,handicap_status) "
                "VALUES ('Synthetic','Golfer','synthetic@example.test',?,'self_reported','ok')", (tee_id,)
            )
            golfer_id = connection.exec_driver_sql("SELECT id FROM golfers").scalar_one()
            connection.exec_driver_sql(
                "INSERT INTO users (username,email,display_name,password_hash,is_admin,session_version) "
                "VALUES ('synthetic@example.test','synthetic@example.test','Synthetic','hash',0,1)"
            )
            user_id = connection.exec_driver_sql("SELECT id FROM users").scalar_one()
            connection.exec_driver_sql("UPDATE users SET golfer_id=? WHERE id=?", (golfer_id, user_id))
            connection.exec_driver_sql(
                "INSERT INTO user_tokens (user_id,token_digest,purpose,expires_at,created_at) "
                "VALUES (?, ?, 'reset_password', '2026-10-07 00:45:00', '2026-10-07 00:00:00')",
                (user_id, "a" * 64),
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    command.upgrade(config, "head")
    assert ScriptDirectory.from_config(config).get_heads() == [NEW_HEAD]
    engine = _engine(url)
    try:
        _assert_invite_schema(engine)
        with engine.connect() as connection:
            row = connection.exec_driver_sql(
                "SELECT username,email,display_name,password_hash,is_admin,session_version FROM users WHERE id=?",
                (user_id,),
            ).one()
            assert row == (
                "synthetic@example.test", "synthetic@example.test", "Synthetic", "hash", 0, 1
            )
            assert connection.exec_driver_sql(
                "SELECT first_name,last_name,email FROM golfers WHERE id=?", (golfer_id,)
            ).one() == ("Synthetic", "Golfer", "synthetic@example.test")
            assert connection.exec_driver_sql(
                "SELECT user_id,token_digest,purpose FROM user_tokens"
            ).one() == (user_id, "a" * 64, "reset_password")
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []

    finally:
        engine.dispose()
