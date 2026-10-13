"""GL-82: matching a written name to the week's golfers, and the card checks. Synthetic names only."""

import pytest

from golf_league.domain.scorecard_review import (
    NO_MATCH,
    NameCandidate,
    NameMatch,
    card_side,
    match_name,
    normalise_name,
    out_total_mismatch,
)

AARON = NameCandidate(1, "Aaron", "Hatchwell", 8)
BLAKE = NameCandidate(2, "Blake", "O'Example", 12)
CASEY = NameCandidate(3, "Casey", "Stone-Example", 3)
MARY_ANN = NameCandidate(4, "Mary Ann", "Quill", 15)
WEEK = [AARON, BLAKE, CASEY, MARY_ANN]


@pytest.mark.parametrize("text,expected", [
    ("  AARON  ", "aaron"), ("Aaron 8", "aaron"), ("O'Example", "oexample"), ("O’Example", "oexample"),
    ("Stone-Example", "stone example"), ("J.  Quill", "j quill"), ("Zoë", "zoë"), ("", ""), (None, ""),
    ("12", ""), ("Mary_Ann", "mary ann"), ("A1", "a1"), ("Aaron 8 12", "aaron"), ("Aaron8", "aaron8"),
])
def test_normalise_name(text, expected):
    assert normalise_name(text) == expected


@pytest.mark.parametrize("written", ["Aaron", "aaron", " AARON ", "Aaron 8", "Aaron.", "Aaron Hatchwell",
                                     "aaron hatchwell", "Aaron H", "Aaron H.", "Aaron Hat"])
def test_a_single_exact_match_preselects_without_a_flag(written):
    assert match_name(written, WEEK) == NameMatch(1)


def test_first_name_only_matches_the_single_candidate_with_that_first_name():
    assert match_name("Mary Ann", WEEK) == NameMatch(4)
    assert match_name("Blake", WEEK) == NameMatch(2)
    assert match_name("Casey Stone Example", WEEK) == NameMatch(3)


@pytest.mark.parametrize("written,golfer", [("Aron", 1), ("Aaronn", 1), ("Blak", 2), ("Casy", 3),
                                            ("Aaron Hatchwall", 1), ("Blake OExampel", 2)])
def test_a_single_close_match_preselects_and_is_flagged(written, golfer):
    assert match_name(written, WEEK) == NameMatch(golfer, "check")


@pytest.mark.parametrize("written", ["", "   ", "8", "Zzyzx", "Bob", "A", "Mary", "Aaron Zzyzx"])
def test_no_match_leaves_the_choice_empty(written):
    assert match_name(written, WEEK) == NO_MATCH
    assert NO_MATCH.golfer_id is None and NO_MATCH.flag == ""


def test_two_golfers_with_the_same_first_name_leave_the_choice_empty():
    week = [AARON, NameCandidate(9, "Aaron", "Otherly", 14), BLAKE]
    assert match_name("Aaron", week) == NO_MATCH
    assert match_name("Aaron", week, handicap_written=20) == NO_MATCH  # matches neither handicap
    # A written last name or initial still settles it without the handicap.
    assert match_name("Aaron O", week) == NameMatch(9)
    assert match_name("Aaron Hatchwell", week) == NameMatch(1)


def test_a_written_handicap_breaks_a_same_first_name_tie_and_is_flagged():
    week = [AARON, NameCandidate(9, "Aaron", "Otherly", 14)]
    assert match_name("Aaron", week, handicap_written=14) == NameMatch(9, "handicap")
    assert match_name("Aaron", week, handicap_written=8) == NameMatch(1, "handicap")


def test_a_handicap_shared_by_both_does_not_break_the_tie():
    week = [AARON, NameCandidate(9, "Aaron", "Otherly", 8)]
    assert match_name("Aaron", week, handicap_written=8) == NO_MATCH


def test_the_handicap_is_never_used_without_a_name_tie():
    # One Aaron: preselected plainly, whatever was written beside the name.
    assert match_name("Aaron", WEEK, handicap_written=99) == NameMatch(1)
    # No name match at all: a matching handicap selects nobody.
    assert match_name("Zzyzx", WEEK, handicap_written=8) == NO_MATCH


def test_two_close_matches_leave_the_choice_empty():
    week = [NameCandidate(1, "Jon", "Example"), NameCandidate(2, "Jan", "Example")]
    assert match_name("Jnn", week) == NO_MATCH


def test_only_the_given_candidates_are_considered_and_duplicates_count_once():
    assert match_name("Aaron", [BLAKE, CASEY]) == NO_MATCH
    # A golfer listed twice (a side of two matches) is still one golfer.
    assert match_name("Aaron", [AARON, AARON, BLAKE]) == NameMatch(1)


@pytest.mark.parametrize("header,side", [
    ((1, 2, 3, 4, 5, 6, 7, 8, 9), "front"), ((10, 11, 12, 13, 14, 15, 16, 17, 18), "back"),
    ((1, 2, 3), "front"), ((), None), ((8, 9, 10), None), ((0, 1), None), ((19,), None),
])
def test_card_side(header, side):
    assert card_side(header) == side


def test_out_total_matching_the_sum_is_no_mismatch():
    assert out_total_mismatch([5] * 9, 45, 7) is None
    assert out_total_mismatch([5] * 9, None, 7) is None
    assert out_total_mismatch([5, None] + [5] * 7, 40, 7) is None  # an incomplete row cannot be compared
    assert out_total_mismatch([], 40, 7) is None


def test_out_total_mismatch_names_both_numbers():
    mismatch = out_total_mismatch([5] * 9, 47, None)
    assert (mismatch.shown_sum, mismatch.out_total, mismatch.may_be_net) == (45, 47, False)


def test_a_lower_total_beside_a_handicap_may_be_the_net_score():
    exact = out_total_mismatch([5] * 9, 38, 7)  # 45 minus 7
    assert exact.may_be_net
    # The task's example: cells misread, so the sum shown is not the real gross.
    observed = out_total_mismatch([6, 6, 6, 6, 6, 6, 6, 5, 5], 43, 7)
    assert (observed.shown_sum, observed.out_total, observed.may_be_net) == (52, 43, True)
    assert not out_total_mismatch([5] * 9, 38, None).may_be_net
    assert not out_total_mismatch([5] * 9, 50, 7).may_be_net  # a higher total is not a net score
    assert out_total_mismatch([5] * 9, 47, -2).may_be_net  # a plus handicap: net is above gross
