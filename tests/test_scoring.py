"""Pure scoring rules: net scores, Match Play points and Stroke Play points."""

from golf_league.domain.scoring import (
    DEFAULT_SCORING_TYPE,
    VALID_SCORING_TYPES,
    GolferCard,
    match_play_hole_points,
    net_score_for_hole,
    score_match,
    stroke_play_points,
)


def _card(golfer_id, name, handicap, strokes_by_hole):
    return GolferCard(
        golfer_id=golfer_id, name=name, handicap=handicap,
        strokes_by_hole=strokes_by_hole,
    )


# --- net score per hole -------------------------------------------------------

def test_net_equals_gross_without_handicap():
    assert net_score_for_hole(5, None, 1) == 5
    assert net_score_for_hole(5, 0, 1) == 5


def test_net_subtracts_one_on_covered_holes():
    # Handicap 4 covers stroke indexes 1-4.
    assert net_score_for_hole(5, 4, 1) == 4
    assert net_score_for_hole(5, 4, 2) == 4
    assert net_score_for_hole(5, 4, 3) == 4
    assert net_score_for_hole(5, 4, 4) == 4
    # Not covered on SI 5+.
    assert net_score_for_hole(5, 4, 5) == 5
    assert net_score_for_hole(5, 4, 9) == 5


def test_net_subtracts_one_per_handicap_point():
    assert net_score_for_hole(6, 1, 1) == 5
    assert net_score_for_hole(6, 1, 2) == 6
    assert net_score_for_hole(6, 2, 1) == 5
    assert net_score_for_hole(6, 2, 2) == 5
    assert net_score_for_hole(6, 2, 3) == 6


# --- match play hole points ---------------------------------------------------

def test_match_play_hole_points_lower_wins():
    assert match_play_hole_points(4, 5) == (2, 0)
    assert match_play_hole_points(5, 4) == (0, 2)


def test_match_play_hole_points_tie():
    assert match_play_hole_points(4, 4) == (1, 1)


# --- stroke play points -------------------------------------------------------

def test_stroke_play_points_difference():
    assert stroke_play_points(32, 35) == (3, 0)
    assert stroke_play_points(35, 32) == (0, 3)


def test_stroke_play_points_tie():
    assert stroke_play_points(32, 32) == (0, 0)


# --- full match scoring: match play ------------------------------------------

# Wyandot front 9 stroke indexes: hole 9=SI1, 3=SI2, 2=SI3, 7=SI4, then 1,4,5,6,8 = SI5-9.
SI_FRONT_9 = {9: 1, 3: 2, 2: 3, 7: 4, 1: 5, 4: 6, 5: 7, 6: 8, 8: 9}


def test_match_play_full_round_example():
    # Handicap 4: subtract 1 on holes 9, 3, 2, 7 (SIs 1-4).
    a = _card(1, "Aaron", 4, {9: 5, 3: 4, 2: 5, 7: 4, 1: 4, 4: 4, 5: 4, 6: 4, 8: 4})
    b = _card(2, "Beatrice", 0, {9: 4, 3: 4, 2: 4, 7: 4, 1: 4, 4: 4, 5: 4, 6: 4, 8: 4})
    result = score_match("match_play", SI_FRONT_9, a, b)

    assert result.scoring_type == "match_play"
    assert len(result.holes) == 9

    # Hole 9: A net 4 (5-1), B net 4 -> tie, 1 each.
    h9 = next(h for h in result.holes if h.hole_number == 9)
    assert h9.net_by_golfer == {1: 4, 2: 4}
    assert h9.points_by_golfer == {1: 1, 2: 1}

    # Hole 3: A net 3 (4-1), B net 4 -> A wins hole, 2-0.
    h3 = next(h for h in result.holes if h.hole_number == 3)
    assert h3.net_by_golfer == {1: 3, 2: 4}
    assert h3.points_by_golfer == {1: 2, 2: 0}

    # Round totals: A gross 38 net 34 (4 strokes off); B gross 36 net 36.
    ra, rb = result.rounds
    assert ra.gross_total == 38 and ra.net_total == 34
    assert rb.gross_total == 36 and rb.net_total == 36
    # A wins the round: +2 bonus.
    assert ra.points == 13  # hole points 11 + round bonus 2
    assert rb.points == 7   # hole points 7


def test_match_play_tie_round_bonus_both():
    a = _card(1, "Aaron", 4, {9: 4, 3: 4, 2: 4, 7: 4, 1: 4, 4: 4, 5: 4, 6: 4, 8: 4})
    b = _card(2, "Beatrice", 4, {9: 4, 3: 4, 2: 4, 7: 4, 1: 4, 4: 4, 5: 4, 6: 4, 8: 4})
    result = score_match("match_play", SI_FRONT_9, a, b)
    ra, rb = result.rounds
    assert ra.net_total == rb.net_total == 32
    assert ra.points == rb.points  # both get the round bonus


# --- full match scoring: stroke play -----------------------------------------

def test_stroke_play_full_round_example():
    # Player 1 net 32, player 2 net 35 -> player 1 gets 3 points.
    a = _card(1, "Aaron", 0, {9: 3, 3: 3, 2: 3, 7: 3, 1: 4, 4: 4, 5: 4, 6: 4, 8: 4})
    b = _card(2, "Beatrice", 0, {9: 4, 3: 4, 2: 4, 7: 4, 1: 4, 4: 4, 5: 4, 6: 4, 8: 3})
    result = score_match("stroke_play", SI_FRONT_9, a, b)

    assert result.scoring_type == "stroke_play"
    ra, rb = result.rounds
    assert ra.net_total == 32 and rb.net_total == 35
    assert ra.points == 3 and rb.points == 0
    # Hole points are zero for stroke play.
    for h in result.holes:
        assert h.points_by_golfer == {1: 0, 2: 0}


def test_stroke_play_tie():
    a = _card(1, "Aaron", 0, {9: 4, 3: 4, 2: 4, 7: 4, 1: 4, 4: 4, 5: 4, 6: 4, 8: 4})
    b = _card(2, "Beatrice", 0, {9: 4, 3: 4, 2: 4, 7: 4, 1: 4, 4: 4, 5: 4, 6: 4, 8: 4})
    result = score_match("stroke_play", SI_FRONT_9, a, b)
    ra, rb = result.rounds
    assert ra.points == 0 and rb.points == 0


# --- validation ---------------------------------------------------------------

def test_valid_scoring_types():
    assert VALID_SCORING_TYPES == ("match_play", "stroke_play")
    assert DEFAULT_SCORING_TYPE == "match_play"


def test_score_match_rejects_unknown_type():
    a = _card(1, "A", 0, {1: 4})
    b = _card(2, "B", 0, {1: 4})
    try:
        score_match("bogus", {1: 1}, a, b)
    except ValueError as exc:
        assert "bogus" in str(exc)
    else:
        raise AssertionError("expected ValueError")
