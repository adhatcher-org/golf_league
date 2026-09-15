"""Milestone M0 exit-criteria tests: "Empty repo -> make check, Docker health demo".

These tests exercise the application's real entry points (`Settings`,
`create_app`, Alembic migrations) exactly as they run in a genuinely empty
checkout: no `.env`, no `data/` directory, and `SESSION_SECRET` absent from
the process environment. They are fast, use only temporary SQLite files, and
are meant to run as part of `make check`.

The Docker half of the exit criterion ("Docker health demo") is deliberately
NOT exercised here: it needs `docker compose` and a real container network,
which does not belong inside `make check`. That half is covered by the
standalone script `tests/milestones/M0/docker_health_demo.sh`, whose output is
recorded as evidence in the milestone test report instead.
"""

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from pydantic import ValidationError

from golf_league.app import create_app
from golf_league.config import Settings
from golf_league.migrations import current_revision, upgrade_to_head

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MIGRATIONS_DIR = _REPO_ROOT / "migrations"


def _head_revision() -> str:
    """Return the Alembic head revision from the committed migration scripts."""
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS_DIR))
    return ScriptDirectory.from_config(config).get_current_head()


def _clear_ambient_app_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate the empty-repo environment: no SESSION_SECRET, no DATABASE_URL."""
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)


def test_settings_require_session_secret_with_no_ambient_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh checkout has no `.env` and no ambient SESSION_SECRET.

    `Settings()` must fail closed (raise) instead of silently making up a
    secret, since the repo's `Settings.session_secret` has no default. This
    is what makes `docker-compose.yml`'s
    `SESSION_SECRET=${SESSION_SECRET:?...}` and the "empty repo" scenario
    meaningful: a missing secret is a hard configuration error, not a silent
    default.
    """
    _clear_ambient_app_env(monkeypatch)

    with pytest.raises(ValidationError):
        Settings()


def test_migrations_upgrade_a_fresh_temporary_database_to_head(
    tmp_path: Path,
) -> None:
    """Alembic migrations apply cleanly to a brand new database file.

    This is what `make check` and container startup both rely on: a fresh
    SQLite file (no prior `data/`) reaches the same head revision recorded
    in the committed migration scripts.
    """
    db_path = tmp_path / "fresh.db"
    database_url = f"sqlite:///{db_path}"

    assert not db_path.exists()

    upgrade_to_head(database_url)

    assert db_path.exists()
    assert current_revision(database_url) == _head_revision()

    # Safe to call twice: startup may run it again without side effects.
    upgrade_to_head(database_url)
    assert current_revision(database_url) == _head_revision()


def test_healthz_and_readyz_ok_end_to_end_with_no_ambient_session_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full app wiring succeeds against a fresh database with an explicit secret.

    Exercises the same path the container uses: config -> engine ->
    migrations -> FastAPI lifespan -> health endpoints, through the real
    `create_app` entry point and a real HTTP client, with no ambient
    SESSION_SECRET in the process environment (the secret is supplied
    explicitly, the way `docker-compose.yml` supplies it via `.env`).
    """
    _clear_ambient_app_env(monkeypatch)

    db_path = tmp_path / "app.db"
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        session_secret="milestone-test-secret-not-a-real-secret",
    )

    with TestClient(create_app(settings=settings)) as client:
        healthz = client.get("/healthz")
        assert healthz.status_code == 200
        assert healthz.json() == {"status": "ok"}

        readyz = client.get("/readyz")
        assert readyz.status_code == 200
        assert readyz.json() == {"status": "ok"}

    assert db_path.exists()
    assert current_revision(f"sqlite:///{db_path}") == _head_revision()


def test_readyz_reports_503_when_the_database_is_unreachable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/readyz` fails honestly when migrations cannot reach the database.

    GL-02's contract: `/readyz` is 503 "when migrations failed, or the
    database is unreachable", while `/healthz` keeps answering regardless.
    This exercises that distinction end to end, still with no ambient
    SESSION_SECRET, using a database path whose parent is a file so SQLite
    genuinely cannot open it (as opposed to a missing directory, which the
    app is expected to create).
    """
    _clear_ambient_app_env(monkeypatch)

    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory\n")
    settings = Settings(
        database_url=f"sqlite:///{blocker}/app.db",
        session_secret="milestone-test-secret-not-a-real-secret",
    )

    with TestClient(create_app(settings=settings)) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 503
