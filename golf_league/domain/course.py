"""Pure validation for courses, tee sets and tee ratings.

No I/O, no database, no HTTP. Every validator returns a list of field
errors (empty when the value is valid) so a route can re-render a form
with per-field messages instead of raising.
"""

from decimal import Decimal, DecimalException, InvalidOperation

VALID_GENDERS = ("men", "women")
VALID_SCOPES = ("front", "back", "full")
VALID_NINES = ("front", "back")


def derive_stroke_index_9(
    stroke_index_18_by_hole: dict[int, int], nine_by_hole: dict[int, str]
) -> dict[int, int]:
    """Rank each 18-hole index within its explicitly stored nine."""
    result: dict[int, int] = {}
    for nine in VALID_NINES:
        numbers = sorted(
            (stroke_index_18_by_hole[hole], hole)
            for hole in stroke_index_18_by_hole
            if nine_by_hole.get(hole) == nine
        )
        for rank, (_, hole) in enumerate(numbers, start=1):
            result[hole] = rank
    return result


def validate_hole_grid(holes: list[dict[str, object]], tee_set_ids: set[int]) -> dict[str, str]:  # noqa: C901
    """Validate a complete persisted course grid without I/O."""
    errors: dict[str, str] = {}
    if len(holes) != 18:
        return {"holes": "Exactly 18 holes are required."}
    numbers: list[int] = []
    indices: list[int] = []
    by_nine: dict[str, list[dict[str, object]]] = {"front": [], "back": []}
    for row in holes:
        number, par, si18, si9, nine = (row.get(key) for key in ("number", "par", "stroke_index_18", "stroke_index_9", "nine"))
        if not all(isinstance(value, int) and not isinstance(value, bool) for value in (number, par, si18, si9)):
            return {"holes": "Hole numbers, par, and stroke indexes must be whole numbers."}
        if number < 1 or number > 18 or par < 1 or par > 10 or si18 < 1 or si18 > 18 or si9 < 1 or si9 > 9:
            return {"holes": "Hole values are outside their allowed range."}
        if nine not in VALID_NINES:
            return {"holes": "Nine must be front or back."}
        numbers.append(number)
        indices.append(si18)
        by_nine[nine].append(row)
        yardages = row.get("yardages")
        if not isinstance(yardages, dict) or set(yardages) != tee_set_ids:
            return {"yardages": "Every hole needs a yardage for every tee set."}
        for yards in yardages.values():
            if not isinstance(yards, int) or isinstance(yards, bool) or yards <= 0:
                return {"yardages": "Yardages must be positive whole numbers."}
    if sorted(numbers) != list(range(1, 19)) or sorted(indices) != list(range(1, 19)):
        return {"holes": "Hole numbers and 18-hole stroke indexes must each be a permutation of 1 to 18."}
    for nine, rows in by_nine.items():
        if len(rows) != 9 or sorted(row["stroke_index_9"] for row in rows) != list(range(1, 10)):
            errors["holes"] = f"{nine.title()} nine stroke indexes must be a permutation of 1 to 9."
    return errors


def validate_gender(value: str) -> str | None:
    """Return an error message, or None when `value` is `men` or `women`."""
    if value not in VALID_GENDERS:
        return "Gender must be 'men' or 'women'."
    return None


def validate_positive_int(value: object, field_name: str) -> str | None:
    """Return an error message, or None when `value` is a positive integer.

    Accepts an `int` or a string that parses cleanly as one; rejects
    floats-as-strings, empty strings, and non-positive values.
    """
    if isinstance(value, bool):
        return f"{field_name} must be a positive whole number."
    if isinstance(value, int):
        parsed = value
    else:
        text = str(value).strip()
        if not text or not (text.isdecimal() or (text.startswith("-") and text[1:].isdecimal())):
            return f"{field_name} must be a positive whole number."
        parsed = int(text)
    if parsed <= 0:
        return f"{field_name} must be a positive whole number."
    return None


def validate_rating(value: object) -> str | None:
    """Return an error message, or None when `value` is a finite rating number.

    Rejects empty strings, non-numeric text (`abc`), non-finite values
    (`NaN`, `Infinity`), and anything at or below zero — none of these are
    silently clamped to 0.
    """
    text = str(value).strip()
    if not text:
        return "Rating is required."
    try:
        parsed = Decimal(text)
    except InvalidOperation:
        return "Rating must be a finite number."
    if not parsed.is_finite():
        return "Rating must be a finite number."
    if parsed <= 0:
        return "Rating must be greater than zero."
    try:
        too_large = abs(parsed) >= Decimal("1000")
    except DecimalException:
        return "Rating must be less than 1000."
    if too_large:
        return "Rating must be less than 1000."
    try:
        fits_one_decimal = parsed == parsed.quantize(Decimal("0.1"))
    except DecimalException:
        return "Rating may have at most one decimal place."
    if not fits_one_decimal:
        return "Rating may have at most one decimal place."
    return None


def to_rating_decimal(value: object) -> Decimal:
    """Convert an already-validated rating value to a `Decimal`.

    Callers must call `validate_rating` first; this raises for invalid
    input rather than guessing a default.
    """
    return Decimal(str(value).strip())
