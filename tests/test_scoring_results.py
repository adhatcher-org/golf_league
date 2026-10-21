"""R-RESULTS@1: signed nine-hole snapshot scoring (pure) and season/week standings (service).

Synthetic data only.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_matchup_generation import create_generated
from test_matchup_generation import matchup_db as _matchup_db

from golf_league.domain.scoring import (
    GolferCard,
    score_result_match,
    strokes_for_relative,
)
from golf_league.models import (
    HoleScore,
    PlayerMatch,
    TeamMatch,
    User,
    Week,
    WeekHandicap,
)
from golf_league.services.scores import season_result, week_holes, week_result

matchup_db = _matchup_db

SI_FRONT_9 = {9: 1, 3: 2, 2: 3, 7: 4, 1: 5, 4: 6, 5: 7, 6: 8, 8: 9}
ALL_HOLES = tuple(sorted(SI_FRONT_9))


def _card(golfer_id, name, handicap, strokes_by_hole):
    return GolferCard(
        golfer_id=golfer_id, name=name, handicap=handicap,
        strokes_by_hole=strokes_by_hole,
    )


def _full_strokes(value):
    return dict.fromkeys(ALL_HOLES, value)


# --- strokes_for_relative -----------------------------------------------------

def test_strokes_for_relative_zero_is_all_zeros():
    assert strokes_for_relative(0, SI_FRONT_9) == dict.fromkeys(ALL_HOLES, 0)


def test_strokes_for_relative_nine_is_one_each():
    assert strokes_for_relative(9, SI_FRONT_9) == dict.fromkeys(ALL_HOLES, 1)


def test_strokes_for_relative_ten_gives_extra_to_index_one():
    out = strokes_for_relative(10, SI_FRONT_9)
    assert out[9] == 2  # stroke index 1 is hole 9
    assert all(value == 1 for hole, value in out.items() if hole != 9)


def test_strokes_for_relative_twelve_gives_extra_to_indexes_one_to_three():
    out = strokes_for_relative(12, SI_FRONT_9)
    assert out[9] == 2 and out[3] == 2 and out[2] == 2  # indexes 1, 2, 3
    assert all(value == 1 for hole, value in out.items() if hole not in (9, 3, 2))


# --- score_result_match: match play ------------------------------------------

def test_match_play_lower_net_wins_each_hole():
    # Handicap 4 gets strokes on stroke indexes 1-4; all 4s -> net 3 there, 4 elsewhere.
    # Handicap 0, all 5s -> net 5 everywhere. The 4-handicap golfer wins every hole.
    a = _card(1, "Aaron", 4, _full_strokes(4))
    b = _card(2, "Beatrice", 0, _full_strokes(5))
    result = score_result_match("match_play", SI_FRONT_9, a, b)
    assert result is not None
    for hole in result.holes:
        assert hole.net_by_golfer[1] < hole.net_by_golfer[2]
        assert hole.points_by_golfer == {1: 2, 2: 0}
    rounds = {r.golfer_id: r for r in result.rounds}
    assert rounds[1].gross_total == 36 and rounds[1].net_total == 32
    assert rounds[2].gross_total == 45 and rounds[2].net_total == 45
    assert rounds[1].points == 20  # 18 hole points + 2 round bonus
    assert rounds[2].points == 0


def test_match_play_tie_hole_splits_points():
    a = _card(1, "Aaron", 0, _full_strokes(4))
    b = _card(2, "Beatrice", 0, _full_strokes(4))
    result = score_result_match("match_play", SI_FRONT_9, a, b)
    assert result is not None
    for hole in result.holes:
        assert hole.points_by_golfer == {1: 1, 2: 1}
    rounds = {r.golfer_id: r for r in result.rounds}
    assert rounds[1].points == rounds[2].points  # both get the round bonus


def test_signed_handicap_raises_net_total():
    # Six holes at 5, three at 4 -> gross 42; at handicap -2 the net is 44.
    a = _card(1, "Aaron", -2, {9: 5, 3: 5, 2: 5, 7: 5, 1: 5, 4: 5, 5: 4, 6: 4, 8: 4})
    b = _card(2, "Beatrice", 0, _full_strokes(5))
    result = score_result_match("match_play", SI_FRONT_9, a, b)
    assert result is not None
    rounds = {r.golfer_id: r for r in result.rounds}
    assert rounds[1].gross_total == 42 and rounds[1].net_total == 44


def test_missing_handicap_means_pending():
    assert score_result_match("match_play", SI_FRONT_9,
                              _card(1, "Aaron", None, _full_strokes(4)),
                              _card(2, "Beatrice", 0, _full_strokes(5))) is None
    assert score_result_match("match_play", SI_FRONT_9,
                              _card(1, "Aaron", 4, _full_strokes(4)),
                              _card(2, "Beatrice", None, _full_strokes(5))) is None


def test_stroke_play_uses_net_round_total_difference():
    # Aaron net 32, Beatrice net 45 -> Aaron gets the 13-point difference.
    a = _card(1, "Aaron", 4, _full_strokes(4))
    b = _card(2, "Beatrice", 0, _full_strokes(5))
    result = score_result_match("stroke_play", SI_FRONT_9, a, b)
    assert result is not None
    rounds = {r.golfer_id: r for r in result.rounds}
    assert rounds[1].net_total == 32 and rounds[2].net_total == 45
    assert rounds[1].points == 13 and rounds[2].points == 0
    for hole in result.holes:
        assert hole.points_by_golfer == {1: 0, 2: 0}


def test_score_result_match_rejects_unknown_type():
    a = _card(1, "A", 0, {1: 4})
    b = _card(2, "B", 0, {1: 4})
    with pytest.raises(ValueError):
        score_result_match("bogus", {1: 1}, a, b)


# --- service-level: week_result ----------------------------------------------

def _week_player_matches(session, week_id):
    return list(session.scalars(
        select(PlayerMatch)
        .join(TeamMatch, TeamMatch.id == PlayerMatch.team_match_id)
        .where(TeamMatch.week_id == week_id)
        .order_by(PlayerMatch.id)
    ))


def _complete_week(session, week_id, tee_set_id, nine, user_id, plan):
    """plan: {player_match_id: {golfer_id: (handicap_strokes, gross_strokes)}}."""
    holes = week_holes(session, week_id)
    hole_id_by_number = {hole.number: hole.id for hole in holes}
    for match_id, golfers in plan.items():
        for golfer_id, (handicap, gross) in golfers.items():
            session.add(WeekHandicap(
                week_id=week_id, golfer_id=golfer_id,
                tee_set_id=tee_set_id, nine=nine, strokes=handicap, source="seeded",
            ))
            for number in hole_id_by_number:
                session.add(HoleScore(
                    player_match_id=match_id, golfer_id=golfer_id,
                    hole_id=hole_id_by_number[number], strokes=gross,
                    entered_by_user_id=user_id,
                ))


def test_week_result_credits_sides_to_scheduled_teams(matchup_db):
    engine, ids = matchup_db
    with Session(engine) as session:
        create_generated(session, ids, week_index=0)
        week_id = ids["weeks"][0]
        week = session.get(Week, week_id)
        user = User(
            username="scorer@example.test", email="scorer@example.test",
            display_name="Scorer", password_hash="x",
        )
        session.add(user)
        session.flush()
        # Side a (home team): handicap 4, all 4s -> net 32, wins every hole -> 20 points.
        # Side b (away team): handicap 0, all 5s -> net 45 -> 0 points.
        plan = {}
        for match in _week_player_matches(session, week_id):
            plan[match.id] = {
                match.a_golfer_id: (4, 4),
                match.b_golfer_id: (0, 5),
            }
        _complete_week(session, week_id, ids["tee"], week.nine, user.id, plan)
        session.commit()
        result = week_result(session, week_id)

    assert result is not None
    players = {p.golfer_id: p for p in result.players}
    for golfer_id in ids["golfers"][:4]:  # side a of each match, team A
        assert players[golfer_id].points == 20 and players[golfer_id].matches == 1
    for golfer_id in ids["golfers"][4:8]:  # side b of each match, team B
        assert players[golfer_id].points == 0 and players[golfer_id].matches == 1
    teams = {t.team_id: t for t in result.teams}
    assert teams[ids["teams"][0]].points == 80 and teams[ids["teams"][0]].matches == 1
    assert teams[ids["teams"][1]].points == 0 and teams[ids["teams"][1]].matches == 1
    assert ids["teams"][2] not in teams and ids["teams"][3] not in teams


def test_week_result_excludes_pending_and_missing_handicap(matchup_db):
    engine, ids = matchup_db
    with Session(engine) as session:
        create_generated(session, ids, week_index=0)
        week_id = ids["weeks"][0]
        week = session.get(Week, week_id)
        user = User(
            username="scorer@example.test", email="scorer@example.test",
            display_name="Scorer", password_hash="x",
        )
        session.add(user)
        session.flush()
        matches = _week_player_matches(session, week_id)
        plan = {}
        # Match 0: complete, side a wins -> points.
        plan[matches[0].id] = {
            matches[0].a_golfer_id: (4, 4),
            matches[0].b_golfer_id: (0, 5),
        }
        # Match 1: side b has complete scores but no WeekHandicap -> pending.
        plan[matches[1].id] = {
            matches[1].a_golfer_id: (4, 4),
            matches[1].b_golfer_id: (0, 5),
        }
        _complete_week(session, week_id, ids["tee"], week.nine, user.id, plan)
        # Remove match 1's side b handicap so it is missing.
        session.execute(
            WeekHandicap.__table__.delete().where(
                WeekHandicap.week_id == week_id,
                WeekHandicap.golfer_id == matches[1].b_golfer_id,
            )
        )
        # Match 2: side a has only 8 of 9 holes -> incomplete -> pending.
        plan[matches[2].id] = {
            matches[2].a_golfer_id: (4, 4),
        }
        holes = week_holes(session, week_id)
        hole_id_by_number = {hole.number: hole.id for hole in holes}
        for number in hole_id_by_number:
            if number == 9:
                continue
            session.add(HoleScore(
                player_match_id=matches[2].id, golfer_id=matches[2].a_golfer_id,
                hole_id=hole_id_by_number[number], strokes=4, entered_by_user_id=user.id,
            ))
        session.commit()
        result = week_result(session, week_id)

    assert result is not None
    players = {p.golfer_id: p for p in result.players}
    # Match 0 side a: scored; match 0 side b: scored but 0 points; still a played match.
    assert players[matches[0].a_golfer_id].points == 20 and players[matches[0].a_golfer_id].matches == 1
    assert players[matches[0].b_golfer_id].points == 0 and players[matches[0].b_golfer_id].matches == 1
    # Match 1: pending (missing handicap) -> no match credit for either side.
    assert players.get(matches[1].a_golfer_id) is None
    assert players.get(matches[1].b_golfer_id) is None
    # Match 2: pending (incomplete scores) -> no match credit.
    assert players.get(matches[2].a_golfer_id) is None
    assert players.get(matches[2].b_golfer_id) is None


def test_week_result_self_match_counts_once_for_team(matchup_db):
    engine, ids = matchup_db
    with Session(engine) as session:
        create_generated(session, ids, self_match=True, week_index=0)
        week_id = ids["weeks"][0]
        week = session.get(Week, week_id)
        user = User(
            username="scorer@example.test", email="scorer@example.test",
            display_name="Scorer", password_hash="x",
        )
        session.add(user)
        session.flush()
        plan = {}
        # Both sides of a self-match: same handicap and scores -> tie every hole.
        for match in _week_player_matches(session, week_id):
            plan[match.id] = {
                match.a_golfer_id: (4, 4),
                match.b_golfer_id: (4, 4),
            }
        _complete_week(session, week_id, ids["tee"], week.nine, user.id, plan)
        session.commit()
        result = week_result(session, week_id)

    assert result is not None
    players = {p.golfer_id: p for p in result.players}
    golfers = []
    for match in _week_player_matches(session, week_id):
        golfers.extend((match.a_golfer_id, match.b_golfer_id))
    for golfer_id in golfers:
        assert players[golfer_id].points == 11 and players[golfer_id].matches == 1
    teams = {t.team_id: t for t in result.teams}
    # Both sides of a self-match credit the same team; the team match counts once.
    assert teams[ids["teams"][0]].points == 44 and teams[ids["teams"][0]].matches == 1


# --- service-level: season_result --------------------------------------------

def test_season_result_aggregates_across_weeks(matchup_db):
    engine, ids = matchup_db
    with Session(engine) as session:
        create_generated(session, ids, week_index=0)
        create_generated(session, ids, week_index=1)
        week_ids = [ids["weeks"][0], ids["weeks"][1]]
        weeks = [session.get(Week, week_id) for week_id in week_ids]
        user = User(
            username="scorer@example.test", email="scorer@example.test",
            display_name="Scorer", password_hash="x",
        )
        session.add(user)
        session.flush()
        for week_id, week in zip(week_ids, weeks, strict=True):
            plan = {}
            for match in _week_player_matches(session, week_id):
                plan[match.id] = {
                    match.a_golfer_id: (4, 4),
                    match.b_golfer_id: (0, 5),
                }
            _complete_week(session, week_id, ids["tee"], week.nine, user.id, plan)
        session.commit()
        result = season_result(session, ids["season"])

    assert result is not None
    teams = {t.team_id: t for t in result.teams}
    assert teams[ids["teams"][0]].points == 160 and teams[ids["teams"][0]].matches == 2
    assert teams[ids["teams"][1]].points == 0 and teams[ids["teams"][1]].matches == 2
    players = {p.golfer_id: p for p in result.players}
    for golfer_id in ids["golfers"][:4]:
        assert players[golfer_id].points == 40 and players[golfer_id].matches == 2
    # All three weeks of the season appear, even the one with no scored matches.
    assert len(result.weeks) == 3


def test_season_result_unknown_season_is_none(matchup_db):
    engine, ids = matchup_db
    with Session(engine) as session:
        assert season_result(session, 999999) is None
