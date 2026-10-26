"""Route and service tests for GL-104: Handicap trend view."""

import re
from datetime import date, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from golf_league.models import Course, Golfer, Season, TeeSet, User, Week, WeekHandicap
from golf_league.services.auth import hash_password
from golf_league.services.handicaps import get_handicap_trend


def _login(client: TestClient, email: str, password: str) -> None:
    login_page = client.get("/login")
    match = re.search(r'name="csrf_token" value="([^"]*)"', login_page.text)
    assert match, "no csrf_token field found"
    csrf_token = match.group(1)
    response = client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303


class TestHandicapTrendService:
    """Service layer tests for handicap trend."""

    def test_get_handicap_trend_empty(self, session: Session):
        """Empty trend when no handicaps exist."""
        golfer = Golfer(first_name="John", last_name="Doe", handicap_source="seeded", handicap_status="ok")
        session.add(golfer)
        session.commit()

        trend = get_handicap_trend(session, golfer.id)
        assert trend is not None
        assert trend.golfer_id == golfer.id
        assert trend.golfer_name == "John Doe"
        assert len(trend.weeks) == 0

    def test_get_handicap_trend_with_data(self, session: Session):
        """Trend shows stored handicaps with source labels."""
        course = Course(name="Trend Test Course")
        session.add(course)
        session.flush()
        tee_set = TeeSet(course_id=course.id, name="Test Tee", color_label="White", gender="open", total_yards=3000, sort_order=1)
        session.add(tee_set)
        session.commit()

        golfer = Golfer(first_name="Jane", last_name="Smith", handicap_source="seeded", handicap_status="ok")
        other_golfer = Golfer(first_name="Other", last_name="Golfer", handicap_source="seeded", handicap_status="ok")
        season = Season(year=2026, course_id=course.id, start_date=date(2026, 1, 1),
                        end_date=date(2026, 12, 31), first_week_nine="front", status="active")
        session.add_all([golfer, other_golfer, season])
        session.commit()

        week1 = Week(season_id=season.id, index=1, play_date=date(2026, 3, 15), nine="front")
        week2 = Week(season_id=season.id, index=2, play_date=date(2026, 3, 22), nine="back")
        session.add_all([week1, week2])
        session.commit()

        h1 = WeekHandicap(week_id=week1.id, golfer_id=golfer.id, tee_set_id=tee_set.id, nine="front", strokes=12, source="seeded")
        h2 = WeekHandicap(week_id=week2.id, golfer_id=golfer.id, tee_set_id=tee_set.id, nine="back", strokes=11, source="carried")
        other_handicap = WeekHandicap(week_id=week1.id, golfer_id=other_golfer.id,
                                     tee_set_id=tee_set.id, nine="front", strokes=8, source="seeded")
        session.add_all([h1, h2, other_handicap])
        session.commit()

        trend = get_handicap_trend(session, golfer.id)
        assert trend is not None
        assert len(trend.weeks) == 2
        assert trend.weeks[0].week_id == week1.id
        assert trend.weeks[0].week_date == "Mar 15, 2026"
        assert trend.weeks[0].week_index == 1
        assert trend.weeks[0].nine == "front"
        assert trend.weeks[0].strokes == 12
        assert trend.weeks[0].source == "seeded"
        assert trend.weeks[1].week_id == week2.id
        assert trend.weeks[1].week_date == "Mar 22, 2026"
        assert trend.weeks[1].week_index == 2
        assert trend.weeks[1].nine == "back"
        assert trend.weeks[1].strokes == 11
        assert trend.weeks[1].source == "carried"

    def test_get_handicap_trend_nonexistent_golfer(self, session: Session):
        """Returns None for a golfer that does not exist."""
        trend = get_handicap_trend(session, 99999)
        assert trend is None

    def test_get_handicap_trend_by_season(self, session: Session):
        """Filters trend by season when season_id is provided."""
        course = Course(name="Trend Test Course")
        session.add(course)
        session.flush()
        tee_set = TeeSet(course_id=course.id, name="Test Tee", color_label="White", gender="open", total_yards=3000, sort_order=1)
        session.add(tee_set)
        session.commit()

        golfer = Golfer(first_name="Bob", last_name="Jones", handicap_source="seeded", handicap_status="ok")
        season1 = Season(year=2025, course_id=course.id, start_date=date(2025, 1, 1),
                         end_date=date(2025, 12, 31), first_week_nine="front", status="complete")
        season2 = Season(year=2026, course_id=course.id, start_date=date(2026, 1, 1),
                         end_date=date(2026, 12, 31), first_week_nine="front", status="active")
        session.add_all([golfer, season1, season2])
        session.commit()

        w1 = Week(season_id=season1.id, index=1, play_date=date(2025, 3, 15), nine="front")
        w2 = Week(season_id=season2.id, index=1, play_date=date(2026, 3, 15), nine="front")
        session.add_all([w1, w2])
        session.commit()

        h1 = WeekHandicap(week_id=w1.id, golfer_id=golfer.id, tee_set_id=tee_set.id, nine="front", strokes=15, source="seeded")
        h2 = WeekHandicap(week_id=w2.id, golfer_id=golfer.id, tee_set_id=tee_set.id, nine="front", strokes=14, source="seeded")
        session.add_all([h1, h2])
        session.commit()

        trend_all = get_handicap_trend(session, golfer.id)
        assert len(trend_all.weeks) == 2

        trend_2026 = get_handicap_trend(session, golfer.id, season_id=season2.id)
        assert len(trend_2026.weeks) == 1
        assert trend_2026.weeks[0].week_id == w2.id


class TestHandicapTrendRoutes:
    """Route tests for handicap trend view."""

    def test_my_handicap_trend_unauthenticated(self, client: TestClient):
        """Unauthenticated users get 401."""
        response = client.get("/handicaps/trend", follow_redirects=False)
        assert response.status_code == 401

    def test_my_handicap_trend_unlinked_403(self, client: TestClient):
        """A verified user not linked to a golfer gets 403."""
        with Session(client.app.state.engine) as session:
            u = User(
                username="nolink@example.test",
                email="nolink@example.test",
                display_name="NoLink",
                password_hash=hash_password("pw!"),
                email_verified_at=datetime(2024, 1, 1),
                golfer_id=None,
                is_admin=False,
            )
            session.add(u)
            session.commit()

        _login(client, "nolink@example.test", "pw!")
        response = client.get("/handicaps/trend", follow_redirects=False)
        assert response.status_code == 403

    def test_golfer_handicap_trend_not_found(self, admin_client: TestClient):
        """Returns 404 for non-existent golfer."""
        response = admin_client.get("/handicaps/99999")
        assert response.status_code == 404

    def test_admin_handicap_trend_requires_admin(self, client: TestClient):
        """Non-admin users get 403 for admin route."""
        with Session(client.app.state.engine) as session:
            u = User(
                username="player@example.test",
                email="player@example.test",
                display_name="Player",
                password_hash=hash_password("pw!"),
                email_verified_at=datetime(2024, 1, 1),
                golfer_id=None,
                is_admin=False,
            )
            session.add(u)
            session.commit()

        _login(client, "player@example.test", "pw!")
        response = client.get("/handicaps/1", follow_redirects=False)
        assert response.status_code == 403
