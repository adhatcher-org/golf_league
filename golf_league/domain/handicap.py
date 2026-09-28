"""Pure 9-hole course-handicap calculation with explicit Decimal semantics."""

from decimal import Decimal
from math import floor


def _require_decimal(value: object, name: str) -> Decimal:
    """Return a finite Decimal, rejecting implicit conversion from floats."""
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be a Decimal; float values are not accepted.")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite.")
    return value


def _require_positive_int(value: object, name: str) -> int:
    """Return a positive built-in integer for a course measurement."""
    if type(value) is not int:
        raise TypeError(f"{name} must be an int.")
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero.")
    return value


def course_handicap(index: Decimal, rating: Decimal, slope: int, par: int) -> int:
    """Return a 9-hole playing handicap rounded to the higher value on ties."""
    index = _require_decimal(index, "index")
    rating = _require_decimal(rating, "rating")
    slope = _require_positive_int(slope, "slope")
    par = _require_positive_int(par, "par")
    if rating <= 0:
        raise ValueError("rating must be greater than zero.")

    value = index * (Decimal(slope) / Decimal(113)) + (rating - Decimal(par))
    # `round()` uses banker's rounding and ROUND_HALF_UP sends negative ties
    # away from zero; floor(value + 0.5) sends every exact tie higher instead.
    return floor(value + Decimal("0.5"))
