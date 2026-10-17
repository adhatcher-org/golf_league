"""Pure scorecard rules (GL-79)."""

import pytest

from golf_league.domain.scorecard import (
    MAX_STROKES_PER_HOLE,
    group_by_match,
    is_blank,
    validate_strokes,
)


def test_max_strokes_constant():
    assert MAX_STROKES_PER_HOLE == 15


@pytest.mark.parametrize("value,expected", [("4", 4), ("1", 1), ("15", 15), (" 7 ", 7), (6, 6)])
def test_valid_strokes(value, expected):
    assert validate_strokes(value) == expected


@pytest.mark.parametrize("value", ["", "   ", None])
def test_blank_is_none_and_flagged_blank(value):
    assert validate_strokes(value) is None
    assert is_blank(value)


@pytest.mark.parametrize("value", ["0", "16", "-1", "4.5", "٤", "abc", True, 0, 16, 4.5, "+4"])
def test_rejected_without_raising(value):
    assert validate_strokes(value) is None
    assert not is_blank(value) or value in ("", None)


def test_group_by_match_complete_card():
    grouping = group_by_match([(1, 10, 2), (2, 10, 1), (3, 11, 4), (4, 11, 3)])
    assert grouping.groups == {10: (1, 2), 11: (3, 4)}
    assert grouping.incomplete is False


def test_group_by_match_one_side_or_cross_match_is_incomplete():
    assert group_by_match([(1, 10, 2), (2, 10, 1), (3, 11, 4)]).incomplete is True
    assert group_by_match([(1, 10, 2), (3, 11, 4)]).incomplete is True
