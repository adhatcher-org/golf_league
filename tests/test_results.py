"""Results navigation and access: /seasons/{id}/results and /seasons/{id}/results/weeks/{week}.

Synthetic data only.
"""

from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from golf_league.models import (
    Course,
    Golfer,
    HoleScore,
    PlayerMatch,
    Season,
    SeasonGolfer,
    Team,
    TeamMatch,
    TeamMember,
    User,
    Week,
    WeekHandicap,
)
from golf_league.services.auth import create_session_cookie, hash_password
from golf_league.services.scores import week_holes


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


def _season_week_course(client) -> tuple[int, int, int]:
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
        return season.id, week.id, course.id


def _tee_set_id(client) -> int:
    engine = _engine(client)
    with Session(engine) as session:
        course = session.execute(
            select(Course).where(Course.name == "Wyandot Golf Club")
        ).scalar_one()
        return next(t.id for t in course.tee_sets if t.name == "Deer")


def _complete_week(client, season_id, week_id, tee_set_id, complete=True):
    """Two teams (A/B), two golfers each, one team match with two player matches,
    week handicaps, and (unless complete is False) a full card for each side.

    Match play with every handicap at zero: an all-4 side beats an all-5 side
    20 points to 0. Returns (teams, golfers, player_match_ids).
    """
    engine = _engine(client)
    with Session(engine) as session:
        teams, golfers = [], []
        for label, number in (("A", 1), ("B", 2)):
            team = Team(season_id=season_id, name=label, number=number, sort_order=number)
            session.add(team)
            session.flush()
            teams.append(team.id)
            for position in (1, 2):
                golfer = Golfer(
                    first_name=f"{label}{position}", last_name="Example",
                    email=f"{label}{position}@example.test",
                    default_tee_set_id=tee_set_id,
                    handicap_strokes=0, handicap_source="self_reported",
                    handicap_status="ok",
                )
                session.add(golfer)
                session.flush()
                golfers.append(golfer.id)
                session.add(SeasonGolfer(season_id=season_id, golfer_id=golfer.id))
                session.add(TeamMember(
                    team_id=team.id, season_id=season_id, golfer_id=golfer.id,
                    position=position,
                ))
        session.flush()

        team_match = TeamMatch(week_id=week_id, home_team_id=teams[0], away_team_id=teams[1])
        session.add(team_match)
        session.flush()
        player_match_ids = []
        for position, (a_id, b_id) in enumerate(((golfers[0], golfers[2]), (golfers[1], golfers[3])), start=1):
            pm = PlayerMatch(
                team_match_id=team_match.id, position_label=str(position),
                a_golfer_id=a_id, b_golfer_id=b_id,
            )
            session.add(pm)
            session.flush()
            player_match_ids.append(pm.id)

        for golfer_id in golfers:
            session.add(WeekHandicap(
                week_id=week_id, golfer_id=golfer_id, tee_set_id=tee_set_id,
                nine="front", strokes=0, source="seeded",
            ))

        if complete:
            holes = week_holes(session, week_id)
            hole_by_number = {hole.number: hole.id for hole in holes}
            scorer = User(
                username="scorer@example.test", email="scorer@example.test",
                display_name="Scorer", password_hash=hash_password("synthetic-password"),
            )
            session.add(scorer)
            session.flush()
            for pm_id in player_match_ids:
                pm = session.get(PlayerMatch, pm_id)
                for golfer_id in (pm.a_golfer_id, pm.b_golfer_id):
                    strokes = 4 if golfer_id in (golfers[0], golfers[1]) else 5
                    for _number, hole_id in hole_by_number.items():
                        session.add(HoleScore(
                            player_match_id=pm_id, golfer_id=golfer_id,
                            hole_id=hole_id, strokes=strokes,
                            entered_by_user_id=scorer.id,
                        ))
        session.commit()
    return teams, golfers, player_match_ids


def test_anonymous_week_results_is_401(client):
    season_id, week_id, _ = _season_week_course(client)
    response = client.get(f"/seasons/{season_id}/results/weeks/{week_id}")
    assert response.status_code == 401


def test_week_results_shows_leaders_points_and_ranks(client):
    season_id, week_id, _ = _season_week_course(client)
    teams, golfers, _ = _complete_week(client, season_id, week_id, _tee_set_id(client))
    _as(client, _user(client, "player@example.test"))
    response = client.get(f"/seasons/{season_id}/results/weeks/{week_id}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    text = response.text
    # Side A (all 4s) beats side B (all 5s) in both matches: 20 points each.
    assert "A1 Example" in text and "A2 Example" in text
    assert "B1 Example" in text and "B2 Example" in text
    # Tied leaders share rank 1; the two zero-point golfers share rank 3.
    assert text.count("A1 Example") == 1 and text.count("A2 Example") == 1
    # Team A = 40 across two matches, team B = 0.
    assert "<td>A</td><td>40</td>" in text and "<td>B</td><td>0</td>" in text


def test_week_results_only_complete_matches_contribute(client):
    season_id, week_id, _ = _season_week_course(client)
    tee_set_id = _tee_set_id(client)
    teams, golfers, player_match_ids = _complete_week(client, season_id, week_id, tee_set_id)
    # Make match 2's away side incomplete: the match is pending and contributes nothing.
    engine = _engine(client)
    with Session(engine) as session:
        session.execute(
            HoleScore.__table__.delete().where(
                HoleScore.player_match_id == player_match_ids[1],
                HoleScore.golfer_id == golfers[3],
            )
        )
        session.commit()
    _as(client, _user(client, "player@example.test"))
    response = client.get(f"/seasons/{season_id}/results/weeks/{week_id}")
    assert response.status_code == 200
    text = response.text
    # Match 1's home side scored 20; match 2 is pending so its golfers earn nothing.
    assert "A1 Example" in text and "20" in text
    assert "A2 Example" not in text and "B2 Example" not in text


def test_week_results_reconciles_player_and_team_points(client):
    season_id, week_id, _ = _season_week_course(client)
    _complete_week(client, season_id, week_id, _tee_set_id(client))
    _as(client, _user(client, "player@example.test"))
    response = client.get(f"/seasons/{season_id}/results/weeks/{week_id}")
    assert response.status_code == 200
    text = response.text
    # Every complete match credits its points to both a player and a team, so
    # the week's player points and team points must agree (reconcile with GL-98).
    import re
    # The table renders rank, name, points, matches per row; grab the points cell.
    points = [int(m) for m in re.findall(r"<td>(?:A|B)\d Example</td><td>(\d+)</td>", text)]
    assert sum(points) == 40  # 20 + 20 + 0 + 0


def test_week_results_cancelled_week_empty_state(client):
    season_id, week_id, _ = _season_week_course(client)
    engine = _engine(client)
    with Session(engine) as session:
        session.get(Week, week_id).status = "cancelled"
        session.commit()
    _as(client, _user(client, "player@example.test"))
    response = client.get(f"/seasons/{season_id}/results/weeks/{week_id}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "Week cancelled" in response.text
    assert "no matches were played" in response.text


def test_week_results_rain_date_ungenerated_week_empty_state(client):
    season_id, week_id, _ = _season_week_course(client)
    engine = _engine(client)
    with Session(engine) as session:
        rain_date = Week(
            season_id=season_id, index=2, play_date=date(2026, 9, 3),
            nine=None, week_type="rain_date", status="scheduled",
        )
        session.add(rain_date)
        session.commit()
        rain_week_id = rain_date.id
    _as(client, _user(client, "player@example.test"))
    response = client.get(f"/seasons/{season_id}/results/weeks/{rain_week_id}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "Matches not yet generated" in response.text
    assert "no matches have been generated yet" in response.text


def test_week_results_pending_week_empty_state(client):
    season_id, week_id, _ = _season_week_course(client)
    tee_set_id = _tee_set_id(client)
    engine = _engine(client)
    with Session(engine) as session:
        teams, golfers = [], []
        for label, number in (("A", 1), ("B", 2)):
            team = Team(season_id=season_id, name=label, number=number, sort_order=number)
            session.add(team)
            session.flush()
            teams.append(team.id)
            for position in (1, 2):
                golfer = Golfer(
                    first_name=f"{label}{position}", last_name="Example",
                    email=f"{label}{position}@example.test",
                    default_tee_set_id=tee_set_id,
                    handicap_strokes=0, handicap_source="self_reported",
                    handicap_status="ok",
                )
                session.add(golfer)
                session.flush()
                golfers.append(golfer.id)
                session.add(SeasonGolfer(season_id=season_id, golfer_id=golfer.id))
                session.add(TeamMember(
                    team_id=team.id, season_id=season_id, golfer_id=golfer.id,
                    position=position,
                ))
        team_match = TeamMatch(week_id=week_id, home_team_id=teams[0], away_team_id=teams[1])
        session.add(team_match)
        session.flush()
        for position, (a_id, b_id) in enumerate(((golfers[0], golfers[2]), (golfers[1], golfers[3])), start=1):
            session.add(PlayerMatch(
                team_match_id=team_match.id, position_label=str(position),
                a_golfer_id=a_id, b_golfer_id=b_id,
            ))
        for golfer_id in golfers:
            session.add(WeekHandicap(
                week_id=week_id, golfer_id=golfer_id, tee_set_id=tee_set_id,
                nine="front", strokes=0, source="seeded",
            ))
        # Matches are generated but no scores are entered: pending.
        session.commit()
    _as(client, _user(client, "player@example.test"))
    response = client.get(f"/seasons/{season_id}/results/weeks/{week_id}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "Scores pending" in response.text
    assert "none are complete yet" in response.text


def test_week_results_links_to_scores_and_skins(client):
    season_id, week_id, _ = _season_week_course(client)
    _complete_week(client, season_id, week_id, _tee_set_id(client))
    _as(client, _user(client, "player@example.test"))
    response = client.get(f"/seasons/{season_id}/results/weeks/{week_id}")
    assert response.status_code == 200
    text = response.text
    assert f'href="/weeks/{week_id}/scores"' in text
    assert f'href="/seasons/{season_id}/results/weeks/{week_id}/skins"' in text
    assert f'href="/seasons/{season_id}/results/weeks/{week_id}/net-skins"' in text
