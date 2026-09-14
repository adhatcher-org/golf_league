from sqlalchemy import text


def test_probe_engine_fk(engine):
    with engine.connect() as conn:
        result = conn.execute(text("PRAGMA foreign_keys")).scalar()
        assert result == 1

def test_probe_session(session):
    result = session.execute(text("PRAGMA foreign_keys")).scalar()
    assert result == 1
