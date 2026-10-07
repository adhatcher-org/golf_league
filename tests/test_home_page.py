"""Verified landing page chooses upcoming league matchups deterministically."""

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_matchup_generation import clock
from test_my_schedule import _seed

from golf_league.models import Base, Golfer, Season, TeamMatch, User, Week
from golf_league.services.matchups import create_team_match, generate_player_matches
from golf_league.services.player_schedule import get_home_view

TODAY = date(2026, 8, 27)


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return TODAY

    monkeypatch.setattr("golf_league.routers.home.date", FixedDate)


def test_anonymous_and_unverified_redirect_to_home_login(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/"
    _seed(client)
    with Session(client.app.state.engine) as session:
        session.scalar(select(User)).email_verified_at = None
        session.commit()
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/"


def test_home_all_league_matches_snapshot_handicaps_links_and_escaped_names(client):
    ids = _seed(client)
    with Session(client.app.state.engine) as session:
        pairing, _ = create_team_match(session, ids["weeks"][0], ids["teams"][2], ids["teams"][3], clock=clock)
        generate_player_matches(session, pairing.id, clock=clock)
        golfer = session.get(Golfer, ids["golfers"][0])
        golfer.first_name = "<script>alert(1)</script>"
        golfer.email = "private@example.test"
        golfer.phone = "555-0100"
        golfer.notes = "PRIVATE_IMPORT_NOTE"
        golfer.handicap_strokes = 99
        session.commit()
        before = {
            table.name: session.execute(select(table)).all()
            for table in Base.metadata.sorted_tables
        }
    page = client.get("/")
    assert page.status_code == 200
    for content in ("Week 1", "Aug 27, 2026", "Front nine", "A vs B", "C vs D", "C1 Example", "D1 Example", "Home handicap", "-1", "&lt;script&gt;alert(1)&lt;/script&gt;"):
        assert content in page.text
    assert '<script>alert(1)</script>' not in page.text
    assert f'href="/weeks/{ids["weeks"][0]}"' in page.text
    assert 'href="/my/schedule"' in page.text
    for private in ("private@example.test", "555-0100", "PRIVATE_IMPORT_NOTE", "mailto:", "tel:"):
        assert private not in page.text
    with Session(client.app.state.engine) as session:
        after = {
            table.name: session.execute(select(table)).all()
            for table in Base.metadata.sorted_tables
        }
        assert before == after


def test_next_week_skips_played_cancelled_and_past_dates(client):
    ids = _seed(client)
    with Session(client.app.state.engine) as session:
        first = session.get(Week, ids["weeks"][0])
        first.status = "played"
        second = session.get(Week, ids["weeks"][1])
        second.status = "cancelled"
        season, view = get_home_view(session, TODAY)
        assert season.id == ids["season"]
        assert view.week.id == ids["weeks"][2]
        first.status = "scheduled"
        second.status = "scheduled"
        _, view = get_home_view(session, date(2026, 9, 1))
        assert view.week.id == second.id
        _, view = get_home_view(session, TODAY)
        assert view.week.id == first.id  # Today is inclusive.
        session.commit()
        first.status = "played"
        second.status = "cancelled"
        session.commit()
    page = client.get("/")
    assert page.status_code == 200
    assert "Week 3" in page.text
    assert "Matchups have not been generated yet." in page.text


def test_date_before_index_and_index_breaks_date_ties(client):
    ids = _seed(client)
    with Session(client.app.state.engine) as session:
        first = session.get(Week, ids["weeks"][0])
        second = session.get(Week, ids["weeks"][1])
        first.play_date = date(2026, 9, 10)
        second.play_date = TODAY
        _, view = get_home_view(session, TODAY)
        assert view.week.id == second.id
        first.play_date = TODAY
        _, view = get_home_view(session, TODAY)
        assert view.week.id == first.id


def test_rain_date_requires_scheduled_matches(client):
    ids = _seed(client)
    with Session(client.app.state.engine) as session:
        rain = session.get(Week, ids["weeks"][1])
        rain.week_type = "rain_date"
        rain.nine = None
        rain.play_date = TODAY
        first = session.get(Week, ids["weeks"][0])
        first.play_date = date(2026, 9, 20)
        _, view = get_home_view(session, TODAY)
        assert view.week.id == ids["weeks"][2]
        # Pairings on an activated rain date make it eligible, without writes by GET.
        session.add(TeamMatch(week_id=rain.id, home_team_id=ids["teams"][0], away_team_id=ids["teams"][1], is_self_match=False))
        _, view = get_home_view(session, TODAY)
        assert view.week.id == rain.id
        rain.status = "cancelled"
        _, view = get_home_view(session, TODAY)
        assert view.week.id == ids["weeks"][2]
        rain.status = "played"
        _, view = get_home_view(session, TODAY)
        assert view.week.id == ids["weeks"][2]


def test_no_season_empty_state_and_home_allows_unlinked_verified_user(admin_client):
    page = admin_client.get("/")
    assert page.status_code == 200
    assert "No season is available yet." in page.text
    assert 'href="/my/schedule"' in page.text


def test_no_upcoming_week_and_ungenerated_week_empty_states(client):
    ids = _seed(client)
    with Session(client.app.state.engine) as session:
        for week in session.scalars(select(Week)):
            week.status = "played"
        session.commit()
    page = client.get("/")
    assert page.status_code == 200
    assert "No upcoming scheduled week." in page.text
    with Session(client.app.state.engine) as session:
        session.get(Week, ids["weeks"][1]).status = "scheduled"
        session.commit()
    page = client.get("/")
    assert page.status_code == 200
    assert "Week 2" in page.text
    assert "Matchups have not been generated yet." in page.text


def test_home_selects_active_season_otherwise_most_recent(client):
    ids = _seed(client)
    with Session(client.app.state.engine) as session:
        original = session.get(Season, ids["season"])
        newer = Season(year=2027, course_id=original.course_id, start_date=date(2027, 8, 26), end_date=date(2027, 10, 28), play_weekday=3, status="draft", first_week_nine="front")
        session.add(newer)
        session.flush()
        assert get_home_view(session, TODAY)[0].id == original.id
        original.status = "complete"
        season, view = get_home_view(session, TODAY)
        assert season.id == newer.id
        assert view is None
