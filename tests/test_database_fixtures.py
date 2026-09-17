from sqlalchemy import text


def test_engine_fixture_enforces_foreign_keys(engine):
    """Test that the engine fixture has foreign keys enabled."""
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_session_fixture_is_usable_and_isolated(session):
    """Test that the session fixture is usable and isolated."""
    assert session.execute(text("PRAGMA foreign_keys")).scalar() == 1
    assert session.execute(text("SELECT 1")).scalar() == 1
