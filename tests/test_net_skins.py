"""GL-102: weekly net skins — pure calculator, service and routes.

Synthetic data only.
"""

from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from golf_league.domain.scoring import (
    GolferCard,
    NetSkinsCalculation,
    net_skins,
)
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
    WeekHandicap,
)
from golf_league.services.auth import create_session_cookie, hash_password
from golf_league.services.course_seed import seed_wyandot
from golf_league.services.scores import week_net_skins

FRONT = tuple(range(1, 10))
BACK = tuple(range(10, 19))

# stroke_index_9 for each nine: front is 1..9 on holes 1..9; back mirrors
# Wyandot's back nine holding indexes 1, 2 and 3 among its holes.
SI_FRONT = {hole: hole for hole in FRONT}
SI_BACK = {10: 1, 11: 2, 12: 3, 13: 4, 14: 5, 15: 6, 16: 7, 17: 8, 18: 9}


def _card(golfer_id, name, handicap, strokes_by_hole):
    return GolferCard(golfer_id=golfer_id, name=name, handicap=handicap,
                      strokes_by_hole=strokes_by_hole)


def _full(value, holes=FRONT):
    return dict.fromkeys(holes, value)


# ---------------------------------------------------------------------------
# pure calculator
# ---------------------------------------------------------------------------

def test_relative_strokes_eight_spread_over_indexes_one_to_eight():
    # low = 0 (Aaron), Beatrice = 8 → relative 8 → one stroke on indexes 1-8,
    # none on index 9; net = gross - allocated.
    aaron = _card(1, "Aaron", 0, _full(5))
    beatrice = _card(2, "Beatrice", 8, _full(5))
    result = net_skins([aaron, beatrice], FRONT, SI_FRONT)
    assert isinstance(result, NetSkinsCalculation)
    golfers = {g.golfer_id: g for g in result.golfers}
    assert golfers[2].allocated_total == 8 and golfers[2].net_total == 37
    assert golfers[2].allocated_by_hole[9] == 0 and golfers[2].net_by_hole[9] == 5
    # Beatrice's 4 beats Aaron's 5 on holes 1-8; hole 9 is tied at 5 → carry.
    assert all(h.winner_id == 2 for h in result.holes[:8])
    assert result.holes[8].winner_id is None and result.holes[8].units == 0
    assert result.carried == 1
    assert golfers[2].units == 8 and golfers[2].hole_wins == 8


def test_relative_strokes_nine_is_one_on_every_hole():
    aaron = _card(1, "Aaron", 0, _full(5))
    beatrice = _card(2, "Beatrice", 9, _full(6))
    result = net_skins([aaron, beatrice], FRONT, SI_FRONT)
    golfers = {g.golfer_id: g for g in result.golfers}
    assert all(golfers[2].allocated_by_hole[h] == 1 for h in FRONT)
    assert golfers[2].allocated_total == 9 and golfers[2].net_total == 45
    assert all(golfers[2].net_by_hole[h] == 5 for h in FRONT)
    # Tied at 5 on every hole: nothing awarded, carry stays unawarded.
    assert all(h.winner_id is None and h.units == 0 for h in result.holes)
    assert result.carried == 9


def test_relative_strokes_eighteen_is_two_on_every_hole():
    aaron = _card(1, "Aaron", 0, _full(5))
    beatrice = _card(2, "Beatrice", 18, _full(7))
    result = net_skins([aaron, beatrice], FRONT, SI_FRONT)
    golfers = {g.golfer_id: g for g in result.golfers}
    assert all(golfers[2].allocated_by_hole[h] == 2 for h in FRONT)
    assert all(golfers[2].net_by_hole[h] == 5 for h in FRONT)
    # Tied at 5 on every hole → nothing awarded.
    assert result.carried == 9


def test_relative_strokes_twenty_two_is_two_everywhere_plus_one_on_one_to_four():
    aaron = _card(1, "Aaron", 0, _full(5))
    beatrice = _card(2, "Beatrice", 22, _full(7))
    result = net_skins([aaron, beatrice], FRONT, SI_FRONT)
    golfers = {g.golfer_id: g for g in result.golfers}
    assert all(golfers[2].allocated_by_hole[h] == 3 for h in (1, 2, 3, 4))
    assert all(golfers[2].allocated_by_hole[h] == 2 for h in (5, 6, 7, 8, 9))
    assert golfers[2].allocated_total == 22 and golfers[2].net_total == 41
    # Beatrice net: 4 on holes 1-4 (wins), 5 on holes 5-9 (ties Aaron at 5).
    assert all(h.winner_id == 2 for h in result.holes[:4])
    assert all(h.winner_id is None and h.units == 0 for h in result.holes[4:])
    assert result.carried == 5
    assert golfers[2].units == 4 and golfers[2].hole_wins == 4


def test_negative_baseline_low_handicap_gets_no_strokes():
    # low = -2 (Aaron), Beatrice = 0 → relative 2 → one stroke on indexes 1,2.
    aaron = _card(1, "Aaron", -2, _full(5))
    beatrice = _card(2, "Beatrice", 0, _full(5))
    result = net_skins([aaron, beatrice], FRONT, SI_FRONT)
    golfers = {g.golfer_id: g for g in result.golfers}
    assert golfers[1].allocated_total == 0 and golfers[1].net_total == 45
    assert golfers[2].allocated_by_hole[1] == 1 and golfers[2].allocated_by_hole[2] == 1
    assert golfers[2].allocated_by_hole[9] == 0
    assert golfers[2].net_by_hole[1] == 4 and golfers[2].net_by_hole[9] == 5


def test_zero_baseline_is_scratch_not_missing():
    aaron = _card(1, "Aaron", 0, _full(4))
    beatrice = _card(2, "Beatrice", 0, _full(4))
    result = net_skins([aaron, beatrice], FRONT, SI_FRONT)
    assert all(h.winner_id is None for h in result.holes)
    assert result.carried == 9


def test_back_nine_uses_its_own_stroke_index_9():
    holes = BACK
    aaron = _card(1, "Aaron", 0, _full(5, holes))
    beatrice = _card(2, "Beatrice", 4, _full(5, holes))
    result = net_skins([aaron, beatrice], holes, SI_BACK)
    golfers = {g.golfer_id: g for g in result.golfers}
    # index 1 is on hole 10 ... index 4 on hole 13; those four get one stroke.
    assert golfers[2].allocated_by_hole[10] == 1
    assert golfers[2].allocated_by_hole[13] == 1
    assert golfers[2].allocated_by_hole[18] == 0
    assert golfers[2].allocated_total == 4
    assert golfers[2].net_by_hole[10] == 4
    assert [h.hole_number for h in result.holes] == list(BACK)
    # Beatrice's net 4 wins holes 10-13; holes 14-18 tie at 5 and carry.
    assert all(h.winner_id == 2 for h in result.holes[:4])
    assert all(h.winner_id is None and h.units == 0 for h in result.holes[4:])
    assert result.carried == 5
    assert golfers[2].units == 4 and golfers[2].hole_wins == 4


def test_unique_low_wins_hole_and_prior_carry():
    # Hole 1 tied at net 4 carries one; hole 2 Aaron's net 3 wins it and the
    # carry; holes 3-9 Aaron wins outright, so nothing carries past hole nine.
    aaron = _card(1, "Aaron", 0, {1: 4, 2: 3} | dict.fromkeys(FRONT[2:], 3))
    beatrice = _card(2, "Beatrice", 0, {1: 4} | _full(4, FRONT[1:]))
    result = net_skins([aaron, beatrice], FRONT, SI_FRONT)
    assert result.holes[0].winner_id is None and result.holes[0].units == 0
    assert result.holes[1].winner_id == 1 and result.holes[1].low_net == 3
    assert result.holes[1].units == 2  # takes the carry
    assert all(h.winner_id == 1 for h in result.holes[2:])
    assert result.carried == 0
    golfers = {g.golfer_id: g for g in result.golfers}
    assert golfers[1].units == 9 and golfers[1].hole_wins == 8


def test_consecutive_carryovers_accumulate():
    # Holes 1 and 2 tied, hole 3 a unique low: the winner takes 3 units.
    aaron = _card(1, "Aaron", 0, {1: 4, 2: 4, 3: 3} | _full(4, FRONT[3:]))
    beatrice = _card(2, "Beatrice", 0, {1: 4, 2: 4, 3: 4} | _full(4, FRONT[3:]))
    result = net_skins([aaron, beatrice], FRONT, SI_FRONT)
    assert result.holes[0].winner_id is None and result.holes[0].units == 0
    assert result.holes[1].winner_id is None and result.holes[1].units == 0
    assert result.holes[2].winner_id == 1 and result.holes[2].units == 3
    golfers = {g.golfer_id: g for g in result.golfers}
    assert golfers[1].units == 3 and golfers[1].hole_wins == 1


def test_carried_remains_visible_and_unawarded_after_hole_nine():
    result = net_skins([
        _card(1, "Aaron", 0, _full(4)),
        _card(2, "Beatrice", 0, _full(4)),
    ], FRONT, SI_FRONT)
    assert all(hole.winner_id is None and hole.units == 0 for hole in result.holes)
    assert result.carried == 9
    golfers = {g.golfer_id: g for g in result.golfers}
    assert golfers[1].units == 0 and golfers[2].units == 0


def test_grid_ranked_by_units_then_wins_then_net_total():
    # Holes 1-3 tied (carry 3). Hole 4 Beatrice net 3 wins, taking 4 units.
    # Holes 5-9 tied after → carry 5 unawarded. Cecilia beats Aaron on net (37 vs 38).
    aaron = _card(1, "Aaron", 0, {1: 4, 2: 4, 3: 4, 4: 4} | _full(5, FRONT[4:]))
    beatrice = _card(2, "Beatrice", 0, {1: 4, 2: 4, 3: 4, 4: 3} | _full(5, FRONT[4:]))
    cecilia = _card(3, "Cecilia", 1, {1: 4, 2: 4, 3: 4, 4: 4} | _full(5, FRONT[4:]))
    result = net_skins([aaron, beatrice, cecilia], FRONT, SI_FRONT)
    assert result.carried == 5
    ordered = [g.golfer_id for g in result.golfers]
    assert ordered == [2, 3, 1]
    units = {g.golfer_id: g.units for g in result.golfers}
    # Cecilia wins hole 1 (her handicap stroke on index 1 makes net 3).
    # Beatrice wins hole 4 (net 3) taking the two-hole carry (3 units).
    # Holes 5-9 are all tied at 5, so the carry stays unawarded.
    assert units[2] == 3 and units[1] == 0 and units[3] == 1
    golfers_net = {g.golfer_id: g.net_total for g in result.golfers}
    assert golfers_net[3] == 40  # 41 gross - 1 allocated stroke
    assert golfers_net[1] == 41 and golfers_net[2] == 40


# ---------------------------------------------------------------------------
# service: week_net_skins
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


def _player_match(session, match, a, b, a_sub=False, b_sub=False):
    row = PlayerMatch(
        team_match_id=match.id, position_label="P1vP2",
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


def _save_snapshot(session, week, golfer, strokes, tee):
    session.add(WeekHandicap(
        week_id=week.id, golfer_id=golfer.id, tee_set_id=tee.id,
        nine=week.nine, strokes=strokes, source="seeded",
    ))
    session.flush()


def _scorer(session):
    user = User(
        username="scorer@example.test", email="scorer@example.test",
        display_name="Scorer", password_hash="x",
    )
    session.add(user)
    session.flush()
    return user.id


def test_week_net_skins_complete_field(session):
    season, week, match, tee = _seed_week(session)
    course = session.get(Course, season.course_id)
    hole_ids = _hole_ids(session, course, week.nine)
    a, b = _add_golfers(session, tee, 2)
    pm = _player_match(session, match, a, b)
    user_id = _scorer(session)
    holes = FRONT if week.nine == "front" else BACK
    _save_snapshot(session, week, a, 5, tee)
    _save_snapshot(session, week, b, 5, tee)
    _save_card(session, pm, a, _full(4, holes), hole_ids, user_id)
    _save_card(session, pm, b, {1: 3} | dict.fromkeys(holes[1:], 5), hole_ids, user_id)
    session.commit()
    result = week_net_skins(session, week.id)
    assert result is not None and not result.pending
    assert result.holes[0].winner_id == b.id and result.holes[0].low_net == 3
    assert result.holes[0].units == 1
    assert result.holes[1].winner_id == a.id and result.holes[1].units == 1
    golfers = {g.golfer_id: g for g in result.golfers}
    assert golfers[a.id].hole_wins == 8 and golfers[a.id].units == 8
    assert golfers[b.id].hole_wins == 1 and golfers[b.id].units == 1
    assert result.carried == 0


def test_week_net_skins_handicap_allocation_affects_net(session):
    # Aaron snapshot 0, Beatrice snapshot 8 → Beatrice gets 8 relative strokes
    # (one on each of the eight lowest stroke indexes, none on index 9).
    season, week, match, tee = _seed_week(session)
    course = session.get(Course, season.course_id)
    hole_ids = _hole_ids(session, course, week.nine)
    a, b = _add_golfers(session, tee, 2)
    pm = _player_match(session, match, a, b)
    user_id = _scorer(session)
    holes = FRONT if week.nine == "front" else BACK
    _save_snapshot(session, week, a, 0, tee)
    _save_snapshot(session, week, b, 8, tee)
    _save_card(session, pm, a, _full(5, holes), hole_ids, user_id)
    _save_card(session, pm, b, _full(5, holes), hole_ids, user_id)
    session.commit()
    result = week_net_skins(session, week.id)
    assert not result.pending
    golfers = {g.golfer_id: g for g in result.golfers}
    assert golfers[a.id].allocated_total == 0  # low handicap gets no strokes
    assert golfers[b.id].allocated_total == 8 and golfers[b.id].net_total == 37
    # Beatrice's strokes win her eight holes; the one tied hole's carry is
    # taken at the next outright win, so nothing carries past hole nine.
    assert result.carried == 0
    assert golfers[b.id].units == 9 and golfers[b.id].hole_wins == 8


def test_week_net_skins_negative_handicap_baseline(session):
    # Aaron snapshot -2, Beatrice snapshot 0 → Beatrice relative 2, strokes on
    # the two lowest stroke indexes only.
    season, week, match, tee = _seed_week(session)
    course = session.get(Course, season.course_id)
    hole_ids = _hole_ids(session, course, week.nine)
    a, b = _add_golfers(session, tee, 2)
    pm = _player_match(session, match, a, b)
    user_id = _scorer(session)
    holes = FRONT if week.nine == "front" else BACK
    _save_snapshot(session, week, a, -2, tee)
    _save_snapshot(session, week, b, 0, tee)
    _save_card(session, pm, a, _full(5, holes), hole_ids, user_id)
    _save_card(session, pm, b, _full(5, holes), hole_ids, user_id)
    session.commit()
    result = week_net_skins(session, week.id)
    assert not result.pending
    golfers = {g.golfer_id: g for g in result.golfers}
    assert golfers[a.id].allocated_total == 0  # negative low handicap: nothing allocated
    assert golfers[b.id].allocated_total == 2
    # Beatrice's two strokes win two holes; their carries are taken at the
    # next outright wins, so nothing carries past hole nine.
    assert result.carried == 0
    assert golfers[b.id].units == 9 and golfers[b.id].hole_wins == 2


def test_week_net_skins_tie_carries_and_unique_wins_it(session):
    season, week, match, tee = _seed_week(session)
    course = session.get(Course, season.course_id)
    hole_ids = _hole_ids(session, course, week.nine)
    a, b = _add_golfers(session, tee, 2)
    pm = _player_match(session, match, a, b)
    user_id = _scorer(session)
    holes = FRONT if week.nine == "front" else BACK
    _save_snapshot(session, week, a, 5, tee)
    _save_snapshot(session, week, b, 5, tee)
    strokes_a = {1: 4, 2: 4, 3: 3} | dict.fromkeys(holes[3:], 4)
    strokes_b = {1: 4, 2: 4, 3: 4} | dict.fromkeys(holes[3:], 4)
    _save_card(session, pm, a, strokes_a, hole_ids, user_id)
    _save_card(session, pm, b, strokes_b, hole_ids, user_id)
    session.commit()
    result = week_net_skins(session, week.id)
    assert not result.pending
    assert result.holes[0].winner_id is None and result.holes[0].units == 0
    assert result.holes[1].winner_id is None and result.holes[1].units == 0
    assert result.holes[2].winner_id == a.id and result.holes[2].units == 3
    assert result.carried == 6


def test_week_net_skins_final_carry_unawarded(session):
    season, week, match, tee = _seed_week(session)
    course = session.get(Course, season.course_id)
    hole_ids = _hole_ids(session, course, week.nine)
    a, b = _add_golfers(session, tee, 2)
    pm = _player_match(session, match, a, b)
    user_id = _scorer(session)
    holes = FRONT if week.nine == "front" else BACK
    _save_snapshot(session, week, a, 5, tee)
    _save_snapshot(session, week, b, 5, tee)
    _save_card(session, pm, a, _full(3, holes), hole_ids, user_id)
    _save_card(session, pm, b, _full(3, holes), hole_ids, user_id)
    session.commit()
    result = week_net_skins(session, week.id)
    assert not result.pending
    assert result.carried == 9
    assert all(hole.winner_id is None for hole in result.holes)


def test_week_net_skins_missing_card_is_pending(session):
    season, week, match, tee = _seed_week(session)
    course = session.get(Course, season.course_id)
    hole_ids = _hole_ids(session, course, week.nine)
    a, b = _add_golfers(session, tee, 2)
    pm = _player_match(session, match, a, b)
    user_id = _scorer(session)
    holes = FRONT if week.nine == "front" else BACK
    _save_snapshot(session, week, a, 5, tee)
    _save_snapshot(session, week, b, 5, tee)
    _save_card(session, pm, a, _full(4, holes), hole_ids, user_id)
    # b has no scores at all
    session.commit()
    result = week_net_skins(session, week.id)
    assert result is not None and result.pending
    assert result.holes == [] and result.golfers == [] and result.carried == 0


def test_week_net_skins_missing_snapshot_is_pending(session):
    # Complete cards but Beatrice lacks a WeekHandicap snapshot → pending, never zero.
    season, week, match, tee = _seed_week(session)
    course = session.get(Course, season.course_id)
    hole_ids = _hole_ids(session, course, week.nine)
    a, b = _add_golfers(session, tee, 2)
    pm = _player_match(session, match, a, b)
    user_id = _scorer(session)
    holes = FRONT if week.nine == "front" else BACK
    _save_snapshot(session, week, a, 5, tee)
    _save_card(session, pm, a, _full(4, holes), hole_ids, user_id)
    _save_card(session, pm, b, _full(4, holes), hole_ids, user_id)
    session.commit()
    result = week_net_skins(session, week.id)
    assert result.pending


def test_week_net_skins_duplicate_conflicting_card_is_pending(session):
    season, week, match, tee = _seed_week(session)
    course = session.get(Course, season.course_id)
    hole_ids = _hole_ids(session, course, week.nine)
    holes = FRONT if week.nine == "front" else BACK
    a, b, c = _add_golfers(session, tee, 3)
    user_id = _scorer(session)
    pm1 = _player_match(session, match, a, b)
    _save_snapshot(session, week, a, 5, tee)
    _save_snapshot(session, week, b, 5, tee)
    _save_snapshot(session, week, c, 5, tee)
    _save_card(session, pm1, a, _full(4, holes), hole_ids, user_id)
    _save_card(session, pm1, b, _full(4, holes), hole_ids, user_id)
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
    _save_card(session, pm2, a, {holes[0]: 7}, hole_ids, user_id)  # a scores hole 1 twice
    session.commit()
    result = week_net_skins(session, week.id)
    assert result.pending


def test_week_net_skins_includes_substitutes(session):
    season, week, match, tee = _seed_week(session)
    course = session.get(Course, season.course_id)
    hole_ids = _hole_ids(session, course, week.nine)
    a, sub = _add_golfers(session, tee, 2, subs={1})
    pm = _player_match(session, match, a, sub, b_sub=True)
    user_id = _scorer(session)
    holes = FRONT if week.nine == "front" else BACK
    _save_snapshot(session, week, a, 5, tee)
    _save_snapshot(session, week, sub, 5, tee)
    _save_card(session, pm, a, _full(4, holes), hole_ids, user_id)
    _save_card(session, pm, sub, _full(4, holes), hole_ids, user_id)
    session.commit()
    result = week_net_skins(session, week.id)
    assert not result.pending
    assert {g.golfer_id for g in result.golfers} == {a.id, sub.id}


def test_week_net_skins_both_nines(session):
    user_id = _scorer(session)
    for nine in ("front", "back"):
        season, week, match, tee = _seed_week(session, nine=nine)
        course = session.get(Course, season.course_id)
        hole_ids = _hole_ids(session, course, week.nine)
        a, b = _add_golfers(session, tee, 2)
        pm = _player_match(session, match, a, b)
        holes = FRONT if nine == "front" else BACK
        _save_snapshot(session, week, a, 5, tee)
        _save_snapshot(session, week, b, 5, tee)
        _save_card(session, pm, a, _full(4, holes), hole_ids, user_id)
        _save_card(session, pm, b, _full(5, holes), hole_ids, user_id)
        session.commit()
        result = week_net_skins(session, week.id)
        assert not result.pending
        assert [hole.hole_number for hole in result.holes] == list(holes)
        assert all(hole.winner_id == a.id for hole in result.holes)
        assert result.carried == 0


def test_week_net_skins_unknown_week_is_none(session):
    assert week_net_skins(session, 999999) is None


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
    """Seed a scheduled week with one match, two complete cards and snapshots."""
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
        _save_snapshot(session, week, a, 5, tee)
        _save_snapshot(session, week, b, 5, tee)
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


def test_anonymous_net_skins_is_401(client):
    season_id, week_id, _, _ = _route_season_week(client)
    response = client.get(f"/seasons/{season_id}/results/weeks/{week_id}/net-skins")
    assert response.status_code == 401


def test_verified_net_skins_is_200_with_no_store(client):
    season_id, week_id, _, _ = _route_season_week(client)
    _as(client, _user(client, "player@example.test"))
    response = client.get(f"/seasons/{season_id}/results/weeks/{week_id}/net-skins")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def test_net_skins_page_shows_winners_and_grid(client):
    season_id, week_id, _, _ = _route_season_week(client)
    _as(client, _user(client, "player@example.test"))
    response = client.get(f"/seasons/{season_id}/results/weeks/{week_id}/net-skins")
    assert response.status_code == 200
    text = response.text
    assert "Beatrice Example" in text  # wins hole 1 outright (net 3)
    assert "Aaron Example" in text     # wins holes 2-9
    assert "Hole 1" in text and "Low net" in text
    assert "Winner" in text and "Units won" in text
    assert "Net score grid" in text
    assert "Allocated" in text


def test_net_skins_page_shows_pending_when_a_card_is_missing(client):
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
        _save_snapshot(session, week, a, 5, tee)
        _save_snapshot(session, week, b, 5, tee)
        for number in holes:
            session.add(HoleScore(
                player_match_id=pm.id, golfer_id=a.id,
                hole_id=holes[number], strokes=4, entered_by_user_id=scorer.id,
            ))
        session.commit()

    _as(client, _user(client, "player@example.test"))
    response = client.get(f"/seasons/{season_id}/results/weeks/{week_id}/net-skins")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "Net skins pending" in response.text


def test_net_skins_page_shows_pending_when_a_snapshot_is_missing(client):
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
            season_id=season.id, index=3, play_date=date(2026, 9, 10),
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
            username="scorer3@example.test", email="scorer3@example.test",
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
        _save_snapshot(session, week, a, 5, tee)
        # b has complete card but NO snapshot
        for number in holes:
            session.add(HoleScore(
                player_match_id=pm.id, golfer_id=a.id,
                hole_id=holes[number], strokes=4, entered_by_user_id=scorer.id,
            ))
            session.add(HoleScore(
                player_match_id=pm.id, golfer_id=b.id,
                hole_id=holes[number], strokes=4, entered_by_user_id=scorer.id,
            ))
        session.commit()

    _as(client, _user(client, "player@example.test"))
    response = client.get(f"/seasons/{season_id}/results/weeks/{week_id}/net-skins")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "Net skins pending" in response.text


def test_unknown_net_skins_week_is_404(client):
    season_id, _, _, _ = _route_season_week(client)
    _as(client, _user(client, "player@example.test"))
    response = client.get(f"/seasons/{season_id}/results/weeks/999999/net-skins")
    assert response.status_code == 404


def test_net_skins_week_not_in_season_is_404(client):
    season_id, week_id, _, _ = _route_season_week(client)
    _as(client, _user(client, "player@example.test"))
    response = client.get(f"/seasons/{season_id + 1}/results/weeks/{week_id}/net-skins")
    assert response.status_code == 404
