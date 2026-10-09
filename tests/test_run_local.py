"""scripts/run_local.py: the local journey runner keeps its safety rails and seeds synthetic data."""

import importlib.util
import stat
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from golf_league.config import Settings
from golf_league.database import make_engine
from golf_league.models import PlayerMatch, TeamMatch, User, Week

ROOT = Path(__file__).resolve().parent.parent


def _module():
    spec = importlib.util.spec_from_file_location("run_local", ROOT / "scripts" / "run_local.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("inside", [".", "data", "tmp/nested"])
def test_refuses_a_data_dir_inside_the_repository(inside):
    with pytest.raises(SystemExit, match="inside the repository"):
        _module().prepare_environment(ROOT / inside, 8000)


def test_refuses_to_run_with_a_relay_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "relay.example.test")
    with pytest.raises(SystemExit, match="SMTP_HOST"):
        _module().prepare_environment(tmp_path / "local", 8000)


def test_environment_is_local_http_with_a_private_generated_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    env = _module().prepare_environment(tmp_path / "local", 8123)
    managed = Path(env["MANAGED_ENV_PATH"])
    assert stat.S_IMODE(managed.stat().st_mode) == 0o600
    assert env["EXTERNAL_BASE_URL"] == "http://127.0.0.1:8123"
    assert env["ADMIN_EMAIL"] == "admin@example.test"
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    settings = Settings()
    assert settings.session_secret and settings.external_base_url == "http://127.0.0.1:8123"


def test_file_mail_sender_writes_private_files(tmp_path):
    sender = _module().FileMailSender(tmp_path / "mail")
    sender.send("someone@example.test", "Reset your password", "http://127.0.0.1:8000/reset/x")
    (written,) = (tmp_path / "mail").iterdir()
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    assert "http://127.0.0.1:8000/reset/x" in written.read_text(encoding="utf-8")


def test_demo_seed_creates_personas_admin_and_pairings_once(tmp_path, monkeypatch):
    module = _module()
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.test")
    monkeypatch.setenv("ADMIN_PASSWORD", "local-admin-password")
    url = f"sqlite:///{tmp_path / 'demo.db'}"
    assert module.seed_demo(url) is True
    assert module.seed_demo(url) is False

    engine = make_engine(url)
    try:
        with Session(engine) as session:
            users = {user.email: user for user in session.scalars(select(User))}
            assert users["admin@example.test"].is_admin
            assert set(module.PERSONAS.values()) <= set(users)
            assert users["player-unlinked@example.test"].golfer_id is None
            assert session.scalar(select(func.count(TeamMatch.id))) == 8
            assert session.scalar(select(func.count(PlayerMatch.id))) == 32
            assert session.scalar(select(func.count(PlayerMatch.id)).where(PlayerMatch.b_is_sub)) == 1
            weeks = session.scalars(select(Week).order_by(Week.index)).all()
            assert [week.week_type for week in weeks].count("match") == 7
            assert weeks[0].status == "cancelled" and weeks[-1].week_type == "rain_date"
    finally:
        engine.dispose()
