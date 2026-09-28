from decimal import Decimal

import pytest

from golf_league.domain.handicap import course_handicap


def test_index_0_rating_36_par_36_slope_113_is_0():
    assert course_handicap(Decimal("0"), Decimal("36"), 113, 36) == 0


def test_index_10_rating_36_par_36_slope_113_is_10():
    assert course_handicap(Decimal("10"), Decimal("36"), 113, 36) == 10


def test_index_minus_3_rating_36_par_36_slope_113_is_minus_3():
    assert course_handicap(Decimal("-3"), Decimal("36"), 113, 36) == -3


def test_rating_35_5_at_zero_index_rounds_to_minus_0():
    assert course_handicap(Decimal("0"), Decimal("35.5"), 113, 36) == 0


def test_tie_at_positive_half_rounds_up():
    assert course_handicap(Decimal("0"), Decimal("38.5"), 113, 36) == 3
    assert course_handicap(Decimal("0"), Decimal("39.5"), 113, 36) == 4


def test_tie_at_negative_half_rounds_towards_zero():
    assert course_handicap(Decimal("0"), Decimal("33.5"), 113, 36) == -2
    assert course_handicap(Decimal("0"), Decimal("32.5"), 113, 36) == -3


def test_a_long_decimal_expansion_rounds_to_the_nearest_integer():
    assert course_handicap(Decimal("12"), Decimal("33.9"), 114, 36) == 10


def test_zero_slope_raises():
    with pytest.raises(ValueError, match="slope"):
        course_handicap(Decimal("0"), Decimal("36"), 0, 36)


@pytest.mark.parametrize(
    ("index", "rating"),
    [
        (Decimal("NaN"), Decimal("36")),
        (Decimal("Infinity"), Decimal("36")),
        (Decimal("0"), Decimal("NaN")),
        (Decimal("0"), Decimal("-Infinity")),
    ],
)
def test_non_finite_input_raises(index, rating):
    with pytest.raises(ValueError, match="finite"):
        course_handicap(index, rating, 113, 36)


@pytest.mark.parametrize(
    ("index", "rating", "slope", "par"),
    [
        (None, Decimal("36"), 113, 36),
        (Decimal("0"), None, 113, 36),
        (Decimal("0"), Decimal("36"), "113", 36),
        (Decimal("0"), Decimal("36"), 113, "36"),
        (Decimal("0"), Decimal("36"), True, 36),
        (Decimal("0"), Decimal("36"), 113, False),
    ],
)
def test_non_numeric_input_raises(index, rating, slope, par):
    with pytest.raises(TypeError):
        course_handicap(index, rating, slope, par)


@pytest.mark.parametrize(
    ("index", "rating", "slope", "par"),
    [
        (0.0, Decimal("36"), 113, 36),
        (Decimal("0"), 36.0, 113, 36),
        (Decimal("0"), Decimal("36"), 113.0, 36),
        (Decimal("0"), Decimal("36"), 113, 36.0),
    ],
)
def test_float_input_is_rejected(index, rating, slope, par):
    with pytest.raises(TypeError):
        course_handicap(index, rating, slope, par)


@pytest.mark.parametrize("rating", [Decimal("0"), Decimal("-0.1"), Decimal("-34.0")])
def test_zero_or_negative_rating_raises(rating):
    with pytest.raises(ValueError, match="rating"):
        course_handicap(Decimal("0"), rating, 113, 36)


@pytest.mark.parametrize("index", [Decimal("0"), Decimal("-3")])
@pytest.mark.parametrize("rating", [Decimal("33.9"), Decimal("36"), Decimal("37")])
def test_a_plus_handicap_index_is_accepted_at_every_rating(index, rating):
    assert isinstance(course_handicap(index, rating, 113, 36), int)
