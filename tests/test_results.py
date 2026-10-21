"""Results navigation and access: /seasons/{id}/results and /seasons/{id}/results/weeks/{week}.

Synthetic data only.
"""

from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from golf_league.models import Course, Season, User, Week
from golf_league.services.auth import create_session_cookie, hash_password


def _engine(client):
    return client.app.state.engine


def _user(client, email, *, admin=False, verified=True) -> int:
    with Session(_engine(client)) as session:
        user = User(
            username=email, email=email,
            display_name=email.split("@")[0].title(),
            password_hash=hash_password("synthetic-password"),
            is_admin=admin,
            email_verified_at=datetime.now(UTC).replace(tzinfo=None) if verified else None,
        )
        session.add(user)
        session.commit()
        return user.id


def _as(client, user_id) -> None:
    """Log `user_id` in through a signed session cookie (bypasses the login form)."""
    with Session(_engine(client)) as session:
        user = session.get(User, user_id)
        cookie = create_session_cookie(
            user.id, user.session_version, client.app.state.settings.session_secret,
        )
    client.cookies.clear()
    client.cookies.set("session", cookie)


def _season_and_week(client) -> tuple[int, int]:
    engine = _engine(client)
    with Session(engine) as session:
        course = session.execute(
            select(Course).where(Course.name == "Wyandot Golf Club")
        ).scalar_one()
        season = Season(
            year=2026, course_id=course.id,
            start_date=date(2026, 8, 27), end_date=date(2026, 10, 29),
            play_weekday=3, status="active", first_week_nine="front",
            scoring_type="match_play",
        )
        session.add(season)
        session.flush()
        week = Week(
            season_id=season.id, index=1, play_date=date(2026, 8, 27),
            nine="front", week_type="match", status="scheduled",
        )
        session.add(week)
        session.commit()
        return season.id, week.id


def test_anonymous_season_results_is_401(client):
    season_id, _ = _season_and_week(client)
    response = client.get(f"/seasons/{season_id}/results")
    assert response.status_code == 401


def test_verified_season_results_is_200_with_no_store(client):
    season_id, _ = _season_and_week(client)
    _as(client, _user(client, "player@example.test"))
    response = client.get(f"/seasons/{season_id}/results")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def test_unknown_season_results_is_404(client):
    _as(client, _user(client, "player@example.test"))
    response = client.get("/seasons/999999/results")
    assert response.status_code == 404


def test_week_results_is_200_with_no_store(client):
    season_id, week_id = _season_and_week(client)
    _as(client, _user(client, "player@example.test"))
    response = client.get(f"/seasons/{season_id}/results/weeks/{week_id}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def test_week_not_in_season_is_404(client):
    season_id, _ = _season_and_week(client)
    _as(client, _user(client, "player@example.test"))
    engine = _engine(client)
    with Session(engine) as session:
        course = session.execute(
            select(Course).where(Course.name == "Wyandot Golf Club")
        ).scalar_one()
        other = Season(
            year=2027, course_id=course.id,
            start_date=date(2027, 1, 1), end_date=date(2027, 12, 31),
            play_weekday=3, status="active", first_week_nine="front",
            scoring_type="match_play",
        )
        session.add(other)
        session.flush()
        other_week = Week(
            season_id=other.id, index=1, play_date=date(2027, 1, 8),
            nine="front", week_type="match", status="scheduled",
        )
        session.add(other_week)
        session.commit()
        other_week_id = other_week.id
    response = client.get(f"/seasons/{season_id}/results/weeks/{other_week_id}")
    assert response.status_code == 404


def test_unknown_week_is_404(client):
    season_id, _ = _season_and_week(client)
    _as(client, _user(client, "player@example.test"))
    response = client.get(f"/seasons/{season_id}/results/weeks/999999")
    assert response.status_code == 404
