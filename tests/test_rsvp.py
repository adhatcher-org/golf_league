"""GL-106: private player attendance and explicit admin reminders."""

from datetime import UTC, date, datetime

from conftest import _extract_csrf
from sqlalchemy import select
from sqlalchemy.orm import Session

from golf_league.models import (
    Course,
    Golfer,
    PlayerMatch,
    Season,
    SeasonGolfer,
    Team,
    TeamMatch,
    User,
    Week,
    WeekRsvp,
)
from golf_league.services.auth import create_session_cookie, hash_password
from golf_league.services.rsvp import get_rsvp, reminder_recipients, week_summary


def _setup(client):
    with Session(client.app.state.engine) as session:
        course = session.scalar(select(Course).where(Course.name == "Wyandot Golf Club"))
        season = Season(year=2026, course_id=course.id, start_date=date(2026, 8, 1),
                        end_date=date(2026, 10, 31), play_weekday=3, status="active",
                        first_week_nine="front", scoring_type="match_play")
        session.add(season)
        session.flush()
        week = Week(season_id=season.id, index=1, play_date=date(2026, 8, 27),
                    nine="front", week_type="match", status="scheduled")
        session.add(week)
        session.flush()
        golfer_ids = []
        for name, email in (("Alice", "alice@example.test"), ("Bob", "bob@example.test"),
                            ("Carol", "carol@example.test"), ("NoMail", None),
                            ("Outside", "outside@example.test")):
            golfer = Golfer(first_name=name, last_name="Example", email=email,
                            handicap_strokes=0, handicap_source="self_reported", handicap_status="ok")
            session.add(golfer)
            session.flush()
            golfer_ids.append(golfer.id)
        for golfer_id in golfer_ids[:2] + golfer_ids[3:4]:
            session.add(SeasonGolfer(season_id=season.id, golfer_id=golfer_id))
        teams = []
        for number in (1, 2):
            team = Team(season_id=season.id, name=f"Team {number}", number=number, sort_order=number)
            session.add(team)
            session.flush()
            teams.append(team.id)
        pairing = TeamMatch(week_id=week.id, home_team_id=teams[0], away_team_id=teams[1])
        session.add(pairing)
        session.flush()
        session.add(PlayerMatch(team_match_id=pairing.id, position_label="1",
                                a_golfer_id=golfer_ids[0], b_golfer_id=golfer_ids[2]))
        player = User(username="alice@example.test", email="player@example.test", display_name="Alice",
                      password_hash=hash_password("synthetic-password"), golfer_id=golfer_ids[0],
                      email_verified_at=datetime.now(UTC))
        admin = User(username="admin@example.test", email="admin@example.test", display_name="Admin",
                     password_hash=hash_password("synthetic-password"), is_admin=True,
                     email_verified_at=datetime.now(UTC))
        session.add_all((player, admin))
        session.commit()
        return week.id, golfer_ids, player.id, admin.id


def _as(client, user_id):
    with Session(client.app.state.engine) as session:
        user = session.get(User, user_id)
        cookie = create_session_cookie(user.id, user.session_version,
                                       client.app.state.settings.session_secret)
    client.cookies.clear()
    client.cookies.set("session", cookie)


def test_player_upsert_is_scoped_and_validated(client):
    week_id, golfers, player_id, _ = _setup(client)
    path = f"/weeks/{week_id}/rsvp"
    assert client.get(path).status_code == 401
    _as(client, player_id)
    page = client.get(path)
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    token = _extract_csrf(page.text)
    assert client.post(path, data={"status": "playing", "csrf_token": "bad"}).status_code == 403
    invalid = client.post(path, data={"status": "unknown", "csrf_token": token})
    assert invalid.status_code == 422
    assert "Choose playing" in invalid.text
    for status in ("playing", "maybe"):
        response = client.post(path, data={"status": status, "golfer_id": str(golfers[1]),
                                           "csrf_token": token}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["cache-control"] == "no-store"
    with Session(client.app.state.engine) as session:
        assert get_rsvp(session, week_id, golfers[0]).status == "maybe"
        assert get_rsvp(session, week_id, golfers[1]) is None
        assert len(session.scalars(select(WeekRsvp)).all()) == 1
    assert 'value="maybe" checked' in client.get(path).text


def test_admin_headcount_and_reminder_recipients(client):
    week_id, golfers, player_id, admin_id = _setup(client)
    _as(client, player_id)
    token = _extract_csrf(client.get(f"/weeks/{week_id}/rsvp").text)
    client.post(f"/weeks/{week_id}/rsvp", data={"status": "playing", "csrf_token": token})
    with Session(client.app.state.engine) as session:
        summary = week_summary(session, week_id)
        assert (summary.playing, summary.not_playing, summary.maybe, summary.no_response) == (1, 0, 0, 3)
        assert reminder_recipients(summary) == ["bob@example.test", "carol@example.test"]
        assert golfers[4] not in [row.golfer_id for row in summary.rows]
    _as(client, admin_id)
    page = client.get(f"/admin/weeks/{week_id}/scores")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert "Playing: 1" in page.text
    assert "No response: 3" in page.text
    assert "Alice Example" in page.text and "Carol Example" in page.text
    assert "Outside Example" not in page.text
    assert client.post(f"/admin/weeks/{week_id}/rsvp/reminders",
                       data={"csrf_token": "bad"}).status_code == 403
    response = client.post(f"/admin/weeks/{week_id}/rsvp/reminders",
                           data={"csrf_token": _extract_csrf(page.text)}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["cache-control"] == "no-store"
    assert [mail["to"] for mail in client.app.state.email_sender.sent] == [
        "bob@example.test", "carol@example.test",
    ]


def test_unverified_and_unlinked_denied(client):
    week_id, _, player_id, admin_id = _setup(client)
    with Session(client.app.state.engine) as session:
        session.get(User, player_id).email_verified_at = None
        session.commit()
    _as(client, player_id)
    assert client.get(f"/weeks/{week_id}/rsvp").status_code == 403
    assert client.post(f"/weeks/{week_id}/rsvp", data={}).status_code == 403
    with Session(client.app.state.engine) as session:
        session.get(User, admin_id).golfer_id = None
        session.commit()
    _as(client, admin_id)
    assert client.get(f"/weeks/{week_id}/rsvp").status_code == 403
    assert client.post(f"/weeks/{week_id}/rsvp", data={}).status_code == 403
