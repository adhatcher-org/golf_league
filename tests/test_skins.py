"""GL-101: weekly gross skins — pure calculator, service and routes.

Synthetic data only.
"""

from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from golf_league.domain.scoring import GolferCard, gross_skins
from golf_league.models import (
    Course,
    Golfer,
    Hole,
    HoleScore,
    PlayerMatch,
    Season,
    Team,
    TeamMatch,
    TeeSet,
    User,
    Week,
)
from golf_league.services.auth import create_session_cookie, hash_password
from golf_league.services.course_seed import seed_wyandot
from golf_league.services.scores import week_skins

FRONT = tuple(range(1, 10))
BACK = tuple(range(10, 19))


# ---------------------------------------------------------------------------
# pure calculator
# ---------------------------------------------------------------------------

def _card(golfer_id, name, strokes_by_hole):
    return GolferCard(golfer_id=golfer_id, name=name, handicap=None,
                      strokes_by_hole=strokes_by_hole)


def _full(value, holes=FRONT):
    return dict.fromkeys(holes, value)


def test_tied_low_carries_one_unit_with_no_winner():
    # Only hole 1 is tied at 3: no winner there, one unit carried; Aaron's
    # unique low 4 wins every later hole outright.
    aaron = _card(1, "Aaron", {1: 3} | dict.fromkeys(FRONT[1:], 4))
    beatrice = _card(2, "Beatrice", {1: 3} | dict.fromkeys(FRONT[1:], 5))
    result = gross_skins([aaron, beatrice], FRONT)
    hole = result.holes[0]
    assert hole.hole_number == 1 and hole.low_score == 3
    assert hole.winner_id is None and hole.units == 0
    assert result.holes[1].winner_id == 1 and result.holes[1].units == 2  # takes the carry
    assert result.carried == 0
    golfers = {g.golfer_id: g for g in result.golfers}
    assert golfers[1].hole_wins == 8 and golfers[1].units == 9  # hole 2 takes the carry
    assert golfers[2].hole_wins == 0 and golfers[2].units == 0


def test_unique_low_wins_hole_and_prior_carry():
    # Hole 1 tied at 3 carries one unit; hole 2 a unique low 4 wins it and the
    # carry; later holes are decided outright so nothing carries past hole nine.
    rest = dict.fromkeys(FRONT[2:], 5)
    a = _card(1, "Aaron", {1: 3, 2: 4} | dict.fromkeys(FRONT[2:], 4))
    b = _card(2, "Beatrice", {1: 3, 2: 5} | rest)
    result = gross_skins([a, b], FRONT)
    hole1, hole2 = result.holes[0], result.holes[1]
    assert hole1.winner_id is None and hole1.units == 0
    assert hole2.winner_id == 1 and hole2.low_score == 4 and hole2.units == 2
    assert all(hole.winner_id == 1 for hole in result.holes[2:])
    assert result.carried == 0
    golfers = {g.golfer_id: g for g in result.golfers}
    assert golfers[1].hole_wins == 8 and golfers[1].units == 9  # hole 2 takes the carry, then 7 outright
    assert golfers[2].hole_wins == 0 and golfers[2].units == 0


def test_consecutive_carryovers_accumulate():
    # Holes 1 and 2 tied, hole 3 a unique low: the winner takes 3 units.
    a = _card(1, "Aaron", {1: 3, 2: 4, 3: 4} | _full(5, FRONT[3:]))
    b = _card(2, "Beatrice", {1: 3, 2: 4, 3: 5} | _full(5, FRONT[3:]))
    result = gross_skins([a, b], FRONT)
    assert result.holes[0].winner_id is None and result.holes[0].units == 0
    assert result.holes[1].winner_id is None and result.holes[1].units == 0
    assert result.holes[2].winner_id == 1 and result.holes[2].units == 3
    golfers = {g.golfer_id: g for g in result.golfers}
    assert golfers[1].units == 3 and golfers[1].hole_wins == 1


def test_carried_remains_visible_and_unawarded_after_hole_nine():
    # Every hole tied: nothing is ever awarded, the carry stays unawarded.
    result = gross_skins([_card(1, "Aaron", _full(4)), _card(2, "Beatrice", _full(4))], FRONT)
    assert all(hole.winner_id is None and hole.units == 0 for hole in result.holes)
    assert result.carried == 9
    golfers = {g.golfer_id: g for g in result.golfers}
    assert golfers[1].units == 0 and golfers[2].units == 0


def test_unique_win_without_carry_is_one_unit():
    result = gross_skins([
        _card(1, "Aaron", _full(4)),
        _card(2, "Beatrice", _full(5)),
    ], FRONT)
    assert all(hole.winner_id == 1 and hole.units == 1 for hole in result.holes)
    golfers = {g.golfer_id: g for g in result.golfers}
    assert golfers[1].units == 9 and golfers[1].hole_wins == 9
    assert golfers[2].units == 0


def test_grid_ranked_by_units_then_wins_then_total():
    # Holes 1-3: ties (carry 3). Hole 4: Beatrice's 3 wins outright, taking 4 units.
    # Holes 5-9 all tied: nothing more awarded. Cecilia beats Aaron on total (38 vs 39).
    aaron = _card(1, "Aaron", {1: 3, 2: 3, 3: 4, 4: 4} | _full(5, FRONT[4:]))
    beatrice = _card(2, "Beatrice", {1: 4, 2: 4, 3: 3, 4: 3} | _full(5, FRONT[4:]))
    cecilia = _card(3, "Cecilia", {1: 3, 2: 3, 3: 3, 4: 4} | _full(5, FRONT[4:]))
    result = gross_skins([aaron, beatrice, cecilia], FRONT)
    assert result.carried == 5  # holes 5-9 all tied after Beatrice takes the hole-4 carry
    ordered = [g.golfer_id for g in result.golfers]
    assert ordered == [2, 3, 1]
    units = {g.golfer_id: g.units for g in result.golfers}
    assert units[2] == 4 and units[1] == 0 and units[3] == 0


def test_back_nine_uses_ten_through_eighteen():
    tail_aaron = dict.fromkeys(BACK[2:], 4)
    tail_beatrice = dict.fromkeys(BACK[2:], 5)
    aaron = _card(1, "Aaron", {10: 3, 11: 3} | tail_aaron)
    beatrice = _card(2, "Beatrice", {10: 4, 11: 3} | tail_beatrice)
    result = gross_skins([aaron, beatrice], BACK)
    assert [hole.hole_number for hole in result.holes] == list(BACK)
    assert result.holes[0].winner_id == 1 and result.holes[0].low_score == 3
    assert result.holes[1].winner_id is None and result.holes[1].low_score == 3
    assert result.holes[1].units == 0
    assert result.holes[2].winner_id == 1 and result.holes[2].units == 2  # takes the hole-11 carry
    assert all(hole.winner_id == 1 for hole in result.holes[3:])
    assert result.carried == 0


# ---------------------------------------------------------------------------
# service: week_skins
# ---------------------------------------------------------------------------

def _seed_week(session: Session, nine: str = "front"):
    seed_wyandot(session)
    course = session.execute(
        select(Course).where(Course.name == "Wyandot Golf Club")
    ).scalar_one()
    tee = session.execute(
        select(TeeSet).where(TeeSet.course_id == course.id, TeeSet.name == "Deer")
    ).scalar_one()
    season = Season(
        year=2026, course_id=course.id, start_date=date(2026, 8, 27),
        end_date=date(2026, 10, 29), play_weekday=3, status="active",
        first_week_nine=nine, scoring_type="match_play",
    )
    session.add(season)
    session.flush()
    week = Week(
        season_id=season.id, index=1, play_date=date(2026, 8, 27),
        nine=nine, week_type="match", status="scheduled",
    )
    session.add(week)
    session.flush()
    home = Team(season_id=season.id, name="Home", number=1, sort_order=1)
    away = Team(season_id=season.id, name="Away", number=2, sort_order=2)
    session.add_all([home, away])
    session.flush()
    match = TeamMatch(
        week_id=week.id, home_team_id=home.id, away_team_id=away.id,
        is_self_match=False, sort_order=0,
    )
    session.add(match)
    session.flush()
    return season, week, match, tee


def _add_golfers(session, tee, count=4, subs=()):
    golfers = []
    for i in range(count):
        is_sub = i in subs
        golfer = Golfer(
            first_name=f"G{i + 1}", last_name="Example",
            default_tee_set_id=tee.id, handicap_strokes=None if is_sub else 5,
            handicap_source="self_reported",
            handicap_status="needs_entry" if is_sub else "ok",
            is_active=True,
        )
        session.add(golfer)
        session.flush()
        golfers.append(golfer)
    return golfers


def _player_match(session, match, a, b, a_sub=False, b_sub=False, position_label="P1vP2"):
    row = PlayerMatch(
        team_match_id=match.id, position_label=position_label,
        a_golfer_id=a.id, b_golfer_id=b.id,
        a_is_sub=a_sub, b_is_sub=b_sub, vs_own_handicap=False,
        manually_adjusted=False,
    )
    session.add(row)
    session.flush()
    return row


def _hole_ids(session, course, nine):
    return {
        hole.number: hole.id for hole in session.scalars(
            select(Hole).where(Hole.course_id == course.id, Hole.nine == nine)
            .order_by(Hole.number)
        )
    }


def _save_card(session, match, golfer, strokes_by_hole, hole_ids, user_id):
    for number, strokes in strokes_by_hole.items():
        session.add(HoleScore(
            player_match_id=match.id, golfer_id=golfer.id,
            hole_id=hole_ids[number], strokes=strokes,
            entered_by_user_id=user_id,
        ))


def _scorer(session):
    user = User(
        username="scorer@example.test", email="scorer@example.test",
        display_name="Scorer", password_hash="x",
    )
    session.add(user)
    session.flush()
    return user.id


def test_week_skins_complete_field(session):
    season, week, match, tee = _seed_week(session)
    course = session.get(Course, season.course_id)
    hole_ids = _hole_ids(session, course, week.nine)
    a, b = _add_golfers(session, tee, 2)
    pm = _player_match(session, match, a, b)
    user_id = _scorer(session)
    _save_card(session, pm, a, _full(4, FRONT if week.nine == "front" else BACK), hole_ids, user_id)
    # b: hole 1 a unique low 3, rest 5
    other_nine = FRONT if week.nine == "front" else BACK
    strokes_b = {1: 3} | dict.fromkeys(other_nine[1:], 5)
    _save_card(session, pm, b, strokes_b, hole_ids, user_id)
    session.commit()
    result = week_skins(session, week.id)
    assert result is not None and not result.pending
    hole1, hole2 = result.holes[0], result.holes[1]
    assert hole1.winner_id == b.id and hole1.low_score == 3 and hole1.units == 1
    assert hole2.winner_id == a.id and hole2.low_score == 4 and hole2.units == 1
    golfers = {g.golfer_id: g for g in result.golfers}
    assert golfers[a.id].hole_wins == 8 and golfers[a.id].units == 8
    assert golfers[b.id].hole_wins == 1 and golfers[b.id].units == 1
    assert result.carried == 0
    assert golfers[a.id].total_strokes == 36 and golfers[b.id].total_strokes == 43


def test_week_skins_tie_carries_and_unique_wins_it(session):
    season, week, match, tee = _seed_week(session)
    course = session.get(Course, season.course_id)
    hole_ids = _hole_ids(session, course, week.nine)
    a, b = _add_golfers(session, tee, 2)
    pm = _player_match(session, match, a, b)
    user_id = _scorer(session)
    holes = FRONT if week.nine == "front" else BACK
    # holes 1 and 2 tied (carry 2), hole 3 a unique low 4 for a
    strokes_a = {1: 3, 2: 4, 3: 4} | dict.fromkeys(holes[3:], 5)
    strokes_b = {1: 3, 2: 4, 3: 5} | dict.fromkeys(holes[3:], 5)
    _save_card(session, pm, a, strokes_a, hole_ids, user_id)
    _save_card(session, pm, b, strokes_b, hole_ids, user_id)
    session.commit()
    result = week_skins(session, week.id)
    assert not result.pending
    assert result.holes[0].winner_id is None and result.holes[0].units == 0
    assert result.holes[1].winner_id is None and result.holes[1].units == 0
    assert result.holes[2].winner_id == a.id and result.holes[2].units == 3
    # holes 4-9 are all tied at 5, so the carry accumulates and stays unawarded
    assert result.carried == 6
    golfers = {g.golfer_id: g for g in result.golfers}
    assert golfers[a.id].units == 3


def test_week_skins_final_carry_unawarded(session):
    season, week, match, tee = _seed_week(session)
    course = session.get(Course, season.course_id)
    hole_ids = _hole_ids(session, course, week.nine)
    a, b = _add_golfers(session, tee, 2)
    pm = _player_match(session, match, a, b)
    user_id = _scorer(session)
    holes = FRONT if week.nine == "front" else BACK
    _save_card(session, pm, a, _full(3, holes), hole_ids, user_id)
    _save_card(session, pm, b, _full(3, holes), hole_ids, user_id)
    session.commit()
    result = week_skins(session, week.id)
    assert not result.pending
    assert result.carried == 9
    assert all(hole.winner_id is None for hole in result.holes)


def test_week_skins_missing_card_is_pending(session):
    season, week, match, tee = _seed_week(session)
    course = session.get(Course, season.course_id)
    hole_ids = _hole_ids(session, course, week.nine)
    a, b = _add_golfers(session, tee, 2)
    pm = _player_match(session, match, a, b)
    user_id = _scorer(session)
    _save_card(session, pm, a, _full(4, FRONT if week.nine == "front" else BACK), hole_ids, user_id)
    # b has no scores at all
    session.commit()
    result = week_skins(session, week.id)
    assert result is not None and result.pending
    assert result.holes == [] and result.golfers == [] and result.carried == 0


def test_week_skins_incomplete_card_is_pending(session):
    season, week, match, tee = _seed_week(session)
    course = session.get(Course, season.course_id)
    hole_ids = _hole_ids(session, course, week.nine)
    a, b = _add_golfers(session, tee, 2)
    pm = _player_match(session, match, a, b)
    user_id = _scorer(session)
    holes = FRONT if week.nine == "front" else BACK
    _save_card(session, pm, a, _full(4, holes), hole_ids, user_id)
    _save_card(session, pm, b, dict.fromkeys(holes[:-1], 4), hole_ids, user_id)  # one hole missing
    session.commit()
    result = week_skins(session, week.id)
    assert result.pending


def test_week_skins_duplicate_conflicting_card_is_pending(session):
    # One golfer is a side of two matches in the week and scores the same hole
    # twice: that golfer's card is duplicate/conflicting, so the field is pending.
    season, week, match, tee = _seed_week(session)
    course = session.get(Course, season.course_id)
    hole_ids = _hole_ids(session, course, week.nine)
    holes = FRONT if week.nine == "front" else BACK
    a, b, c = _add_golfers(session, tee, 3)
    user_id = _scorer(session)
    pm1 = _player_match(session, match, a, b)
    _save_card(session, pm1, a, _full(4, holes), hole_ids, user_id)
    _save_card(session, pm1, b, _full(4, holes), hole_ids, user_id)
    # a plays in a second match (fresh teams so no team-per-week conflict).
    team3 = Team(season_id=season.id, name="Third", number=3, sort_order=3)
    team4 = Team(season_id=season.id, name="Fourth", number=4, sort_order=4)
    session.add_all([team3, team4])
    session.flush()
    match2 = TeamMatch(
        week_id=week.id, home_team_id=team3.id, away_team_id=team4.id,
        is_self_match=False, sort_order=1,
    )
    session.add(match2)
    session.flush()
    pm2 = _player_match(session, match2, a, c)
    # a scores hole 1 a second time -> duplicate/conflicting card
    _save_card(session, pm2, a, {holes[0]: 7}, hole_ids, user_id)
    session.commit()
    result = week_skins(session, week.id)
    assert result.pending


def test_week_skins_includes_substitutes(session):
    season, week, match, tee = _seed_week(session)
    course = session.get(Course, season.course_id)
    hole_ids = _hole_ids(session, course, week.nine)
    a, sub = _add_golfers(session, tee, 2, subs={1})
    pm = _player_match(session, match, a, sub, b_sub=True)
    user_id = _scorer(session)
    holes = FRONT if week.nine == "front" else BACK
    _save_card(session, pm, a, _full(4, holes), hole_ids, user_id)
    _save_card(session, pm, sub, _full(4, holes), hole_ids, user_id)
    session.commit()
    result = week_skins(session, week.id)
    assert not result.pending
    assert {g.golfer_id for g in result.golfers} == {a.id, sub.id}


def test_week_skins_recalculates_after_edit_and_clear(session):
    season, week, match, tee = _seed_week(session)
    course = session.get(Course, season.course_id)
    hole_ids = _hole_ids(session, course, week.nine)
    a, b = _add_golfers(session, tee, 2)
    pm = _player_match(session, match, a, b)
    user_id = _scorer(session)
    holes = FRONT if week.nine == "front" else BACK
    _save_card(session, pm, a, _full(4, holes), hole_ids, user_id)
    _save_card(session, pm, b, _full(4, holes), hole_ids, user_id)
    session.commit()
    result = week_skins(session, week.id)
    assert result.carried == 9  # all tied

    # edit: a takes hole 1 outright
    row = session.scalars(
        select(HoleScore).where(
            HoleScore.player_match_id == pm.id, HoleScore.golfer_id == a.id,
            HoleScore.hole_id == hole_ids[holes[0]],
        )
    ).one()
    row.strokes = 3
    session.commit()
    result = week_skins(session, week.id)
    assert not result.pending
    assert result.holes[0].winner_id == a.id and result.holes[0].units == 1
    assert result.carried == 8  # holes 2-9 are still tied at 4

    # clear: delete all scores -> pending again
    session.execute(HoleScore.__table__.delete())
    session.commit()
    result = week_skins(session, week.id)
    assert result.pending


def test_week_skins_both_nines(session):
    user_id = _scorer(session)
    for nine in ("front", "back"):
        season, week, match, tee = _seed_week(session, nine=nine)
        course = session.get(Course, season.course_id)
        hole_ids = _hole_ids(session, course, week.nine)
        a, b = _add_golfers(session, tee, 2)
        pm = _player_match(session, match, a, b)
        holes = FRONT if nine == "front" else BACK
        _save_card(session, pm, a, _full(4, holes), hole_ids, user_id)
        _save_card(session, pm, b, _full(5, holes), hole_ids, user_id)
        session.commit()
        result = week_skins(session, week.id)
        assert not result.pending
        assert [hole.hole_number for hole in result.holes] == list(holes)
        assert all(hole.winner_id == a.id for hole in result.holes)
        assert result.carried == 0


def test_week_skins_unknown_week_is_none(session):
    assert week_skins(session, 999999) is None


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

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


def _route_season_week(client, nine="front"):
    """Seed a scheduled week with one match and two complete cards; return ids."""
    engine = _engine(client)
    with Session(engine) as session:
        seed_wyandot(session)
        course = session.execute(
            select(Course).where(Course.name == "Wyandot Golf Club")
        ).scalar_one()
        tee = session.execute(
            select(TeeSet).where(TeeSet.course_id == course.id, TeeSet.name == "Deer")
        ).scalar_one()
        season = Season(
            year=2026, course_id=course.id, start_date=date(2026, 8, 27),
            end_date=date(2026, 10, 29), play_weekday=3, status="active",
            first_week_nine=nine, scoring_type="match_play",
        )
        session.add(season)
        session.flush()
        week = Week(
            season_id=season.id, index=1, play_date=date(2026, 8, 27),
            nine=nine, week_type="match", status="scheduled",
        )
        session.add(week)
        session.flush()
        home = Team(season_id=season.id, name="Home", number=1, sort_order=1)
        away = Team(season_id=season.id, name="Away", number=2, sort_order=2)
        session.add_all([home, away])
        session.flush()
        match = TeamMatch(
            week_id=week.id, home_team_id=home.id, away_team_id=away.id,
            is_self_match=False, sort_order=0,
        )
        session.add(match)
        session.flush()
        a = Golfer(first_name="Aaron", last_name="Example", default_tee_set_id=tee.id,
                   handicap_strokes=5, handicap_source="self_reported", handicap_status="ok")
        b = Golfer(first_name="Beatrice", last_name="Example", default_tee_set_id=tee.id,
                   handicap_strokes=5, handicap_source="self_reported", handicap_status="ok")
        session.add_all([a, b])
        session.flush()
        pm = PlayerMatch(
            team_match_id=match.id, position_label="P1vP2",
            a_golfer_id=a.id, b_golfer_id=b.id,
        )
        session.add(pm)
        session.flush()
        scorer = User(
            username="scorer@example.test", email="scorer@example.test",
            display_name="Scorer", password_hash="x",
        )
        session.add(scorer)
        session.flush()
        holes = {
            hole.number: hole.id for hole in session.scalars(
                select(Hole).where(Hole.course_id == course.id, Hole.nine == nine)
                .order_by(Hole.number)
            )
        }
        for golfer, strokes in ((a, _full(4, FRONT if nine == "front" else BACK)),
                                (b, {1: 3} | dict.fromkeys(
                                    (FRONT if nine == "front" else BACK)[1:], 5))):
            for number, strokes_value in strokes.items():
                session.add(HoleScore(
                    player_match_id=pm.id, golfer_id=golfer.id,
                    hole_id=holes[number], strokes=strokes_value,
                    entered_by_user_id=scorer.id,
                ))
        session.commit()
        return season.id, week.id, a.id, b.id


def test_anonymous_skins_is_401(client):
    season_id, week_id, _, _ = _route_season_week(client)
    response = client.get(f"/seasons/{season_id}/results/weeks/{week_id}/skins")
    assert response.status_code == 401


def test_verified_skins_is_200_with_no_store(client):
    season_id, week_id, _, _ = _route_season_week(client)
    _as(client, _user(client, "player@example.test"))
    response = client.get(f"/seasons/{season_id}/results/weeks/{week_id}/skins")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def test_skins_page_shows_winners_and_grid(client):
    season_id, week_id, a_id, b_id = _route_season_week(client)
    _as(client, _user(client, "player@example.test"))
    response = client.get(f"/seasons/{season_id}/results/weeks/{week_id}/skins")
    assert response.status_code == 200
    text = response.text
    assert "Beatrice Example" in text  # wins hole 1 outright
    assert "Aaron Example" in text     # wins holes 2-9
    assert "Hole 1" in text and "Low score" in text
    assert "Winner" in text and "Units won" in text
    assert "Gross score grid" in text


def test_skins_page_shows_pending_when_a_card_is_missing(client):
    engine = _engine(client)
    with Session(engine) as session:
        seed_wyandot(session)
        course = session.execute(
            select(Course).where(Course.name == "Wyandot Golf Club")
        ).scalar_one()
        tee = session.execute(
            select(TeeSet).where(TeeSet.course_id == course.id, TeeSet.name == "Deer")
        ).scalar_one()
        season = Season(
            year=2026, course_id=course.id, start_date=date(2026, 8, 27),
            end_date=date(2026, 10, 29), play_weekday=3, status="active",
            first_week_nine="front", scoring_type="match_play",
        )
        session.add(season)
        session.flush()
        season_id = season.id
        week = Week(
            season_id=season.id, index=2, play_date=date(2026, 9, 3),
            nine="front", week_type="match", status="scheduled",
        )
        session.add(week)
        session.flush()
        week_id = week.id
        home = Team(season_id=season.id, name="Home", number=1, sort_order=1)
        away = Team(season_id=season.id, name="Away", number=2, sort_order=2)
        session.add_all([home, away])
        session.flush()
        match = TeamMatch(
            week_id=week.id, home_team_id=home.id, away_team_id=away.id,
            is_self_match=False, sort_order=0,
        )
        session.add(match)
        session.flush()
        a = Golfer(first_name="Aaron", last_name="Example", default_tee_set_id=tee.id,
                   handicap_strokes=5, handicap_source="self_reported", handicap_status="ok")
        b = Golfer(first_name="Beatrice", last_name="Example", default_tee_set_id=tee.id,
                   handicap_strokes=5, handicap_source="self_reported", handicap_status="ok")
        session.add_all([a, b])
        session.flush()
        pm = PlayerMatch(
            team_match_id=match.id, position_label="P1vP2",
            a_golfer_id=a.id, b_golfer_id=b.id,
        )
        session.add(pm)
        session.flush()
        scorer = User(
            username="scorer2@example.test", email="scorer2@example.test",
            display_name="Scorer", password_hash="x",
        )
        session.add(scorer)
        session.flush()
        holes = {
            hole.number: hole.id for hole in session.scalars(
                select(Hole).where(Hole.course_id == course.id, Hole.nine == "front")
                .order_by(Hole.number)
            )
        }
        for number in holes:
            session.add(HoleScore(
                player_match_id=pm.id, golfer_id=a.id,
                hole_id=holes[number], strokes=4, entered_by_user_id=scorer.id,
            ))
        session.commit()

    _as(client, _user(client, "player@example.test"))
    response = client.get(f"/seasons/{season_id}/results/weeks/{week_id}/skins")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "Skins pending" in response.text


def test_unknown_skins_week_is_404(client):
    season_id, _, _, _ = _route_season_week(client)
    _as(client, _user(client, "player@example.test"))
    response = client.get(f"/seasons/{season_id}/results/weeks/999999/skins")
    assert response.status_code == 404


def test_skins_week_not_in_season_is_404(client):
    season_id, week_id, _, _ = _route_season_week(client)
    _as(client, _user(client, "player@example.test"))
    response = client.get(f"/seasons/{season_id + 1}/results/weeks/{week_id}/skins")
    assert response.status_code == 404
