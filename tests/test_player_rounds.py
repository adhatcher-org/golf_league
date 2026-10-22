"""GL-103: a linked player sees only their saved rounds."""

from datetime import UTC, date, datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from golf_league.models import (
    Course,
    Golfer,
    HoleScore,
    PlayerMatch,
    Season,
    Team,
    TeamMatch,
    User,
    Week,
    WeekHandicap,
)
from golf_league.services.auth import create_session_cookie, hash_password
from golf_league.services.player_rounds import get_player_rounds
from golf_league.services.scores import week_holes


def _as(client, golfer_id=None, *, verified=True):
    with Session(client.app.state.engine) as session:
        email = f"viewer-{golfer_id}@example.test"
        user = session.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(
                username=email, email=email, display_name="Viewer",
                password_hash=hash_password("synthetic-password"),
                golfer_id=golfer_id,
            )
            session.add(user)
        user.email_verified_at = datetime.now(UTC) if verified else None
        session.commit()
        cookie = create_session_cookie(
            user.id, user.session_version, client.app.state.settings.session_secret,
        )
    client.cookies.clear()
    client.cookies.set("session", cookie)


def _saved_week(client):
    """One scored match for the viewer and another scored match to exclude."""
    with Session(client.app.state.engine) as session:
        course = session.scalar(select(Course).where(Course.name == "Wyandot Golf Club"))
        tee_id = course.tee_sets[0].id
        season = Season(
            year=2026, course_id=course.id, start_date=date(2026, 8, 1),
            end_date=date(2026, 10, 31), play_weekday=3, status="active",
            first_week_nine="front", scoring_type="match_play",
        )
        session.add(season)
        session.flush()
        week = Week(
            season_id=season.id, index=1, play_date=date(2026, 8, 27),
            nine="front", week_type="match", status="scheduled",
        )
        session.add(week)
        session.flush()
        teams = []
        for number in (1, 2):
            team = Team(season_id=season.id, name=f"Team {number}", number=number,
                        sort_order=number)
            session.add(team)
            session.flush()
            teams.append(team.id)
        golfers = []
        for name in ("Viewer", "Opponent", "Private", "Hidden"):
            golfer = Golfer(
                first_name=name, last_name="Example",
                email=f"{name.lower()}@example.test",
                default_tee_set_id=tee_id,
                handicap_strokes=0, handicap_source="self_reported",
                handicap_status="ok",
            )
            session.add(golfer)
            session.flush()
            golfers.append(golfer.id)
        team_match = TeamMatch(
            week_id=week.id, home_team_id=teams[0], away_team_id=teams[1],
        )
        session.add(team_match)
        session.flush()
        matches = []
        for position, sides in enumerate(((golfers[0], golfers[1]),
                                          (golfers[2], golfers[3])), start=1):
            match = PlayerMatch(
                team_match_id=team_match.id, position_label=str(position),
                a_golfer_id=sides[0], b_golfer_id=sides[1],
            )
            session.add(match)
            session.flush()
            matches.append(match.id)
        for golfer_id, strokes in zip(golfers, (-2, 0, 0, 0), strict=True):
            session.add(WeekHandicap(
                week_id=week.id, golfer_id=golfer_id, tee_set_id=tee_id,
                nine="front", strokes=strokes, source="seeded",
            ))
        scorer = User(
            username="scorer@example.test", email="scorer@example.test",
            display_name="Scorer", password_hash=hash_password("synthetic-password"),
        )
        session.add(scorer)
        session.flush()
        for match_id, sides in zip(matches, ((golfers[0], golfers[1]),
                                              (golfers[2], golfers[3])), strict=True):
            for golfer_id, gross in zip(sides, (4, 5), strict=True):
                for hole in week_holes(session, week.id):
                    session.add(HoleScore(
                        player_match_id=match_id, golfer_id=golfer_id,
                        hole_id=hole.id, strokes=gross,
                        entered_by_user_id=scorer.id,
                    ))
        session.commit()
        return week.id, golfers, matches


def test_player_rounds_list_own_gross_signed_net_result_points_and_privacy(client):
    week_id, golfers, _ = _saved_week(client)
    with Session(client.app.state.engine) as session:
        assert get_player_rounds(session, golfers[0])[0].points == 18
    _as(client, golfers[0])
    response = client.get("/my/rounds")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "Week 1" in response.text
    assert "2026-08-27" in response.text
    assert "Front nine" in response.text
    assert "Against Opponent Example" in response.text
    assert "Gross 36" in response.text
    assert "Net 38" in response.text  # 36 - (-2), never 34
    assert "Won" in response.text
    assert "18 points" in response.text
    assert "Gross strokes by hole" in response.text
    assert response.text.count("<td>4</td>") == 9
    assert "Private Example" not in response.text
    assert "Hidden Example" not in response.text
    detail = client.get(f"/my/rounds/{week_id}")
    assert detail.status_code == 200
    assert detail.headers["cache-control"] == "no-store"


def test_missing_snapshot_keeps_gross_and_shows_pending(client):
    week_id, golfers, _ = _saved_week(client)
    with Session(client.app.state.engine) as session:
        session.execute(delete(WeekHandicap).where(
            WeekHandicap.week_id == week_id, WeekHandicap.golfer_id == golfers[0],
        ))
        session.commit()
    _as(client, golfers[0])
    response = client.get("/my/rounds")
    assert response.status_code == 200
    assert "Gross 36" in response.text
    assert "Net pending" in response.text
    assert "Result pending" in response.text


def test_missing_opponent_snapshot_keeps_own_net_but_result_pending(client):
    week_id, golfers, _ = _saved_week(client)
    with Session(client.app.state.engine) as session:
        session.execute(delete(WeekHandicap).where(
            WeekHandicap.week_id == week_id, WeekHandicap.golfer_id == golfers[1],
        ))
        session.commit()
    _as(client, golfers[0])
    response = client.get("/my/rounds")
    assert response.status_code == 200
    assert "Net 38" in response.text
    assert "Result pending" in response.text


def test_other_players_rounds_are_never_in_projection(client):
    week_id, golfers, _ = _saved_week(client)
    with Session(client.app.state.engine) as session:
        rounds = get_player_rounds(session, golfers[2], week_id)
    assert len(rounds) == 1
    assert rounds[0].opponent_name == "Hidden Example"
    _as(client, golfers[2])
    response = client.get(f"/my/rounds/{week_id}")
    assert response.status_code == 200
    assert "Hidden Example" in response.text
    assert "Opponent Example" not in response.text
    assert "Viewer Example" not in response.text


def test_anonymous_unverified_unlinked_and_unknown_week(client):
    week_id, golfers, _ = _saved_week(client)
    assert client.get("/my/rounds").status_code == 401
    _as(client, golfers[0], verified=False)
    assert client.get("/my/rounds").status_code == 403
    _as(client)
    assert client.get("/my/rounds").status_code == 403
    _as(client, golfers[0])
    unknown = client.get(f"/my/rounds/{week_id + 9999}")
    assert unknown.status_code == 404
    assert unknown.headers["cache-control"] == "no-store"


def test_weeks_without_player_scores_are_omitted(client):
    week_id, golfers, matches = _saved_week(client)
    with Session(client.app.state.engine) as session:
        session.execute(delete(HoleScore).where(
            HoleScore.player_match_id == matches[0],
            HoleScore.golfer_id == golfers[0],
        ))
        session.commit()
        assert get_player_rounds(session, golfers[0]) == ()
    _as(client, golfers[0])
    response = client.get(f"/my/rounds/{week_id}")
    assert response.status_code == 200
    assert "No past rounds with saved scores yet." in response.text
