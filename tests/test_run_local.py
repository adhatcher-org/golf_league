"""scripts/run_local.py: the local journey runner keeps its safety rails and seeds synthetic data."""

import importlib.util
import stat
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from golf_league.config import Settings
from golf_league.database import make_engine
from golf_league.models import (
    HoleScore,
    MatchScoreState,
    PlayerMatch,
    TeamMatch,
    User,
    Week,
)

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


def test_a_relay_saved_in_the_managed_file_never_replaces_the_file_sender(tmp_path, monkeypatch):
    """Journey J13 saves SMTP_HOST at /admin/config; the next local start must still not send."""
    from fastapi.testclient import TestClient

    module = _module()
    for name in ("ADMIN_EMAIL", "ADMIN_PASSWORD", "SESSION_SECRET"):
        monkeypatch.delenv(name, raising=False)
    env = module.prepare_environment(tmp_path / "local", 8000)
    with Path(env["MANAGED_ENV_PATH"]).open("a", encoding="utf-8") as handle:
        handle.write('SMTP_HOST="relay.example.test"\n')
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert Settings().smtp_host == "relay.example.test"  # the managed file really does carry it

    app = module.build_app(tmp_path / "local")
    with TestClient(app):
        assert isinstance(app.state.email_sender, module.FileMailSender)
        assert app.state.settings.smtp_host == ""


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
    assert module.seed_demo(url, today=date(2026, 10, 15)) is True
    assert module.seed_demo(url, today=date(2026, 10, 15)) is False

    engine = make_engine(url)
    try:
        with Session(engine) as session:
            users = {user.email: user for user in session.scalars(select(User))}
            assert users["admin@example.test"].is_admin
            assert set(module.PERSONAS.values()) <= set(users)
            assert users["player-unlinked@example.test"].golfer_id is None
            assert session.scalar(select(func.count(TeamMatch.id))) == 12
            assert session.scalar(select(func.count(PlayerMatch.id))) == 48
            assert session.scalar(select(func.count(PlayerMatch.id)).where(PlayerMatch.b_is_sub)) == 1
            weeks = session.scalars(select(Week).order_by(Week.index)).all()
            assert [week.week_type for week in weeks].count("match") == 8
            assert weeks[0].status == "cancelled" and weeks[9].week_type == "rain_date"
    finally:
        engine.dispose()


def test_demo_seed_adds_a_scores_week_today_with_saved_locked_and_empty_matches_once(tmp_path, monkeypatch):
    from golf_league.services.scores import player_entry_state

    module = _module()
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.test")
    monkeypatch.setenv("ADMIN_PASSWORD", "local-admin-password")
    url = f"sqlite:///{tmp_path / 'demo.db'}"
    today = date(2026, 10, 15)
    assert module.seed_demo(url, today=today) is True
    assert module.seed_demo(url, today=today) is False  # once: nothing is added a second time

    engine = make_engine(url)
    try:
        with Session(engine) as session:
            week = session.scalar(select(Week).where(Week.index == 11))
            assert (week.play_date, week.week_type, week.status, week.nine) == (today, "match", "scheduled", "front")
            regular = session.scalar(select(User).where(User.email == module.PERSONAS["regular"]))
            assert player_entry_state(session, week.id, regular.golfer_id, today).state == "open"
            matches = session.scalars(select(PlayerMatch).join(TeamMatch).where(TeamMatch.week_id == week.id)).all()
            assert len(matches) == 16
            scored = {row[0]: row[1] for row in session.execute(
                select(HoleScore.player_match_id, func.count(HoleScore.id))
                .join(PlayerMatch).join(TeamMatch).where(TeamMatch.week_id == week.id)
                .group_by(HoleScore.player_match_id)).all()}
            assert sorted(scored.values()) == [18, 18]
            locked = session.scalars(select(MatchScoreState).where(MatchScoreState.locked_at.is_not(None))).all()
            assert [state.player_match_id for state in locked] == [max(scored)]
            assert session.scalar(select(func.count(HoleScore.id))) == 36
    finally:
        engine.dispose()


def test_the_stand_in_reader_reads_the_regular_personas_latest_match(tmp_path, monkeypatch):
    module = _module()
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.test")
    monkeypatch.setenv("ADMIN_PASSWORD", "local-admin-password")
    url = f"sqlite:///{tmp_path / 'demo.db'}"
    module.seed_demo(url, today=date(2026, 10, 15))
    card = module.StandInReader(url).read_scorecard(b"", hole_count=9)
    assert [player.name for player in card.players][0] == "Demo11"
    assert len(card.players) == 2 and card.players[0].scores == (4,) * 9 and card.players[0].uncertain[0]
